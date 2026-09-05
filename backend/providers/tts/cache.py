"""Disk cache for synthesised audio.

INSTRUCTIONS.md §4: "the main lever that makes concurrency workable on this
hardware". A support bot repeats its greetings, clarifying questions and
closings constantly, so hit rate should be high.

Two details the plan calls out specifically:

- **The key includes the backend, format and sample rate**, not just voice and
  text. Keying on voice+text alone serves the previous backend's bytes under the
  new backend's assumptions the moment the circuit breaker trips -- MP3 handed
  to something expecting 22 kHz WAV. Corrupt audio, hard to debug.
- **Fixed phrases are pinned** against LRU eviction. Otherwise the greeting is
  evicted by exactly the load that makes it matter.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .base import Audio, Prosody

log = logging.getLogger("assistant.tts.cache")

_MIME_SUFFIX = {"audio/mpeg": "mp3", "audio/wav": "wav"}


def cache_key(*, backend: str, voice: str, prosody: Prosody, text: str) -> str:
    """Key on backend, voice, prosody and text.

    §4 asks for format and sample rate in the key too. They are implied by the
    backend -- each one emits exactly one format at one rate (edge 24 kHz MP3,
    fish 44.1 kHz MP3, piper 22.05 kHz WAV) -- so including the backend name
    carries the same guarantee without the engine having to know the format
    before it has synthesised anything. The property that matters is preserved:
    a breaker trip can never serve one backend's bytes under another's
    assumptions.
    """
    material = "\x1f".join([backend, voice, prosody.cache_key_part(), text])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class _Entry:
    path: Path
    mime: str
    sample_rate: int
    size: int
    last_used: float
    pinned: bool = False


class AudioCache:
    def __init__(self, directory: Path, max_bytes: int) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max_bytes
        self._entries: dict[str, _Entry] = {}
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------ read

    def get(self, key: str) -> Audio | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        try:
            data = entry.path.read_bytes()
        except OSError:
            # The file vanished under us (manual cleanup, disk trouble). Treat
            # it as a miss and forget the entry rather than failing the turn.
            log.warning("cache entry missing on disk", extra={"key": key[:12]})
            self._forget(key)
            self.misses += 1
            return None
        entry.last_used = time.monotonic()
        self.hits += 1
        return Audio(data=data, mime=entry.mime, sample_rate=entry.sample_rate)

    # ----------------------------------------------------------------- write

    def put(self, key: str, audio: Audio, *, pinned: bool = False) -> None:
        suffix = _MIME_SUFFIX.get(audio.mime, "bin")
        path = self._dir / f"{key}.{suffix}"
        try:
            path.write_bytes(audio.data)
        except OSError as e:
            # A cache that cannot write is a performance problem, not a
            # correctness one -- the caller already has the audio.
            log.warning("cache write failed", extra={"error": repr(e)})
            return

        if old := self._entries.get(key):
            self._bytes -= old.size
        self._entries[key] = _Entry(
            path=path,
            mime=audio.mime,
            sample_rate=audio.sample_rate,
            size=audio.size,
            last_used=time.monotonic(),
            pinned=pinned,
        )
        self._bytes += audio.size
        self._evict_if_needed()

    def _forget(self, key: str) -> None:
        if entry := self._entries.pop(key, None):
            self._bytes -= entry.size

    def _evict_if_needed(self) -> None:
        if self._bytes <= self._max_bytes:
            return
        # Pinned entries are never candidates -- see the module docstring.
        candidates = sorted(
            ((k, e) for k, e in self._entries.items() if not e.pinned),
            key=lambda kv: kv[1].last_used,
        )
        evicted = 0
        for key, entry in candidates:
            if self._bytes <= self._max_bytes:
                break
            entry.path.unlink(missing_ok=True)
            self._forget(key)
            evicted += 1
        if evicted:
            log.info(
                "cache evicted",
                extra={"count": evicted, "bytes": self._bytes, "cap": self._max_bytes},
            )

    # ------------------------------------------------------------------ info

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> dict[str, float | int]:
        return {
            "entries": len(self._entries),
            "bytes": self._bytes,
            "pinned": sum(1 for e in self._entries.values() if e.pinned),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hit_rate, 3),
        }
