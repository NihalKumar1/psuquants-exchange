"""Trade or Tighten in the engine: width auction, MM quote, forced-trade window, and kicks.

The market in these tests has tick 5, max position 1000, forced-trade size 10 and a 30 s timer.
"""

import json
from dataclasses import replace

import pytest

from exchange.engine import (
    Exchange,
    ForcedTradeEnded,
    MarketMakerChosen,
    MarketStatus,
    Rejected,
    Side,
    SideAssigned,
    TradeOrTightenCancelled,
    from_dict,
    to_dict,
)
from helpers import ScriptedCoin, asks, bids, make_config, trades_in

BUY, SELL = Side.BUY, Side.SELL
TICK = 5


def new_exchange(clock, *coin_flips, max_position=1000):
    """An exchange with one CREATED market, "cars". `coin_flips` are the random sides, in order."""
    ex = Exchange(clock=clock, rng=ScriptedCoin(*coin_flips))
    ex.create_market(make_config(tick_size=TICK, max_position=max_position))
    return ex


def join_all(ex, *names):
    return [ex.join(name)[0].trader_id for name in names]


def market(ex):
    return ex.markets["cars"]


def reason(events):
    """The reason of the single Rejected event a command returned."""
    (event,) = events
    assert isinstance(event, Rejected), event
    return event.reason


def run_to_mm_quoting(ex, mm, width=10):
    ex.start_auction("cars")
    ex.submit_width("cars", mm, width)
    ex.close_auction("cars")


def run_to_forced_trade(ex, mm, width=10, bid=100, ask=110):
    run_to_mm_quoting(ex, mm, width)
    ex.submit_mm_quote("cars", mm, bid, ask)
    ex.start_forced_trade("cars")


def fills(events):
    """Each trade as (buyer, seller, price, size), in print order."""
    return [(t.buyer_id, t.seller_id, t.price, t.size) for t in trades_in(events)]


# --- Starting -------------------------------------------------------------------------------


def test_the_auction_starts_only_from_created(clock):
    ex = new_exchange(clock)

    ex.start_auction("cars")

    assert market(ex).status is MarketStatus.AUCTION
    with pytest.raises(ValueError, match="cannot start the auction"):
        ex.start_auction("cars")
    with pytest.raises(ValueError, match="cannot open"):
        ex.open_market("cars")  # no skipping out once Trade or Tighten has started


def test_opening_directly_is_still_possible(clock):
    ex = new_exchange(clock)

    ex.open_market("cars")

    assert market(ex).status is MarketStatus.OPEN


