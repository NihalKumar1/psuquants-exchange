"""Reset: the admin ends this game and starts a new one in the same room.

Traders connected at that moment are carried into the new game under the same names, starting
flat. Everyone else (offline or kicked) is dropped and can join the new game like anyone.
"""

import time

import pytest
from fastapi.testclient import TestClient

from exchange.engine import Exchange, MarketStatus
from exchange.server.app import create_app
from exchange.server.room import Room

CODE = "1234"
SECRET = "let-me-in"
SETTINGS = {"title": "Cars in Centre County", "tick_size": 500, "max_position": 50,
            "forced_trade_size": 1, "forced_trade_seconds": 30}


@pytest.fixture
def room(ex):
    """A room whose exchange already has one open market, "cars"."""
    return Room(code=CODE, exchange=ex)


def join(room, name):
    trader_id, token, _ = room.join(CODE, name)
    return trader_id, token


# --- The room -------------------------------------------------------------------------------


def test_reset_clears_the_game_but_keeps_the_room_code(room):
    room.exchange.mark_info_drop("median income released")
    room.exchange.lock_joining()

    room.reset()

    assert room.code == CODE
    assert room.exchange.markets == {}
    assert room.exchange.info_drops == []
    assert room.exchange.joining_locked is False
    assert room.exchange.events == []  # nobody was connected, so the new log is empty


def test_connected_traders_are_carried_over_in_join_order(room):
    alice, _ = join(room, "Alice")
    join(room, "Bob")  # Bob is offline
    carol, _ = join(room, "Carol")
    alice_socket, carol_socket = object(), object()
    room.connect(carol, carol_socket)
    room.connect(alice, alice_socket)

    room.reset()

    assert room.exchange.traders == {"t1": "Alice", "t2": "Carol"}
    assert room.connections == {"t1": alice_socket, "t2": carol_socket}


def test_carried_over_traders_get_a_new_token_and_old_tokens_stop_working(room):
    alice, alice_old_token = join(room, "Alice")
    _, bob_token = join(room, "Bob")
    room.connect(alice, object())

    carried = room.reset()

    ((new_id, new_token),) = carried
    assert new_id == "t1"
    assert room.rejoin(new_token) == "t1"
    assert room.rejoin(alice_old_token) is None
    assert room.rejoin(bob_token) is None


def test_an_offline_trader_can_join_the_new_game_by_name(room):
    join(room, "Bob")

    room.reset()

    trader_id, _ = join(room, "Bob")
    assert trader_id == "t1"


def test_a_kicked_trader_gets_a_fresh_start(room):
    alice, _ = join(room, "Alice")
    room.exchange.kick(alice)

    room.reset()

    trader_id, _ = join(room, "Alice")
    assert trader_id == "t1"
    assert room.exchange.kicked == set()


# --- End to end -----------------------------------------------------------------------------


@pytest.fixture
def client(room):
    # "with" makes every socket in a test share one event loop, like the real server does.
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
    """Send a trader command that is accepted. Returns each trader's update."""
    sender.send_json(command)
    updates = [ws.receive_json() for ws in traders]
    for update in updates:
        assert update["type"] == "update", update
    admin.receive_json()
    return updates


def reset(admin, traders):
    """Reset the game. Returns the admin state and each trader's (welcome, snapshot)."""
    admin.send_json({"type": "reset"})
    received = []
    for ws in traders:
        welcome, snapshot, notice = ws.receive_json(), ws.receive_json(), ws.receive_json()
        assert welcome["type"] == "welcome", welcome
        assert snapshot["type"] == "snapshot", snapshot
        assert notice == {"type": "reset"}
        received.append((welcome, snapshot))
    state = admin.receive_json()
    assert state["type"] == "admin_state", state
    return state, received


def test_reset_moves_connected_traders_into_a_new_empty_game(client, room):
    join(room, "Bob")  # joined earlier, offline now
    with client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join_everyone(admin, {"Alice": alice})

        state, [(welcome, snapshot)] = reset(admin, [alice])

    assert welcome["trader_id"] == "t1" and welcome["name"] == "Alice"
    assert room.rejoin(welcome["token"]) == "t1"
    assert snapshot["markets"] == {} and snapshot["tape"] == {}
    assert state["room_code"] == CODE
    assert state["markets"] == {}
    assert [(row["name"], row["connected"]) for row in state["traders"]] == [("Alice", True)]


def test_after_reset_a_carried_over_trader_trades_under_their_new_id(client, room):
    join(room, "Bob")  # Bob is t1 in the old game, so Alice is t2 there but t1 after reset
    with client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join_everyone(admin, {"Alice": alice})
        reset(admin, [alice])
        admin_does(admin, [alice], {"type": "create_market", **SETTINGS})
        admin_does(admin, [alice], {"type": "open_market", "market_id": "m1"})

        (update,) = trader_does(alice, {"type": "limit", "market_id": "m1", "side": "buy",
                                        "price": 100_000, "size": 3}, admin, [alice])

    (level,) = update["markets"]["m1"]["bids"]
    assert [(order["name"], order["size"]) for order in level["orders"]] == [("Alice", 3)]
    assert update["me"]["m1"]["orders"][0]["size"] == 3


def run_up_to_forced_trade(admin, alice, bob, seconds):
    """Create market m1 and take it through Trade or Tighten into the forced-trade window."""
    everyone = [alice, bob]
    admin_does(admin, everyone, {"type": "create_market", **SETTINGS,
                                 "forced_trade_seconds": seconds})
    admin_does(admin, everyone, {"type": "start_auction", "market_id": "m1"})
    trader_does(alice, {"type": "width", "market_id": "m1", "width": 1000}, admin, everyone)
    admin_does(admin, everyone, {"type": "close_auction", "market_id": "m1"})
    trader_does(alice, {"type": "mm_quote", "market_id": "m1", "bid": 100_000, "ask": 101_000},
                admin, everyone)
    admin_does(admin, everyone, {"type": "start_forced_trade", "market_id": "m1"})


def test_reset_stops_the_old_games_forced_trade_timer(client, room, clock):
    # Market ids start again at m1 after a reset, so an old timer left running would end the
    # new game's m1 window early.
    room.exchange = Exchange(clock=clock)
    with client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice, \
            client.websocket_connect("/ws") as bob:
        admin_login(admin)
        join_everyone(admin, {"Alice": alice, "Bob": bob})
        run_up_to_forced_trade(admin, alice, bob, seconds=1)
        reset(admin, [alice, bob])
        run_up_to_forced_trade(admin, alice, bob, seconds=30)

        time.sleep(1.5)  # past the old 1-second timer

        assert room.exchange.markets["m1"].status is MarketStatus.FORCED_TRADE
