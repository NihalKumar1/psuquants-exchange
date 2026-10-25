"""Delta-hedging rounds over real WebSockets (SPEC Q160-Q195): the admin's round commands, the
round clock that moves the price, the greeks toggle, and what traders, the admin and the
review page are sent."""

import random
import time

import pytest
from fastapi.testclient import TestClient

from exchange.engine import Exchange
from exchange.server.app import create_app
from exchange.server.room import Room

CODE = "1234"
SECRET = "let-me-in"
ROUND = {
    "type": "create_round", "round_id": "SPX", "title": "S&P", "start_price": 100,
    "tick_size": 1, "realized_vol": 0.4, "implied_vol": 0.2, "minutes": 1,
    "options": [{"kind": "call", "strike": 100}, {"kind": "put", "strike": 95}],
    "max_position": 100, "dollars_per_point": 1, "house_half_spread": 1, "house_size": 10,
}
NEVER = 3600  # a round clock that never ticks during a test


@pytest.fixture
def room(clock):
    return Room(code=CODE, exchange=Exchange(clock=clock, rng=random.Random(7)))


def make_client(room, round_interval=NEVER):
    return TestClient(create_app(room, admin_secret=SECRET, review_interval=0,
                                 round_interval=round_interval))


def admin_login(ws):
    ws.send_json({"type": "admin_login", "secret": SECRET})
    assert ws.receive_json() == {"type": "admin_welcome"}
    return ws.receive_json()


def join(ws, name):
    ws.send_json({"type": "join", "code": CODE, "name": name})
    assert ws.receive_json()["type"] == "welcome"
    return ws.receive_json()


def admin_does(admin, command):
    admin.send_json(command)
    state = admin.receive_json()
    assert state["type"] == "admin_state", state
    return state


def receive_until(ws, condition, limit=500):
    """Read messages until one passes `condition`; return it."""
    for _ in range(limit):
        message = ws.receive_json()
        if condition(message):
            return message
    raise AssertionError("no such message")


# --- Admin commands ----------------------------------------------------------------------------


def test_the_admin_creates_and_starts_a_round(room):
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()

        state = admin_does(admin, ROUND)
        update = alice.receive_json()
        assert list(state["rounds"]) == ["SPX"]
        assert state["rounds"]["SPX"]["status"] == "created"
        assert update["rounds"]["SPX"]["status"] == "created"
        assert update["columns"] == []  # nothing is open yet

        state = admin_does(admin, {"type": "start_round", "round_id": "SPX"})
        update = alice.receive_json()
        assert update["rounds"]["SPX"]["status"] == "running"
        assert update["columns"] == ["SPX", "SPX-C-100", "SPX-P-95"]
        book = update["markets"]["SPX"]
        assert book["round_id"] == "SPX"
        assert (book["best_bid"], book["best_ask"]) == (99, 101)
        assert book["bids"][0]["orders"][0]["name"] == "House"
        # the house is not one of the students in the positions list
        assert [row["name"] for row in book["positions"]] == ["Alice"]


def test_round_commands_that_make_no_sense_are_refused(room):
    with make_client(room) as client, client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        admin.send_json({**ROUND, "implied_vol": 0})
        assert admin.receive_json() == {"type": "admin_error",
                                        "reason": "the implied vol must be a number above 0"}
        admin.send_json({**ROUND, "options": "lots"})
        assert admin.receive_json() == {"type": "admin_error", "reason": "bad message"}
        admin.send_json({"type": "halt_round", "round_id": "SPX"})
        assert admin.receive_json()["reason"] == "unknown round 'SPX'"
        admin_does(admin, ROUND)
        admin.send_json({"type": "halt", "market_id": "SPX"})
        assert "use the round's controls" in admin.receive_json()["reason"]


# --- What traders see --------------------------------------------------------------------------


