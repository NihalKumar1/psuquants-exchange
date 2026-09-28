"""The admin page end to end: real WebSocket messages for the admin and for traders."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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


@pytest.fixture
def client(room):
    # "with" makes every socket in a test share one event loop, like the real server does.
    # Without it, each socket gets its own loop and the app's shared lock can hang.
    with TestClient(create_app(room, admin_secret=SECRET)) as client:
        yield client


def admin_login(ws):
    """Log in as the admin; returns the first admin state."""
    ws.send_json({"type": "admin_login", "secret": SECRET})
    assert ws.receive_json() == {"type": "admin_welcome"}
    state = ws.receive_json()
    assert state["type"] == "admin_state", state
    return state


def join(ws, name):
    """Join as a trader; returns the welcome message."""
    ws.send_json({"type": "join", "code": CODE, "name": name})
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome", welcome
    assert ws.receive_json()["type"] == "snapshot"
    return welcome


def rows(state):
    """The admin's traders table as {name: row}."""
    return {row["name"]: row for row in state["traders"]}


def names_on(update, side, market_id="cars"):
    levels = update["markets"][market_id][side]
    return [(order["name"], order["size"]) for level in levels for order in level["orders"]]


# --- Logging in -----------------------------------------------------------------------------


def test_admin_page_is_served(client):
    response = client.get("/admin")

    assert response.status_code == 200
    assert "admin" in response.text.lower()


