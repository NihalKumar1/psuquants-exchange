"""The room: room code, joining and rejoining, one live connection per trader, and commands."""

import pytest

from exchange.engine import Exchange, MarketStatus, OrderAccepted, Rejected, TraderJoined, to_dict
from exchange.server.room import Room
from helpers import asks, bids, open_market, trades_in

CODE = "1234"


@pytest.fixture
def room(ex):
    return Room(code=CODE, exchange=ex)


def join(room, name):
    trader_id, token, _ = room.join(CODE, name)
    return trader_id, token


# --- Joining --------------------------------------------------------------------------------


def test_wrong_room_code_is_refused(room):
    with pytest.raises(ValueError, match="wrong room code"):
        room.join("9999", "Alice")
    assert room.exchange.traders == {}


def test_room_code_may_have_spaces_around_it(room):
    trader_id, _, _ = room.join(" 1234 ", "Alice")

    assert trader_id == "t1"


def test_join_returns_trader_id_token_and_the_logged_event(room):
    trader_id, token, events = room.join(CODE, "Alice")

    assert trader_id == "t1"
    assert token
    assert [type(event) for event in events] == [TraderJoined]


def test_each_join_gets_a_different_token(room):
    _, alice_token = join(room, "Alice")
    _, bob_token = join(room, "Bob")

    assert alice_token != bob_token


def test_bad_name_is_refused_with_the_engine_reason(room):
    with pytest.raises(ValueError, match="1 to 20 characters"):
        room.join(CODE, "")


def test_token_rejoins_the_same_trader(room):
    trader_id, token = join(room, "Alice")

    assert room.rejoin(token) == trader_id


def test_unknown_token_does_not_rejoin(room):
    assert room.rejoin("made-up") is None


def test_name_is_taken_while_its_trader_is_connected(room):
    trader_id, _ = join(room, "Alice")
    room.connect(trader_id, "socket-1")

    with pytest.raises(ValueError, match="taken"):
        room.join(CODE, "alice")


def test_same_name_reclaims_a_disconnected_trader(room):
    trader_id, _ = join(room, "Alice")
    room.connect(trader_id, "socket-1")
    room.disconnect(trader_id, "socket-1")

    reclaimed_id, token, events = room.join(CODE, " ALICE ")

    assert reclaimed_id == trader_id
    assert events == []  # nobody new joined, so nothing is logged
    assert room.rejoin(token) == trader_id
    assert room.exchange.traders == {"t1": "Alice"}


def test_tokens_never_go_into_the_event_log(room):
    _, token = join(room, "Alice")

    assert token not in str([to_dict(event) for event in room.exchange.events])


def test_locked_joining_refuses_new_names(room):
    room.exchange.lock_joining()

    with pytest.raises(ValueError, match="joining is locked"):
        room.join(CODE, "Alice")


def test_locked_joining_still_lets_an_existing_trader_reclaim_their_name(room):
    trader_id, token = join(room, "Alice")
    room.exchange.lock_joining()

    reclaimed_id, _, _ = room.join(CODE, "alice")

    assert reclaimed_id == trader_id
    assert room.rejoin(token) == trader_id


def test_a_kicked_trader_cannot_come_back_by_token_or_name(room):
    trader_id, token = join(room, "Alice")
    room.exchange.kick(trader_id)

    assert room.rejoin(token) is None
    with pytest.raises(ValueError, match="removed from this game"):
        room.join(CODE, "Alice")


# --- Connections ----------------------------------------------------------------------------


def test_newest_connection_wins(room):
    trader_id, _ = join(room, "Alice")

    assert room.connect(trader_id, "socket-1") is None
    assert room.connect(trader_id, "socket-2") == "socket-1"  # the caller tells it to go away
    assert room.connections == {trader_id: "socket-2"}


def test_an_old_connection_closing_does_not_disconnect_the_new_one(room):
    trader_id, _ = join(room, "Alice")
    room.connect(trader_id, "socket-1")
    room.connect(trader_id, "socket-2")

    room.disconnect(trader_id, "socket-1")

    assert room.connections == {trader_id: "socket-2"}


# --- Commands -------------------------------------------------------------------------------


def test_limit_command(room):
    alice, _ = join(room, "Alice")

    room.handle(alice, {"type": "limit", "market_id": "cars", "side": "buy",
                        "price": 36, "size": 20})

    assert bids(room.exchange) == [(36, [(alice, 20)])]


def test_quote_command(room):
    alice, _ = join(room, "Alice")

    room.handle(alice, {"type": "quote", "market_id": "cars", "bid_price": 35,
                        "ask_price": 38, "size": 100})

    assert bids(room.exchange) == [(35, [(alice, 100)])]
    assert asks(room.exchange) == [(38, [(alice, 100)])]


