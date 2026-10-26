"""The trading timer over real WebSockets: the server's clock ends trading when time is up,
the admin's +1 min and End now, and the true value never reaching a trader."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from exchange.engine import Exchange
from exchange.server.app import create_app
from exchange.server.room import Room
from helpers import FakeClock

CODE = "1234"
SECRET = "let-me-in"
MARKET = {"type": "create_market", "title": "Cars in Centre County", "tick_size": 1,
          "max_position": 50, "forced_trade_size": 5, "forced_trade_seconds": 30,
          "trading_seconds": 300}


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def room(clock):
    return Room(code=CODE, exchange=Exchange(clock=clock))


def make_client(room):
    return TestClient(create_app(room, admin_secret=SECRET, review_interval=0,
                                 clock_interval=0.01))


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
    for _ in range(limit):
        message = ws.receive_json()
        if condition(message):
            return message
    raise AssertionError("no such message")


def opened(admin, alice, **settings):
    admin_does(admin, {**MARKET, **settings})
    alice.receive_json()
    state = admin_does(admin, {"type": "open_market", "market_id": "m1"})
    update = alice.receive_json()
    return state, update


def test_traders_and_the_admin_see_the_time_left(room, clock):
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()
        state, update = opened(admin, alice)

        assert update["markets"]["m1"]["trading_seconds_left"] == 300
        assert state["markets"]["m1"]["trading_seconds_left"] == 300
        assert update["markets"]["m1"]["time_up"] is False


def test_when_time_is_up_the_server_halts_the_market(room, clock):
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()
        opened(admin, alice, trading_seconds=60)

        clock.now += timedelta(seconds=60)
        update = receive_until(alice, lambda m: m.get("type") == "update"
                               and m["markets"]["m1"]["status"] == "halted")
        assert update["markets"]["m1"]["time_up"] is True
        state = receive_until(admin, lambda m: m["markets"]["m1"]["status"] == "halted")
        assert state["markets"]["m1"]["time_up"] is True


def test_with_a_true_value_the_market_settles_itself(room, clock):
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()
        opened(admin, alice, trading_seconds=60, true_value=1234)

        clock.now += timedelta(seconds=60)
        update = receive_until(alice, lambda m: m.get("type") == "update"
                               and m["markets"]["m1"]["status"] == "settled")
        assert update["markets"]["m1"]["settlement_value"] == 1234


def test_the_true_value_never_reaches_a_trader_but_the_admin_sees_it(room, clock):
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()
        state, update = opened(admin, alice, true_value=987654)

        assert state["markets"]["m1"]["true_value"] == 987654
        assert "987654" not in str(update)
        assert "true_value" not in update["markets"]["m1"]


def test_the_admin_adds_a_minute_and_ends_trading_early(room, clock):
    with make_client(room) as client, \
            client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()
        opened(admin, alice)

        state = admin_does(admin, {"type": "add_trading_time", "market_id": "m1", "seconds": 60})
        assert state["markets"]["m1"]["trading_seconds_left"] == 360
        state = admin_does(admin, {"type": "end_trading", "market_id": "m1"})
        assert state["markets"]["m1"]["status"] == "halted"
        assert state["markets"]["m1"]["time_up"] is True


def test_the_admin_page_has_the_timer_and_true_value_boxes(room):
    with make_client(room) as client:
        page = client.get("/admin").text
    assert 'id="f-trading-minutes"' in page
    assert 'id="f-true-value"' in page
