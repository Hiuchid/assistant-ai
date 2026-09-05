"""End-of-session item generation.

INSTRUCTIONS.md §9. One LLM call per conversation, reading turns **from the
database** in `turns.id` order -- never from a transcript the client supplied,
and never per-turn.

Three properties matter more than the prose quality:

1. **It never drops a lead.** A malformed model response is retried once, then
   written as `type='other'` with the raw transcript as the summary. Losing the
   message entirely is the one unacceptable outcome.
2. **Exactly one item per conversation.** Enforced by the unique constraint on
   `tickets.conversation_id`, not by checking first: session-end and the
   inactivity sweeper genuinely race, and the database is the only arbiter that
   cannot lose that race.
3. **The degraded path needs no model at all.** A conversation captured by the
   scripted interview (§6) already has its fields; asking an unavailable LLM to
   summarise them would defeat the point.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo

import asyncpg
from pydantic import BaseModel, Field, ValidationError, field_validator

from .degraded import DegradedCapture, Step
from .persistence import Store
from .prompts.summarize import system_prompt, user_prompt
from .providers.llm import GroqLadder, LadderExhausted, Message, Token

log = logging.getLogger("assistant.summarize")

Mode = Literal["owner", "visitor"]


class TicketFields(TypedDict):
    """Exactly the columns insert_ticket writes.

    A TypedDict rather than a loose dict so the ** unpack at the call site is
    checked instead of silenced -- these two shapes drifting apart would be a
    runtime error in the one code path that must never fail.
    """

    type: str
    title: str
    summary: str
    intent: str | None
    action_items: list[str]
    urgency: str
    contact: dict[str, str]
    requested_slot: str | None
    due_at: datetime | None


OWNER_TYPES = ("note", "task", "reminder", "other")
VISITOR_TYPES = ("message", "request", "other")


class TicketDraft(BaseModel):
    """What the model is asked to produce. Validated before it touches the DB."""

    type: str
    title: str
    summary: str
    intent: str | None = None
    action_items: list[str] = Field(default_factory=list)
    urgency: Literal["low", "medium", "high"] | None = "low"
    contact: dict[str, str] = Field(default_factory=dict)
    requested_slot: str | None = None
    due_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def _title_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty title")
        return v.strip()[:200]

    @field_validator("contact", mode="before")
    @classmethod
    def _stringify_contact(cls, v: object) -> object:
        # Models sometimes emit numbers for phone. Coerce rather than reject:
        # a phone number is the single most valuable field in the whole record.
        if isinstance(v, dict):
            return {str(k): str(val) for k, val in v.items() if val is not None}
        return {}

    @field_validator("action_items", mode="before")
    @classmethod
    def _listify(cls, v: object) -> object:
        if isinstance(v, str):
            return [v] if v.strip() else []
        return v


def render_transcript(turns: list[tuple[str, str, bool]]) -> str:
    """Flatten to text for the model.

    Cancelled agent turns are marked rather than dropped: barge-in means the
    caller heard part of it and replied to that, so removing it makes the
    exchange read as a non-sequitur.
    """
    lines = []
    for role, text, cancelled in turns:
        who = "Caller" if role == "customer" else "Assistant"
        suffix = " [cut off]" if cancelled else ""
        lines.append(f"{who}: {text}{suffix}")
    return "\n".join(lines)


def _extract_json(raw: str) -> dict[str, object]:
    """Pull the JSON object out of a reply that may have prose around it."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("JSON was not an object")
    return parsed


def _coerce_type(draft_type: str, mode: Mode) -> str:
    allowed = OWNER_TYPES if mode == "owner" else VISITOR_TYPES
    candidate = draft_type.strip().lower()
    return candidate if candidate in allowed else "other"


