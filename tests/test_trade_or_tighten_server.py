"""Trade or Tighten end to end: real WebSocket messages for the admin and for traders."""

import pytest
from fastapi.testclient import TestClient

from exchange.engine import Exchange, ForcedTradeEnded, Side
from exchange.server.app import create_app
from exchange.server.room import Room
from helpers import ScriptedCoin

CODE = "1234"
SECRET = "let-me-in"
SETTINGS = {"title": "Cars in Centre County", "tick_size": 500, "max_position": 50,
            "forced_trade_size": 1, "forced_trade_seconds": 30}
MARKET = "m1"  # the first market the admin creates


@pytest.fixture
def room(clock):
    """A room with no markets yet. Every random side is SELL."""
    return Room(code=CODE, exchange=Exchange(clock=clock, rng=ScriptedCoin(Side.SELL)))


@pytest.fixture
def client(room):
    with TestClient(create_app(room, admin_secret=SECRET)) as client:
        yield client


def admin_login(ws):
    ws.send_json({"type": "admin_login", "secret": SECRET})
    assert ws.receive_json() == {"type": "admin_welcome"}
    assert ws.receive_json()["type"] == "admin_state"


def join_everyone(admin, traders_by_name):
    """Join each trader in turn; everyone already in (and the admin) hears about it."""
    joined = []
    for name, ws in traders_by_name.items():
        ws.send_json({"type": "join", "code": CODE, "name": name})
        assert ws.receive_json()["type"] == "welcome"
        assert ws.receive_json()["type"] == "snapshot"
        for other in joined:
            other.receive_json()
        admin.receive_json()
        joined.append(ws)


def admin_does(admin, traders, command):
    """Send an admin command. Returns the admin state and each trader's update."""
    admin.send_json(command)
    state = admin.receive_json()
    assert state["type"] == "admin_state", state
    return state, [ws.receive_json() for ws in traders]


def trader_does(sender, command, admin, traders):
    """Send a trader command that is accepted. Returns the admin state and each trader's update."""
    sender.send_json(command)
    updates = [ws.receive_json() for ws in traders]
    for update in updates:
        assert update["type"] == "update", update
    return admin.receive_json(), updates


def test_a_full_trade_or_tighten_opening(client):
    with client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice, \
            client.websocket_connect("/ws") as bob, \
            client.websocket_connect("/ws") as carol:
        admin_login(admin)
        join_everyone(admin, {"Alice": alice, "Bob": bob, "Carol": carol})
        everyone = [alice, bob, carol]
        admin_does(admin, everyone, {"type": "create_market", **SETTINGS})

        state, updates = admin_does(admin, everyone, {"type": "start_auction", "market_id": MARKET})
        assert state["markets"][MARKET]["status"] == "auction"
        assert [u["markets"][MARKET]["status"] for u in updates] == ["auction"] * 3

        trader_does(alice, {"type": "width", "market_id": MARKET, "width": 2000}, admin, everyone)
        _, updates = trader_does(bob, {"type": "width", "market_id": MARKET, "width": 1000},
                                 admin, everyone)
        tot = updates[2]["markets"][MARKET]["tot"]
        assert (tot["best_width"], tot["best_holder"]) == (1000, "Bob")

        state, (a, b, c) = admin_does(admin, everyone, {"type": "close_auction", "market_id": MARKET})
        assert state["markets"][MARKET]["status"] == "mm_quoting"
        assert (a["me"][MARKET]["is_mm"], b["me"][MARKET]["is_mm"]) == (False, True)

        _, (a, b, c) = trader_does(bob, {"type": "mm_quote", "market_id": MARKET,
                                         "bid": 100_000, "ask": 101_000}, admin, everyone)
        tot = c["markets"][MARKET]["tot"]
        assert (tot["mm"], tot["bid"], tot["ask"]) == ("Bob", 100_000, 101_000)

        _, (a, b, c) = admin_does(admin, everyone,
                                  {"type": "start_forced_trade", "market_id": MARKET})
        assert a["markets"][MARKET]["status"] == "forced_trade"
        assert a["markets"][MARKET]["tot"]["seconds_left"] == 30

        state, (a, b, c) = trader_does(alice, {"type": "choose_side", "market_id": MARKET,
                                               "side": "buy"}, admin, everyone)
        assert a["me"][MARKET]["my_choice"] == "buy"
        assert c["me"][MARKET]["my_choice"] is None
        assert state["markets"][MARKET]["choices"] == {"buy": 1, "sell": 0, "undecided": 1}

        # Carol never chooses: she gets the scripted random side, SELL.
        _, (a, b, c) = admin_does(admin, everyone, {"type": "end_forced_trade", "market_id": MARKET})

    assert c["markets"][MARKET]["status"] == "open"
    assert [(t["buyer"], t["seller"], t["price"], t["size"], t["forced"])
            for t in c["new_trades"]] == [
        ("Alice", "Bob", 101_000, 1, True),
        ("Bob", "Carol", 100_000, 1, True),
    ]
    assert c["me"][MARKET]["position"] == -1


