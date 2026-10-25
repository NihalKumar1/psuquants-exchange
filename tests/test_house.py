"""The house market maker in a delta-hedging round (SPEC Q163, Q172-Q173, Q186-Q190).

The house keeps one bid and one offer of the round's house size in every market of a running
round, around fair (the fair price for the underlying, Black-Scholes at the implied vol for an
option). It re-quotes after every move, after every fill against it, and on resume. Its spread
widens after a big move and when it carries a big position, and it shades both prices away
from its position.
"""

import math

import pytest

from exchange.engine import Exchange, Rejected, Side, TradeExecuted, from_dict, to_dict
from exchange.engine.house import HOUSE_ID, house_quote
from helpers import FakeClock
from test_delta_round import ScriptedNormal, make_round, run_to_expiry

BUY, SELL = Side.BUY, Side.SELL


def started(rng=None, **changes):
    exchange = Exchange(clock=FakeClock(), rng=rng)
    exchange.create_round(make_round(**changes))
    exchange.start_round("SPX")
    return exchange


def house_orders(ex, market_id):
    """The house's resting orders as {"buy": (price, size), "sell": (price, size)}."""
    found = {}
    for order in ex.book(market_id).all_orders():
        if order.trader_id == HOUSE_ID:
            assert order.side.value not in found, "the house should have one order per side"
            found[order.side.value] = (order.price, order.remaining)
    return found


# --- Quoting -----------------------------------------------------------------------------------


def test_the_house_quotes_every_market_when_the_round_starts():
    ex = started()
    assert house_orders(ex, "SPX") == {"buy": (99, 10), "sell": (101, 10)}
    # the 100 call is worth about 7.97: 6.97 rounds down to 6, 8.97 rounds up to 9
    assert house_orders(ex, "SPX-C-100") == {"buy": (6, 10), "sell": (9, 10)}
    put = ex.rounds["SPX"].greeks("SPX-P-95").value
    assert house_orders(ex, "SPX-P-95") == {"buy": (math.floor(put - 1), 10),
                                           "sell": (math.ceil(put + 1), 10)}


def test_the_house_is_not_a_student():
    ex = started(rng=ScriptedNormal(0.0))
    ex.take("SPX", "alice", BUY, 101, 3)
    ex.step_round("SPX")
    assert HOUSE_ID not in ex.traders
    assert HOUSE_ID not in ex.rounds["SPX"].traders()
    assert ex.position("SPX", HOUSE_ID) == -3


def test_after_each_move_the_house_requotes_around_the_new_fair_price():
    ex = started(rng=ScriptedNormal(0.5, -0.8))
    ex.step_round("SPX")
    fair = ex.rounds["SPX"].fair_price
    assert house_orders(ex, "SPX") == {"buy": (math.floor(fair - 1), 10),
                                      "sell": (math.ceil(fair + 1), 10)}
    call = ex.rounds["SPX"].greeks("SPX-C-100").value
    assert house_orders(ex, "SPX-C-100") == {"buy": (math.floor(call - 1), 10),
                                            "sell": (math.ceil(call + 1), 10)}
    ex.step_round("SPX")
    fair = ex.rounds["SPX"].fair_price
    assert house_orders(ex, "SPX")["buy"] == (math.floor(fair - 1), 10)


def test_the_house_never_rejects_its_own_quotes():
    ex = started(rng=ScriptedNormal(*[0.3, -0.3] * 20))
    for _ in range(40):
        ex.step_round("SPX")
    assert not any(isinstance(event, Rejected) for event in ex.events)


def test_the_house_has_no_max_position():
    ex = started(max_position=5, house_size=10)
    assert house_orders(ex, "SPX")["sell"] == (101, 10)
    events = ex.take("SPX", "alice", BUY, 101, 10)
    assert sum(t.size for t in events if isinstance(t, TradeExecuted)) == 5  # alice's own limit
    for buyer in ("b", "c", "d", "e", "f"):  # the house keeps selling well past 5
        ex.take("SPX", buyer, BUY, ex.book("SPX").best_ask(), 5)
    assert ex.position("SPX", HOUSE_ID) == -30


def test_the_house_doesnt_bid_for_an_option_worth_next_to_nothing():
    ex = started(options=(("call", 200),))
    # worth about 0.0001: no bid, and fair + 1 rounds up to an offer of 2
    assert house_orders(ex, "SPX-C-200") == {"sell": (2, 10)}


# --- Re-quoting after a fill, widening and skew -----------------------------------------------


def test_a_fill_against_the_house_makes_it_requote_at_once():
    ex = started()
    events = ex.take("SPX", "alice", BUY, 101, 10)  # takes the whole offer
    # house short 10 of size 10: half-spread 1 x (1 + 10/40) = 1.25
    assert house_orders(ex, "SPX") == {"buy": (98, 10), "sell": (102, 10)}
    assert any(event.trader_id == HOUSE_ID for event in events
               if type(event).__name__ == "OrderAccepted")  # returned with alice's events


