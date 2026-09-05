"""Sentence-boundary splitting for the LLM token stream.

INSTRUCTIONS.md §7.1: as soon as the first complete sentence arrives, hand it to
TTS and start streaming audio back while the LLM keeps generating. Waiting for
the full reply costs seconds the latency budget does not have.

Deliberately simple. A full NLP sentence tokenizer would be more accurate and
would also be CPU on the request path, which §2 forbids. The failure mode of
getting this slightly wrong is a marginally odd pause, not a broken reply.
"""

from __future__ import annotations

import re

# Terminator followed by whitespace, or end of input.
_BOUNDARY = re.compile(r"(?<=[.!?])[\"')\]]*\s")

# Abbreviations that end in a period but do not end a sentence. Short list on
# purpose -- the assistant's replies are plain spoken English, not prose with
# citations.
_ABBREVIATIONS = frozenset(
    {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "no", "vs", "etc", "e.g", "i.e"}
)

# Below this, a "sentence" is usually an initial or a decimal that slipped
# through. Holding it back and letting it merge with what follows sounds better
# than synthesising a two-word fragment on its own.
MIN_SENTENCE_CHARS = 12


def _is_false_boundary(buffer: str, end: int) -> bool:
    """True if the terminator at `end` is an abbreviation or a decimal point."""
    head = buffer[:end].rstrip()
    if not head.endswith("."):
        return False
    # 3.14 -- digit either side of the period.
    if len(head) >= 2 and head[-2].isdigit() and end < len(buffer):
        after = buffer[end:].lstrip()
        if after and after[0].isdigit():
            return True
    last_word = re.split(r"[\s(\[]", head[:-1])[-1].lower()
    return last_word in _ABBREVIATIONS


class SentenceSplitter:
    """Accumulates streamed tokens and emits complete sentences."""

    def __init__(self, min_chars: int = MIN_SENTENCE_CHARS) -> None:
        self._buffer = ""
        self._min_chars = min_chars

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return any sentences that are now complete."""
        self._buffer += text
        out: list[str] = []

        while True:
            match = _BOUNDARY.search(self._buffer)
            if match is None:
                break
            end = match.start() + 1
            if _is_false_boundary(self._buffer, end):
                # Skip past this terminator and keep looking in the same buffer.
                nxt = _BOUNDARY.search(self._buffer, match.end())
                if nxt is None:
                    break
                end = nxt.start() + 1

            candidate = self._buffer[:end].strip()
            if len(candidate) < self._min_chars:
                # Too short to stand alone; wait for more text to merge with.
                break
            out.append(candidate)
            self._buffer = self._buffer[match.end():]

        return out

    def flush(self) -> str | None:
        """Whatever is left when the stream ends.

        The last sentence of a reply usually has no trailing whitespace, so it
        never matches the boundary pattern -- this is how it gets spoken.
        """
        remainder = self._buffer.strip()
        self._buffer = ""
        return remainder or None
