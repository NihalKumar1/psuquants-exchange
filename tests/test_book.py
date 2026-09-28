"""Matching engine: price-time priority, partial fills, crossing limits, takes, cancels, validation."""

import pytest

from exchange.engine import (
    Exchange,
    OrderAccepted,
    OrderCancelled,
    OrderRested,
    Rejected,
    Side,
)
from helpers import accepted_id, asks, bids, make_config, open_market, trades_in

BUY, SELL = Side.BUY, Side.SELL


# --- Price-time priority -------------------------------------------------------------------


def test_spec_case_36_bid_for_20_then_36_bid_for_15_hit_for_25(ex, clock):
    clock.set("09:30:01")
    first = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 20))
    clock.set("09:30:04")
    second = accepted_id(ex.place_limit("cars", "bob", BUY, 36, 15))

    clock.set("09:30:10")
    events = ex.take("cars", "carol", SELL, 36, 25)  # "hit the 36 bid, 25"

    fills = [(trade.buy_order_id, trade.price, trade.size) for trade in trades_in(events)]
    assert fills == [(first, 36, 20), (second, 36, 5)]
    assert bids(ex) == [(36, [("bob", 10)])]


def test_better_price_fills_before_an_earlier_order(ex):
    ex.place_limit("cars", "alice", BUY, 35, 10)
    ex.place_limit("cars", "bob", BUY, 36, 10)

    events = ex.place_limit("cars", "carol", SELL, 35, 15)

    fills = [(trade.buyer_id, trade.price, trade.size) for trade in trades_in(events)]
    assert fills == [("bob", 36, 10), ("alice", 35, 5)]
    assert bids(ex) == [(35, [("alice", 5)])]
    assert asks(ex) == []


def test_partially_filled_order_keeps_its_place_in_line(ex):
    ex.place_limit("cars", "alice", BUY, 36, 20)
    ex.place_limit("cars", "bob", BUY, 36, 10)

    ex.take("cars", "carol", SELL, 36, 5)
    assert bids(ex) == [(36, [("alice", 15), ("bob", 10)])]

    events = ex.take("cars", "dave", SELL, 36, 16)
    fills = [(trade.buyer_id, trade.size) for trade in trades_in(events)]
    assert fills == [("alice", 15), ("bob", 1)]
    assert bids(ex) == [(36, [("bob", 9)])]


# --- Limit orders ---------------------------------------------------------------------------


def test_limit_that_does_not_cross_rests(ex):
    events = ex.place_limit("cars", "alice", BUY, 36, 10)

    assert [type(event) for event in events] == [OrderAccepted, OrderRested]
    assert bids(ex) == [(36, [("alice", 10)])]


def test_crossing_limit_trades_at_resting_prices_and_remainder_rests(ex):
    ex.place_limit("cars", "alice", SELL, 37, 5)
    ex.place_limit("cars", "bob", SELL, 38, 5)

    events = ex.place_limit("cars", "carol", BUY, 40, 15)

    fills = [(trade.seller_id, trade.price, trade.size) for trade in trades_in(events)]
    assert fills == [("alice", 37, 5), ("bob", 38, 5)]
    assert isinstance(events[-1], OrderRested)
    assert bids(ex) == [(40, [("carol", 5)])]
    assert asks(ex) == []


def test_trade_records_buyer_seller_and_aggressor(ex):
    ex.place_limit("cars", "alice", SELL, 37, 5)
    (trade,) = trades_in(ex.place_limit("cars", "bob", BUY, 37, 5))

    assert (trade.buyer_id, trade.seller_id, trade.aggressor_side) == ("bob", "alice", BUY)
    assert ex.trades("cars") == [trade]


# --- Take (click on the best bid / offer) ---------------------------------------------------


def test_take_fills_at_clicked_price_and_cancels_the_rest(ex):
    ex.place_limit("cars", "alice", SELL, 37, 5)
    ex.place_limit("cars", "bob", SELL, 38, 5)

    events = ex.take("cars", "carol", BUY, 37, 8)

    fills = [(trade.seller_id, trade.price, trade.size) for trade in trades_in(events)]
    assert fills == [("alice", 37, 5)]
    assert isinstance(events[-1], OrderCancelled)
    assert bids(ex) == []  # a take never rests
    assert asks(ex) == [(38, [("bob", 5)])]