def test_traders_see_the_fair_price_and_time_but_not_the_realized_vol(room):
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()
        admin_does(admin, ROUND)
        alice.receive_json()
        state = admin_does(admin, {"type": "start_round", "round_id": "SPX"})
        view = alice.receive_json()["rounds"]["SPX"]

        assert view["fair_price"] == 100
        assert view["implied_vol"] == 0.2
        assert (view["step"], view["total_steps"], view["seconds_left"]) == (0, 60, 60)
        assert view["markets"] == ["SPX", "SPX-C-100", "SPX-P-95"]
        assert view["options"] == [
            {"market_id": "SPX-C-100", "kind": "call", "strike": 100},
            {"market_id": "SPX-P-95", "kind": "put", "strike": 95},
        ]
        assert "realized_vol" not in view
        assert state["rounds"]["SPX"]["realized_vol"] == 0.4  # the admin sees it


def test_greeks_reach_traders_only_while_shown(room):
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()
        admin_does(admin, ROUND)
        alice.receive_json()
        admin_does(admin, {"type": "start_round", "round_id": "SPX"})
        update = alice.receive_json()
        assert update["greeks_shown"] is False
        assert update["rounds"]["SPX"]["greeks"] is None
        assert update["my_rounds"] == {}

        # alice buys 10 calls from the house
        alice.send_json({"type": "take", "market_id": "SPX-C-100", "side": "buy",
                         "price": update["markets"]["SPX-C-100"]["best_ask"], "size": 10})
        update = alice.receive_json()
        admin.receive_json()
        assert update["my_rounds"] == {}

        state = admin_does(admin, {"type": "show_greeks"})
        update = alice.receive_json()
        assert update["greeks_shown"] is True
        greeks = update["rounds"]["SPX"]["greeks"]
        assert set(greeks) == {"SPX", "SPX-C-100", "SPX-P-95"}
        call = greeks["SPX-C-100"]
        assert 0.5 < call["delta"] < 0.6 and call["gamma"] > 0 and 7 < call["value"] < 9
        mine = update["my_rounds"]["SPX"]
        assert mine["net_delta"] == pytest.approx(10 * call["delta"])
        assert mine["net_gamma"] == pytest.approx(10 * call["gamma"])
        assert mine["hedging_error"] == 0
        assert state["greeks_shown"] is True

        admin_does(admin, {"type": "hide_greeks"})
        update = alice.receive_json()
        assert update["rounds"]["SPX"]["greeks"] is None
        assert update["my_rounds"] == {}


def test_the_admin_always_sees_greeks_the_house_and_everyones_delta(room):
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()
        admin_does(admin, ROUND)
        alice.receive_json()
        admin_does(admin, {"type": "start_round", "round_id": "SPX"})
        update = alice.receive_json()
        alice.send_json({"type": "take", "market_id": "SPX-C-100", "side": "buy",
                         "price": update["markets"]["SPX-C-100"]["best_ask"], "size": 10})
        alice.receive_json()
        state = admin.receive_json()

        view = state["rounds"]["SPX"]
        assert view["greeks"]["SPX-C-100"]["delta"] > 0  # hidden from traders, not the admin
        assert (view["house_half_spread"], view["house_size"]) == (1, 10)
        assert view["house"]["markets"]["SPX-C-100"]["position"] == -10
        assert "total" in view["house"]
        (row,) = state["traders"]
        assert row["rounds"]["SPX"]["net_delta"] == pytest.approx(
            10 * view["greeks"]["SPX-C-100"]["delta"])
        assert row["rounds"]["SPX"]["hedging_error"] == 0


# --- The round clock ---------------------------------------------------------------------------


