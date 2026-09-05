"""Per-conversation state.

Phase 1 keeps this in memory. Phase 4 makes the database the source of truth
(§3.3) and this becomes a cache in front of it -- turns are written as they
happen, and the ticket is always built from the database, never from here.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from .providers.llm import Message

Role = Literal["customer", "agent"]

# §6: send the system prompt plus a sliding window of recent turns. Windowing
# saves little on a short conversation -- most of the cost is in the early
# turns -- but it bounds the tail, and the tail is what breaks the daily budget.
WINDOW_TURNS = 6


@dataclass
class Turn:
    role: Role
    text: str
    ts: float = field(default_factory=time.time)
    # Barge-in (§7.4) cuts an agent turn off mid-delivery. What the visitor
    # actually heard is what the ticket must reflect, so the partial text is
    # kept and flagged rather than discarded. Unused until Phase 3.
    cancelled: bool = False


@dataclass
class Conversation:
    id: str
    system_prompt: str
    channel: Literal["text", "voice"] = "text"
    mode: Literal["owner", "visitor"] = "visitor"
    turns: list[Turn] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)

    def add(self, role: Role, text: str, *, cancelled: bool = False) -> None:
        self.turns.append(Turn(role=role, text=text, cancelled=cancelled))
        self.last_activity_at = time.time()

    def messages(self) -> list[Message]:
        """The system prompt plus the sliding window, in provider format.

        TODO(phase-4): §6 also calls for a running summary of turns older than
        the window. Not implemented -- conversations currently truncate rather
        than compress, so anything said early is simply forgotten once it falls
        out of the window.
        """
        messages: list[Message] = [
            {"role": "system", "content": self.system_prompt}
        ]
        for turn in self.turns[-WINDOW_TURNS:]:
            role: Literal["user", "assistant"] = (
                "user" if turn.role == "customer" else "assistant"
            )
            messages.append({"role": role, "content": turn.text})
        return messages

    def idle_for(self) -> float:
        return time.time() - self.last_activity_at


class SessionStore:
    """In-memory conversations, evicted when idle.

    Phase 4 replaces this with Supabase plus the inactivity sweeper that drives
    ticket generation (§9).
    """

    def __init__(self, idle_timeout_s: int) -> None:
        self._idle_timeout_s = idle_timeout_s
        self._conversations: dict[str, Conversation] = {}

    def create(self, system_prompt: str) -> Conversation:
        conversation = Conversation(id=str(uuid.uuid4()), system_prompt=system_prompt)
        self._conversations[conversation.id] = conversation
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    def drop(self, conversation_id: str) -> None:
        self._conversations.pop(conversation_id, None)

    def evict_idle(self) -> int:
        stale = [
            cid
            for cid, c in self._conversations.items()
            if c.idle_for() > self._idle_timeout_s
        ]
        for cid in stale:
            del self._conversations[cid]
        return len(stale)

    def __len__(self) -> int:
        return len(self._conversations)
