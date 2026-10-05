"""The server end to end: real WebSocket messages through FastAPI's test client."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from exchange.server.app import create_app
from exchange.server.room import Room
from exchange.server.views import trader_view
from helpers import open_market

CODE = "1234"
SECRET = "test-secret"


@pytest.fixture
def room(ex):
    return Room(code=CODE, exchange=ex)


@pytest.fixture
def client(room):
    # "with" makes every socket in a test share one event loop, like the real server does.
    # Without it, each socket gets its own loop and the app's shared lock can hang.
    with TestClient(create_app(room, admin_secret=SECRET)) as client:
        yield client


def join(ws, name, code=CODE):
    """Join over a socket; returns the welcome and snapshot messages."""
    ws.send_json({"type": "join", "code": code, "name": name})
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome", welcome
    snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot", snapshot
    return welcome, snapshot


def limit(ws, side, price, size, market_id="cars"):
    ws.send_json({"type": "limit", "market_id": market_id, "side": side,
                  "price": price, "size": size})


def names_on(update, side, market_id="cars"):
    """[(name, size), ...] across the shown levels of one side of the book."""
    levels = update["markets"][market_id][side]
    return [(order["name"], order["size"]) for level in levels for order in level["orders"]]


# --- Page and joining -----------------------------------------------------------------------


def test_trader_page_is_served(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "<html" in response.text.lower()


@pytest.mark.parametrize("path", ["/", "/admin", "/static/trader.js", "/static/style.css"])
def test_browsers_must_recheck_pages_and_scripts_every_time(client, path):
    # Otherwise a browser can keep running an old trader.js after the code changes.
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_wrong_room_code_gets_an_error_and_the_socket_closes(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "join", "code": "9999", "name": "Alice"})

        assert ws.receive_json() == {"type": "error", "reason": "wrong room code"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_join_gets_a_welcome_and_a_snapshot(client):
    with client.websocket_connect("/ws") as ws:
        welcome, snapshot = join(ws, " Alice ")

    assert welcome["trader_id"] == "t1"
    assert welcome["name"] == "Alice"
    assert welcome["token"]
    assert snapshot["markets"]["cars"]["title"] == "Test market cars"
    assert snapshot["me"]["cars"]["position"] == 0
    assert snapshot["total"] == {"realized": 0, "unrealized": 0, "total": 0}
    assert snapshot["tape"] == {"cars": []}


def test_others_are_told_when_someone_new_joins(client):
    with client.websocket_connect("/ws") as alice, client.websocket_connect("/ws") as bob:
        join(alice, "Alice")
        join(bob, "Bob")

        update = alice.receive_json()

    assert update["type"] == "update"
    positions = update["markets"]["cars"]["positions"]
    assert [(row["name"], row["position"]) for row in positions] == [("Alice", 0), ("Bob", 0)]


def test_name_is_taken_while_its_owner_is_connected(client):
    with client.websocket_connect("/ws") as alice, client.websocket_connect("/ws") as other:
        join(alice, "Alice")
        other.send_json({"type": "join", "code": CODE, "name": "alice"})

        reply = other.receive_json()

    assert reply["type"] == "error"
    assert "taken" in reply["reason"]


def test_typing_the_same_name_reclaims_a_disconnected_trader(client):
    with client.websocket_connect("/ws") as ws:
        first, _ = join(ws, "Alice")

    with client.websocket_connect("/ws") as ws:
        again, _ = join(ws, "ALICE")

    assert again["trader_id"] == first["trader_id"]
    assert again["name"] == "Alice"


def test_token_rejoins_the_same_trader(client):
    with client.websocket_connect("/ws") as ws:
        welcome, _ = join(ws, "Alice")

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "rejoin", "token": welcome["token"]})
        again = ws.receive_json()
        snapshot = ws.receive_json()

    assert again["type"] == "welcome"
    assert again["trader_id"] == welcome["trader_id"]
    assert snapshot["type"] == "snapshot"


def test_unknown_token_gets_an_error(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "rejoin", "token": "made-up"})

        reply = ws.receive_json()

    assert reply["type"] == "error"


def test_opening_a_second_tab_replaces_the_first(client):
    with client.websocket_connect("/ws") as first:
        welcome, _ = join(first, "Alice")
        with client.websocket_connect("/ws") as second:
            second.send_json({"type": "rejoin", "token": welcome["token"]})
            assert second.receive_json()["type"] == "welcome"
            assert second.receive_json()["type"] == "snapshot"

            assert first.receive_json() == {"type": "opened_elsewhere"}
            with pytest.raises(WebSocketDisconnect):
                first.receive_json()


# --- Trading --------------------------------------------------------------------------------


def test_an_order_appears_in_everyones_book_with_the_owners_name(client):
    with client.websocket_connect("/ws") as alice, client.websocket_connect("/ws") as bob:
        join(alice, "Alice")
        join(bob, "Bob")
        alice.receive_json()  # Bob joined

        limit(alice, "sell", 40, 10)

        assert names_on(alice.receive_json(), "asks") == [("Alice", 10)]
        assert names_on(bob.receive_json(), "asks") == [("Alice", 10)]


def test_a_click_take_trades_and_everyone_gets_the_new_trade(client, room):
    with client.websocket_connect("/ws") as alice, client.websocket_connect("/ws") as bob:
        join(alice, "Alice")
        join(bob, "Bob")
        alice.receive_json()  # Bob joined
        limit(alice, "sell", 40, 10)
        alice.receive_json()
        bob.receive_json()

        bob.send_json({"type": "take", "market_id": "cars", "side": "buy", "price": 40, "size": 4})
        alice_update = alice.receive_json()
        bob_update = bob.receive_json()

    for update in (alice_update, bob_update):
        (trade,) = update["new_trades"]
        assert (trade["buyer"], trade["seller"], trade["price"], trade["size"]) == ("Bob", "Alice", 40, 4)
        assert names_on(update, "asks") == [("Alice", 6)]

    # Bob sees Alice's position, but only his own PnL.
    positions = {row["name"]: row["position"] for row in bob_update["markets"]["cars"]["positions"]}
    assert positions == {"Alice": -4, "Bob": 4}
    assert bob_update["me"]["cars"] == trader_view(room.exchange, "cars", "t2")
    assert alice_update["me"]["cars"]["position"] == -4


def test_the_snapshot_has_the_whole_tape(client):
    with client.websocket_connect("/ws") as alice:
        join(alice, "Alice")
        limit(alice, "sell", 40, 10)
        alice.receive_json()
        with client.websocket_connect("/ws") as bob:
            join(bob, "Bob")
            alice.receive_json()  # Bob joined
            bob.send_json({"type": "take", "market_id": "cars", "side": "buy",
                           "price": 40, "size": 4})
            bob.receive_json()
            alice.receive_json()

            with client.websocket_connect("/ws") as carol:
                _, snapshot = join(carol, "Carol")

    (trade,) = snapshot["tape"]["cars"]
    assert (trade["buyer"], trade["seller"]) == ("Bob", "Alice")


def test_a_rejection_goes_only_to_the_sender(client):
    with client.websocket_connect("/ws") as alice, client.websocket_connect("/ws") as bob:
        join(alice, "Alice")
        join(bob, "Bob")
        alice.receive_json()  # Bob joined

        limit(alice, "buy", 36.5, 1)
        rejection = alice.receive_json()

        limit(bob, "buy", 30, 1)
        bob_next = bob.receive_json()  # Bob's own update, not Alice's rejection

    assert rejection["type"] == "rejected"
    assert "tick" in rejection["reason"]
    assert bob_next["type"] == "update"


def test_malformed_messages_are_rejected_without_closing_the_socket(client):
    with client.websocket_connect("/ws") as alice:
        join(alice, "Alice")

        alice.send_json({"type": "limit", "market_id": "cars"})
        assert alice.receive_json() == {"type": "rejected", "command": "limit",
                                        "market_id": "cars", "reason": "bad message"}
        alice.send_text("not json")
        assert alice.receive_json()["reason"] == "bad message"

        limit(alice, "buy", 30, 1)
        assert alice.receive_json()["type"] == "update"


def test_a_rejection_says_which_market_it_was_in(client, room):
    open_market(room.exchange, market_id="homes", tick_size=5)
    with client.websocket_connect("/ws") as alice:
        join(alice, "Alice")

        limit(alice, "buy", 101, 1, market_id="homes")
        off_tick = alice.receive_json()
        alice.send_json({"type": "take", "market_id": "homes"})
        malformed = alice.receive_json()

    assert (off_tick["type"], off_tick["market_id"]) == ("rejected", "homes")
    assert (malformed["reason"], malformed["market_id"]) == ("bad message", "homes")


def test_a_rejection_without_a_market_has_no_market_id(client):
    with client.websocket_connect("/ws") as alice:
        join(alice, "Alice")

        alice.send_json({"type": "nonsense"})

        assert alice.receive_json() == {"type": "rejected", "command": "nonsense",
                                        "market_id": None, "reason": "bad message"}


def test_cancel_and_cancel_all(client):
    with client.websocket_connect("/ws") as alice:
        join(alice, "Alice")
        limit(alice, "buy", 30, 1)
        (order,) = alice.receive_json()["me"]["cars"]["orders"]

        alice.send_json({"type": "cancel", "market_id": "cars", "order_id": order["order_id"]})
        assert alice.receive_json()["me"]["cars"]["orders"] == []

        limit(alice, "buy", 30, 1)
        limit(alice, "sell", 50, 1)
        alice.receive_json()
        alice.receive_json()
        alice.send_json({"type": "cancel_all"})
        update = alice.receive_json()

    assert update["me"]["cars"]["orders"] == []
    assert update["markets"]["cars"]["bids"] == []
    assert update["markets"]["cars"]["asks"] == []


# --- Several markets at once ----------------------------------------------------------------


def test_two_markets_trade_side_by_side_until_one_settles(client, room):
    open_market(room.exchange, market_id="homes")
    with client.websocket_connect("/ws") as alice, client.websocket_connect("/admin/ws") as admin:
        _, snapshot = join(alice, "Alice")
        admin.send_json({"type": "admin_login", "secret": SECRET})
        admin.receive_json()  # welcome
        admin.receive_json()  # state

        limit(alice, "buy", 30, 1, market_id="cars")
        cars_order = alice.receive_json()
        limit(alice, "sell", 900, 2, market_id="homes")
        both = alice.receive_json()

        admin.send_json({"type": "settle", "market_id": "homes", "value": 850})
        after_settle = alice.receive_json()

    assert (snapshot["columns"], snapshot["book_depth"]) == (["cars", "homes"], 5)
    assert names_on(cars_order, "bids", "cars") == [("Alice", 1)]
    assert names_on(both, "bids", "cars") == [("Alice", 1)]
    assert names_on(both, "asks", "homes") == [("Alice", 2)]
    # Settled: no column any more and the book is full depth again, but it stays in the tables.
    assert (after_settle["columns"], after_settle["book_depth"]) == (["cars"], 10)
    assert after_settle["table_markets"] == ["cars", "homes"]


# --- The "done" reply (used by the load test to time each command) ----------------------------


def test_a_command_with_a_ref_gets_done_after_its_update(client):
    with client.websocket_connect("/ws") as alice:
        join(alice, "Alice")

        alice.send_json({"type": "limit", "market_id": "cars", "side": "buy", "price": 30,
                         "size": 1, "ref": 7})

        assert alice.receive_json()["type"] == "update"
        assert alice.receive_json() == {"type": "done", "ref": 7}


def test_a_rejected_or_malformed_command_with_a_ref_still_gets_done(client):
    with client.websocket_connect("/ws") as alice:
        join(alice, "Alice")

        alice.send_json({"type": "limit", "market_id": "cars", "side": "buy", "price": 30,
                         "size": 0, "ref": "a"})
        assert alice.receive_json()["type"] == "rejected"
        assert alice.receive_json() == {"type": "done", "ref": "a"}

        alice.send_json({"type": "limit", "ref": "b"})
        assert alice.receive_json()["reason"] == "bad message"
        assert alice.receive_json() == {"type": "done", "ref": "b"}


def test_a_command_without_a_ref_gets_no_done(client):
    # Browsers never send a ref, so they never get "done" messages.
    with client.websocket_connect("/ws") as alice:
        join(alice, "Alice")

        limit(alice, "buy", 30, 1)
        alice.send_json({"type": "limit", "market_id": "cars", "side": "buy", "price": 31,
                         "size": 1, "ref": 1})

        assert alice.receive_json()["type"] == "update"  # the first order: no "done" after it
        assert alice.receive_json()["type"] == "update"
        assert alice.receive_json() == {"type": "done", "ref": 1}
