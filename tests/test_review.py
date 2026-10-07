"""The review screen's numbers: one trader's fills in one market, with the running position,
realized PnL and MTM PnL after each fill, the edge against the settlement value, and the
market's price history for the chart."""

from exchange.engine import Exchange, PricePoint, Side
from exchange.engine.review import mark_at, total_edge, trader_fills
from helpers import ScriptedCoin, make_config

BUY, SELL = Side.BUY, Side.SELL


def join_all(ex, *names):
    return [ex.join(name)[0].trader_id for name in names]


def history(ex, market_id="cars"):
    """The price history as [(mark, last_price), ...]."""
    return [(point.mark, point.last_price) for point in ex.markets[market_id].price_history]


# --- Price history (for the chart) ----------------------------------------------------------


def test_the_history_records_each_change_of_mark_or_last_price(ex, clock):
    alice, bob = join_all(ex, "Alice", "Bob")
    clock.set("09:31:00")
    ex.place_limit("cars", alice, BUY, 30, 5)    # one side only, nothing traded: no mark yet
    ex.place_limit("cars", bob, SELL, 40, 5)     # mid 35
    ex.place_limit("cars", bob, SELL, 45, 5)     # deeper offer: mark unchanged, no new point
    clock.set("09:32:00")
    ex.take("cars", alice, BUY, 40, 5)           # trade at 40; offers now 45: mid 37.5

    assert history(ex) == [(35, None), (37.5, 40)]
    points = ex.markets["cars"].price_history
    assert [point.ts for point in points] == [clock.now.replace(minute=31), clock.now]


