"""Ladder fallthrough tests.

INSTRUCTIONS.md §12: the ladder is the highest-risk component and the easiest to
test, with mocked 429s. Phase 1's acceptance criterion is that a 429 on the top
rung falls through without dropping the turn -- verified here rather than by
waiting to be rate limited in production.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from backend.config import LadderRung, Settings
from backend.providers.llm import (
    Completed,
    GroqLadder,
    LadderExhausted,
    Message,
    StreamInterrupted,
    Token,
)

MESSAGES: list[Message] = [{"role": "user", "content": "hello"}]


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "groq_api_key": "test-key",
        "allowed_origins": ["https://example.com"],
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _sse(*chunks: dict[str, object]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _content_chunk(text: str) -> dict[str, object]:
    return {"choices": [{"delta": {"content": text}}]}


def _usage_chunk(prompt: int, completion: int) -> dict[str, object]:
    return {
        "choices": [],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def _ok_stream(text: str = "OK") -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse(_content_chunk(text), _usage_chunk(10, 2)),
        headers={"content-type": "text/event-stream"},
    )


def _rate_limited() -> httpx.Response:
    return httpx.Response(
        429, json={"error": {"message": "rate limit"}}, headers={"retry-after": "7"}
    )


def _ladder_of(*models: str) -> tuple[LadderRung, ...]:
    return tuple(
        LadderRung(model=m, requests_per_day=1000, tokens_per_minute=8000)
        for m in models
    )


class _Recorder:
    """Serves scripted responses and records which models were tried."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = iter(responses)
        self.models_tried: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.models_tried.append(json.loads(request.content)["model"])
        return next(self._responses)


async def _drain(ladder: GroqLadder) -> tuple[str, Completed | None]:
    text, completed = "", None
    async for event in ladder.stream(MESSAGES):
        if isinstance(event, Token):
            text += event.text
        else:
            completed = event
    return text, completed


@pytest.fixture
def three_rungs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        Settings, "ladder", property(lambda self: _ladder_of("rung-1", "rung-2", "rung-3"))
    )
    yield


@pytest.mark.anyio
async def test_429_on_top_rung_falls_through(three_rungs: None) -> None:
    """The Phase 1 acceptance criterion: a 429 must not drop the turn."""
    recorder = _Recorder([_rate_limited(), _ok_stream("hello there")])
    async with GroqLadder(
        _settings(), transport=httpx.MockTransport(recorder)
    ) as ladder:
        text, completed = await _drain(ladder)

    assert text == "hello there"
    assert completed is not None
    assert completed.model == "rung-2"
    assert recorder.models_tried == ["rung-1", "rung-2"]


@pytest.mark.anyio
async def test_every_rung_exhausted_raises(three_rungs: None) -> None:
    recorder = _Recorder([_rate_limited(), _rate_limited(), _rate_limited()])
    async with GroqLadder(
        _settings(), transport=httpx.MockTransport(recorder)
    ) as ladder:
        with pytest.raises(LadderExhausted) as exc:
            await _drain(ladder)

    assert recorder.models_tried == ["rung-1", "rung-2", "rung-3"]
    # The failure must name the retry-after so the cause is diagnosable.
    assert "retry-after=7" in str(exc.value)


@pytest.mark.anyio
async def test_failure_after_tokens_does_not_retry(three_rungs: None) -> None:
    """A rung that dies mid-stream must not be replayed on the next rung.

    The client has already rendered partial text; a second model's answer
    written over the top would be visibly incoherent.
    """
    truncated = httpx.Response(
        200,
        content=b'data: {"choices":[{"delta":{"content":"par"}}]}\n\ndata: {oops\n\n',
        headers={"content-type": "text/event-stream"},
    )
    recorder = _Recorder([truncated, _ok_stream("should not be used")])
    async with GroqLadder(
        _settings(), transport=httpx.MockTransport(recorder)
    ) as ladder:
        with pytest.raises(StreamInterrupted):
            await _drain(ladder)

    assert recorder.models_tried == ["rung-1"]


@pytest.mark.anyio
async def test_reasoning_is_not_emitted_as_content(three_rungs: None) -> None:
    """gpt-oss emits reasoning before content; it must never reach the client."""
    response = httpx.Response(
        200,
        content=_sse(
            {"choices": [{"delta": {"reasoning": "the user greeted me, so"}}]},
            _content_chunk("Hello."),
            _usage_chunk(12, 3),
        ),
        headers={"content-type": "text/event-stream"},
    )
    async with GroqLadder(
        _settings(), transport=httpx.MockTransport(_Recorder([response]))
    ) as ladder:
        text, completed = await _drain(ladder)

    assert text == "Hello."
    assert completed is not None
    assert completed.completion_tokens == 3


@pytest.mark.anyio
async def test_reasoning_effort_sent_only_for_reasoning_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Settings,
        "ladder",
        property(
            lambda self: (
                LadderRung(
                    model="openai/gpt-oss-20b",
                    reasoning_effort="low",
                    requests_per_day=1000,
                    tokens_per_minute=8000,
                ),
            )
        ),
    )
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _ok_stream()

    async with GroqLadder(
        _settings(), transport=httpx.MockTransport(handler)
    ) as ladder:
        await _drain(ladder)

    # Without this the model burns its completion budget on reasoning tokens and
    # returns an empty string with finish_reason=length.
    assert sent[0]["reasoning_effort"] == "low"