def test_take_gets_a_better_price_if_the_book_improved_before_the_click(ex):
    ex.place_limit("cars", "alice", SELL, 38, 5)  # carol sees 38 as the best offer...
    ex.place_limit("cars", "bob", SELL, 37, 5)  # ...but this arrives before her click

    events = ex.take("cars", "carol", BUY, 38, 5)

    fills = [(trade.seller_id, trade.price, trade.size) for trade in trades_in(events)]
    assert fills == [("bob", 37, 5)]


def test_take_on_an_empty_side_is_rejected(ex):
    events = ex.take("cars", "carol", BUY, 37, 5)

    assert [type(event) for event in events] == [Rejected]


def test_take_when_best_price_moved_away_is_rejected(ex):
    ex.place_limit("cars", "alice", SELL, 38, 5)

    events = ex.take("cars", "carol", BUY, 37, 5)

    assert [type(event) for event in events] == [Rejected]
    assert asks(ex) == [(38, [("alice", 5)])]


# --- Cancels --------------------------------------------------------------------------------


def test_cancel_one_order(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))
    ex.place_limit("cars", "alice", BUY, 35, 10)

    events = ex.cancel("cars", "alice", order_id)

    assert [type(event) for event in events] == [OrderCancelled]
    assert bids(ex) == [(35, [("alice", 10)])]


def test_cannot_cancel_someone_elses_order(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))

    events = ex.cancel("cars", "bob", order_id)

    assert [type(event) for event in events] == [Rejected]
    assert bids(ex) == [(36, [("alice", 10)])]


def test_cancel_unknown_order_is_rejected(ex):
    assert [type(event) for event in ex.cancel("cars", "alice", 999)] == [Rejected]


def test_cancel_filled_order_is_rejected(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))
    ex.take("cars", "bob", SELL, 36, 10)

    assert [type(event) for event in ex.cancel("cars", "alice", order_id)] == [Rejected]


def test_cancel_all_in_one_market(ex):
    open_market(ex, market_id="homes")
    ex.place_limit("cars", "alice", BUY, 36, 10)
    ex.place_limit("cars", "alice", SELL, 40, 10)
    ex.place_limit("cars", "bob", BUY, 35, 10)
    ex.place_limit("homes", "alice", BUY, 36, 10)

    events = ex.cancel_all("alice", "cars")

    assert [type(event) for event in events] == [OrderCancelled, OrderCancelled]
    assert bids(ex) == [(35, [("bob", 10)])]
    assert asks(ex) == []
    assert bids(ex, "homes") == [(36, [("alice", 10)])]


def test_cancel_all_in_every_market(ex):
    open_market(ex, market_id="homes")
    ex.place_limit("cars", "alice", BUY, 36, 10)
    ex.place_limit("cars", "bob", BUY, 35, 10)
    ex.place_limit("homes", "alice", BUY, 36, 10)

    ex.cancel_all("alice")

    assert bids(ex) == [(35, [("bob", 10)])]
    assert bids(ex, "homes") == []


# --- Validation -----------------------------------------------------------------------------


def test_price_must_be_a_whole_multiple_of_the_tick(clock):
    ex = Exchange(clock=clock)
    open_market(ex, tick_size=5)

    assert isinstance(ex.place_limit("cars", "alice", BUY, 36, 1)[0], Rejected)
    assert isinstance(ex.place_limit("cars", "alice", BUY, 35.0, 1)[0], Rejected)
    assert isinstance(ex.place_limit("cars", "alice", BUY, 35, 1)[0], OrderAccepted)


@pytest.mark.parametrize("price", [0, -5, -100])
def test_zero_and_negative_prices_are_allowed(clock, price):
    ex = Exchange(clock=clock)
    open_market(ex, tick_size=5)

    assert isinstance(ex.place_limit("cars", "alice", BUY, price, 1)[0], OrderAccepted)


@pytest.mark.parametrize("size", [0, -1, 2.5, True])
def test_size_must_be_a_whole_number_of_at_least_one(ex, size):
    assert [type(event) for event in ex.place_limit("cars", "alice", BUY, 36, size)] == [Rejected]


def test_orders_are_rejected_before_the_market_opens(clock):
    ex = Exchange(clock=clock)
    ex.create_market(make_config())

    (event,) = ex.place_limit("cars", "alice", BUY, 36, 10)

    assert isinstance(event, Rejected)
    assert "not open" in event.reason


def test_orders_for_an_unknown_market_are_rejected(ex):
    assert [type(event) for event in ex.place_limit("nope", "alice", BUY, 36, 10)] == [Rejected]


# --- Two-sided quotes -----------------------------------------------------------------------


