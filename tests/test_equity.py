"""Contract multipliers and starting equity (SPEC Q144-Q152).

Each market has a contract multiplier, "$ per point": a trader's PnL in that market, in the
market's own price units, times the multiplier is their PnL in dollars. Total PnL adds up the
dollars over every market, and equity = the game's starting equity + total PnL.
"""

import math
from dataclasses import replace

import pytest

from exchange.engine import (
    Exchange,
    MarketCreated,
    PnL,
    Side,
    StartingEquitySet,
    from_dict,
    to_dict,
)
from exchange.engine.exchange import DEFAULT_STARTING_EQUITY
from exchange.server.room import Room
from exchange.server.views import admin_message, market_view, total_view
from helpers import make_config

BUY, SELL = Side.BUY, Side.SELL


def open_with(ex, market_id, dollars_per_point, tick_size=1):
    ex.create_market(replace(make_config(market_id, tick_size=tick_size),
                             dollars_per_point=dollars_per_point))
    ex.open_market(market_id)


def round_trip(ex, market_id, buyer, seller, price, size):
    """seller offers, buyer lifts: one trade at `price`."""
    ex.place_limit(market_id, seller, SELL, price, size)
    ex.take(market_id, buyer, BUY, price, size)


# --- The multiplier --------------------------------------------------------------------------


def test_the_multiplier_is_1_unless_set(ex):
    assert ex.markets["cars"].config.dollars_per_point == 1


def test_total_pnl_is_each_markets_pnl_times_its_multiplier():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.01, tick_size=500)   # a ~120,000 market
    open_with(ex, "homes", dollars_per_point=0.2, tick_size=50)    # a ~7,000 market
    round_trip(ex, "cars", "alice", "bob", 120_000, 1)
    round_trip(ex, "cars", "carol", "alice", 125_000, 1)            # alice +5,000 points
    round_trip(ex, "homes", "alice", "bob", 7_000, 1)
    round_trip(ex, "homes", "carol", "alice", 7_250, 1)             # alice +250 points

    total = ex.total_pnl("alice")

    assert total.realized == pytest.approx(5_000 * 0.01 + 250 * 0.2)  # $50 + $50
    assert total.unrealized == 0


def test_unrealized_pnl_is_scaled_too():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.5)
    round_trip(ex, "cars", "alice", "bob", 100, 4)
    ex.place_limit("cars", "carol", BUY, 104, 1)
    ex.place_limit("cars", "dave", SELL, 106, 1)   # mark 105: alice is up 5 x 4 = 20 points

    assert ex.total_pnl("alice") == PnL(realized=0, unrealized=10)


def test_a_markets_own_pnl_stays_in_points():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.01)
    round_trip(ex, "cars", "alice", "bob", 100, 1)
    round_trip(ex, "cars", "carol", "alice", 300, 1)

    assert ex.pnl("cars", "alice") == PnL(realized=200, unrealized=0)


@pytest.mark.parametrize("value", [0, -1, True, "1", math.inf, math.nan, None])
def test_the_multiplier_must_be_a_positive_number(value):
    ex = Exchange()
    config = replace(make_config(), dollars_per_point=value)

    with pytest.raises(ValueError, match=r"\$ per point"):
        ex.create_market(config)


def test_the_multiplier_can_be_a_decimal_or_a_whole_number():
    ex = Exchange()
    ex.create_market(replace(make_config("a"), dollars_per_point=0.25))
    ex.create_market(replace(make_config("b"), dollars_per_point=50))

    assert ex.markets["a"].config.dollars_per_point == 0.25
    assert ex.markets["b"].config.dollars_per_point == 50


def test_editing_a_market_can_change_its_multiplier():
    ex = Exchange()
    ex.create_market(make_config())
    ex.edit_market(replace(make_config(), dollars_per_point=0.1))

    assert ex.markets["cars"].config.dollars_per_point == 0.1


def test_the_multiplier_is_saved_in_the_log_and_replays():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.01)

    replayed = Exchange.replay([from_dict(to_dict(event)) for event in ex.events])

    assert replayed.markets["cars"].config.dollars_per_point == 0.01


