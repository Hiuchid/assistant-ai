"""Authentication tests.

INSTRUCTIONS.md §12 names mode derivation as one of the things that gets real
tests, and §3.7 is the rule they exist to defend: **mode comes from verified
auth, never from the client**. A forged or expired token must never yield owner.
"""

from __future__ import annotations

import pytest

from backend import auth
from backend.signing import b64, make_token

SECRET = "s" * 64
OTHER_SECRET = "t" * 64
USER = "3f8c1b42-0000-4000-8000-000000000001"


# ------------------------------------------------------------------ passwords


def test_hash_verify_round_trip() -> None:
    stored = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", stored)


def test_wrong_password_rejected() -> None:
    stored = auth.hash_password("correct horse battery staple")
    assert not auth.verify_password("Correct horse battery staple", stored)
    assert not auth.verify_password("", stored)


def test_hash_is_salted() -> None:
    """Two users with the same password must not share a hash."""
    a = auth.hash_password("same-password")
    b = auth.hash_password("same-password")
    assert a != b
    assert auth.verify_password("same-password", a)
    assert auth.verify_password("same-password", b)


def test_stored_hash_contains_no_plaintext() -> None:
    stored = auth.hash_password("hunter2")
    assert "hunter2" not in stored
    assert stored.startswith("scrypt$")


def test_empty_password_refused_at_hash_time() -> None:
    with pytest.raises(ValueError):
        auth.hash_password("")


@pytest.mark.parametrize(
    "junk",
    ["", "not-a-hash", "scrypt$only$three$parts", "bcrypt$1$2$3$4$5",
     "scrypt$x$8$1$AAAA$BBBB", "$$$$$"],
)
def test_malformed_stored_hash_is_false_not_a_crash(junk: str) -> None:
    """This value comes from a database column; it must never raise."""
    assert auth.verify_password("anything", junk) is False


def test_parameters_are_read_from_the_stored_hash() -> None:
    """Hashes written under a lower cost must keep verifying after a raise."""
    import hashlib

    from backend.signing import b64 as _b64
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(b"pw", salt=salt, n=2**10, r=8, p=1, dklen=32)
    stored = f"scrypt$1024$8$1${_b64(salt)}${_b64(digest)}"
    assert auth.verify_password("pw", stored)


# ------------------------------------------------------------------- sessions


def test_session_round_trip() -> None:
    token = auth.issue_session(USER, "owner", SECRET)
    session = auth.verify_session(token, SECRET)
    assert session is not None
    assert session.user_id == USER
    assert session.role == "owner"


def test_session_signed_with_another_secret_refused() -> None:
    token = auth.issue_session(USER, "owner", OTHER_SECRET)
    assert auth.verify_session(token, SECRET) is None


def test_expired_session_refused() -> None:
    token = auth.issue_session(USER, "owner", SECRET, ttl_s=-1)
    assert auth.verify_session(token, SECRET) is None


def test_unknown_role_refused() -> None:
    """A validly-signed token claiming a role we do not grant is still refused."""
    token = make_token([USER, "superuser"], SECRET, ttl_s=3600)
    assert auth.verify_session(token, SECRET) is None


def test_forged_token_never_yields_owner() -> None:
    """§3.7, stated as a test: no unsigned input becomes owner."""
    forged = f"{b64(f'{USER}:owner:99999999999'.encode())}.{'A' * 43}"
    assert auth.verify_session(forged, SECRET) is None


@pytest.mark.parametrize("junk", ["", ".", "a.b.c", "nodot", "!!!.???"])
def test_malformed_session_tokens_return_none(junk: str) -> None:
    assert auth.verify_session(junk, SECRET) is None


def test_field_count_is_enforced() -> None:
    """A token with the right signature but the wrong shape is not a session."""
    token = make_token([USER], SECRET, ttl_s=3600)
    assert auth.verify_session(token, SECRET) is None
