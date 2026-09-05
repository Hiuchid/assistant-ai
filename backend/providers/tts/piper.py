"""Piper backend -- the fallback that can never be switched off.

INSTRUCTIONS.md §4. Two things matter here.

**Invoked as a subprocess, never imported.** Piper moved from `rhasspy/piper`
(MIT, archived Oct 2025) to `OHF-Voice/piper1-gpl` under GPL-3.0. Personal use
involves no distribution so no obligation is triggered today, but subprocess
invocation is arm's-length, is better process isolation regardless, and keeps a
commercial pivot open. Do not "simplify" this into an import.

**It writes a WAV file and needs a seekable output.** Passing `-f /dev/stdout`
through a pipe fails, because it seeks back to patch the RIFF header once the
frame count is known. That cost an hour during the Phase 2 benchmark; hence the
temporary file.

Measured on this box: ~2.6 s wall clock, **RTF 0.77x median** for a one-sentence
reply. That passes the <1.0x gate, but 2.6 seconds is far too slow to be primary
-- it is the emergency path, and it will sound noticeably laboured when it runs.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from .base import Audio, Prosody, TTSUnavailable

log = logging.getLogger("assistant.tts.piper")

MIME = "audio/wav"
SAMPLE_RATE = 22050

# Piper has no pitch control. Rate is approximated by length_scale, where
# larger is slower; 1.0 is the model's natural pace.
_BASE_LENGTH_SCALE = 1.0


def _length_scale(prosody: Prosody) -> float:
    """Translate an edge-tts style rate ("-12%") into Piper's length_scale."""
    raw = prosody.rate.strip().rstrip("%")
    try:
        percent = float(raw)
    except ValueError:
        return _BASE_LENGTH_SCALE
    # -12% speed means it should take longer: scale 1/(1+p).
    return _BASE_LENGTH_SCALE / (1.0 + percent / 100.0)


class PiperTTS:
    name = "piper"

    def __init__(self, binary: Path, model: Path, timeout_s: float = 60.0) -> None:
        self._binary = binary
        self._model = model
        self._timeout_s = timeout_s

    async def synthesize(self, text: str, voice: str, prosody: Prosody) -> Audio:
        # `voice` is ignored: the voice is the model file this instance was
        # constructed with. Swapping voices means a second instance.
        if not self._model.exists():
            raise TTSUnavailable(f"piper voice model missing: {self._model}")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out = Path(tmp.name)
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._binary),
                "-m", str(self._model),
                "-f", str(out),
                "--length-scale", f"{_length_scale(prosody):.3f}",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    process.communicate(text.encode("utf-8")), timeout=self._timeout_s
                )
            except TimeoutError as e:
                process.kill()
                await process.wait()
                raise TTSUnavailable(
                    f"piper timed out after {self._timeout_s}s"
                ) from e

            if process.returncode != 0:
                detail = stderr.decode(errors="replace")[-200:]
                raise TTSUnavailable(f"piper exited {process.returncode}: {detail}")

            data = out.read_bytes()
            if not data:
                raise TTSUnavailable("piper produced no audio")
            return Audio(data=data, mime=MIME, sample_rate=SAMPLE_RATE)
        finally:
            out.unlink(missing_ok=True)
