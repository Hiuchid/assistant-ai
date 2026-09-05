"""First-party authentication.

Operator decision: users are our own rows, not Supabase Auth. So this module
owns password hashing and session tokens outright.

**Passwords are hashed with scrypt from `hashlib`.** No new dependency, and
scrypt is memory-hard, which is the property that matters against offline
cracking -- unlike a bare SHA family hash, which is exactly the wrong tool. The
parameters below are the RFC 7914 interactive-login figures.

**Nothing here ever logs a password**, not at DEBUG, not in an exception. The
one place a plaintext password exists is the argument to `hash_password` and
`verify_password`, and neither puts it anywhere.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from hashlib import scrypt
from typing import Literal

from .signing import b64, make_token, read_token, unb64

log = logging.getLogger("assistant.auth")

# RFC 7914 interactive-login parameters. n is the cost; raising it makes both
# login and offline cracking proportionally slower, which is the trade.
_N = 2**14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16

Role = Literal["operator", "owner"]

# An installed app that asks for a password every week is an app people stop
# opening. 30 days, against a token that lives on one device and grants read
# access plus status changes -- not password changes, and not deletion.
SESSION_TTL_S = 30 * 24 * 3600


def hash_password(password: str) -> str:
    """Return `scrypt$n$r$p$salt$hash`, safe to store."""
    if not password:
        raise ValueError("empty password")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash.

    Parameters come from the stored string rather than the constants above, so
    hashes written under older settings keep verifying after the cost is
    raised. Any malformed value is a failure, never an exception -- this is fed
    from a database column and must not become a crash.
    """
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = unb64(hash_b64)
        actual = scrypt(
            password.encode("utf-8"),
            salt=unb64(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class Session:
    user_id: str
    role: Role


def issue_session(
    user_id: str, role: Role, secret: str, *, ttl_s: int = SESSION_TTL_S
) -> str:
    return make_token([user_id, role], secret, ttl_s=ttl_s)


def verify_session(token: str, secret: str) -> Session | None:
    """Return the session, or None if the token is not trustworthy.

    §3.7: mode is derived from this, never from anything the client asserts. A
    forged or expired token must not be silently downgraded to visitor *by this
    function* -- it returns None, and the caller decides. The caller treating
    None as "visitor" is correct; treating a bad token as "owner" would not be.
    """
    fields = read_token(token, secret, expected_fields=2)
    if fields is None:
        return None
    user_id, role = fields
    # Compared with == rather than `in`, because that is what narrows a str to
    # the Literal the dataclass wants. `in` would need a cast or an ignore.
    if role == "operator":
        return Session(user_id=user_id, role="operator")
    if role == "owner":
        return Session(user_id=user_id, role="owner")
    return None
