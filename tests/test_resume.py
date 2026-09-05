"""Resume-token tests.

INSTRUCTIONS.md Phase 4: "a resume attempt with a forged or expired token is
refused." That is the acceptance criterion, and it is the only thing standing
between a guessed UUID and someone else's conversation.
"""

from __future__ import annotations

import time

import pytest

from backend import resume

SECRET = "a" * 64
OTHER_SECRET = "b" * 64
CONVERSATION = "8d300f92-4549-4668-af9c-f4ab76a53155"


def test_round_trip() -> None:
    token = resume.issue(CONVERSATION, SECRET)
    assert resume.verify(token, SECRET) == CONVERSATION


def test_a_bare_uuid_is_not_a_token() -> None:
    """The whole point: possessing the id must not be enough."""
    assert resume.verify(CONVERSATION, SECRET) is None


def test_forged_signature_refused() -> None:
    token = resume.issue(CONVERSATION, SECRET)
    payload, _ = token.split(".", 1)
    assert resume.verify(f"{payload}.{'A' * 43}", SECRET) is None


def test_token_signed_with_another_secret_refused() -> None:
    token = resume.issue(CONVERSATION, OTHER_SECRET)
    assert resume.verify(token, SECRET) is None


def test_tampering_with_the_payload_refused() -> None:
    """Swapping in a different conversation id must invalidate the signature."""
    token = resume.issue(CONVERSATION, SECRET)
    _, signature = token.split(".", 1)
    other = resume._b64(b"00000000-0000-0000-0000-000000000000:99999999999")
    assert resume.verify(f"{other}.{signature}", SECRET) is None


def test_expired_token_refused() -> None:
    token = resume.issue(CONVERSATION, SECRET, ttl_s=-1)
    assert resume.verify(token, SECRET) is None


def test_token_valid_right_up_to_expiry() -> None:
    token = resume.issue(CONVERSATION, SECRET, ttl_s=30)
    assert resume.verify(token, SECRET) == CONVERSATION


@pytest.mark.parametrize(
    "junk",
    ["", ".", "nodot", "a.b.c", "!!!.???", "." * 200],
)
def test_malformed_tokens_return_none_rather_than_raising(junk: str) -> None:
    """Input comes off a public socket; it must never raise."""
    assert resume.verify(junk, SECRET) is None


def test_expiry_is_actually_enforced_not_just_encoded() -> None:
    token = resume.issue(CONVERSATION, SECRET, ttl_s=1)
    assert resume.verify(token, SECRET) == CONVERSATION
    # Rebuild the same token with the clock moved past its expiry.
    past = resume.issue(CONVERSATION, SECRET, ttl_s=-(int(time.time()) % 5 + 1))
    assert resume.verify(past, SECRET) is None
