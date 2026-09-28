"""Max position: orders are clipped to the worst-case room (position + own resting orders)."""

import pytest

from exchange.engine import Exchange, OrderAccepted, Rejected, Side
from helpers import accepted_id, asks, bids, open_market, trades_in

BUY, SELL = Side.BUY, Side.SELL


@pytest.fixture
def ex(clock):
    """One open market with max position 100."""
    exchange = Exchange(clock=clock)
    open_market(exchange, max_position=100)
    return exchange


def get_long_80(ex):
    ex.place_limit("cars", "bob", SELL, 50, 80)
    ex.take("cars", "alice", BUY, 50, 80)
    assert ex.position("cars", "alice") == 80


def test_resting_orders_count_toward_the_limit(ex):
    get_long_80(ex)
    ex.place_limit("cars", "alice", BUY, 40, 15)

    events = ex.place_limit("cars", "alice", BUY, 41, 20)  # 80 + 15 + 20 = 115 > 100

    accepted = events[0]
    assert isinstance(accepted, OrderAccepted)
    assert (accepted.requested_size, accepted.size) == (20, 5)
    assert bids(ex) == [(41, [("alice", 5)]), (40, [("alice", 15)])]


def test_order_clipped_to_zero_is_rejected(ex):
    get_long_80(ex)
    ex.place_limit("cars", "alice", BUY, 40, 20)

    (event,) = ex.place_limit("cars", "alice", BUY, 41, 1)

    assert isinstance(event, Rejected)
    assert "max position" in event.reason


def test_sell_side_room_is_computed_separately(ex):
    get_long_80(ex)
    ex.place_limit("cars", "alice", BUY, 40, 15)  # resting bids don't use up sell room

    first = ex.place_limit("cars", "alice", SELL, 60, 150)  # room = 100 + 80 = 180
    second = ex.place_limit("cars", "alice", SELL, 61, 40)  # room left = 30

    assert first[0].size == 150
    assert (second[0].requested_size, second[0].size) == (40, 30)


def test_quote_sides_are_clipped_independently(ex):
    ex.place_limit("cars", "alice", BUY, 30, 98)

    ex.place_quote("cars", "alice", 35, 38, 5)

    assert bids(ex) == [(35, [("alice", 2)]), (30, [("alice", 98)])]
    assert asks(ex) == [(38, [("alice", 5)])]


def test_take_is_clipped(ex):
    ex.place_limit("cars", "bob", SELL, 50, 100)
    ex.place_limit("cars", "carol", SELL, 50, 100)

    events = ex.take("cars", "alice", BUY, 50, 150)

    assert (events[0].requested_size, events[0].size) == (150, 100)
    assert sum(trade.size for trade in trades_in(events)) == 100
    assert ex.position("cars", "alice") == 100
    assert asks(ex) == [(50, [("carol", 100)])]


def test_sellers_are_clipped_too(ex):
    events = ex.place_limit("cars", "bob", SELL, 50, 200)

    assert (events[0].requested_size, events[0].size) == (200, 100)


def test_room_frees_up_after_a_resting_order_is_cancelled(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 40, 100))
    ex.cancel("cars", "alice", order_id)

    assert ex.place_limit("cars", "alice", BUY, 40, 100)[0].size == 100
