"""Per-model quota ledger.

INSTRUCTIONS.md §6. Phase 1's ladder was purely reactive: send, get a 429, try
the next rung. That burns a request to discover exhaustion and adds a full
round-trip to the turn that finds out. This tracks what is left per model and
refuses *before* dispatch.

Two sources of truth, in priority order:

1. **Provider headers.** Every Groq response carries x-ratelimit-remaining-*
   and x-ratelimit-reset-*. When those are fresh they are authoritative -- they
   account for usage from anywhere, including other processes sharing the key.
2. **Local sliding windows.** Between calls, and before the first response of a
   window, we decrement our own estimate.

The plan called for windows aligned to the provider's wall-clock boundaries.
Reconciling against the provider's own reset headers is strictly better: it
needs no guess about when Groq's day rolls over. The local windows are a
conservative fallback, and sliding windows never over-estimate availability --
they err toward refusing early, which is the safe direction when the
alternative is dropping a caller's turn.

Rate limits are per-organization *and per model*, so each rung is an
independent bucket and they stack (§4).
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field

from .config import LadderRung

log = logging.getLogger("assistant.quota")

# Groq reports resets as "7.66s", "2m59.56s", "1h30m". Parsed leniently: an
# unreadable value simply means we fall back to the local windows.
_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?")


def parse_reset(value: str | None) -> float | None:
    """Seconds until reset, or None if unparseable."""
    if not value:
        return None
    match = _DURATION.fullmatch(value.strip())
    if match is None or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _parse_int(value: str | None, field: str) -> int | None:
    """Parse a rate-limit header, keeping the local estimate if it is garbage.

    Not silently swallowed: an unreadable header means the ledger carries on
    with a stale figure, which is exactly the kind of thing that is impossible
    to diagnose later if nothing records it.
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        log.debug(
            "unparseable rate-limit header", extra={"field": field, "value": value}
        )
        return None


class SlidingWindow:
    """Counts consumption over a trailing period."""

    def __init__(self, limit: int, period_s: float) -> None:
        self.limit = limit
        self.period_s = period_s
        self._events: deque[tuple[float, int]] = deque()
        self._used = 0

    def _expire(self, now: float) -> None:
        while self._events and now - self._events[0][0] > self.period_s:
            _, amount = self._events.popleft()
            self._used -= amount

    def used(self, now: float | None = None) -> int:
        self._expire(now if now is not None else time.monotonic())
        return self._used

    def remaining(self, now: float | None = None) -> int:
        return max(0, self.limit - self.used(now))

    def consume(self, amount: int) -> None:
        now = time.monotonic()
        self._expire(now)
        self._events.append((now, amount))
        self._used += amount


