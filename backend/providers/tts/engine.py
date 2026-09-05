"""TTS engine: cache, per-backend circuit breakers, and an ordered chain.

INSTRUCTIONS.md §4 requires the fallback be **automatic**, not a config flag:
"N consecutive failures trips to Piper automatically, with periodic retry to
trip back. A manual flag requires a human to notice at 3am."

The chain is Fish -> edge -> Piper, in descending order of quality and
ascending order of reliability:

- **Fish Audio** has the voice we want but is slow (1.9-3.0 s) and depends on a
  third party.
- **edge-tts** is fast (555 ms) but is an unofficial wrapper with a history of
  sudden 403s.
- **Piper** is slow (~2.6 s, RTF 0.77x) but runs locally and cannot be switched
  off by anyone else. It is the floor.

Each backend carries its own breaker, so a Fish outage does not implicate
edge-tts and vice versa. The config flag still exists for forcing one backend
during benchmarking.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from .base import Audio, TTSBackend, TTSUnavailable, VoiceProfile
from .cache import AudioCache, cache_key

log = logging.getLogger("assistant.tts")


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    PROBING = "probing"


@dataclass
class _Breaker:
    threshold: int
    cooldown_s: float
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = field(default=0.0)

    def available(self) -> bool:
        if self.state is not BreakerState.OPEN:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown_s:
            self.state = BreakerState.PROBING
            return True
        return False

    def record_success(self) -> bool:
        """Returns True if this closed a previously open breaker."""
        reopened = self.state is not BreakerState.CLOSED
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        return reopened

    def record_failure(self) -> bool:
        """Returns True if this opened the breaker."""
        self.consecutive_failures += 1
        if (
            self.state is BreakerState.PROBING
            or self.consecutive_failures >= self.threshold
        ):
            was_open = self.state is BreakerState.OPEN
            self.state = BreakerState.OPEN
            self.opened_at = time.monotonic()
            return not was_open
        return False


class TTSEngine:
    def __init__(
        self,
        *,
        backends: Sequence[TTSBackend],
        cache: AudioCache,
        failure_threshold: int = 3,
        cooldown_s: float = 300.0,
        force_backend: str | None = None,
    ) -> None:
        if not backends:
            raise ValueError("TTSEngine needs at least one backend")
        self._backends = list(backends)
        self._cache = cache
        self._force_backend = force_backend
        self._breakers = {
            b.name: _Breaker(threshold=failure_threshold, cooldown_s=cooldown_s)
            for b in backends
        }

    @property
    def cache(self) -> AudioCache:
        return self._cache

    def states(self) -> dict[str, str]:
        return {name: b.state.value for name, b in self._breakers.items()}

    def _chain(self) -> list[TTSBackend]:
        if self._force_backend:
            forced = [b for b in self._backends if b.name == self._force_backend]
            if forced:
                return forced
            log.warning(
                "tts_force_backend names an unknown backend; ignoring",
                extra={"requested": self._force_backend},
            )
        return self._backends

    def _key(self, backend: TTSBackend, profile: VoiceProfile, text: str) -> str:
        # Backend participates in the key: without it a breaker trip serves the
        # previous backend's bytes under the new backend's assumptions (§4).
        return cache_key(
            backend=backend.name,
            voice=profile.for_backend(backend.name),
            prosody=profile.prosody,
            text=text,
        )

    async def synthesize(
        self, text: str, profile: VoiceProfile, *, pin: bool = False
    ) -> Audio:
        """Synthesise one sentence, from cache when possible.

        Raises TTSUnavailable only when every backend in the chain has failed.
        """
        failures: list[str] = []

        for backend in self._chain():
            breaker = self._breakers[backend.name]

            key = self._key(backend, profile, text)
            if (cached := self._cache.get(key)) is not None:
                # A cache hit needs no backend, so an open breaker is irrelevant.
                return cached

            if not breaker.available():
                failures.append(f"{backend.name}: breaker open")
                continue

            try:
                audio = await backend.synthesize(
                    text, profile.for_backend(backend.name), profile.prosody
                )
            except TTSUnavailable as e:
                failures.append(f"{backend.name}: {e}")
                if breaker.record_failure():
                    log.error(
                        "tts breaker opened",
                        extra={"backend": backend.name, "error": str(e)[:200]},
                    )
                else:
                    log.warning(
                        "tts backend failed",
                        extra={
                            "backend": backend.name,
                            "consecutive": breaker.consecutive_failures,
                            "error": str(e)[:200],
                        },
                    )
                continue

            if breaker.record_success():
                log.info("tts breaker closed", extra={"backend": backend.name})
            self._cache.put(key, audio, pinned=pin)
            return audio

        raise TTSUnavailable("; ".join(failures))

    async def warm(self, phrases: Sequence[str], profile: VoiceProfile) -> int:
        """Pre-render fixed phrases at startup and pin them (§7.2).

        These are identical every call and cost seconds each otherwise -- which
        matters far more now that the primary backend takes ~2 s. Pinned so the
        greeting is not evicted by exactly the load that makes it matter.
        Failures are logged, not raised: a cold cache is slower, not broken.
        """
        warmed = 0
        for phrase in phrases:
            try:
                await self.synthesize(phrase, profile, pin=True)
                warmed += 1
            except TTSUnavailable as e:
                log.warning(
                    "could not pre-render fixed phrase",
                    extra={"profile": profile.label, "error": str(e)[:200]},
                )
        log.info(
            "tts warm-up complete",
            extra={"profile": profile.label, "warmed": warmed, "total": len(phrases)},
        )
        return warmed