def test_take_command(room):
    alice, _ = join(room, "Alice")
    bob, _ = join(room, "Bob")
    room.handle(alice, {"type": "limit", "market_id": "cars", "side": "sell",
                        "price": 37, "size": 10})

    events = room.handle(bob, {"type": "take", "market_id": "cars", "side": "buy",
                               "price": 37, "size": 3})

    (trade,) = trades_in(events)
    assert (trade.buyer_id, trade.seller_id, trade.price, trade.size) == (bob, alice, 37, 3)


def test_cancel_command(room):
    alice, _ = join(room, "Alice")
    events = room.handle(alice, {"type": "limit", "market_id": "cars", "side": "buy",
                                 "price": 36, "size": 20})
    order_id = next(e.order_id for e in events if isinstance(e, OrderAccepted))

    room.handle(alice, {"type": "cancel", "market_id": "cars", "order_id": order_id})

    assert bids(room.exchange) == []


def test_cancel_all_in_one_market_and_in_all_markets(room):
    open_market(room.exchange, market_id="homes")
    alice, _ = join(room, "Alice")
    for market_id in ("cars", "homes"):
        room.handle(alice, {"type": "limit", "market_id": market_id, "side": "buy",
                            "price": 30, "size": 1})

    room.handle(alice, {"type": "cancel_all", "market_id": "cars"})
    assert bids(room.exchange, "cars") == []
    assert bids(room.exchange, "homes") == [(30, [(alice, 1)])]

    room.handle(alice, {"type": "cancel_all"})
    assert bids(room.exchange, "homes") == []


def test_engine_rejections_come_back_as_events(room):
    alice, _ = join(room, "Alice")

    events = room.handle(alice, {"type": "limit", "market_id": "cars", "side": "buy",
                                 "price": 36.5, "size": 1})

    assert [type(event) for event in events] == [Rejected]


@pytest.mark.parametrize("message", [
    {"type": "dance"},
    {"type": "limit", "market_id": "cars"},  # missing fields
    {"type": "limit", "market_id": "cars", "side": "up", "price": 36, "size": 1},
])
def test_malformed_messages_raise(room, message):
    alice, _ = join(room, "Alice")

    with pytest.raises((ValueError, KeyError)):
        room.handle(alice, message)


# --- Admin commands -------------------------------------------------------------------------

SETTINGS = {"title": "Cars in Centre County", "tick_size": 500, "max_position": 50,
            "forced_trade_size": 1, "forced_trade_seconds": 30}


@pytest.fixture
def empty_room(clock):
    return Room(code=CODE, exchange=Exchange(clock=clock))


def test_create_market_gets_ids_m1_m2(empty_room):
    empty_room.handle_admin({"type": "create_market", **SETTINGS})
    empty_room.handle_admin({"type": "create_market", **SETTINGS, "title": "Homes"})

    markets = empty_room.exchange.markets
    assert list(markets) == ["m1", "m2"]
    assert markets["m2"].config.title == "Homes"
    assert markets["m1"].config.tick_size == 500


def test_market_lifecycle_commands(empty_room):
    exchange = empty_room.exchange
    empty_room.handle_admin({"type": "create_market", **SETTINGS})
    empty_room.handle_admin({"type": "edit_market", "market_id": "m1", **SETTINGS,
                             "tick_size": 100})
    assert exchange.markets["m1"].config.tick_size == 100

    steps = [("open_market", MarketStatus.OPEN), ("halt", MarketStatus.HALTED),
             ("resume", MarketStatus.OPEN)]
    for command, expected in steps:
        empty_room.handle_admin({"type": command, "market_id": "m1"})
        assert exchange.markets["m1"].status is expected

    empty_room.handle_admin({"type": "settle", "market_id": "m1", "value": 118_437})
    assert exchange.markets["m1"].settlement_value == 118_437


def test_trader_admin_commands(room):
    alice, _ = join(room, "Alice")
    bob, _ = join(room, "Bob")

    room.handle_admin({"type": "rename", "trader_id": alice, "name": "Alicia"})
    room.handle_admin({"type": "kick", "trader_id": bob})
    room.handle_admin({"type": "info_drop", "note": "It's over 100k"})
    room.handle_admin({"type": "lock_joining"})

    exchange = room.exchange
    assert exchange.traders[alice] == "Alicia"
    assert exchange.kicked == {bob}
    assert exchange.info_drops[0].note == "It's over 100k"
    assert exchange.joining_locked

    room.handle_admin({"type": "unlock_joining"})
    assert not exchange.joining_locked


def test_admin_mistakes_raise_value_error_with_a_reason(room):
    with pytest.raises(ValueError, match="cannot resume"):
        room.handle_admin({"type": "resume", "market_id": "cars"})


@pytest.mark.parametrize("message", [
    {"type": "dance"},
    {"type": "settle", "market_id": "cars"},  # missing value
])
def test_malformed_admin_messages_raise(room, message):
    with pytest.raises((ValueError, KeyError)):
        room.handle_admin(message)