def test_a_log_saved_before_multipliers_existed_still_replays_with_1():
    data = to_dict(MarketCreated(market_id="cars", title="Cars", tick_size=1, max_position=10,
                                 forced_trade_size=1, forced_trade_seconds=30))
    del data["dollars_per_point"]

    replayed = Exchange.replay([from_dict(data)])

    assert replayed.markets["cars"].config.dollars_per_point == 1


# --- Starting equity -------------------------------------------------------------------------


def test_equity_starts_at_the_default_starting_equity(ex):
    assert DEFAULT_STARTING_EQUITY == 100_000
    assert ex.equity("alice") == 100_000


def test_equity_is_starting_equity_plus_total_pnl_in_dollars():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.5)
    round_trip(ex, "cars", "alice", "bob", 100, 2)
    round_trip(ex, "cars", "carol", "alice", 110, 2)   # +20 points = +$10

    assert ex.equity("alice") == 100_010
    assert ex.equity("bob") == 99_990                  # short 2 from 100, marked at 110: -$10


def test_the_admin_sets_the_starting_equity(ex):
    (event,) = ex.set_starting_equity(1_000_000)

    assert isinstance(event, StartingEquitySet)
    assert ex.starting_equity == 1_000_000
    assert ex.equity("alice") == 1_000_000


def test_starting_equity_can_be_changed_during_a_game(ex):
    ex.set_starting_equity(50_000)
    ex.set_starting_equity(250_000)

    assert ex.starting_equity == 250_000


@pytest.mark.parametrize("amount", [-1, 2.5, True, "100000", None])
def test_starting_equity_must_be_a_whole_number_of_dollars_at_least_0(ex, amount):
    with pytest.raises(ValueError, match="starting equity"):
        ex.set_starting_equity(amount)


def test_starting_equity_replays(ex):
    ex.set_starting_equity(500_000)

    replayed = Exchange.replay([from_dict(to_dict(event)) for event in ex.events])

    assert replayed.starting_equity == 500_000


# --- What browsers get -----------------------------------------------------------------------


def test_market_view_says_the_multiplier():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.01)

    assert market_view(ex, "cars")["dollars_per_point"] == 0.01


def test_total_view_is_in_dollars_rounded_to_cents_with_equity():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.1)
    round_trip(ex, "cars", "alice", "bob", 100, 1)
    round_trip(ex, "cars", "carol", "alice", 103, 1)   # +3 points x 0.1 = $0.30 (0.30000000000000004)

    assert total_view(ex, "alice") == {
        "realized": 0.3, "unrealized": 0, "total": 0.3, "equity": 100_000.3,
    }


def test_the_admin_sees_the_starting_equity(ex):
    ex.set_starting_equity(20_000)

    assert admin_message(Room(code="1234", exchange=ex))["starting_equity"] == 20_000


# --- Admin commands through the room -----------------------------------------------------------


def test_create_market_takes_dollars_per_point():
    room = Room(code="1234", exchange=Exchange())
    room.handle_admin({"type": "create_market", "title": "Cars", "tick_size": 500,
                       "max_position": 50, "forced_trade_size": 1, "forced_trade_seconds": 30,
                       "dollars_per_point": 0.01})

    assert room.exchange.markets["m1"].config.dollars_per_point == 0.01


def test_create_market_without_dollars_per_point_uses_1():
    # Older scripts (e.g. the load test) don't send it.
    room = Room(code="1234", exchange=Exchange())
    room.handle_admin({"type": "create_market", "title": "Cars", "tick_size": 500,
                       "max_position": 50, "forced_trade_size": 1, "forced_trade_seconds": 30})

    assert room.exchange.markets["m1"].config.dollars_per_point == 1


def test_set_starting_equity_command():
    room = Room(code="1234", exchange=Exchange())
    room.handle_admin({"type": "set_starting_equity", "amount": 25_000})

    assert room.exchange.starting_equity == 25_000


def test_reset_keeps_the_starting_equity():
    room = Room(code="1234", exchange=Exchange())
    room.handle_admin({"type": "set_starting_equity", "amount": 25_000})

    room.reset()

    assert room.exchange.starting_equity == 25_000
