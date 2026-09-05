"""Per-conversation state.

Phase 1 keeps this in memory. Phase 4 makes the database the source of truth
(§3.3) and this becomes a cache in front of it -- turns are written as they
happen, and the ticket is always built from the database, never from here.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from .degraded import DegradedCapture
from .providers.llm import Message

Role = Literal["customer", "agent"]

# §6: send the system prompt plus a window of turns, so a long call cannot run
# the daily budget down on its own tail.
#
# It keeps **both ends**, not just the recent ones. A plain sliding window of
# six turns meant three exchanges of memory, and in a message-taking call the
# first thing said is who is calling -- so by the fourth exchange the assistant
# had genuinely forgotten the caller's name and asked for it again. §6 always
# intended a running summary to cover what fell out; that was never built, and
# a window that keeps the opening is most of the same benefit for none of the
# cost, because the opening is where the name, the company and the reason for
# calling live.
#
# Only the middle is dropped, and a marker says so, so the model knows it is
# missing something rather than assuming it was never told.
HEAD_TURNS = 4
TAIL_TURNS = 16


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
    # In-memory id, generated on connect. Distinct from db_id on purpose: this
    # exists for logging from the moment a socket opens, whereas the row is
    # only created once there is something worth recording (§3.3). Scanners
    # open and drop sockets constantly and must not litter the table.
    id: str
    system_prompt: str
    channel: Literal["text", "voice"] = "text"
    mode: Literal["owner", "visitor"] = "visitor"
    # Set from the first utterance Whisper transcribes, or by the
    # widget toggle. Drives the prompt, the voice and the STT hint.
    lang: Literal["en", "ar"] = "en"
    turns: list[Turn] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)

    # Set once the ladder is exhausted (§6). A conversation never comes back out
    # of degraded mode: switching personas mid-call would be jarring, and the
    # scripted interview is only a few questions from finishing anyway.
    degraded: DegradedCapture | None = None

    # Postgres id, set on the first turn or on a verified resume. None until
    # then, and None for the whole session if persistence is unavailable.
    db_id: str | None = None

    def add(self, role: Role, text: str, *, cancelled: bool = False) -> None:
        self.turns.append(Turn(role=role, text=text, cancelled=cancelled))
        self.last_activity_at = time.time()

    def window(self) -> tuple[list[Turn], int]:
        """The turns to send, and how many were left out between them.

        Short conversations -- which is nearly all of them -- go through whole.
        """
        if len(self.turns) <= HEAD_TURNS + TAIL_TURNS:
            return self.turns, 0
        kept = self.turns[:HEAD_TURNS] + self.turns[-TAIL_TURNS:]
        return kept, len(self.turns) - len(kept)

    def messages(self, tz: str = "UTC") -> list[Message]:
        """The system prompt plus the window, in provider format.

        The current local time is appended to the system prompt on every turn,
        not baked in at session start. Without it the model cannot resolve
        "Tuesday" or "tomorrow" and quietly guesses -- which produced a
        calendar entry seven days out instead of on the day asked for. Built
        per turn rather than once, so it stays right in a long conversation.

        §12: this assembles someone's words to send to a provider. It never
        logs them, and the omission marker counts turns rather than quoting
        any.
        """
        now = datetime.now(ZoneInfo(tz))
        messages: list[Message] = [
            {
                "role": "system",
                "content": (
                    f"{self.system_prompt}\n\n"
                    f"The current local time is {now.isoformat(timespec='minutes')} "
                    f"({now.strftime('%A')}). Resolve any relative day or time "
                    f"against it."
                ),
            }
        ]
        kept, omitted = self.window()
        for index, turn in enumerate(kept):
            if omitted and index == HEAD_TURNS:
                messages.append({
                    "role": "system",
                    "content": (
                        f"[{omitted} exchanges from the middle of this "
                        f"conversation are not shown. You were told them. Do "
                        f"not ask again for anything already given.]"
                    ),
                })
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
