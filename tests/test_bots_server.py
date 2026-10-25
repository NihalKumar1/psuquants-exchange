"""Bots and price limits over real WebSockets: the admin's switches, the bots' clock, what the
admin page and traders see, and Reset."""

import random

import pytest
from fastapi.testclient import TestClient

from exchange.engine import Exchange, Side
from exchange.server.app import create_app
from exchange.server.room import Room
from helpers import FakeClock, open_market

CODE = "1234"
SECRET = "let-me-in"
NEVER = 3600  # a bot clock that never ticks during a test


@pytest.fixture
def room():
    exchange = Exchange(clock=FakeClock(), rng=random.Random(3))
    open_market(exchange)
    # A two-sided market from students, so the makers have a price to quote around.
    exchange.place_limit("cars", "s1", Side.BUY, 40, 5)
    exchange.place_limit("cars", "s2", Side.SELL, 50, 5)
    return Room(code=CODE, exchange=exchange)


def make_client(room, bot_interval=NEVER):
    return TestClient(create_app(room, admin_secret=SECRET, review_interval=0,
                                 bot_interval=bot_interval))


def admin_login(ws):
    ws.send_json({"type": "admin_login", "secret": SECRET})
    assert ws.receive_json() == {"type": "admin_welcome"}
    return ws.receive_json()


def admin_does(admin, command):
    admin.send_json(command)
    state = admin.receive_json()
    assert state["type"] == "admin_state", state
    return state


def receive_until(ws, condition, limit=500):
    for _ in range(limit):
        message = ws.receive_json()
        if condition(message):
            return message
    raise AssertionError("no such message")


def test_the_admin_turns_bots_on_and_off(room):
    with make_client(room) as client, client.websocket_connect("/admin/ws") as admin:
        state = admin_login(admin)
        assert state["bots_on"] == {"maker": False, "taker": False}

        state = admin_does(admin, {"type": "start_bots", "kind": "maker"})
        assert state["bots_on"] == {"maker": True, "taker": False}
        makers = [row for row in state["traders"] if row["bot"] == "maker"]
        assert [row["name"] for row in makers] == ["Maker Bot 1", "Maker Bot 2"]

        state = admin_does(admin, {"type": "stop_bots", "kind": "maker"})
        assert state["bots_on"]["maker"] is False


def test_a_switch_already_in_that_state_is_refused(room):
    with make_client(room) as client, client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        admin.send_json({"type": "stop_bots", "kind": "taker"})
        assert admin.receive_json() == {"type": "admin_error",
                                        "reason": "the takers are already off"}


def test_running_bots_trade_and_traders_see_them_by_name(room):
    with make_client(room, bot_interval=0.01) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        alice.send_json({"type": "join", "code": CODE, "name": "Alice"})
        assert alice.receive_json()["type"] == "welcome"
        alice.receive_json()
        admin.receive_json()

        admin_does(admin, {"type": "start_bots", "kind": "maker"})
        admin_does(admin, {"type": "start_bots", "kind": "taker"})

        def bots_in_book(update):
            if update.get("type") != "update":
                return False
            book = update["markets"]["cars"]
            names = {order["name"] for level in book["bids"] + book["asks"]
                     for order in level["orders"]}
            return "Maker Bot 1" in names
        receive_until(alice, bots_in_book)
        update = receive_until(alice, lambda m: m.get("type") == "update" and any(
            trade["buyer"].startswith("Taker Bot") or trade["seller"].startswith("Taker Bot")
            for trade in m["new_trades"]))
        assert "Taker Bot" in str(update["new_trades"])


def test_bots_show_as_bots_on_the_admin_page_with_their_pnl(room):
    with make_client(room) as client, client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        state = admin_does(admin, {"type": "start_bots", "kind": "taker"})
        taker = next(row for row in state["traders"] if row["name"] == "Taker Bot 1")
        assert taker["bot"] == "taker"
        assert taker["connected"] is False
        assert "total" in taker and "cars" in taker["markets"]


def test_reset_turns_the_bots_off(room):
    with make_client(room, bot_interval=0.01) as client, \
            client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        admin_does(admin, {"type": "start_bots", "kind": "taker"})
        admin.send_json({"type": "reset"})
        state = receive_until(admin, lambda m: m.get("type") == "admin_state"
                              and m["traders"] == [])
        assert state["bots_on"] == {"maker": False, "taker": False}
        events_after_reset = len(room.exchange.events)
        # The old clock is gone: nothing new happens in the new game.
        admin_does(admin, {"type": "info_drop", "note": "still quiet"})
        assert len(room.exchange.events) == events_after_reset + 1


def test_the_admin_sets_price_limits_and_traders_see_them():
    room = Room(code=CODE, exchange=Exchange())
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        alice.send_json({"type": "join", "code": CODE, "name": "Alice"})
        alice.receive_json(), alice.receive_json()
        admin.receive_json()

        state = admin_does(admin, {
            "type": "create_market", "title": "Any six", "tick_size": 1, "max_position": 50,
            "forced_trade_size": 5, "forced_trade_seconds": 30, "dollars_per_point": 1,
            "min_price": 0, "max_price": 100})
        assert (state["markets"]["m1"]["min_price"], state["markets"]["m1"]["max_price"]) == (0, 100)
        update = alice.receive_json()
        assert update["markets"]["m1"]["max_price"] == 100

        admin_does(admin, {"type": "open_market", "market_id": "m1"})
        alice.receive_json()
        alice.send_json({"type": "limit", "market_id": "m1", "side": "buy", "price": 500,
                         "size": 1})
        rejected = alice.receive_json()
        assert rejected["type"] == "rejected"
        assert rejected["reason"] == "price must be between 0 and 100"


def test_markets_created_without_limits_have_none():
    room = Room(code=CODE, exchange=Exchange())
    with make_client(room) as client, client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        state = admin_does(admin, {
            "type": "create_market", "title": "Cars", "tick_size": 1, "max_position": 50,
            "forced_trade_size": 5, "forced_trade_seconds": 30})
        assert state["markets"]["m1"]["min_price"] is None


def test_the_admin_page_has_the_bot_switches_and_limit_boxes(room):
    with make_client(room) as client:
        page = client.get("/admin").text
    for element in ('id="toggle-makers"', 'id="toggle-takers"', 'id="f-min-price"',
                    'id="f-max-price"'):
        assert element in page


def test_a_student_cant_take_over_a_bot_by_typing_its_name(room):
    room.exchange.start_bots("maker")
    with pytest.raises(ValueError, match="belongs to a bot"):
        room.join(CODE, "Maker Bot 1")