@pytest.mark.parametrize("first_message", [
    {"type": "admin_login", "secret": "wrong"},
    {"type": "admin_login"},
    {"type": "halt", "market_id": "cars"},  # a command before logging in
])
def test_a_wrong_or_missing_secret_is_refused(client, first_message):
    with client.websocket_connect("/admin/ws") as ws:
        ws.send_json(first_message)

        assert ws.receive_json() == {"type": "error", "reason": "wrong admin secret"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_admin_login_gets_the_room_state(client):
    with client.websocket_connect("/admin/ws") as admin:
        state = admin_login(admin)

    assert state["room_code"] == CODE
    assert state["joining_locked"] is False
    assert state["markets"]["cars"]["status"] == "open"
    assert state["traders"] == []


# --- What the admin sees --------------------------------------------------------------------


def test_admin_sees_traders_connect_trade_and_disconnect(client):
    with client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        with client.websocket_connect("/ws") as alice, client.websocket_connect("/ws") as bob:
            join(alice, "Alice")
            assert rows(admin.receive_json())["Alice"]["connected"] is True
            join(bob, "Bob")
            admin.receive_json()

            alice.send_json({"type": "limit", "market_id": "cars", "side": "sell",
                             "price": 40, "size": 10})
            admin.receive_json()
            bob.send_json({"type": "take", "market_id": "cars", "side": "buy",
                           "price": 40, "size": 4})
            state = admin.receive_json()
            assert rows(state)["Bob"]["markets"]["cars"]["position"] == 4
            assert rows(state)["Alice"]["markets"]["cars"]["position"] == -4

        # Both traders closed their tabs: each disconnect refreshes the admin page.
        admin.receive_json()
        state = admin.receive_json()

    assert rows(state)["Alice"]["connected"] is False
    assert rows(state)["Bob"]["connected"] is False


def test_a_trader_rejection_does_not_refresh_the_admin_page(client):
    with client.websocket_connect("/admin/ws") as admin, client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()

        alice.send_json({"type": "limit", "market_id": "cars", "side": "buy",
                         "price": 30.5, "size": 1})
        assert alice.receive_json()["type"] == "rejected"
        admin.send_json({"type": "info_drop", "note": "check"})
        state = admin.receive_json()  # the next admin message is the info drop's

    assert [drop["note"] for drop in state["info_drops"]] == ["check"]


# --- Admin commands -------------------------------------------------------------------------


def test_a_new_market_reaches_traders_and_opens(client):
    with client.websocket_connect("/admin/ws") as admin, client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()

        admin.send_json({"type": "create_market", **SETTINGS})
        state = admin.receive_json()
        (market_id,) = [m for m in state["markets"] if m != "cars"]
        assert alice.receive_json()["markets"][market_id]["status"] == "created"

        admin.send_json({"type": "open_market", "market_id": market_id})
        assert admin.receive_json()["markets"][market_id]["status"] == "open"
        update = alice.receive_json()

    assert update["markets"][market_id]["status"] == "open"
    assert update["markets"][market_id]["title"] == "Cars in Centre County"


def test_a_halted_market_rejects_trader_orders(client):
    with client.websocket_connect("/admin/ws") as admin, client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()

        admin.send_json({"type": "halt", "market_id": "cars"})
        admin.receive_json()
        assert alice.receive_json()["markets"]["cars"]["status"] == "halted"

        alice.send_json({"type": "limit", "market_id": "cars", "side": "buy",
                         "price": 30, "size": 1})
        reply = alice.receive_json()

    assert reply["type"] == "rejected"
    assert "not open" in reply["reason"]


def test_settling_shows_the_value_to_traders(client):
    with client.websocket_connect("/admin/ws") as admin, client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()

        admin.send_json({"type": "settle", "market_id": "cars", "value": 118_437})
        admin.receive_json()
        market = alice.receive_json()["markets"]["cars"]

    assert market["status"] == "settled"
    assert market["settlement_value"] == 118_437
    assert market["mark"] == 118_437


def test_a_kicked_trader_is_told_disconnected_and_cannot_rejoin(client):
    with client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice, client.websocket_connect("/ws") as bob:
        admin_login(admin)
        welcome = join(alice, "Alice")
        admin.receive_json()
        join(bob, "Bob")
        admin.receive_json()
        alice.receive_json()  # Bob joined

        admin.send_json({"type": "kick", "trader_id": welcome["trader_id"]})

        assert alice.receive_json() == {"type": "kicked"}
        with pytest.raises(WebSocketDisconnect):
            alice.receive_json()
        assert bob.receive_json()["type"] == "update"
        state = admin.receive_json()
        assert (rows(state)["Alice"]["kicked"], rows(state)["Alice"]["connected"]) == (True, False)

    with client.websocket_connect("/ws") as again:
        again.send_json({"type": "rejoin", "token": welcome["token"]})
        assert again.receive_json()["type"] == "error"


def test_a_rename_shows_everywhere(client):
    with client.websocket_connect("/admin/ws") as admin, \
            client.websocket_connect("/ws") as alice, client.websocket_connect("/ws") as bob:
        admin_login(admin)
        welcome = join(alice, "Alice")
        admin.receive_json()
        join(bob, "Bob")
        admin.receive_json()
        alice.receive_json()  # Bob joined
        alice.send_json({"type": "limit", "market_id": "cars", "side": "sell",
                         "price": 40, "size": 10})
        alice.receive_json()
        bob.receive_json()
        admin.receive_json()

        admin.send_json({"type": "rename", "trader_id": welcome["trader_id"], "name": "Alicia"})
        alice_update = alice.receive_json()
        bob_update = bob.receive_json()
        state = admin.receive_json()

    assert alice_update["name"] == "Alicia"
    assert names_on(alice_update, "asks") == [("Alicia", 10)]
    assert names_on(bob_update, "asks") == [("Alicia", 10)]
    assert "Alicia" in rows(state)


def test_an_info_drop_reaches_the_admin_but_not_traders(client):
    with client.websocket_connect("/admin/ws") as admin, client.websocket_connect("/ws") as alice:
        admin_login(admin)
        join(alice, "Alice")
        admin.receive_json()

        admin.send_json({"type": "info_drop", "note": "Over 100k"})
        state = admin.receive_json()
        # If the info drop had reached Alice, this would be the first message she gets next.
        alice.send_json({"type": "limit", "market_id": "cars", "side": "buy",
                         "price": 30, "size": 1})
        update = alice.receive_json()

    assert [drop["note"] for drop in state["info_drops"]] == ["Over 100k"]
    assert len(update["me"]["cars"]["orders"]) == 1
    assert "Over 100k" not in str(update)


def test_locking_joins_refuses_new_names(client):
    with client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)
        admin.send_json({"type": "lock_joining"})
        assert admin.receive_json()["joining_locked"] is True

        with client.websocket_connect("/ws") as alice:
            alice.send_json({"type": "join", "code": CODE, "name": "Alice"})
            reply = alice.receive_json()

    assert reply == {"type": "error", "reason": "joining is locked"}


def test_admin_mistakes_and_bad_messages_come_back_as_admin_errors(client):
    with client.websocket_connect("/admin/ws") as admin:
        admin_login(admin)

        admin.send_json({"type": "resume", "market_id": "cars"})
        mistake = admin.receive_json()
        admin.send_json({"type": "settle", "market_id": "cars"})  # no value
        malformed = admin.receive_json()
        admin.send_text("not json")
        not_json = admin.receive_json()

    assert mistake["type"] == "admin_error"
    assert "cannot resume" in mistake["reason"]
    assert malformed == {"type": "admin_error", "reason": "bad message"}
    assert not_json == {"type": "admin_error", "reason": "bad message"}
