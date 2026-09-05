"""Signed resume tokens.

INSTRUCTIONS.md §10 Phase 4: "a dropped socket resumes the same conversation row
rather than starting a new one." r1 said that without saying how, and the
obvious implementation -- the client presents a conversation_id -- means anyone
holding or guessing a UUID can append to, or read back, someone else's
conversation.

So the server issues an HMAC-signed token at session start and requires it on
resume. The conversation id is still in the token, but it is only honoured when
the signature and expiry check out.

Deliberately not a JWT. There is one issuer, one consumer, one claim and no key
rotation; a JWT library would add a dependency and an algorithm-confusion
foot-gun for no benefit.
"""

from __future__ import annotations

import base64
import hmac
import logging
import time
from hashlib import sha256

log = logging.getLogger("assistant.resume")

# Long enough to survive a page refresh, a tunnel change or a phone locking
# briefly; short enough that a leaked token is not useful for long.
DEFAULT_TTL_S = 3600


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256)
    return _b64(mac.digest())


def issue(conversation_id: str, secret: str, *, ttl_s: int = DEFAULT_TTL_S) -> str:
    payload = _b64(f"{conversation_id}:{int(time.time()) + ttl_s}".encode())
    return f"{payload}.{_sign(payload, secret)}"


def verify(token: str, secret: str) -> str | None:
    """Return the conversation id, or None if the token is not trustworthy.

    Every failure returns None rather than raising or distinguishing between
    causes: a caller that can tell "bad signature" from "expired" from
    "malformed" learns more about the token format than it needs to.
    """
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return None

    expected = _sign(payload, secret)
    # Constant-time: a plain == leaks signature bytes through timing.
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        conversation_id, expiry = _unb64(payload).decode("utf-8").rsplit(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None

    try:
        if int(expiry) < int(time.time()):
            return None
    except ValueError:
        return None

    return conversation_id