@dataclass
class ModelQuota:
    """What is left for one model, from both sources."""

    rung: LadderRung
    requests_minute: SlidingWindow = field(init=False)
    requests_day: SlidingWindow = field(init=False)
    tokens_minute: SlidingWindow = field(init=False)
    tokens_day: SlidingWindow = field(init=False)

    # Provider-reported. None until we have seen a response.
    reported_requests: int | None = None
    reported_tokens: int | None = None
    reported_at: float | None = None
    requests_reset_at: float | None = None
    tokens_reset_at: float | None = None

    # Set by a 429's retry-after. While cold, the rung is skipped outright.
    cold_until: float | None = None

    def __post_init__(self) -> None:
        r = self.rung
        self.requests_minute = SlidingWindow(r.requests_per_minute, 60.0)
        self.requests_day = SlidingWindow(r.requests_per_day, 86_400.0)
        self.tokens_minute = SlidingWindow(r.tokens_per_minute, 60.0)
        self.tokens_day = SlidingWindow(r.tokens_per_day, 86_400.0)

    def refusal(self, estimated_tokens: int) -> str | None:
        """Why this rung cannot serve the request, or None if it can."""
        now = time.monotonic()

        if self.cold_until is not None:
            if now < self.cold_until:
                return f"cold for {self.cold_until - now:.0f}s (429 retry-after)"
            self.cold_until = None

        # Provider numbers win while they are still inside their window.
        if (
            self.reported_requests is not None
            and self._requests_report_fresh(now)
            and self.reported_requests <= 0
        ):
            return "provider reports 0 requests remaining"
        if (
            self.reported_tokens is not None
            and self._tokens_report_fresh(now)
            and self.reported_tokens < estimated_tokens
        ):
            return (
                f"provider reports {self.reported_tokens} tokens remaining, "
                f"need ~{estimated_tokens}"
            )

        if self.requests_minute.remaining(now) < 1:
            return "local requests/minute exhausted"
        if self.requests_day.remaining(now) < 1:
            return "local requests/day exhausted"
        if self.tokens_minute.remaining(now) < estimated_tokens:
            return (
                f"local tokens/minute would be exceeded "
                f"({self.tokens_minute.remaining(now)} left, need ~{estimated_tokens})"
            )
        if self.tokens_day.remaining(now) < estimated_tokens:
            return "local tokens/day would be exceeded"
        return None

    def _requests_report_fresh(self, now: float) -> bool:
        return self.requests_reset_at is None or now < self.requests_reset_at

    def _tokens_report_fresh(self, now: float) -> bool:
        return self.tokens_reset_at is None or now < self.tokens_reset_at

    def record_dispatch(self, estimated_tokens: int) -> None:
        self.requests_minute.consume(1)
        self.requests_day.consume(1)
        self.tokens_minute.consume(estimated_tokens)
        self.tokens_day.consume(estimated_tokens)
        # Decrement the provider's figure too, so a burst between responses does
        # not keep re-reading a stale "plenty left".
        if self.reported_requests is not None:
            self.reported_requests -= 1
        if self.reported_tokens is not None:
            self.reported_tokens -= estimated_tokens

    def reconcile(
        self,
        *,
        remaining_requests: str | None,
        remaining_tokens: str | None,
        reset_requests: str | None,
        reset_tokens: str | None,
        actual_tokens: int,
        estimated_tokens: int,
    ) -> None:
        """Correct the local estimate against what actually happened."""
        now = time.monotonic()

        # Fix the token windows: we charged an estimate, the truth is known now.
        if (correction := actual_tokens - estimated_tokens) != 0:
            self.tokens_minute.consume(correction)
            self.tokens_day.consume(correction)

        if (parsed := _parse_int(remaining_requests, "remaining-requests")) is not None:
            self.reported_requests = parsed
        if (parsed := _parse_int(remaining_tokens, "remaining-tokens")) is not None:
            self.reported_tokens = parsed
        if (secs := parse_reset(reset_requests)) is not None:
            self.requests_reset_at = now + secs
        if (secs := parse_reset(reset_tokens)) is not None:
            self.tokens_reset_at = now + secs
        self.reported_at = now

    def mark_cold(self, retry_after_s: float) -> None:
        self.cold_until = time.monotonic() + retry_after_s


def estimate_tokens(texts: list[str], *, max_output: int) -> int:
    """Rough pre-dispatch token estimate.

    ~4 characters per token is the usual English approximation. It only needs to
    be good enough to avoid dispatching into a wall; the real figure replaces it
    in reconcile(). Deliberately rounds up -- under-estimating causes the 429s
    this class exists to prevent.
    """
    chars = sum(len(t) for t in texts)
    return chars // 3 + max_output


class QuotaLedger:
    def __init__(self, ladder: tuple[LadderRung, ...]) -> None:
        self._quotas = {rung.model: ModelQuota(rung) for rung in ladder}

    def get(self, rung: LadderRung) -> ModelQuota:
        return self._quotas[rung.model]

    def refusal(self, rung: LadderRung, estimated_tokens: int) -> str | None:
        return self._quotas[rung.model].refusal(estimated_tokens)

    def all_exhausted(self, estimated_tokens: int) -> bool:
        return all(
            q.refusal(estimated_tokens) is not None for q in self._quotas.values()
        )

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Remaining headroom per model, for the periodic INFO log (§6)."""
        return {
            model: {
                "req_min": q.requests_minute.remaining(),
                "req_day": q.requests_day.remaining(),
                "tok_min": q.tokens_minute.remaining(),
                "tok_day": q.tokens_day.remaining(),
            }
            for model, q in self._quotas.items()
        }
