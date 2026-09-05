"""Conversation-window tests.

The window is what the model is allowed to remember. It used to be the last
six turns, which is three exchanges -- so on a call of any length the caller's
name, given first, was gone by the time it mattered, and the assistant asked
for it again.

What is under test is that the opening survives no matter how long the call
runs.
"""

from __future__ import annotations

from backend.session import HEAD_TURNS, TAIL_TURNS, Conversation

PROMPT = "You are a secretary."


def conversation(turn_count: int) -> Conversation:
    c = Conversation(id="c1", system_prompt=PROMPT)
    for i in range(turn_count):
        c.add("customer" if i % 2 == 0 else "agent", f"turn {i}")
    return c


def texts(messages: list[dict[str, str]]) -> list[str]:
    return [m["content"] for m in messages]


def test_a_short_conversation_is_sent_whole() -> None:
    c = conversation(HEAD_TURNS + TAIL_TURNS)
    kept, omitted = c.window()
    assert omitted == 0
    assert len(kept) == HEAD_TURNS + TAIL_TURNS


def test_the_opening_survives_a_long_conversation() -> None:
    """The whole point: whoever they said they were at turn zero is still
    there at turn two hundred."""
    c = conversation(200)
    body = texts(c.messages())
    assert "turn 0" in body
    assert "turn 1" in body
    assert "turn 199" in body


def test_only_the_middle_is_dropped() -> None:
    c = conversation(100)
    kept, omitted = c.window()
    assert omitted == 100 - HEAD_TURNS - TAIL_TURNS
    assert [t.text for t in kept[:HEAD_TURNS]] == [f"turn {i}" for i in range(HEAD_TURNS)]
    assert kept[-1].text == "turn 99"


def test_the_gap_is_declared_rather_than_hidden() -> None:
    """A model that does not know it is missing turns will confidently assume
    it was never told."""
    c = conversation(100)
    markers = [t for t in texts(c.messages()) if t.startswith("[")]
    assert len(markers) == 1
    assert "not shown" in markers[0]
    assert "Do not ask again" in markers[0]


def test_no_marker_when_nothing_was_dropped() -> None:
    c = conversation(6)
    assert not [t for t in texts(c.messages()) if t.startswith("[")]


def test_the_marker_sits_between_the_two_halves() -> None:
    c = conversation(100)
    body = texts(c.messages())
    marker = next(i for i, t in enumerate(body) if t.startswith("["))
    # index 0 is the system prompt, so the head occupies 1..HEAD_TURNS
    assert marker == HEAD_TURNS + 1
    assert body[marker - 1] == f"turn {HEAD_TURNS - 1}"
    assert body[marker + 1] == f"turn {100 - TAIL_TURNS}"


def test_the_system_prompt_carries_the_current_time() -> None:
    """Without it the model cannot resolve "Tuesday" and quietly guesses."""
    c = conversation(2)
    system = c.messages(tz="Asia/Beirut")[0]["content"]
    assert PROMPT in system
    assert "The current local time is" in system


def test_roles_are_translated_for_the_provider() -> None:
    c = conversation(2)
    roles = [m["role"] for m in c.messages()]
    assert roles == ["system", "user", "assistant"]
