"""Speech-to-text via Groq Whisper.

INSTRUCTIONS.md §4: `whisper-large-v3-turbo`, offloaded to the API because §2
forbids running Whisper on these cores. The browser sends `webm/opus` straight
from MediaRecorder -- Groq accepts standard containers, so no PCM conversion and
no server-side transcoding.

**Groq counts a 10-second minimum per request**, free tier included. A one-word
"yes" costs the same as ten seconds of speech, which is why the client merges
very short segments before upload (§7.3) and why the budget in §6 is expressed
in requests rather than audio seconds.

Free tier: 20 requests/minute, 2,000/day, 7,200 audio-seconds/hour, 28,800/day.
The 20 RPM ceiling is what sets the voice concurrency cap in §6 -- not CPU.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from ..config import Settings

log = logging.getLogger("assistant.stt")

# Groq rejects uploads above 25 MB on the free tier. An utterance is orders of
# magnitude smaller; anything near this is a bug or an attack, not speech.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Transcript:
    text: str
    stt_ms: float


class STTUnavailable(RuntimeError):
    """Transcription failed. The caller decides whether to ask them to repeat."""


class GroqWhisper:
    def __init__(self, settings: Settings) -> None:
        self._model = settings.stt_model
        self._language = settings.stt_language
        self._client = httpx.AsyncClient(
            base_url=settings.groq_base_url,
            timeout=httpx.Timeout(settings.stt_timeout_s, connect=10.0),
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                # Groq is behind Cloudflare and rejects unusual User-Agents
                # with "error code: 1010".
                "User-Agent": settings.user_agent,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def transcribe(self, audio: bytes, *, mime: str = "audio/webm") -> Transcript:
        if not audio:
            raise STTUnavailable("empty audio")
        if len(audio) > MAX_UPLOAD_BYTES:
            raise STTUnavailable(f"audio too large: {len(audio)} bytes")

        started = time.perf_counter()
        try:
            response = await self._client.post(
                "/audio/transcriptions",
                files={"file": ("utterance.webm", audio, mime)},
                data={
                    "model": self._model,
                    # Pinning the language stops Whisper hallucinating a
                    # translation on short or noisy clips. §14 revisits this
                    # when Arabic arrives -- code-switching needs auto-detect.
                    "language": self._language,
                    "response_format": "json",
                    # Whisper invents plausible-sounding speech for silence.
                    # Temperature 0 makes that less likely, not impossible --
                    # the caller still filters empty-ish results.
                    "temperature": "0",
                },
            )
        except httpx.HTTPError as e:
            raise STTUnavailable(f"stt request failed: {type(e).__name__}: {e}") from e

        stt_ms = (time.perf_counter() - started) * 1000

        if response.status_code != 200:
            detail = response.text[:200]
            if response.status_code == 429:
                # 20 RPM account-wide. The concurrency cap should prevent this;
                # seeing it means the cap needs re-deriving (§6).
                raise STTUnavailable(f"stt rate limited: {detail}")
            raise STTUnavailable(f"stt HTTP {response.status_code}: {detail}")

        text = str(response.json().get("text", "")).strip()
        log.info(
            "stt complete",
            extra={"stt_ms": round(stt_ms), "chars": len(text), "bytes": len(audio)},
        )
        if not text:
            raise STTUnavailable("no speech detected")
        return Transcript(text=text, stt_ms=stt_ms)
