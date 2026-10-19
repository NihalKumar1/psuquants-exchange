"""Amending a resting order (SPEC Q133-Q138).

Shrinking an order at the same price keeps its place in line. Any other change (a new price,
or a bigger size) cancels it and places a new limit order, which joins the back of the line.
"""

from exchange.engine import (
    Exchange,
    OrderAccepted,
    OrderCancelled,
    OrderReduced,
    Rejected,
    Side,
    from_dict,
    to_dict,
)
from helpers import accepted_id, asks, bids, open_market, trades_in

BUY, SELL = Side.BUY, Side.SELL


def rejection(events):
    (event,) = events
    assert isinstance(event, Rejected), events
    return event.reason


# --- Shrinking at the same price keeps priority ----------------------------------------------


def test_shrinking_an_order_keeps_its_place_in_line(ex):
    first = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 20))
    ex.place_limit("cars", "bob", BUY, 36, 15)

    events = ex.amend("cars", "alice", first, 36, 5)

    assert [type(event) for event in events] == [OrderReduced]
    assert bids(ex) == [(36, [("alice", 5), ("bob", 15)])]


def test_a_shrunk_order_still_fills_first(ex):
    first = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 20))
    ex.place_limit("cars", "bob", BUY, 36, 15)
    ex.amend("cars", "alice", first, 36, 5)

    trades = trades_in(ex.take("cars", "carol", SELL, 36, 8))

    assert [(trade.buyer_id, trade.size) for trade in trades] == [("alice", 5), ("bob", 3)]


def test_shrinking_a_partly_filled_order_sets_what_is_left(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 20))
    ex.take("cars", "bob", SELL, 36, 5)  # 15 left

    ex.amend("cars", "alice", order_id, 36, 10)

    order = ex.markets["cars"].orders[order_id]
    assert (order.size, order.remaining) == (15, 10)  # 5 filled + 10 still resting
    assert bids(ex) == [(36, [("alice", 10)])]


def test_shrinking_works_while_halted(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 20))
    ex.halt_market("cars")

    events = ex.amend("cars", "alice", order_id, 36, 5)

    assert [type(event) for event in events] == [OrderReduced]


# --- A new price or a bigger size goes to the back of the line ---------------------------------


def test_a_bigger_size_goes_to_the_back_of_the_line(ex):
    first = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))
    ex.place_limit("cars", "bob", BUY, 36, 15)

    events = ex.amend("cars", "alice", first, 36, 20)

    assert isinstance(events[0], OrderCancelled)
    assert (events[0].order_id, events[0].reason) == (first, "amended")
    replacement = next(event for event in events if isinstance(event, OrderAccepted))
    assert replacement.replaces == first
    assert replacement.order_id != first
    assert bids(ex) == [(36, [("bob", 15), ("alice", 20)])]


def test_a_new_price_goes_to_the_back_of_the_line_there(ex):
    ex.place_limit("cars", "bob", BUY, 37, 15)
    first = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))

    ex.amend("cars", "alice", first, 37, 10)

    assert bids(ex) == [(37, [("bob", 15), ("alice", 10)])]


def test_shrinking_and_moving_the_price_also_goes_to_the_back(ex):
    first = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))

    events = ex.amend("cars", "alice", first, 35, 5)

    assert isinstance(events[0], OrderCancelled)
    assert bids(ex) == [(35, [("alice", 5)])]


def test_an_amend_that_crosses_the_book_trades_like_a_new_order(ex):
    ex.place_limit("cars", "bob", SELL, 40, 4)
    first = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))

    events = ex.amend("cars", "alice", first, 40, 10)

    trades = trades_in(events)
    assert [(trade.buyer_id, trade.seller_id, trade.price, trade.size) for trade in trades] == [
        ("alice", "bob", 40, 4)
    ]
    assert asks(ex) == []
    assert bids(ex) == [(40, [("alice", 6)])]


