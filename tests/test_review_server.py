"""The review page (/review) and the export download (/admin/export), end to end."""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from exchange.engine import Exchange, Side
from exchange.server.app import create_app
from exchange.server.room import Room

CODE = "1234"
SECRET = "let-me-in"


@pytest.fixture
def room(ex):
    """A room whose exchange already has one open market, "cars"."""
    return Room(code=CODE, exchange=ex)


@pytest.fixture
def client(room):
    # A tiny review interval so the tests don't wait a second for each live push.
    with TestClient(create_app(room, admin_secret=SECRET, review_interval=0.01)) as client:
        yield client


def review_login(ws):
    """Log in on the review socket; returns the first review state (nothing picked yet)."""
    ws.send_json({"type": "admin_login", "secret": SECRET})
    assert ws.receive_json() == {"type": "review_welcome"}
    state = ws.receive_json()
    assert state["type"] == "review_state", state
    return state


def admin_login(ws):
    ws.send_json({"type": "admin_login", "secret": SECRET})
    assert ws.receive_json() == {"type": "admin_welcome"}
    state = ws.receive_json()
    assert state["type"] == "admin_state", state
    return state


def receive_until(ws, wanted):
    """Read review states until one passes the `wanted` check, and return it. Live pushes come
    on a timer, so a push showing an earlier moment may arrive first."""
    while True:
        state = ws.receive_json()
        assert state["type"] == "review_state", state
        if wanted(state):
            return state


def join(ws, name):
    ws.send_json({"type": "join", "code": CODE, "name": name})
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome", welcome
    assert ws.receive_json()["type"] == "snapshot"
    return welcome["trader_id"]


# --- The page and logging in ----------------------------------------------------------------


def test_review_page_is_served(client):
    response = client.get("/review")

    assert response.status_code == 200
    assert "review" in response.text.lower()


def test_a_wrong_secret_is_refused_on_the_review_socket(client):
    with client.websocket_connect("/review/ws") as ws:
        ws.send_json({"type": "admin_login", "secret": "wrong"})

        assert ws.receive_json() == {"type": "error", "reason": "wrong admin secret"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_after_logging_in_the_review_page_gets_the_pickers_and_no_review(client, room):
    (alice,) = [room.exchange.join("Alice")[0].trader_id]

    with client.websocket_connect("/review/ws") as ws:
        state = review_login(ws)

    assert state["options"]["markets"] == [
        {"market_id": "cars", "title": "Test market cars", "status": "open"}]
    assert state["options"]["traders"] == [{"trader_id": alice, "name": "Alice", "kicked": False}]
    assert state["review"] is None


# --- Picking and live updates ---------------------------------------------------------------


def test_picking_a_trader_and_market_sends_their_review(client, room):
    ex = room.exchange
    alice, bob = (ex.join(name)[0].trader_id for name in ("Alice", "Bob"))
    ex.place_limit("cars", bob, Side.SELL, 40, 10)
    ex.take("cars", alice, Side.BUY, 40, 4)

    with client.websocket_connect("/review/ws") as ws:
        review_login(ws)
        ws.send_json({"type": "review_pick", "market_id": "cars", "trader_id": alice})
        state = ws.receive_json()

    assert state["type"] == "review_state"
    review = state["review"]
    assert (review["market_id"], review["trader"]) == ("cars", "Alice")
    assert [(row["side"], row["price"], row["size"], row["counterparty"])
            for row in review["rows"]] == [("buy", 40, 4, "Bob")]


def test_picking_an_unknown_market_or_trader_shows_no_review(client):
    with client.websocket_connect("/review/ws") as ws:
        review_login(ws)
        ws.send_json({"type": "review_pick", "market_id": "nope", "trader_id": "t99"})

        assert ws.receive_json()["review"] is None


def test_a_trade_pushes_a_fresh_review(client):
    with client.websocket_connect("/ws") as alice,             client.websocket_connect("/ws") as bob,             client.websocket_connect("/review/ws") as review:
        alice_id = join(alice, "Alice")
        join(bob, "Bob")
        alice.receive_json()  # Alice hears that Bob joined
        review_login(review)
        review.send_json({"type": "review_pick", "market_id": "cars", "trader_id": alice_id})
        state = receive_until(review, lambda state: state["review"] is not None)
        assert state["review"]["rows"] == []

        bob.send_json({"type": "limit", "market_id": "cars", "side": "sell", "price": 40, "size": 10})
        alice.receive_json(), bob.receive_json()
        alice.send_json({"type": "take", "market_id": "cars", "side": "buy", "price": 40, "size": 3})
        alice.receive_json(), bob.receive_json()

        state = receive_until(review, lambda state: state["review"]["rows"] != [])

    assert [(row["side"], row["size"], row["counterparty"]) for row in state["review"]["rows"]] == [
        ("buy", 3, "Bob")]


def test_reset_clears_the_pick(client, room):
    (alice,) = [room.exchange.join("Alice")[0].trader_id]
    with client.websocket_connect("/review/ws") as review, \
            client.websocket_connect("/admin/ws") as admin:
        review_login(review)
        admin_login(admin)
        review.send_json({"type": "review_pick", "market_id": "cars", "trader_id": alice})
        assert review.receive_json()["review"] is not None

        admin.send_json({"type": "reset"})
        assert admin.receive_json()["type"] == "admin_state"
        state = receive_until(review, lambda state: state["review"] is None)

    assert state["review"] is None
    assert state["options"] == {"markets": [], "traders": []}


# --- Export ---------------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Secret": "wrong"}])
def test_export_needs_the_admin_secret(client, headers):
    response = client.get("/admin/export", headers=headers)

    assert response.status_code == 403


def test_export_downloads_the_zip_and_clears_the_unexported_warning(client, room, clock):
    with client.websocket_connect("/admin/ws") as admin:
        assert admin_login(admin)["unexported"] is True

        response = client.get("/admin/export", headers={"X-Admin-Secret": SECRET})
        state = admin.receive_json()

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        'attachment; filename="psuquants-game-2026-09-27-0530.zip"')
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == ["events.csv", "events.json", "orders.csv", "trades.csv"]
    assert state["unexported"] is False


# --- The unexported-changes warning (room) --------------------------------------------------


def test_people_joining_is_not_a_change_worth_exporting(clock):
    room = Room(code=CODE, exchange=Exchange(clock=clock))
    room.exchange.join("Alice")
    assert room.has_unexported_changes() is False

    room.exchange.mark_info_drop("over 100k")
    assert room.has_unexported_changes() is True

    room.mark_exported()
    assert room.has_unexported_changes() is False

    room.exchange.join("Bob")
    assert room.has_unexported_changes() is False


def test_a_new_game_after_reset_has_nothing_to_export(room):
    room.mark_exported()
    room.exchange.mark_info_drop("over 100k")
    alice, token, _ = room.join(CODE, "Alice")
    room.connect(alice, object())

    room.reset()  # Alice is carried over: the new log starts with her joining

    assert room.exported_seq == 0
    assert room.has_unexported_changes() is False