def test_during_trade_or_tighten_orders_halts_and_settles_are_refused(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    ex.start_auction("cars")

    assert "not open" in reason(ex.place_limit("cars", alice, BUY, 100, 1))
    with pytest.raises(ValueError, match="cannot halt"):
        ex.halt_market("cars")
    with pytest.raises(ValueError, match="cannot settle"):
        ex.settle_market("cars", 100)


# --- Widths ---------------------------------------------------------------------------------


def test_the_narrowest_width_is_best(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.start_auction("cars")
    assert market(ex).best_width() is None

    ex.submit_width("cars", alice, 50)
    ex.submit_width("cars", bob, 20)

    assert market(ex).best_width() == (bob, 20)


def test_a_width_must_be_strictly_narrower_than_the_best(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.start_auction("cars")
    ex.submit_width("cars", alice, 20)

    assert "narrower than the best width (20)" in reason(ex.submit_width("cars", bob, 20))
    assert "narrower" in reason(ex.submit_width("cars", bob, 25))
    ex.submit_width("cars", alice, 15)  # the holder may tighten their own width

    assert market(ex).best_width() == (alice, 15)


@pytest.mark.parametrize("width, why", [
    (7, "multiple of the tick size (5)"),
    (-5, "can't be negative"),
    (2.5, "whole number"),
    ("10", "whole number"),
])
def test_bad_widths_are_rejected(clock, width, why):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    ex.start_auction("cars")

    assert why in reason(ex.submit_width("cars", alice, width))
    assert market(ex).best_width() is None


def test_a_width_of_zero_is_allowed(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    ex.start_auction("cars")

    ex.submit_width("cars", alice, 0)

    assert market(ex).best_width() == (alice, 0)


def test_widths_are_taken_only_while_the_auction_is_open(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")

    assert "auction is not open" in reason(ex.submit_width("cars", alice, 10))
    run_to_mm_quoting(ex, alice, width=10)
    assert "auction is not open" in reason(ex.submit_width("cars", bob, 5))


def test_kicked_traders_and_unknown_markets_are_rejected(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.start_auction("cars")
    ex.kick(bob)

    assert "removed" in reason(ex.submit_width("cars", bob, 10))
    assert "unknown market" in reason(ex.submit_width("nope", alice, 10))


# --- Closing the auction --------------------------------------------------------------------


def test_closing_with_no_widths_is_refused(clock):
    ex = new_exchange(clock)
    ex.start_auction("cars")

    with pytest.raises(ValueError, match="no widths"):
        ex.close_auction("cars")
    assert market(ex).status is MarketStatus.AUCTION


def test_closing_makes_the_narrowest_width_the_market_maker(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.start_auction("cars")
    ex.submit_width("cars", alice, 50)
    ex.submit_width("cars", bob, 20)

    ex.close_auction("cars")

    assert market(ex).status is MarketStatus.MM_QUOTING
    assert (market(ex).mm_id, market(ex).mm_width) == (bob, 20)


def test_close_works_only_during_the_auction(clock):
    ex = new_exchange(clock)

    with pytest.raises(ValueError, match="cannot close the auction"):
        ex.close_auction("cars")


# --- The market maker's quote ---------------------------------------------------------------


def test_only_the_market_maker_can_quote(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    run_to_mm_quoting(ex, alice)

    assert "only the market maker" in reason(ex.submit_mm_quote("cars", bob, 100, 110))


@pytest.mark.parametrize("bid, ask, why", [
    (101, 111, "multiples of the tick size (5)"),
    (100, 111, "multiples of the tick size (5)"),
    (110, 100, "bid can't be above the offer"),
    (100, 115, "wider than your width (10)"),
    (100.0, 110, "multiples of the tick size (5)"),
])
def test_bad_quotes_are_rejected(clock, bid, ask, why):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    run_to_mm_quoting(ex, alice, width=10)

    assert why in reason(ex.submit_mm_quote("cars", alice, bid, ask))
    assert market(ex).mm_bid is None


@pytest.mark.parametrize("bid, ask", [(100, 110), (100, 105), (100, 100)])
def test_a_quote_at_or_inside_the_width_is_accepted(clock, bid, ask):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    run_to_mm_quoting(ex, alice, width=10)

    ex.submit_mm_quote("cars", alice, bid, ask)

    assert (market(ex).mm_bid, market(ex).mm_ask) == (bid, ask)


def test_a_width_of_zero_means_bid_equals_offer(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    run_to_mm_quoting(ex, alice, width=0)

    assert "wider than your width (0)" in reason(ex.submit_mm_quote("cars", alice, 100, 105))
    ex.submit_mm_quote("cars", alice, -100, -100)  # negative prices are fine (Q18)
    assert (market(ex).mm_bid, market(ex).mm_ask) == (-100, -100)


def test_the_first_quote_is_final(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    run_to_mm_quoting(ex, alice)
    ex.submit_mm_quote("cars", alice, 100, 110)

    assert "already quoted" in reason(ex.submit_mm_quote("cars", alice, 105, 110))
    assert (market(ex).mm_bid, market(ex).mm_ask) == (100, 110)


def test_quotes_are_taken_only_while_the_market_maker_is_quoting(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    ex.start_auction("cars")

    assert "isn't quoting now" in reason(ex.submit_mm_quote("cars", alice, 100, 110))


def test_the_forced_trade_can_start_only_after_the_quote(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    with pytest.raises(ValueError, match="cannot start the forced trade"):
        ex.start_forced_trade("cars")
    run_to_mm_quoting(ex, alice)

    with pytest.raises(ValueError, match="hasn't quoted yet"):
        ex.start_forced_trade("cars")
    ex.submit_mm_quote("cars", alice, 100, 110)
    ex.start_forced_trade("cars")

    assert market(ex).status is MarketStatus.FORCED_TRADE


# --- Choosing a side ------------------------------------------------------------------------


def test_sides_are_chosen_only_during_the_window(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    run_to_mm_quoting(ex, alice)

    assert "isn't running" in reason(ex.choose_side("cars", bob, BUY))


def test_the_market_maker_does_not_choose(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    run_to_forced_trade(ex, alice)

    assert "market maker" in reason(ex.choose_side("cars", alice, BUY))


def test_a_choice_can_be_changed_and_nothing_trades_until_close(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    run_to_forced_trade(ex, alice)

    ex.choose_side("cars", bob, BUY)
    ex.choose_side("cars", bob, SELL)

    assert market(ex).choices == {bob: SELL}
    assert ex.trades("cars") == []
    assert ex.position("cars", bob) == 0


def test_a_kicked_trader_cannot_choose(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    run_to_forced_trade(ex, alice)
    ex.kick(bob)

    assert "removed" in reason(ex.choose_side("cars", bob, BUY))


# --- Closing the window ---------------------------------------------------------------------


def test_forced_trades_fill_at_the_mm_quote_for_the_forced_size(clock):
    ex = new_exchange(clock)
    alice, bob, carol, dave = join_all(ex, "Alice", "Bob", "Carol", "Dave")
    run_to_forced_trade(ex, alice, width=10, bid=100, ask=110)
    ex.choose_side("cars", bob, BUY)
    ex.choose_side("cars", carol, SELL)
    ex.choose_side("cars", dave, BUY)

    events = ex.end_forced_trade("cars", ended_by="timer")

    # A buyer pays the MM's offer; a seller gets the MM's bid.
    assert fills(events) == [(bob, alice, 110, 10), (alice, carol, 100, 10), (dave, alice, 110, 10)]
    trade = trades_in(events)[0]
    assert trade.forced is True
    assert trade.aggressor_side is BUY
    assert (trade.buy_order_id, trade.sell_order_id) == (None, None)
    # The MM carries the net position like everyone else.
    positions = {t: ex.position("cars", t) for t in (alice, bob, carol, dave)}
    assert positions == {alice: -10, bob: 10, carol: -10, dave: 10}


def test_after_the_window_the_market_opens_with_an_empty_book(clock):
    ex = new_exchange(clock)
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    run_to_forced_trade(ex, alice, bid=100, ask=110)
    ex.choose_side("cars", carol, BUY)
    ex.choose_side("cars", bob, SELL)

    ex.end_forced_trade("cars", ended_by="timer")

    assert market(ex).status is MarketStatus.OPEN
    assert bids(ex) == [] and asks(ex) == []
    assert ex.mark("cars") == 100  # the last forced trade (Bob selling at the MM's bid)
    ex.place_limit("cars", carol, SELL, 120, 1)  # continuous trading works
    assert asks(ex) == [(120, [(carol, 1)])]


def test_print_order_is_final_choice_then_random_sides_in_join_order(clock):
    ex = new_exchange(clock, BUY, SELL)
    alice, bob, carol, dave, erin = join_all(ex, "Alice", "Bob", "Carol", "Dave", "Erin")
    run_to_forced_trade(ex, alice, bid=100, ask=110)
    ex.choose_side("cars", carol, BUY)
    ex.choose_side("cars", bob, SELL)
    ex.choose_side("cars", carol, SELL)  # changing moves Carol behind Bob

    events = ex.end_forced_trade("cars", ended_by="timer")

    assigned = [(e.trader_id, e.side) for e in events if isinstance(e, SideAssigned)]
    assert assigned == [(dave, BUY), (erin, SELL)]
    assert fills(events) == [
        (alice, bob, 100, 10), (alice, carol, 100, 10), (dave, alice, 110, 10),
        (alice, erin, 100, 10),
    ]


def test_late_joiners_are_included_and_kicked_traders_are_not(clock):
    ex = new_exchange(clock, SELL)
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    run_to_forced_trade(ex, alice, bid=100, ask=110)
    ex.choose_side("cars", bob, BUY)
    ex.choose_side("cars", carol, BUY)
    ex.kick(carol)
    (frank,) = join_all(ex, "Frank")  # joins during the window and never chooses

    events = ex.end_forced_trade("cars", ended_by="timer")

    assert fills(events) == [(bob, alice, 110, 10), (alice, frank, 100, 10)]
    assert ex.position("cars", carol) == 0


def test_max_position_is_ignored_for_forced_trades(clock):
    ex = new_exchange(clock, max_position=15)
    alice, bob, carol, dave = join_all(ex, "Alice", "Bob", "Carol", "Dave")
    run_to_forced_trade(ex, alice, bid=100, ask=110)
    for trader in (bob, carol, dave):
        ex.choose_side("cars", trader, BUY)

    ex.end_forced_trade("cars", ended_by="timer")

    assert ex.position("cars", alice) == -30  # twice the limit of 15
    # Afterwards the normal rule applies: Alice can only trade to reduce her short.
    assert "exceed max position" in reason(ex.place_limit("cars", alice, SELL, 200, 1))
    ex.place_limit("cars", alice, BUY, 90, 5)
    assert bids(ex) == [(90, [(alice, 5)])]


def test_the_admin_can_end_the_window_early(clock):
    ex = new_exchange(clock, BUY)
    alice, bob = join_all(ex, "Alice", "Bob")
    run_to_forced_trade(ex, alice)

    events = ex.end_forced_trade("cars", ended_by="admin")

    (ended,) = [e for e in events if isinstance(e, ForcedTradeEnded)]
    assert ended.ended_by == "admin"
    assert market(ex).status is MarketStatus.OPEN
    with pytest.raises(ValueError, match="cannot end the forced trade"):
        ex.end_forced_trade("cars", ended_by="timer")  # e.g. the timer firing afterwards


def test_a_market_maker_alone_opens_with_no_trades(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    run_to_forced_trade(ex, alice)

    ex.end_forced_trade("cars", ended_by="timer")

    assert ex.trades("cars") == []
    assert market(ex).status is MarketStatus.OPEN


def test_seconds_left_counts_down_from_the_timer_length(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    assert ex.forced_trade_seconds_left("cars") is None
    clock.set("09:30:00")
    run_to_forced_trade(ex, alice)

    clock.set("09:30:12")
    assert ex.forced_trade_seconds_left("cars") == 18
    clock.set("09:31:00")
    assert ex.forced_trade_seconds_left("cars") == 0


# --- Kicks during Trade or Tighten ----------------------------------------------------------


def test_kicking_the_best_width_holder_reverts_to_the_next_best(clock):
    ex = new_exchange(clock)
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    ex.start_auction("cars")
    ex.submit_width("cars", alice, 30)
    ex.submit_width("cars", bob, 20)

    ex.kick(bob)

    assert market(ex).best_width() == (alice, 30)
    ex.submit_width("cars", carol, 25)
    assert market(ex).best_width() == (carol, 25)


def test_when_every_width_holder_is_kicked_there_is_no_best(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    ex.start_auction("cars")
    ex.submit_width("cars", alice, 30)

    ex.kick(alice)

    assert market(ex).best_width() is None
    with pytest.raises(ValueError, match="no widths"):
        ex.close_auction("cars")


def test_kicking_the_market_maker_hands_over_to_the_next_best_width(clock):
    ex = new_exchange(clock)
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    ex.start_auction("cars")
    ex.submit_width("cars", alice, 30)
    ex.submit_width("cars", carol, 25)
    ex.submit_width("cars", bob, 20)
    ex.kick(carol)  # kicked earlier, so she is not next in line
    ex.close_auction("cars")
    ex.submit_mm_quote("cars", bob, 100, 120)

    events = ex.kick(bob)

    (chosen,) = [e for e in events if isinstance(e, MarketMakerChosen)]
    assert (chosen.trader_id, chosen.width) == (alice, 30)
    assert market(ex).status is MarketStatus.MM_QUOTING
    assert (market(ex).mm_id, market(ex).mm_width) == (alice, 30)
    assert market(ex).mm_bid is None  # Bob's quote is gone; Alice quotes her own
    ex.submit_mm_quote("cars", alice, 100, 130)
    assert (market(ex).mm_bid, market(ex).mm_ask) == (100, 130)


def test_kicking_the_only_width_holder_returns_the_market_to_created(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    run_to_mm_quoting(ex, alice)

    events = ex.kick(alice)

    assert any(isinstance(e, TradeOrTightenCancelled) for e in events)
    assert market(ex).status is MarketStatus.CREATED
    assert (market(ex).mm_id, market(ex).best_width()) == (None, None)
    ex.start_auction("cars")  # the admin may start again...
    assert market(ex).best_width() is None


def test_after_returning_to_created_the_market_can_open_directly(clock):
    ex = new_exchange(clock)
    (alice,) = join_all(ex, "Alice")
    run_to_mm_quoting(ex, alice)
    ex.kick(alice)

    ex.open_market("cars")  # ...or skip Trade or Tighten this time

    assert market(ex).status is MarketStatus.OPEN


def test_kicking_someone_else_while_the_mm_quotes_changes_nothing(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.start_auction("cars")
    ex.submit_width("cars", bob, 30)
    ex.submit_width("cars", alice, 20)
    ex.close_auction("cars")

    ex.kick(bob)

    assert (market(ex).status, market(ex).mm_id) == (MarketStatus.MM_QUOTING, alice)


def test_kicking_the_market_maker_during_the_window_still_prints_their_trades(clock):
    ex = new_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    run_to_forced_trade(ex, alice, bid=100, ask=110)
    ex.choose_side("cars", bob, BUY)

    ex.kick(alice)
    assert market(ex).status is MarketStatus.FORCED_TRADE
    events = ex.end_forced_trade("cars", ended_by="timer")

    assert fills(events) == [(bob, alice, 110, 10)]
    assert ex.position("cars", alice) == -10


# --- The log --------------------------------------------------------------------------------


def run_session(ex, clock):
    """Three markets: one all the way through (with a kick and coin flips), one stuck in the
    forced-trade window, one in the auction."""
    alice, bob, carol, dave, erin = join_all(ex, "Alice", "Bob", "Carol", "Dave", "Erin")
    ex.start_auction("cars")
    ex.submit_width("cars", alice, 40)
    ex.submit_width("cars", bob, 30)
    ex.submit_width("cars", carol, 35)  # rejected: not narrower
    ex.close_auction("cars")
    ex.kick(bob)  # Alice takes over
    ex.submit_mm_quote("cars", alice, 100, 140)
    clock.set("09:31:00")
    ex.start_forced_trade("cars")
    ex.choose_side("cars", carol, BUY)
    ex.choose_side("cars", carol, SELL)
    ex.end_forced_trade("cars", ended_by="admin")  # Dave and Erin get coin flips
    ex.place_limit("cars", dave, BUY, 105, 3)

    ex.create_market(make_config(market_id="homes", tick_size=TICK))
    ex.start_auction("homes")
    ex.submit_width("homes", dave, 0)
    ex.close_auction("homes")
    ex.submit_mm_quote("homes", dave, 500, 500)
    ex.start_forced_trade("homes")
    ex.choose_side("homes", erin, BUY)

    ex.create_market(make_config(market_id="later", tick_size=TICK))
    ex.start_auction("later")
    ex.submit_width("later", erin, 15)


def snapshot(ex):
    state = {"traders": ex.traders, "kicked": ex.kicked, "next_trade_id": ex.next_trade_id}
    for market_id, m in ex.markets.items():
        state[market_id] = {
            "status": m.status, "widths": m.widths, "best": m.best_width(),
            "mm": (m.mm_id, m.mm_width, m.mm_bid, m.mm_ask),
            "choices": m.choices, "assigned": m.assigned,
            "forced_started_at": m.forced_started_at,
            "trades": m.trades, "bids": bids(ex, market_id), "asks": asks(ex, market_id),
            "positions": {t: ex.position(market_id, t) for t in ex.traders},
            "mark": m.mark(),
        }
    return state


def test_replaying_the_log_rebuilds_trade_or_tighten_state(clock):
    live = new_exchange(clock, BUY, SELL)
    run_session(live, clock)

    # Replay never flips a coin: the random sides are SideAssigned events in the log.
    replayed = Exchange.replay(live.events, clock=clock)

    assert snapshot(replayed) == snapshot(live)
    assert replayed.forced_trade_seconds_left("homes") == live.forced_trade_seconds_left("homes")


def test_trade_or_tighten_events_survive_a_json_round_trip(clock):
    live = new_exchange(clock, BUY, SELL)
    run_session(live, clock)

    text = json.dumps([to_dict(event) for event in live.events])
    events = [from_dict(data) for data in json.loads(text)]

    assert events == live.events
    assert snapshot(Exchange.replay(events)) == snapshot(live)
