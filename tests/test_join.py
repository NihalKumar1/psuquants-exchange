"""Joining the room: each trader gets a stable id and a unique display name, logged as an event."""

import json

import pytest

from exchange.engine import Exchange, TraderJoined, from_dict, to_dict


def test_join_logs_an_event_with_a_new_trader_id(clock):
    ex = Exchange(clock=clock)

    events = ex.join("Alice")

    assert events == [TraderJoined(seq=1, ts=clock.now, trader_id="t1", name="Alice")]
    assert ex.traders == {"t1": "Alice"}


def test_trader_ids_count_up(clock):
    ex = Exchange(clock=clock)
    ex.join("Alice")
    ex.join("Bob")

    assert ex.traders == {"t1": "Alice", "t2": "Bob"}


def test_name_is_trimmed(clock):
    ex = Exchange(clock=clock)

    ex.join("  Alice  ")

    assert ex.traders == {"t1": "Alice"}


@pytest.mark.parametrize("name", ["", "   ", "x" * 21])
def test_name_must_be_1_to_20_characters(clock, name):
    ex = Exchange(clock=clock)

    with pytest.raises(ValueError, match="1 to 20 characters"):
        ex.join(name)
    assert ex.events == []


def test_twenty_characters_is_allowed(clock):
    ex = Exchange(clock=clock)

    ex.join("x" * 20)

    assert ex.traders == {"t1": "x" * 20}


def test_names_are_unique_ignoring_case(clock):
    ex = Exchange(clock=clock)
    ex.join("Alice")

    with pytest.raises(ValueError, match="taken"):
        ex.join(" alice ")
    assert ex.traders == {"t1": "Alice"}


def test_find_trader_ignores_case_and_spaces(clock):
    ex = Exchange(clock=clock)
    ex.join("Alice")

    assert ex.find_trader(" ALICE ") == "t1"
    assert ex.find_trader("Bob") is None


def test_replay_restores_traders_and_carries_on_with_fresh_ids(clock):
    live = Exchange(clock=clock)
    live.join("Alice")
    live.join("Bob")

    text = json.dumps([to_dict(event) for event in live.events])
    replayed = Exchange.replay([from_dict(data) for data in json.loads(text)], clock=clock)

    assert replayed.traders == {"t1": "Alice", "t2": "Bob"}
    replayed.join("Carol")
    assert replayed.traders["t3"] == "Carol"
