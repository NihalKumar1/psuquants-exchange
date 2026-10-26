"""Price limits (SPEC Q204-Q208): an optional min and max price per market, so nobody can put
an absurd number in the book. Every order (bid, offer, quote, click, amend) and the market
maker's Trade or Tighten quote must be inside the range; the settlement value may be anything."""

from dataclasses import replace

import pytest

from exchange.engine import Exchange, Rejected, Side, from_dict, to_dict
from helpers import accepted_id, bids, make_config, trades_in

BUY, SELL = Side.BUY, Side.SELL


def limited(min_price=0, max_price=100, **config):
    ex = Exchange()
    ex.create_market(replace(make_config(**config), min_price=min_price, max_price=max_price))
    ex.open_market("cars")
    return ex


def reason(events):
    (event,) = events
    assert isinstance(event, Rejected)
    return event.reason


def test_markets_have_no_limits_unless_set(ex):
    config = ex.markets["cars"].config
    assert (config.min_price, config.max_price) == (None, None)
    assert not isinstance(ex.place_limit("cars", "alice", BUY, 10**9, 1)[0], Rejected)


@pytest.mark.parametrize("price", [0, 37, 100])
def test_prices_inside_the_range_are_fine(price):
    ex = limited()
    assert not isinstance(ex.place_limit("cars", "alice", BUY, price, 1)[0], Rejected)


@pytest.mark.parametrize("side, price", [(BUY, -1), (BUY, 101), (SELL, 1000), (SELL, -50)])
def test_prices_outside_the_range_are_rejected(side, price):
    ex = limited()
    assert reason(ex.place_limit("cars", "alice", side, price, 1)) == \
        "price must be between 0 and 100"


def test_only_a_min_or_only_a_max():
    ex = limited(min_price=30, max_price=None)
    assert reason(ex.place_limit("cars", "alice", BUY, 29, 1)) == "price must be at least 30"
    assert not isinstance(ex.place_limit("cars", "alice", BUY, 10**6, 1)[0], Rejected)

    ex = limited(min_price=None, max_price=180)
    assert reason(ex.place_limit("cars", "alice", SELL, 181, 1)) == "price must be at most 180"


def test_quotes_and_amends_are_checked_too():
    ex = limited()
    quote = ex.place_quote("cars", "alice", 90, 120, 5)
    assert [e.reason for e in quote if isinstance(e, Rejected)] == \
        ["price must be between 0 and 100"]  # the bid rests, the offer is refused

    order_id = accepted_id(ex.place_limit("cars", "bob", BUY, 50, 5))
    assert reason(ex.amend("cars", "bob", order_id, 150, 5)) == "price must be between 0 and 100"
    assert bids(ex)[1] == (50, [("bob", 5)])  # the order stays as it was


def test_a_take_in_range_still_trades():
    ex = limited()
    ex.place_limit("cars", "alice", SELL, 60, 5)
    assert trades_in(ex.take("cars", "bob", BUY, 60, 5))


def test_the_market_makers_quote_must_be_in_range():
    ex = Exchange()
    ex.create_market(replace(make_config(), min_price=0, max_price=100))
    ex.join("Alice")
    ex.start_auction("cars")
    ex.submit_width("cars", "t1", 10)
    ex.close_auction("cars")

    (rejected,) = ex.submit_mm_quote("cars", "t1", 95, 105)
    assert rejected.reason == "prices must be between 0 and 100"
    assert not isinstance(ex.submit_mm_quote("cars", "t1", 90, 100)[0], Rejected)


def test_settlement_can_be_outside_the_range():
    ex = limited()
    ex.settle_market("cars", 250)
    assert ex.markets["cars"].settlement_value == 250


@pytest.mark.parametrize("min_price, max_price, wanted", [
    (100, 100, "min price must be below max price"),
    (100, 0, "min price must be below max price"),
    (1.5, None, "min price must be a whole number"),
    (None, "lots", "max price must be a whole number"),
])
def test_bad_limits_are_refused(min_price, max_price, wanted):
    with pytest.raises(ValueError, match=wanted):
        Exchange().create_market(replace(make_config(), min_price=min_price, max_price=max_price))


def test_limits_can_be_edited_before_the_market_opens_and_survive_replay():
    ex = Exchange()
    ex.create_market(make_config())
    ex.edit_market(replace(make_config(), min_price=30, max_price=180))
    assert (ex.markets["cars"].config.min_price, ex.markets["cars"].config.max_price) == (30, 180)

    copy = Exchange.replay([from_dict(to_dict(event)) for event in ex.events])
    assert copy.markets["cars"].config.max_price == 180