def test_a_partial_fill_also_requotes():
    ex = started()
    ex.take("SPX", "alice", BUY, 101, 4)
    assert house_orders(ex, "SPX")["sell"][1] == 10  # back to full size


def test_a_big_position_widens_and_skews_the_house():
    ex = started(house_size=10)
    for buyer in ("a", "b", "c", "d"):  # house sells 40 = 4 quote sizes
        ex.take("SPX", buyer, BUY, ex.book("SPX").best_ask(), 10)
    assert ex.position("SPX", HOUSE_ID) == -40
    # half-spread 1 x (1 + 40/40) = 2, then shaded up 1 tick because the house is short
    assert house_orders(ex, "SPX") == {"buy": (99, 10), "sell": (103, 10)}


def test_a_move_bigger_than_usual_widens_the_house_for_that_second():
    ex = started(rng=ScriptedNormal(3.0, 0.5))
    ex.step_round("SPX")  # a 3-standard-deviation move: 3x the half-spread
    fair = ex.rounds["SPX"].fair_price
    assert house_orders(ex, "SPX") == {"buy": (math.floor(fair - 3), 10),
                                      "sell": (math.ceil(fair + 3), 10)}
    ex.step_round("SPX")  # an ordinary move: back to the base half-spread
    fair = ex.rounds["SPX"].fair_price
    assert house_orders(ex, "SPX") == {"buy": (math.floor(fair - 1), 10),
                                      "sell": (math.ceil(fair + 1), 10)}


@pytest.mark.parametrize("fair, position, speed, expected", [
    (100, 0, 0, (99, 101)),        # quiet and flat: the base half-spread
    (100, 0, 0.9, (99, 101)),      # an ordinary move doesn't widen
    (100, 0, 2, (98, 102)),        # a 2x move doubles it
    (100, 40, 0, (97, 101)),       # long 4 sizes: doubled, then shaded down a tick
    (100, -80, 0, (99, 105)),     # short 8 sizes: tripled, then shaded up 2 ticks
    (100.4, 0, 0, (99, 102)),      # rounded out to the tick
])
def test_the_widening_and_skew_formula(fair, position, speed, expected):
    bid, ask = house_quote(fair, half_spread=1, size=10, tick=1, position=position, speed=speed)
    assert (bid, ask) == expected


def test_quotes_round_out_to_a_bigger_tick():
    assert house_quote(1003, half_spread=5, size=10, tick=5, position=0, speed=0) == (995, 1010)


def test_no_bid_at_zero_or_below():
    assert house_quote(0.4, half_spread=1, size=10, tick=1, position=0, speed=0) == (None, 2)


# --- The house picks off stale orders ----------------------------------------------------------


def test_the_new_quote_trades_with_a_students_stale_order():
    ex = started(rng=ScriptedNormal(3.0))
    ex.place_limit("SPX", "alice", SELL, 100, 2)  # a fine offer before the move
    ex.step_round("SPX")                          # up about 8%: the house bid is now above it
    assert ex.position("SPX", "alice") == -2
    assert ex.position("SPX", HOUSE_ID) == 2
    assert ex.trades("SPX")[-1].price == 100      # at alice's resting price


# --- Halts and expiry --------------------------------------------------------------------------


def test_while_halted_the_house_leaves_its_quotes_and_requotes_on_resume():
    ex = started(rng=ScriptedNormal(0.0))
    ex.halt_round("SPX")
    assert house_orders(ex, "SPX") == {"buy": (99, 10), "sell": (101, 10)}
    ex.resume_round("SPX")
    assert house_orders(ex, "SPX") == {"buy": (99, 10), "sell": (101, 10)}
    resumed_at = ex.events.index(next(e for e in ex.events
                                      if type(e).__name__ == "RoundResumed"))
    assert any(type(e).__name__ == "OrderAccepted" for e in ex.events[resumed_at:])


def test_the_house_stops_quoting_once_the_round_settles():
    draws = [0.1] * 60
    ex = started(rng=ScriptedNormal(*draws))
    run_to_expiry(ex, draws)
    last = ex.events[-1]
    assert type(last).__name__ == "MarketSettled"


# --- Replay ------------------------------------------------------------------------------------


def test_replay_rebuilds_the_houses_orders_and_position():
    draws = [0.4, -0.2, 3.0, 0.0]
    ex = started(rng=ScriptedNormal(*draws))
    ex.place_limit("SPX", "alice", SELL, 100, 2)
    ex.step_round("SPX")
    ex.take("SPX-C-100", "bob", BUY, ex.book("SPX-C-100").best_ask(), 7)
    ex.step_round("SPX")
    ex.step_round("SPX")
    ex.halt_round("SPX")
    ex.resume_round("SPX")
    ex.step_round("SPX")

    rebuilt = Exchange.replay([from_dict(to_dict(event)) for event in ex.events])
    for market_id in ex.markets:
        assert house_orders(rebuilt, market_id) == house_orders(ex, market_id)
        assert rebuilt.position(market_id, HOUSE_ID) == ex.position(market_id, HOUSE_ID)
