"""HMAC token primitives, shared by resume tokens and session tokens.

Deliberately not JWT. There is one issuer, one consumer, no key rotation and a
handful of claims; a JWT library would add a dependency and an
algorithm-confusion foot-gun for no benefit.

The format is `<base64url(payload)>.<base64url(hmac-sha256)>`. Payload meaning
is the caller's business -- this module only proves it has not been altered.
"""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256)
    return b64(mac.digest())


def make_token(fields: list[str], secret: str, *, ttl_s: int) -> str:
    """Sign `fields` plus an absolute expiry.

    `:` separates fields, so no field may contain one. Callers pass ids and
    role names, which never do.
    """
    payload = b64(":".join([*fields, str(int(time.time()) + ttl_s)]).encode("utf-8"))
    return f"{payload}.{sign(payload, secret)}"


def read_token(token: str, secret: str, *, expected_fields: int) -> list[str] | None:
    """Return the fields if the token is genuine and unexpired, else None.

    Every failure returns None rather than raising or distinguishing between
    causes: a caller that can tell "bad signature" from "expired" from
    "malformed" learns more about the token format than it needs to.
    """
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return None

    # Constant-time: a plain == leaks signature bytes through timing.
    if not hmac.compare_digest(signature, sign(payload, secret)):
        return None

    try:
        parts = unb64(payload).decode("utf-8").split(":")
    except (ValueError, UnicodeDecodeError):
        return None

    if len(parts) != expected_fields + 1:
        return None

    try:
        if int(parts[-1]) < int(time.time()):
            return None
    except ValueError:
        return None

    return parts[:-1]
