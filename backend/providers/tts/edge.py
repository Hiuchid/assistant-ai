"""edge-tts backend.

INSTRUCTIONS.md §4: primary, because server-side it is an outbound HTTP call and
costs the VPS essentially no CPU. Measured on this box at **555 ms median** for a
one-sentence reply, inside the ~600 ms gate.

It is an unofficial wrapper around Microsoft Edge's read-aloud endpoint, with
undocumented limits and a real history of breaking -- recurring 403 /
WSServerHandshakeError failures tied to the Sec-MS-GEC token, reported as
recently as Jan 2026. The version is pinned and the engine wraps this in a
circuit breaker. Expect it to break; do not be surprised when it does.
"""

from __future__ import annotations

import logging

import edge_tts

from .base import Audio, Prosody, TTSUnavailable

log = logging.getLogger("assistant.tts.edge")

# edge-tts returns MP3. Sent to the browser as-is: decodeAudioData sniffs the
# container, so no transcoding happens anywhere (see base.py).
MIME = "audio/mpeg"
SAMPLE_RATE = 24000


class EdgeTTS:
    name = "edge"

    async def synthesize(self, text: str, voice: str, prosody: Prosody) -> Audio:
        chunks: list[bytes] = []
        try:
            stream = edge_tts.Communicate(
                text, voice, rate=prosody.rate, pitch=prosody.pitch
            ).stream()
            async for chunk in stream:
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
        except Exception as e:
            # Deliberately broad: edge-tts raises aiohttp errors, its own
            # NoAudioReceived, and bare RuntimeErrors depending on how Microsoft
            # is failing today. The engine only needs to know it did not work.
            raise TTSUnavailable(f"edge-tts failed: {type(e).__name__}: {e}") from e

        data = b"".join(chunks)
        if not data:
            # A successful call returning nothing is the documented shape of
            # some throttling responses. Must raise, not return empty audio --
            # the circuit breaker counts exceptions (see base.TTSBackend).
            raise TTSUnavailable("edge-tts returned no audio")

        return Audio(data=data, mime=MIME, sample_rate=SAMPLE_RATE)