def test_the_old_orders_size_is_freed_when_checking_max_position():
    exchange = Exchange()
    open_market(exchange, max_position=10)
    order_id = accepted_id(exchange.place_limit("cars", "alice", BUY, 36, 10))

    # Without freeing the old 10, there would be no room at all.
    exchange.amend("cars", "alice", order_id, 37, 10)

    assert bids(exchange) == [(37, [("alice", 10)])]


def test_a_bigger_size_is_clipped_to_max_position_like_a_new_order():
    exchange = Exchange()
    open_market(exchange, max_position=10)
    order_id = accepted_id(exchange.place_limit("cars", "alice", BUY, 36, 4))

    events = exchange.amend("cars", "alice", order_id, 36, 25)

    replacement = next(event for event in events if isinstance(event, OrderAccepted))
    assert (replacement.requested_size, replacement.size) == (25, 10)


# --- Rejections leave the original order alone -------------------------------------------------


def test_moving_the_price_while_halted_is_rejected_and_the_order_stays(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))
    ex.halt_market("cars")

    reason = rejection(ex.amend("cars", "alice", order_id, 37, 10))

    assert reason == "market is not open"
    assert bids(ex) == [(36, [("alice", 10)])]


def test_an_amend_that_would_trade_with_your_own_order_is_rejected(ex):
    ex.place_limit("cars", "alice", SELL, 40, 5)
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))

    reason = rejection(ex.amend("cars", "alice", order_id, 40, 10))

    assert reason == "would trade with your own order"
    assert bids(ex) == [(36, [("alice", 10)])]


def test_an_off_tick_price_is_rejected():
    exchange = Exchange()
    open_market(exchange, tick_size=5)
    order_id = accepted_id(exchange.place_limit("cars", "alice", BUY, 35, 10))

    reason = rejection(exchange.amend("cars", "alice", order_id, 37, 10))

    assert reason == "price must be a multiple of the tick size (5)"


def test_the_rejection_names_the_amend(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))

    (event,) = ex.amend("cars", "alice", order_id, 36, 10)

    assert event.command == f"amend {order_id} to 10 @ 36"
    assert event.reason == "nothing to change"


def test_size_must_be_at_least_1(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))

    reason = rejection(ex.amend("cars", "alice", order_id, 36, 0))

    assert reason == "size must be a whole number of at least 1 (use cancel to remove the order)"


def test_only_the_owner_can_amend(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))

    assert rejection(ex.amend("cars", "bob", order_id, 36, 5)) == "not your order"


def test_an_order_that_is_no_longer_resting_cant_be_amended(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))
    ex.cancel("cars", "alice", order_id)

    assert rejection(ex.amend("cars", "alice", order_id, 36, 5)) == "no such resting order"


def test_a_settled_markets_orders_cant_be_amended(ex):
    order_id = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 10))
    ex.settle_market("cars", 40)

    assert rejection(ex.amend("cars", "alice", order_id, 36, 5)) == "market is settled"


def test_a_kicked_trader_cant_amend(ex):
    alice = ex.join("Alice")[0].trader_id
    order_id = accepted_id(ex.place_limit("cars", alice, BUY, 36, 10))
    ex.kick(alice)

    assert rejection(ex.amend("cars", alice, order_id, 36, 5)) == "you were removed from the game"


def test_unknown_market(ex):
    assert rejection(ex.amend("nope", "alice", 1, 36, 5)) == "unknown market"


# --- The log -------------------------------------------------------------------------------------


def test_amends_replay_to_the_same_book(ex):
    first = accepted_id(ex.place_limit("cars", "alice", BUY, 36, 20))
    second = accepted_id(ex.place_limit("cars", "bob", BUY, 36, 15))
    ex.amend("cars", "alice", first, 36, 5)
    ex.amend("cars", "bob", second, 37, 15)

    saved = [to_dict(event) for event in ex.events]
    replayed = Exchange.replay([from_dict(data) for data in saved])

    assert bids(replayed) == bids(ex) == [(37, [("bob", 15)]), (36, [("alice", 5)])]
    assert replayed.next_order_id == ex.next_order_id
