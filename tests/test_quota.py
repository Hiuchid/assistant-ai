"""Quota ledger tests.

INSTRUCTIONS.md §12 names the ledger as one of the components that gets real
unit tests, with mocked 429s and a controllable clock. Phase 1.5's acceptance
criterion is that the ladder skips a rung on *predicted* exhaustion, not just on
an observed 429 -- proven here rather than by waiting to be rate limited.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.config import LadderRung, Settings
from backend.providers.llm import GroqLadder, LadderExhausted, Message, Token
from backend.quota import QuotaLedger, SlidingWindow, estimate_tokens, parse_reset

MESSAGES: list[Message] = [{"role": "user", "content": "hello"}]


def _rung(model: str, **kw: int) -> LadderRung:
    base: dict[str, int] = {
        "requests_per_minute": 30,
        "requests_per_day": 1000,
        "tokens_per_minute": 8000,
        "tokens_per_day": 200_000,
    }
    base.update(kw)
    return LadderRung(model=model, **base)  # type: ignore[arg-type]


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        groq_api_key="test-key", allowed_origins=["https://example.com"]
    )


# ---------------------------------------------------------------- primitives


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7.66s", 7.66),
        ("2m59.56s", 179.56),
        ("1h30m", 5400.0),
        ("", None),
        (None, None),
        ("not-a-duration", None),
    ],
)
def test_parse_reset(text: str | None, expected: float | None) -> None:
    result = parse_reset(text)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_sliding_window_expires() -> None:
    window = SlidingWindow(limit=10, period_s=60.0)
    window.consume(7)
    assert window.remaining() == 3
    # Age every event past the period without sleeping.
    window._events = type(window._events)((t - 120, n) for t, n in window._events)
    assert window.remaining() == 10


def test_estimate_rounds_up_rather_than_under() -> None:
    """Under-estimating causes the 429s the ledger exists to prevent."""
    estimate = estimate_tokens(["a" * 300], max_output=100)
    assert estimate >= 200


# ------------------------------------------------------------------- ledger


def test_refuses_when_request_budget_spent() -> None:
    ledger = QuotaLedger((_rung("m", requests_per_day=2),))
    rung = _rung("m", requests_per_day=2)
    assert ledger.refusal(rung, 10) is None
    ledger.get(rung).record_dispatch(10)
    ledger.get(rung).record_dispatch(10)
    assert "requests/day" in (ledger.refusal(rung, 10) or "")


def test_refuses_when_token_budget_too_small_for_this_turn() -> None:
    rung = _rung("m", tokens_per_minute=500)
    ledger = QuotaLedger((rung,))
    assert ledger.refusal(rung, 100) is None
    assert ledger.refusal(rung, 900) is not None


def test_provider_report_overrides_optimistic_local_state() -> None:
    """The key wins: usage from elsewhere must not be invisible to us."""
    rung = _rung("m")
    ledger = QuotaLedger((rung,))
    quota = ledger.get(rung)
    # Locally we think we are untouched, but Groq says the bucket is empty.
    quota.reconcile(
        remaining_requests="0",
        remaining_tokens="8000",
        reset_requests="60s",
        reset_tokens="60s",
        actual_tokens=10,
        estimated_tokens=10,
    )
    assert "provider reports 0 requests" in (ledger.refusal(rung, 10) or "")


def test_stale_provider_report_is_ignored_after_reset() -> None:
    rung = _rung("m")
    ledger = QuotaLedger((rung,))
    quota = ledger.get(rung)
    quota.reconcile(
        remaining_requests="0",
        remaining_tokens="0",
        reset_requests="1s",
        reset_tokens="1s",
        actual_tokens=10,
        estimated_tokens=10,
    )
    assert ledger.refusal(rung, 10) is not None
    # Once the reported window has passed, local state governs again.
    quota.requests_reset_at = quota.tokens_reset_at = 0.0
    assert ledger.refusal(rung, 10) is None


def test_reconcile_corrects_an_over_estimate() -> None:
    rung = _rung("m", tokens_per_minute=1000)
    ledger = QuotaLedger((rung,))
    quota = ledger.get(rung)
    quota.record_dispatch(800)
    assert quota.tokens_minute.remaining() == 200
    quota.reconcile(
        remaining_requests=None,
        remaining_tokens=None,
        reset_requests=None,
        reset_tokens=None,
        actual_tokens=100,
        estimated_tokens=800,
    )
    assert quota.tokens_minute.remaining() == 900


def test_cold_rung_is_skipped_then_recovers() -> None:
    rung = _rung("m")
    ledger = QuotaLedger((rung,))
    quota = ledger.get(rung)
    quota.mark_cold(30.0)
    assert "cold" in (ledger.refusal(rung, 10) or "")
    quota.cold_until = 0.0  # let the cooldown lapse
    assert ledger.refusal(rung, 10) is None


# ------------------------------------------------- ledger wired to the ladder


@pytest.mark.anyio
async def test_exhausted_rung_is_skipped_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Phase 1.5 criterion: predicted exhaustion, not a burnt request."""
    rungs = (_rung("rung-1", requests_per_day=0), _rung("rung-2"))
    monkeypatch.setattr(Settings, "ladder", property(lambda self: rungs))

    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(json.loads(request.content)["model"])
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                b'data: {"choices":[],"usage":'
                b'{"prompt_tokens":5,"completion_tokens":1}}\n\n'
                b"data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    async with GroqLadder(
        _settings(), transport=httpx.MockTransport(handler)
    ) as ladder:
        chunks = [
            e.text async for e in ladder.stream(MESSAGES) if isinstance(e, Token)
        ]
        text = "".join(chunks)

    assert text == "hi"
    # rung-1 was never contacted -- that is the whole point.
    assert tried == ["rung-2"]


@pytest.mark.anyio
async def test_all_rungs_exhausted_raises_without_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rungs = (_rung("a", requests_per_day=0), _rung("b", requests_per_day=0))
    monkeypatch.setattr(Settings, "ladder", property(lambda self: rungs))

    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append("called")
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    async with GroqLadder(
        _settings(), transport=httpx.MockTransport(handler)
    ) as ladder:
        with pytest.raises(LadderExhausted):
            async for _ in ladder.stream(MESSAGES):
                pass

    assert tried == []


@pytest.mark.anyio
async def test_429_marks_the_rung_cold_for_the_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rungs = (_rung("hot"), _rung("spare"))
    monkeypatch.setattr(Settings, "ladder", property(lambda self: rungs))

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "hot":
            return httpx.Response(429, json={}, headers={"retry-after": "30s"})
        return httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    async with GroqLadder(
        _settings(), transport=httpx.MockTransport(handler)
    ) as ladder:
        async for _ in ladder.stream(MESSAGES):
            pass
        # A second turn must not retry the rung we were just told to back off.
        assert "cold" in (ladder.ledger.refusal(rungs[0], 10) or "")