def test_the_clock_moves_the_price_and_settles_the_round_at_expiry(room):
    with make_client(room, round_interval=0.001) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()
        admin_does(admin, ROUND)
        alice.receive_json()
        admin_does(admin, {"type": "start_round", "round_id": "SPX"})

        moved = receive_until(alice, lambda m: m["rounds"]["SPX"]["step"] >= 1)
        assert moved["type"] == "update"
        settled = receive_until(alice, lambda m: m["rounds"]["SPX"]["status"] == "settled")
        assert settled["rounds"]["SPX"]["step"] == 60
        assert settled["rounds"]["SPX"]["seconds_left"] == 0
        assert all(settled["markets"][m]["status"] == "settled"
                   for m in ("SPX", "SPX-C-100", "SPX-P-95"))
        assert room.exchange.rounds["SPX"].steps_taken == 60


def test_halting_stops_the_clock_and_resuming_restarts_it(room):
    with make_client(room, round_interval=0.001) as client, \
            client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        admin_does(admin, ROUND)
        admin_does(admin, {"type": "start_round", "round_id": "SPX"})
        receive_until(admin, lambda m: m.get("rounds", {}).get("SPX", {}).get("step", 0) >= 2)

        admin.send_json({"type": "halt_round", "round_id": "SPX"})
        halted = receive_until(admin, lambda m: m.get("rounds", {}).get("SPX", {})
                               .get("status") == "halted")
        steps = halted["rounds"]["SPX"]["step"]
        time.sleep(0.1)  # long enough for dozens of ticks
        assert room.exchange.rounds["SPX"].steps_taken == steps

        admin.send_json({"type": "resume_round", "round_id": "SPX"})
        receive_until(admin, lambda m: m.get("rounds", {}).get("SPX", {})
                      .get("status") == "settled")
        assert room.exchange.rounds["SPX"].steps_taken == 60


def test_halting_and_resuming_quickly_doesnt_double_the_clock(room):
    with make_client(room, round_interval=0.05) as client, \
            client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        admin_does(admin, ROUND)
        admin_does(admin, {"type": "start_round", "round_id": "SPX"})
        for _ in range(5):
            admin_does(admin, {"type": "halt_round", "round_id": "SPX"})
            admin_does(admin, {"type": "resume_round", "round_id": "SPX"})
        start = room.exchange.rounds["SPX"].steps_taken
        time.sleep(0.5)  # about 10 ticks at one clock; 20 or more if there were two
        assert room.exchange.rounds["SPX"].steps_taken - start <= 14


def test_reset_stops_the_clock(room):
    with make_client(room, round_interval=0.001) as client, \
            client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        admin_does(admin, ROUND)
        admin_does(admin, {"type": "start_round", "round_id": "SPX"})
        admin.send_json({"type": "reset"})
        receive_until(admin, lambda m: m.get("type") == "admin_state" and m["rounds"] == {})
        time.sleep(0.05)
        assert room.exchange.rounds == {}
        assert room.exchange.events == []


# --- The review page ---------------------------------------------------------------------------


def test_the_leaderboard_shows_each_rounds_hedging_error(room):
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice, \
            client.websocket_connect("/ws") as bob, \
            client.websocket_connect("/review/ws") as review:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()
        join(bob, "Bob")
        alice.receive_json()
        admin.receive_json()
        admin_does(admin, ROUND)
        alice.receive_json(), bob.receive_json()
        admin_does(admin, {"type": "start_round", "round_id": "SPX"})
        update = alice.receive_json()
        bob.receive_json()
        alice.send_json({"type": "take", "market_id": "SPX-C-100", "side": "buy",
                         "price": update["markets"]["SPX-C-100"]["best_ask"], "size": 10})
        alice.receive_json(), bob.receive_json(), admin.receive_json()

        review.send_json({"type": "admin_login", "secret": SECRET})
        assert review.receive_json() == {"type": "review_welcome"}
        board = review.receive_json()["leaderboard"]
        assert board["rounds"] == [{"round_id": "SPX", "title": "S&P"}]
        rows = {row["name"]: row for row in board["rows"]}
        assert rows["Alice"]["hedging_errors"] == {"SPX": 0}
        assert rows["Bob"]["hedging_errors"] == {"SPX": None}  # never held an option
        assert "House" not in rows
