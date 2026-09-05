"""Degraded-capture tests.

INSTRUCTIONS.md §6: given that Groq is a single point of failure for both STT
and the LLM, this path is load-bearing rather than decorative. "Test it
deliberately; do not let it rot."

Phase 1.5's acceptance criterion: with every model exhausted, a conversation
still completes, still captures contact details, and still produces exactly one
item.
"""

from __future__ import annotations

import httpx
import pytest

from backend.config import LadderRung, Settings
from backend.degraded import ALL_FIXED_LINES, PROMPTS, DegradedCapture, Step
from backend.providers.llm import GroqLadder, LadderExhausted, Message

MESSAGES: list[Message] = [{"role": "user", "content": "is anyone there?"}]


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        groq_api_key="test-key", allowed_origins=["https://example.com"]
    )


def test_full_interview_captures_every_answer() -> None:
    capture = DegradedCapture()
    opening = capture.open()
    assert len(opening) == 2
    assert opening[1] == PROMPTS[Step.NAME]

    assert capture.submit("Sam Okonkwo") == [PROMPTS[Step.REASON]]
    assert capture.submit("the outstanding invoice") == [PROMPTS[Step.CONTACT]]
    assert capture.submit("07700 900123") == [PROMPTS[Step.WHEN]]
    closing = capture.submit("tomorrow morning")

    assert capture.complete
    assert len(closing) == 1
    assert capture.answers[Step.NAME] == "Sam Okonkwo"
    assert capture.answers[Step.CONTACT] == "07700 900123"


def test_produces_a_usable_item() -> None:
    capture = DegradedCapture()
    capture.open()
    for answer in ["Dana", "a quote for print work", "dana@example.com", "Friday"]:
        capture.submit(answer)

    assert capture.title() == "Dana - a quote for print work"
    summary = capture.summary()
    assert "dana@example.com" in summary
    assert "Friday" in summary
    # A human reads this precisely because no model was available to parse it.
    assert capture.contact() == {"name": "Dana", "raw": "dana@example.com"}


def test_partial_interview_still_yields_something() -> None:
    """A caller who hangs up early must still leave a usable trace."""
    capture = DegradedCapture()
    capture.open()
    capture.submit("Priya")

    assert not capture.complete
    assert capture.title() == "Message from Priya"
    assert "(not given)" in capture.summary()
    assert capture.contact() == {"name": "Priya"}


def test_anonymous_caller_does_not_produce_an_empty_title() -> None:
    capture = DegradedCapture()
    assert capture.title() == "Message taken (no LLM available)"


def test_speech_after_the_script_is_kept_not_discarded() -> None:
    capture = DegradedCapture()
    capture.open()
    for answer in ["Ada", "the roof", "0123", "any time"]:
        capture.submit(answer)
    capture.submit("actually it's urgent, there's water coming in")

    assert "water coming in" in capture.answers[Step.REASON]


def test_every_fixed_line_is_exposed_for_cache_warming() -> None:
    """Phase 2 pre-renders and pins these; a line missed here costs seconds."""
    assert len(ALL_FIXED_LINES) == len(PROMPTS) + 2  # opening + closing
    assert all(line.strip() for line in ALL_FIXED_LINES)


@pytest.mark.anyio
async def test_exhausted_ladder_is_what_triggers_the_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam between Phase 1's ladder and this path.

    main.py catches LadderExhausted and opens a DegradedCapture; this pins the
    exception actually being raised when every rung is out of quota.
    """
    rungs = (
        LadderRung(
            model="a", requests_per_day=0, tokens_per_minute=8000, tokens_per_day=0
        ),
    )
    monkeypatch.setattr(Settings, "ladder", property(lambda self: rungs))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made when quota is spent")

    async with GroqLadder(
        _settings(), transport=httpx.MockTransport(handler)
    ) as ladder:
        with pytest.raises(LadderExhausted):
            async for _ in ladder.stream(MESSAGES):
                pass

    # And the caller is still served: the script needs nothing from the ladder.
    capture = DegradedCapture()
    assert capture.open()[0].startswith("I'm afraid")
