"""The trading timer (SPEC Q219-Q227): a market can have a trading time; when it runs out the
market halts by itself, and settles by itself if the admin entered the true value in advance."""

from dataclasses import replace
from datetime import timedelta

import pytest

from exchange.engine import (
    Exchange,
    MarketStatus,
    Side,
    TradingTimeAdded,
    TradingTimeUp,
    from_dict,
    to_dict,
)
from helpers import FakeClock, ScriptedCoin, make_config

BUY, SELL = Side.BUY, Side.SELL


def timed(seconds=300, true_value=None, open_now=True):
    clock = FakeClock()  # 09:30:00
    ex = Exchange(clock=clock)
    ex.create_market(replace(make_config(), trading_seconds=seconds, true_value=true_value))
    if open_now:
        ex.open_market("cars")
    return ex, clock


def later(clock, seconds):
    clock.now += timedelta(seconds=seconds)


# --- The clock ---------------------------------------------------------------------------------


def test_a_market_without_a_trading_time_has_no_clock(ex):
    assert ex.trading_seconds_left("cars") is None
    assert ex.due_trading_ends() == []


def test_the_clock_starts_when_continuous_trading_starts():
    ex, clock = timed(open_now=False)
    assert ex.trading_seconds_left("cars") is None  # not open yet
    later(clock, 50)
    ex.open_market("cars")
    later(clock, 60)
    assert ex.trading_seconds_left("cars") == 240


def test_after_trade_or_tighten_the_clock_starts_at_the_forced_trades():
    clock = FakeClock()
    ex = Exchange(clock=clock, rng=ScriptedCoin(BUY))
    ex.create_market(replace(make_config(), trading_seconds=120))
    ex.join("Alice")
    ex.join("Bob")
    ex.start_auction("cars")
    ex.submit_width("cars", "t1", 10)
    ex.close_auction("cars")
    ex.submit_mm_quote("cars", "t1", 40, 50)
    ex.start_forced_trade("cars")
    later(clock, 30)
    ex.end_forced_trade("cars", ended_by="timer")
    later(clock, 20)
    assert ex.trading_seconds_left("cars") == 100


def test_the_market_is_due_when_its_time_is_up():
    ex, clock = timed(seconds=60)
    later(clock, 59)
    assert ex.due_trading_ends() == []
    later(clock, 1)
    assert ex.due_trading_ends() == ["cars"]
    assert ex.trading_seconds_left("cars") == 0


def test_a_halt_pauses_the_clock_and_resume_continues_it():
    ex, clock = timed(seconds=300)
    later(clock, 60)
    ex.halt_market("cars")
    later(clock, 600)  # ten minutes halted
    assert ex.trading_seconds_left("cars") == 240
    assert ex.due_trading_ends() == []  # a halted market isn't due
    ex.resume_market("cars")
    later(clock, 40)
    assert ex.trading_seconds_left("cars") == 200


# --- Time's up ---------------------------------------------------------------------------------


def test_when_time_is_up_the_market_halts():
    ex, clock = timed(seconds=60)
    later(clock, 60)
    events = ex.end_trading("cars", ended_by="timer")

    assert isinstance(events[0], TradingTimeUp) and events[0].ended_by == "timer"
    assert ex.markets["cars"].status is MarketStatus.HALTED
    assert ex.markets["cars"].time_up
    assert ex.trading_seconds_left("cars") is None
    assert ex.due_trading_ends() == []


def test_with_the_true_value_entered_in_advance_it_settles_itself():
    ex, clock = timed(seconds=60, true_value=4_200)
    ex.place_limit("cars", "alice", SELL, 4_000, 5)
    ex.take("cars", "bob", BUY, 4_000, 5)
    later(clock, 60)
    ex.end_trading("cars", ended_by="timer")

    assert ex.markets["cars"].status is MarketStatus.SETTLED
    assert ex.markets["cars"].settlement_value == 4_200
    assert ex.pnl("cars", "bob").total == 1_000


def test_the_admin_can_end_trading_early():
    ex, clock = timed(seconds=300)
    events = ex.end_trading("cars", ended_by="admin")
    assert events[0].ended_by == "admin"
    assert ex.markets["cars"].status is MarketStatus.HALTED


def test_ending_trading_needs_a_running_clock():
    ex, _ = timed(seconds=60)
    ex.end_trading("cars", ended_by="admin")
    with pytest.raises(ValueError, match="no trading clock running"):
        ex.end_trading("cars", ended_by="admin")


def test_after_time_is_up_resume_trades_on_with_no_clock():
    ex, clock = timed(seconds=60)
    later(clock, 60)
    ex.end_trading("cars", ended_by="timer")
    ex.resume_market("cars")
    later(clock, 600)
    assert ex.markets["cars"].status is MarketStatus.OPEN
    assert ex.trading_seconds_left("cars") is None
    assert ex.due_trading_ends() == []


def test_the_admin_can_still_settle_by_hand_at_any_time():
    ex, _ = timed(seconds=300, true_value=10)
    ex.settle_market("cars", 12)  # e.g. the answer turned out to be different
    assert ex.markets["cars"].settlement_value == 12
    assert ex.trading_seconds_left("cars") is None


# --- Adding time -------------------------------------------------------------------------------


def test_adding_time_moves_the_end_later():
    ex, clock = timed(seconds=60)
    later(clock, 30)
    events = ex.add_trading_time("cars", 60)
    assert isinstance(events[0], TradingTimeAdded)
    assert ex.trading_seconds_left("cars") == 90


def test_adding_time_while_halted_adds_to_what_is_left():
    ex, clock = timed(seconds=60)
    ex.halt_market("cars")
    ex.add_trading_time("cars", 60)
    later(clock, 1000)
    assert ex.trading_seconds_left("cars") == 120


@pytest.mark.parametrize("seconds", [0, -60, 1.5])
def test_added_time_must_be_a_positive_whole_number_of_seconds(seconds):
    ex, _ = timed()
    with pytest.raises(ValueError, match="whole number of seconds"):
        ex.add_trading_time("cars", seconds)


def test_no_time_can_be_added_without_a_running_clock(ex):
    with pytest.raises(ValueError, match="no trading clock running"):
        ex.add_trading_time("cars", 60)


# --- Settings ----------------------------------------------------------------------------------


@pytest.mark.parametrize("changes, wanted", [
    (dict(trading_seconds=0), "trading time"),
    (dict(trading_seconds=2.5), "trading time"),
    (dict(true_value=3.5), "true value must be a whole number"),
])
def test_bad_timer_settings_are_refused(changes, wanted):
    with pytest.raises(ValueError, match=wanted):
        Exchange().create_market(replace(make_config(), **changes))


def test_the_timer_and_true_value_survive_editing_and_replay():
    ex = Exchange()
    ex.create_market(make_config())
    ex.edit_market(replace(make_config(), trading_seconds=90, true_value=7))
    config = ex.markets["cars"].config
    assert (config.trading_seconds, config.true_value) == (90, 7)

    ex.open_market("cars")
    ex.end_trading("cars", ended_by="admin")
    copy = Exchange.replay([from_dict(to_dict(event)) for event in ex.events])
    assert copy.markets["cars"].settlement_value == 7
    assert copy.markets["cars"].time_up
