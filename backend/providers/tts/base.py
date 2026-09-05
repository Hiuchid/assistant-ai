"""The TTS backend interface.

INSTRUCTIONS.md §4: one interface, two implementations, swappable by config and
by an automatic circuit breaker.

**Wire format decision (§4 required this be settled before building).**
edge-tts returns MP3; Piper returns 22.05 kHz WAV. The plan asked for one wire
format so the client has one decode path, while forbidding server-side
transcoding on the hot path -- the CPU cost §2 rules out.

Both are satisfied by sending each backend's native bytes with their mime type
and decoding in the browser with Web Audio's `decodeAudioData`, which sniffs the
container and handles MP3 and WAV alike. The client genuinely has one code path;
the server transcodes nothing.

This works because synthesis is per-sentence (§7.1), so every chunk is a
*complete* audio file rather than a fragment of a stream. `decodeAudioData`
cannot decode partial files, so this design and sentence splitting depend on
each other -- do not switch to mid-sentence chunking without replacing the
client's decoder with MediaSource.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Prosody:
    """Delivery settings, applied by backends that support them.

    §4: the Jarvis feel comes mostly from the persona prompt, with prosody a
    secondary lever -- slightly slower and lower reads as composed rather than
    chirpy. edge-tts takes these directly; Piper approximates rate via
    length_scale and ignores pitch.
    """

    rate: str = "+0%"
    pitch: str = "+0Hz"

    def cache_key_part(self) -> str:
        return f"{self.rate}|{self.pitch}"


NEUTRAL = Prosody()


@dataclass(frozen=True)
class VoiceProfile:
    """A voice, expressed once per backend that can produce it.

    Voice identifiers are backend-specific and not interchangeable: edge-tts
    takes names like "en-GB-RyanNeural", Fish Audio takes a 32-char
    reference_id, and Piper takes none at all (its voice is the model file it
    was constructed with). When the circuit breaker moves synthesis to another
    backend mid-conversation, the profile supplies that backend's own id rather
    than passing a meaningless string.

    §1 gives owner and visitor modes different voices, so there is one profile
    per mode.
    """

    label: str
    voices: dict[str, str]
    prosody: Prosody = NEUTRAL

    def for_backend(self, backend_name: str) -> str:
        # Piper and any other single-voice backend legitimately map to "".
        return self.voices.get(backend_name, "")

    def cache_key_part(self, backend_name: str) -> str:
        return f"{self.for_backend(backend_name)}|{self.prosody.cache_key_part()}"


@dataclass(frozen=True)
class Audio:
    """A complete, self-contained audio file for one sentence."""

    data: bytes
    mime: str
    sample_rate: int

    @property
    def size(self) -> int:
        return len(self.data)


class TTSBackend(Protocol):
    """A synthesiser.

    Implementations must be safe to call concurrently and must raise on
    failure rather than returning empty audio -- the circuit breaker counts
    exceptions, and silently returning nothing would look like success.
    """

    name: str

    async def synthesize(self, text: str, voice: str, prosody: Prosody) -> Audio: ...


class TTSUnavailable(RuntimeError):
    """This backend could not synthesise. The caller may try another."""
