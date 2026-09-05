"""The no-LLM capture path.

INSTRUCTIONS.md §6: when every rung of the ladder is exhausted, the assistant
must still capture the lead. It must not fail the conversation and it must not
drop the caller.

This is a fixed script, not a model. It asks four questions in order, records
the answers verbatim, and produces an item for human triage. Every line here is
also pre-rendered and pinned in the TTS cache (§7.2) so the degraded path costs
nothing at all -- no LLM, no synthesis, no quota.

Given §4's single point of failure (Groq serves both STT and the LLM), this path
is load-bearing rather than decorative. Test it deliberately; do not let it rot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Step(Enum):
    """Ordered: each answer advances to the next."""

    NAME = "name"
    REASON = "reason"
    CONTACT = "contact"
    WHEN = "when"
    DONE = "done"


# Deliberately plain. The caller should not be able to tell that anything is
# wrong, only that the assistant is being brisk.
PROMPTS: dict[Step, str] = {
    Step.NAME: "Of course. Could I take your name?",
    Step.REASON: "Thank you. And what's it regarding?",
    Step.CONTACT: "Got it. What's the best number or email to reach you on?",
    Step.WHEN: "Noted. Is there a time that suits you for a call back?",
}

OPENING = (
    "I'm afraid I'm having trouble with my connection, so let me just take your "
    "details and make sure this reaches the right person."
)

CLOSING = (
    "Thank you. I've recorded all of that and it will be passed on. "
    "Someone will be in touch."
)

_ORDER: list[Step] = [Step.NAME, Step.REASON, Step.CONTACT, Step.WHEN, Step.DONE]

# Every fixed line, for the Phase 2 cache warm-up. Pinned against LRU eviction
# so the greeting is never evicted by exactly the load that makes it matter.
ALL_FIXED_LINES: tuple[str, ...] = (OPENING, CLOSING, *PROMPTS.values())


@dataclass
class DegradedCapture:
    """A scripted interview. Deterministic, no model involved."""

    step: Step = Step.NAME
    answers: dict[Step, str] = field(default_factory=dict)
    started: bool = False

    def open(self) -> list[str]:
        """The lines to say when falling into degraded mode mid-conversation."""
        self.started = True
        return [OPENING, PROMPTS[Step.NAME]]

    def submit(self, text: str) -> list[str]:
        """Record an answer and return the next thing to say."""
        if self.step is Step.DONE:
            # Anything said after the script finishes is still worth keeping
            # rather than discarding, so append it to the reason.
            self.answers[Step.REASON] = (
                f"{self.answers.get(Step.REASON, '')}\n{text}".strip()
            )
            return [CLOSING]

        self.answers[self.step] = text.strip()
        self.step = _ORDER[_ORDER.index(self.step) + 1]

        if self.step is Step.DONE:
            return [CLOSING]
        return [PROMPTS[self.step]]

    @property
    def complete(self) -> bool:
        return self.step is Step.DONE

    def title(self) -> str:
        name = self.answers.get(Step.NAME, "").strip()
        reason = self.answers.get(Step.REASON, "").strip()
        if name and reason:
            return f"{name} - {reason}"[:120]
        if name:
            return f"Message from {name}"[:120]
        return "Message taken (no LLM available)"

    def summary(self) -> str:
        """Human-readable, for the ticket body.

        Written for a person to read at a glance -- this item exists precisely
        because no model was available to write a nicer one.
        """
        lines = ["Captured without the assistant (all models exhausted).", ""]
        labels = {
            Step.NAME: "Name",
            Step.REASON: "Regarding",
            Step.CONTACT: "Contact",
            Step.WHEN: "Preferred time",
        }
        for step, label in labels.items():
            lines.append(f"{label}: {self.answers.get(step, '(not given)')}")
        return "\n".join(lines)

    def contact(self) -> dict[str, str]:
        out = {}
        if name := self.answers.get(Step.NAME):
            out["name"] = name
        if contact := self.answers.get(Step.CONTACT):
            # Not parsed into phone/email: the model that would normally do that
            # is the thing that is unavailable. A human reads this.
            out["raw"] = contact
        return out
