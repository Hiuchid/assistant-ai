"""Fish Audio backend.

Chosen for character rather than speed. Measured on this box: **1.9-3.0 s per
sentence**, against edge-tts's 555 ms. That is a deliberate trade -- see §4 --
and it is why the fixed phrases are pre-rendered and pinned at startup, and why
the cache matters more with this backend than with any other.

The `s2.1-pro-free` model is free with **no request-count limit**, only a
concurrency cap of 5. That is the opposite shape to Groq's Orpheus (fast, but
100 requests/day) and is what makes it usable at all.

Free-tier output is limited to **personal, non-commercial use** by Fish Audio's
terms. This assistant is personal, so that fits -- but it would block a
commercial pivot, and that constraint should be re-read before any such change.

Voices are community-uploaded models referenced by `reference_id`. The owner
profile uses a Jarvis voice; note it is a third-party clone of a real actor's
voice, which is the reason visitor mode uses a generic butler instead (§4).
"""

from __future__ import annotations

import logging

import httpx

from .base import Audio, Prosody, TTSUnavailable

log = logging.getLogger("assistant.tts.fish")

MIME = "audio/mpeg"
SAMPLE_RATE = 44100

_ENDPOINT = "https://api.fish.audio/v1/tts"


class FishAudioTTS:
    name = "fish"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "s2.1-pro-free",
        timeout_s: float = 30.0,
        user_agent: str = "assistant-ai/0.1",
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # Selects the free tier. Sent as a header, not a body field.
                "model": model,
                "User-Agent": user_agent,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def synthesize(self, text: str, voice: str, prosody: Prosody) -> Audio:
        # Fish exposes no rate/pitch controls; prosody is carried in the voice
        # model itself. Accepted for interface compatibility and ignored, which
        # is why the cache key still includes it -- switching backends must not
        # collide with entries synthesised under different prosody.
        if not voice:
            raise TTSUnavailable("fish requires a reference_id; none in this profile")

        try:
            response = await self._client.post(
                _ENDPOINT,
                json={"text": text, "reference_id": voice, "format": "mp3"},
            )
        except httpx.HTTPError as e:
            raise TTSUnavailable(f"fish request failed: {type(e).__name__}: {e}") from e

        if response.status_code != 200:
            detail = response.text[:200]
            if response.status_code == 429:
                # Concurrency cap (5), not a daily quota. Worth distinguishing:
                # backing off briefly fixes this, unlike a spent budget.
                raise TTSUnavailable(f"fish concurrency limited: {detail}")
            raise TTSUnavailable(f"fish HTTP {response.status_code}: {detail}")

        data = response.content
        if not data:
            raise TTSUnavailable("fish returned no audio")
        return Audio(data=data, mime=MIME, sample_rate=SAMPLE_RATE)
