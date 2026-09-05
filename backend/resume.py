"""Signed resume tokens.

INSTRUCTIONS.md §10 Phase 4: "a dropped socket resumes the same conversation row
rather than starting a new one." r1 said that without saying how, and the
obvious implementation -- the client presents a conversation_id -- means anyone
holding or guessing a UUID can append to, or read back, someone else's
conversation.

So the server issues an HMAC-signed token at session start and requires it on
resume. The conversation id is still inside the token, but it is only honoured
once the signature and expiry check out.
"""

from __future__ import annotations

from .signing import make_token, read_token

# Long enough to survive a page refresh, a tunnel change or a phone locking
# briefly; short enough that a leaked token is not useful for long.
DEFAULT_TTL_S = 3600


def issue(conversation_id: str, secret: str, *, ttl_s: int = DEFAULT_TTL_S) -> str:
    return make_token([conversation_id], secret, ttl_s=ttl_s)


def verify(token: str, secret: str) -> str | None:
    """Return the conversation id, or None if the token is not trustworthy."""
    fields = read_token(token, secret, expected_fields=1)
    return fields[0] if fields else None