class Summarizer:
    def __init__(self, store: Store, ladder: GroqLadder, *, tz: str = "UTC") -> None:
        self._store = store
        self._ladder = ladder
        self._tz = ZoneInfo(tz)

    async def _ask(
        self, transcript: str, mode: Mode, lang: str = "en"
    ) -> TicketDraft | None:
        messages: list[Message] = [
            {
                "role": "system",
                "content": system_prompt(
                    mode,
                    now_iso=datetime.now(self._tz).isoformat(timespec="minutes"),
                    lang=lang,
                ),
            },
            {"role": "user", "content": user_prompt(transcript)},
        ]
        chunks: list[str] = []
        async for event in self._ladder.stream(messages):
            if isinstance(event, Token):
                chunks.append(event.text)
        raw = "".join(chunks)
        try:
            return TicketDraft.model_validate(_extract_json(raw))
        except (ValueError, ValidationError, json.JSONDecodeError) as e:
            log.warning(
                "summariser produced unusable output",
                extra={"error": str(e)[:200], "chars": len(raw)},
            )
            return None

    async def run(self, conversation_id: str) -> bool:
        """Generate and store the item. Returns True if a row was written.

        False means one already existed -- the normal outcome when session-end
        and the sweeper both fire, and not an error.
        """
        meta = await self._store.conversation_meta(conversation_id)
        if meta is None:
            log.warning("summarise skipped: conversation gone")
            return False

        mode: Mode = "owner" if meta["mode"] == "owner" else "visitor"
        # Stored as a locale ("ar-LB"); the prompt only needs the language.
        lang = "ar" if str(meta.get("lang") or "").startswith("ar") else "en"
        turns = await self._store.transcript(conversation_id)
        if not turns:
            log.info("summarise skipped: no turns")
            await self._store.end_conversation(conversation_id)
            return False

        transcript = render_transcript(
            [(t.role, t.text, t.cancelled) for t in turns]
        )

        draft: TicketDraft | None = None
        row: TicketFields
        if not meta["degraded"]:
            try:
                draft = await self._ask(transcript, mode, lang)
                if draft is None:
                    # §9: retry once. Model output is nondeterministic and a
                    # second attempt frequently parses.
                    log.info("retrying summariser once")
                    draft = await self._ask(transcript, mode, lang)
            except LadderExhausted as e:
                log.warning(
                    "summariser had no model available",
                    extra={"error": str(e)[:200]},
                )

        if draft is not None:
            row = TicketFields(
                type=_coerce_type(draft.type, mode),
                title=draft.title,
                summary=draft.summary,
                intent=draft.intent,
                action_items=draft.action_items,
                urgency=draft.urgency or "low",
                contact=draft.contact,
                requested_slot=draft.requested_slot,
                due_at=draft.due_at,
            )
        else:
            # §9: never drop the lead. A human can read a raw transcript; they
            # cannot read a message that was thrown away.
            log.error("falling back to a raw-transcript item")
            row = TicketFields(
                type="other",
                title="Unsummarised conversation - needs reading",
                summary=transcript[:4000],
                intent=None,
                action_items=[],
                urgency="low",
                contact={},
                requested_slot=None,
                due_at=None,
            )

        try:
            written = await self._store.insert_ticket(
                conversation_id, mode=mode, **row
            )
        except asyncpg.PostgresError as e:
            log.error("ticket insert failed", extra={"error": repr(e)})
            return False

        await self._store.end_conversation(conversation_id)
        if written:
            log.info(
                "item created",
                extra={"type": row["type"], "mode": mode, "turns": len(turns)},
            )
        else:
            log.info("item already existed; nothing written")
        return written


def draft_from_degraded(capture: DegradedCapture) -> TicketFields:
    """Build an item from the scripted interview, with no model involved (§6).

    The conversation reached this path precisely because no model was
    available, so asking one to summarise it would defeat the point. The fields
    were captured verbatim; a human reads them.
    """
    return TicketFields(
        type="other",
        title=capture.title(),
        summary=capture.summary(),
        intent=None,
        action_items=[],
        urgency="low",
        contact=capture.contact(),
        requested_slot=capture.answers.get(Step.WHEN),
        # No model ran, so nothing parsed a date out of the free text.
        due_at=None,
    )
