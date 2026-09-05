"""Repair transliterated English in Arabic transcripts.

Lebanese speakers mix English into Arabic constantly, and Whisper writes those
English words in Arabic letters: `quote` comes back as `كووت`, `printing` as
`البرينتنغ`, `please` as `تبليز`.

Nothing fixes this at the transcription step. Measured (§14):

- forcing `ar` -> mangled
- no language at all -> detects Arabic, same mangling
- a Whisper `prompt` seeded with the English words in Latin script -> no
  improvement whatsoever

So it is repaired afterwards. A single cheap completion rewrites the
transliterated words back into Latin script and leaves genuine Arabic alone.
Measured on qwen3.8-27b: ~150 ms and ~220 tokens, which is cheap enough to run
on every Arabic utterance rather than trying to guess which ones need it.

Two things it deliberately does not do: translate, or correct grammar. The
transcript is a record of what someone said, and the moment it starts being
improved it stops being that.
"""

from __future__ import annotations

import logging

from .providers.llm import GroqLadder, LadderExhausted, Message, Token

log = logging.getLogger("assistant.repair")

# Short utterances are almost always greetings or a yes/no, where there is
# nothing to repair and the round trip is pure cost.
MIN_WORDS = 3

SYSTEM = """\
You repair Arabic speech-to-text from Lebanese speakers, who mix English into \
Arabic constantly. The recogniser writes those English words in Arabic letters, \
where they read as nonsense. Write them back in Latin letters.

Worked examples:
  كووت -> quote          البرينتنغ -> printing      تبليز -> please
  روجين / أورجنت -> urgent   إيميل -> email          ميتينغ -> meeting
  أوردر -> order         ديزاين -> design           بروجكت -> project
  ديليفري -> delivery    بدجت -> budget             أونلاين -> online
  واتساب -> WhatsApp     بوكينغ -> booking          كانسل -> cancel

If the Arabic definite article "ال" is stuck to an English word, separate it: \
"الأوردر" becomes "الـ order", never "الorder".

Rules:
- Change ONLY words that are English written in Arabic script.
- Leave every genuine Arabic word exactly as it is. Do not translate Arabic, \
do not fix grammar, do not add or remove words, do not add punctuation.
- A word that reads as normal Arabic is Arabic. Do not force English onto it.
- Names of people and places are not English words. Leave them.
- If nothing needs changing, return the input unchanged.
- Return ONLY the repaired text, with no explanation and no tags.\
"""


def _looks_wrong(original: str, repaired: str) -> bool:
    """Reject a repair that clearly did something other than asked.

    A model that starts explaining itself, or returns half the sentence, has
    not repaired anything -- and a mangled transcript is still better than a
    truncated or invented one.
    """
    if not repaired:
        return True
    if "<think" in repaired or "\n" in repaired.strip():
        return True
    # Latin script is added, never removed, so the result should not shrink
    # much. Half the length means it dropped content.
    return len(repaired) < len(original) * 0.6


async def repair_arabic(text: str, ladder: GroqLadder) -> str:
    """Return the transcript with English words restored, or the input as-is.

    Never raises and never returns something worse than it was given: every
    failure path falls back to the original text.
    """
    if len(text.split()) < MIN_WORDS:
        return text

    messages: list[Message] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": text},
    ]
    try:
        chunks: list[str] = []
        async for event in ladder.stream(messages):
            if isinstance(event, Token):
                chunks.append(event.text)
        repaired = "".join(chunks).strip()
    except LadderExhausted:
        # Out of quota is not a reason to lose the transcript.
        log.info("transcript repair skipped: no model available")
        return text
    except Exception as e:
        log.warning("transcript repair failed", extra={"error": repr(e)})
        return text

    if _looks_wrong(text, repaired):
        log.warning("transcript repair rejected", extra={"chars": len(repaired)})
        return text
    if repaired != text:
        log.info("transcript repaired", extra={"before": len(text), "after": len(repaired)})
    return repaired
