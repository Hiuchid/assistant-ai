"""Summariser tests.

INSTRUCTIONS.md §12 names the summariser as one of the components that gets
real tests. §9's acceptance criteria are the interesting ones, and they are all
failure paths: a malformed response must still produce an item, and firing
session-end and the sweeper together must still produce exactly one.

The live happy path is verified against the real service; what matters here is
everything that goes wrong.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.degraded import DegradedCapture, Step
from backend.summarize import (
    TicketDraft,
    TicketFields,
    _coerce_type,
    _extract_json,
    draft_from_degraded,
    render_transcript,
)

# ------------------------------------------------------------ JSON extraction


def test_extracts_json_wrapped_in_prose() -> None:
    """Models add "Here is the JSON:" no matter how firmly you ask them not to."""
    raw = (
        "Sure! Here you go:\n"
        '```json\n{"type": "message", "title": "x"}\n```\n'
        "Hope that helps."
    )
    assert _extract_json(raw) == {"type": "message", "title": "x"}


def test_extracts_bare_json() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}


@pytest.mark.parametrize(
    "raw",
    ["", "no json here at all", "{unclosed", "}{", "[1, 2, 3]"],
)
def test_unusable_output_raises_rather_than_returning_junk(raw: str) -> None:
    with pytest.raises((ValueError, json.JSONDecodeError)):
        _extract_json(raw)


# ----------------------------------------------------------------- validation


def test_draft_coerces_numeric_contact_values() -> None:
    """A phone number is the most valuable field; coerce rather than reject it."""
    draft = TicketDraft.model_validate(
        {
            "type": "message",
            "title": "Call back",
            "summary": "s",
            "contact": {"phone": 3456789, "name": "Dana"},
        }
    )
    assert draft.contact == {"phone": "3456789", "name": "Dana"}


def test_draft_accepts_a_string_where_a_list_was_asked_for() -> None:
    draft = TicketDraft.model_validate(
        {"type": "task", "title": "t", "summary": "s", "action_items": "Ring them"}
    )
    assert draft.action_items == ["Ring them"]


def test_draft_drops_null_contact_values() -> None:
    draft = TicketDraft.model_validate(
        {
            "type": "message",
            "title": "t",
            "summary": "s",
            "contact": {"phone": None, "email": "a@b.c"},
        }
    )
    assert draft.contact == {"email": "a@b.c"}


def test_empty_title_is_rejected() -> None:
    with pytest.raises(ValueError):
        TicketDraft.model_validate({"type": "note", "title": "   ", "summary": "s"})


def test_contact_that_is_not_an_object_becomes_empty() -> None:
    draft = TicketDraft.model_validate(
        {"type": "note", "title": "t", "summary": "s", "contact": "Dana, 0123"}
    )
    assert draft.contact == {}


# ---------------------------------------------------------------- type safety


@pytest.mark.parametrize(
    ("given", "mode", "expected"),
    [
        ("task", "owner", "task"),
        ("TASK", "owner", "task"),
        ("message", "visitor", "message"),
        # A visitor conversation cannot produce an owner-side type, whatever
        # the model says.
        ("reminder", "visitor", "other"),
        ("message", "owner", "other"),
        ("complete nonsense", "visitor", "other"),
        ("", "owner", "other"),
    ],
)
def test_type_is_constrained_to_the_mode(given: str, mode: Any, expected: str) -> None:
    assert _coerce_type(given, mode) == expected


# ------------------------------------------------------------- transcript


def test_cancelled_turns_are_marked_not_dropped() -> None:
    """Barge-in means the caller replied to a partial sentence.

    Dropping it makes the exchange read as a non-sequitur to the summariser.
    """
    rendered = render_transcript(
        [
            ("customer", "Are you open Sunday?", False),
            ("agent", "Let me just check that for", True),
            ("customer", "Actually never mind.", False),
        ]
    )
    assert "[cut off]" in rendered
    assert "Let me just check that for" in rendered
    assert rendered.startswith("Caller: Are you open Sunday?")


# --------------------------------------------------------- degraded fallback


def test_degraded_capture_becomes_a_usable_item_without_a_model() -> None:
    capture = DegradedCapture()
    capture.open()
    for answer in ["Dana", "a quote for flyers", "03 456 789", "Thursday morning"]:
        capture.submit(answer)

    fields: TicketFields = draft_from_degraded(capture)
    assert fields["type"] == "other"
    assert fields["title"] == "Dana - a quote for flyers"
    assert fields["requested_slot"] == "Thursday morning"
    assert fields["contact"] == {"name": "Dana", "raw": "03 456 789"}
    assert "03 456 789" in fields["summary"]


def test_degraded_item_survives_a_caller_who_hung_up_early() -> None:
    capture = DegradedCapture()
    capture.open()
    capture.submit("Priya")

    fields = draft_from_degraded(capture)
    assert fields["title"] == "Message from Priya"
    # No model ran, so nothing parsed a date out of the free text.
    assert fields["due_at"] is None
    assert fields["requested_slot"] is None
    assert "(not given)" in fields["summary"]


def test_degraded_fields_match_what_insert_ticket_expects() -> None:
    """The TypedDict is unpacked into insert_ticket; the shapes must agree."""
    capture = DegradedCapture()
    capture.open()
    fields = draft_from_degraded(capture)
    assert set(fields) == {
        "type", "title", "summary", "intent", "action_items",
        "urgency", "contact", "requested_slot", "due_at",
    }
    assert capture.answers.get(Step.WHEN) is None