def test_points_carry_the_seq_of_the_event_that_changed_the_price(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", alice, BUY, 30, 5)
    (accepted, rested) = ex.place_limit("cars", bob, SELL, 40, 5)

    (point,) = ex.markets["cars"].price_history
    assert point == PricePoint(seq=rested.seq, ts=rested.ts, mark=35, last_price=None)


def test_settling_adds_no_point_and_records_when_it_opened_and_settled(ex, clock):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", alice, BUY, 30, 5)
    ex.place_limit("cars", bob, SELL, 40, 5)
    clock.set("10:00:00")
    ex.settle_market("cars", 99)

    market = ex.markets["cars"]
    assert history(ex) == [(35, None)]  # the settlement value is drawn as its own line
    assert market.opened_at == clock.now.replace(hour=9, minute=30)
    assert market.settled_at == clock.now


def test_forced_trades_add_points_and_the_market_opens_at_their_time(clock):
    ex = Exchange(clock=clock, rng=ScriptedCoin(SELL))
    ex.create_market(make_config(tick_size=5))
    mm, alice, bob = join_all(ex, "Mm", "Alice", "Bob")
    ex.start_auction("cars")
    ex.submit_width("cars", mm, 10)
    ex.close_auction("cars")
    ex.submit_mm_quote("cars", mm, 100, 110)
    ex.start_forced_trade("cars")
    ex.choose_side("cars", alice, BUY)
    clock.set("09:31:00")
    ex.end_forced_trade("cars", ended_by="admin")  # Alice buys at 110, Bob sells at 100

    # The MM's quote never rests, so the book is empty and the mark is the last price.
    assert history(ex) == [(110, 110), (100, 100)]
    assert ex.markets["cars"].opened_at == clock.now


def test_replay_rebuilds_the_same_history(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", alice, BUY, 30, 5)
    ex.place_limit("cars", bob, SELL, 40, 5)
    ex.take("cars", alice, BUY, 40, 2)
    ex.settle_market("cars", 38)

    replayed = Exchange.replay(ex.events).markets["cars"]

    assert replayed.price_history == ex.markets["cars"].price_history
    assert (replayed.opened_at, replayed.settled_at) == (
        ex.markets["cars"].opened_at, ex.markets["cars"].settled_at)


def test_mark_at_is_the_mark_as_of_the_last_point_at_or_before_a_seq():
    points = [PricePoint(seq=5, ts=None, mark=35, last_price=None),
              PricePoint(seq=9, ts=None, mark=40, last_price=40)]

    assert mark_at(points, 4) is None
    assert mark_at(points, 5) == 35
    assert mark_at(points, 8) == 35
    assert mark_at(points, 9) == 40
    assert mark_at(points, 100) == 40


# --- One trader's fills ---------------------------------------------------------------------


def test_each_fill_shows_side_counterparty_and_running_position_and_realized_pnl(ex):
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    ex.place_limit("cars", bob, SELL, 40, 10)
    ex.take("cars", alice, BUY, 40, 10)          # Alice +10 @ 40 from Bob
    ex.place_limit("cars", carol, BUY, 45, 4)
    ex.take("cars", alice, SELL, 45, 4)          # Alice sells 4 @ 45 to Carol: +20 realized

    rows = trader_fills(ex.markets["cars"], alice)

    assert [(row.side, row.trade.price, row.trade.size, row.counterparty_id) for row in rows] == [
        (BUY, 40, 10, bob), (SELL, 45, 4, carol)]
    assert [(row.position, row.realized) for row in rows] == [(10, 0), (6, 20)]
    assert [row.trade.trade_id for row in rows] == [1, 2]


def test_a_trader_with_no_fills_has_no_rows(ex):
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    ex.place_limit("cars", bob, SELL, 40, 10)
    ex.take("cars", alice, BUY, 40, 10)

    assert trader_fills(ex.markets["cars"], carol) == []


def test_mtm_uses_the_mark_right_after_the_fill_before_the_rest_of_the_order_rests(ex):
    alice, bob, carol, dave = join_all(ex, "Alice", "Bob", "Carol", "Dave")
    ex.place_limit("cars", bob, SELL, 39, 10)
    ex.place_limit("cars", carol, BUY, 30, 5)
    ex.place_limit("cars", dave, SELL, 50, 5)
    # Alice bids 40 for 30: she buys Bob's 10 at 39, then her other 20 rest at 40.
    ex.place_limit("cars", alice, BUY, 40, 30)

    (row,) = trader_fills(ex.markets["cars"], alice)

    # Right after the fill the book is 30 bid, 50 offered: mark 40, so MTM = (40 - 39) x 10.
    # (Once her 20 rest at 40, the mark is 45, but that comes after the fill.)
    assert row.unrealized == 10
    assert ex.mark("cars") == 45


def test_mtm_after_a_fill_that_flattens_the_position_is_zero(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", bob, SELL, 40, 5)
    ex.take("cars", alice, BUY, 40, 5)
    ex.place_limit("cars", bob, BUY, 42, 5)
    ex.take("cars", alice, SELL, 42, 5)

    rows = trader_fills(ex.markets["cars"], alice)

    assert [(row.position, row.realized, row.unrealized) for row in rows] == [
        (5, 0, 0), (0, 10, 0)]


def test_forced_trades_are_rows_for_the_trader_and_for_the_market_maker(clock):
    ex = Exchange(clock=clock, rng=ScriptedCoin(SELL))
    ex.create_market(make_config(tick_size=5))
    mm, alice, bob = join_all(ex, "Mm", "Alice", "Bob")
    ex.start_auction("cars")
    ex.submit_width("cars", mm, 10)
    ex.close_auction("cars")
    ex.submit_mm_quote("cars", mm, 100, 110)
    ex.start_forced_trade("cars")
    ex.choose_side("cars", alice, BUY)
    ex.end_forced_trade("cars", ended_by="admin")  # Alice buys 10 at 110, Bob sells 10 at 100

    (alice_row,) = trader_fills(ex.markets["cars"], alice)
    assert (alice_row.side, alice_row.counterparty_id, alice_row.trade.forced) == (BUY, mm, True)
    assert (alice_row.position, alice_row.unrealized) == (10, 0)  # mark = last price 110

    mm_rows = trader_fills(ex.markets["cars"], mm)
    assert [(row.side, row.trade.price, row.counterparty_id) for row in mm_rows] == [
        (SELL, 110, alice), (BUY, 100, bob)]
    # Short 10 at 110, then buys 10 at 100: flat with +100 realized.
    assert [(row.position, row.realized, row.unrealized) for row in mm_rows] == [
        (-10, 0, 0), (0, 100, 0)]


# --- Edge against the settlement value ------------------------------------------------------


def test_there_is_no_edge_before_settlement(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", bob, SELL, 40, 10)
    ex.take("cars", alice, BUY, 40, 10)

    rows = trader_fills(ex.markets["cars"], alice)

    assert [row.edge for row in rows] == [None]
    assert total_edge(ex.markets["cars"], rows) is None


def test_edge_is_settlement_minus_price_times_signed_size(ex):
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    ex.place_limit("cars", bob, SELL, 40, 10)
    ex.take("cars", alice, BUY, 40, 10)          # bought 10 at 40
    ex.place_limit("cars", carol, BUY, 50, 4)
    ex.take("cars", alice, SELL, 50, 4)          # sold 4 at 50
    ex.settle_market("cars", 44)

    rows = trader_fills(ex.markets["cars"], alice)

    # (44 - 40) x 10 = 40 for the buy; (44 - 50) x -4 = 24 for the sell.
    assert [row.edge for row in rows] == [40, 24]
    assert total_edge(ex.markets["cars"], rows) == 64


def test_total_edge_of_no_rows_is_zero_once_settled(ex):
    (alice,) = join_all(ex, "Alice")
    ex.settle_market("cars", 44)

    rows = trader_fills(ex.markets["cars"], alice)

    assert rows == []
    assert total_edge(ex.markets["cars"], rows) == 0