def test_quote_adds_a_bid_and_an_offer_and_keeps_existing_orders(ex):
    ex.place_limit("cars", "bob", BUY, 30, 1)

    ex.place_quote("cars", "bob", 35, 38, 100)  # "35 at 38, 100 up"

    assert bids(ex) == [(35, [("bob", 100)]), (30, [("bob", 1)])]
    assert asks(ex) == [(38, [("bob", 100)])]


@pytest.mark.parametrize("bid, ask", [(38, 37), (37, 37)])
def test_quote_is_rejected_unless_bid_is_below_offer(ex, bid, ask):
    assert [type(event) for event in ex.place_quote("cars", "bob", bid, ask, 5)] == [Rejected]
    assert bids(ex) == [] and asks(ex) == []


def test_quote_side_that_crosses_trades_immediately(ex):
    ex.place_limit("cars", "alice", SELL, 37, 10)

    events = ex.place_quote("cars", "bob", 38, 40, 5)

    fills = [(trade.seller_id, trade.price, trade.size) for trade in trades_in(events)]
    assert fills == [("alice", 37, 5)]
    assert bids(ex) == []
    assert asks(ex) == [(37, [("alice", 5)]), (40, [("bob", 5)])]


# --- Self-trade -----------------------------------------------------------------------------


def test_limit_that_would_trade_with_own_order_is_rejected_entirely(ex):
    ex.place_limit("cars", "alice", SELL, 37, 5)
    ex.place_limit("cars", "bob", SELL, 37, 5)

    events = ex.place_limit("cars", "alice", BUY, 37, 10)

    assert [type(event) for event in events] == [Rejected]
    assert events[0].reason == "would trade with your own order"
    assert ex.trades("cars") == []
    assert bids(ex) == []
    assert asks(ex) == [(37, [("alice", 5), ("bob", 5)])]


def test_take_that_would_trade_with_own_order_is_rejected(ex):
    ex.place_limit("cars", "alice", BUY, 36, 5)

    events = ex.take("cars", "alice", SELL, 36, 1)

    assert [type(event) for event in events] == [Rejected]
    assert bids(ex) == [(36, [("alice", 5)])]


def test_rejected_even_if_others_would_fill_first(ex):
    # Bob's 36 offer fills first, but the rest of the order would reach Alice's own 37.
    ex.place_limit("cars", "bob", SELL, 36, 5)
    ex.place_limit("cars", "alice", SELL, 37, 5)

    events = ex.place_limit("cars", "alice", BUY, 37, 10)

    assert [type(event) for event in events] == [Rejected]
    assert ex.trades("cars") == []
    assert asks(ex) == [(36, [("bob", 5)]), (37, [("alice", 5)])]


def test_order_that_fills_before_reaching_own_order_is_allowed(ex):
    ex.place_limit("cars", "bob", SELL, 36, 5)
    ex.place_limit("cars", "alice", SELL, 37, 5)

    events = ex.place_limit("cars", "alice", BUY, 37, 5)  # all 5 fill from bob at 36

    assert [(t.seller_id, t.price, t.size) for t in trades_in(events)] == [("bob", 36, 5)]
    assert asks(ex) == [(37, [("alice", 5)])]


def test_own_order_behind_the_limit_price_does_not_matter(ex):
    ex.place_limit("cars", "alice", SELL, 40, 5)
    ex.place_limit("cars", "bob", SELL, 36, 5)

    events = ex.place_limit("cars", "alice", BUY, 38, 8)  # 5 from bob; 3 rest at 38

    assert [(t.seller_id, t.size) for t in trades_in(events)] == [("bob", 5)]
    assert bids(ex) == [(38, [("alice", 3)])]
    assert asks(ex) == [(40, [("alice", 5)])]


def test_quote_side_that_would_trade_with_own_order_is_rejected_on_its_own(ex):
    ex.place_limit("cars", "alice", SELL, 36, 5)

    events = ex.place_quote("cars", "alice", 36, 40, 10)  # the 36 bid would hit her 36 offer

    assert [type(event) for event in events] == [Rejected, OrderAccepted, OrderRested]
    assert bids(ex) == []
    assert asks(ex) == [(36, [("alice", 5)]), (40, [("alice", 10)])]


# --- Admin lifecycle ------------------------------------------------------------------------


def test_creating_a_duplicate_market_raises(ex):
    with pytest.raises(ValueError):
        ex.create_market(make_config())


def test_opening_an_open_market_raises(ex):
    with pytest.raises(ValueError):
        ex.open_market("cars")