def test_the_timer_ends_the_window_by_itself(client, room):
    with client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice, \
            client.websocket_connect("/ws") as bob:
        admin_login(admin)
        join_everyone(admin, {"Alice": alice, "Bob": bob})
        everyone = [alice, bob]
        admin_does(admin, everyone, {"type": "create_market", **SETTINGS,
                                     "forced_trade_seconds": 1})
        admin_does(admin, everyone, {"type": "start_auction", "market_id": MARKET})
        trader_does(alice, {"type": "width", "market_id": MARKET, "width": 1000}, admin, everyone)
        admin_does(admin, everyone, {"type": "close_auction", "market_id": MARKET})
        trader_does(alice, {"type": "mm_quote", "market_id": MARKET,
                            "bid": 100_000, "ask": 101_000}, admin, everyone)
        admin_does(admin, everyone, {"type": "start_forced_trade", "market_id": MARKET})

        # Nobody does anything; about a second later the server closes the window itself.
        update = bob.receive_json()
        alice.receive_json()
        admin.receive_json()

    assert update["markets"][MARKET]["status"] == "open"
    assert [(t["buyer"], t["seller"]) for t in update["new_trades"]] == [("Alice", "Bob")]
    (ended,) = [e for e in room.exchange.events if isinstance(e, ForcedTradeEnded)]
    assert ended.ended_by == "timer"


def test_admin_mistakes_in_trade_or_tighten_come_back_as_admin_errors(client):
    with client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        admin_does(admin, [], {"type": "create_market", **SETTINGS})
        admin_does(admin, [], {"type": "start_auction", "market_id": MARKET})

        replies = []
        for kind in ("close_auction", "start_forced_trade", "end_forced_trade"):
            admin.send_json({"type": kind, "market_id": MARKET})
            replies.append(admin.receive_json())

    assert [reply["type"] for reply in replies] == ["admin_error"] * 3
    assert "no widths" in replies[0]["reason"]
    assert "cannot start the forced trade" in replies[1]["reason"]
    assert "cannot end the forced trade" in replies[2]["reason"]


def test_rejections_and_bad_messages_go_only_to_the_sender(client):
    with client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice, \
            client.websocket_connect("/ws") as bob:
        admin_login(admin)
        join_everyone(admin, {"Alice": alice, "Bob": bob})
        everyone = [alice, bob]
        admin_does(admin, everyone, {"type": "create_market", **SETTINGS})
        admin_does(admin, everyone, {"type": "start_auction", "market_id": MARKET})

        replies = []
        for command in (
            {"type": "width", "market_id": MARKET, "width": 1200},  # off the 500 tick
            {"type": "width", "market_id": MARKET},  # no width
            {"type": "choose_side", "market_id": MARKET, "side": "sideways"},
            {"type": "mm_quote", "market_id": MARKET, "bid": 1000},  # no ask
        ):
            alice.send_json(command)
            replies.append(alice.receive_json())
        # If any of those had reached Bob, this would not be the first message he gets next.
        _, (_, bob_update) = trader_does(bob, {"type": "width", "market_id": MARKET,
                                               "width": 500}, admin, everyone)

    assert [reply["type"] for reply in replies] == ["rejected"] * 4
    assert "multiple of the tick" in replies[0]["reason"]
    assert [reply["reason"] for reply in replies[1:]] == ["bad message"] * 3
    assert bob_update["markets"][MARKET]["tot"]["best_holder"] == "Bob"
