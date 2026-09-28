"""Admin commands in the engine: create/edit, halt/resume, settle, info drops, kick, rename, lock."""

from dataclasses import replace

import pytest

from exchange.engine import (
    Exchange,
    InfoDropMarked,
    MarketStatus,
    OrderCancelled,
    PnL,
    Rejected,
    Side,
    TraderKicked,
    TraderRenamed,
)
from exchange.server.views import market_view, trade_view
from helpers import accepted_id, asks, bids, make_config, open_market, trades_in

BUY, SELL = Side.BUY, Side.SELL


def join_all(ex, *names):
    """Join each name and return their trader ids in the same order."""
    return [ex.join(name)[0].trader_id for name in names]


def status(ex, market_id="cars"):
    return ex.markets[market_id].status


# --- Create and edit ------------------------------------------------------------------------


@pytest.mark.parametrize("setting, value, reason", [
    ("title", "   ", "title"),
    ("tick_size", 0, "tick size"),
    ("max_position", 0, "max position"),
    ("max_position", 2.5, "max position"),
    ("forced_trade_size", 0, "forced-trade size"),
    ("forced_trade_seconds", 0, "forced-trade seconds"),
])
def test_create_market_checks_every_setting(clock, setting, value, reason):
    ex = Exchange(clock=clock)

    with pytest.raises(ValueError, match=reason):
        ex.create_market(replace(make_config(), **{setting: value}))
    assert ex.markets == {}


def test_create_market_trims_the_title(clock):
    ex = Exchange(clock=clock)
    ex.create_market(replace(make_config(), title="  Cars in Centre County  "))

    assert ex.markets["cars"].config.title == "Cars in Centre County"


def test_a_created_market_can_be_edited(clock):
    ex = Exchange(clock=clock)
    ex.create_market(make_config())

    ex.edit_market(replace(make_config(), title="Homes", tick_size=500, max_position=20,
                           forced_trade_size=2, forced_trade_seconds=45))

    config = ex.markets["cars"].config
    assert (config.title, config.tick_size, config.max_position) == ("Homes", 500, 20)
    assert (config.forced_trade_size, config.forced_trade_seconds) == (2, 45)


def test_edit_checks_the_settings_too(clock):
    ex = Exchange(clock=clock)
    ex.create_market(make_config())

    with pytest.raises(ValueError, match="tick size"):
        ex.edit_market(replace(make_config(), tick_size=-5))
    assert ex.markets["cars"].config.tick_size == 1


def test_an_opened_market_cannot_be_edited(ex):
    with pytest.raises(ValueError, match="only before"):
        ex.edit_market(replace(make_config(), tick_size=5))


def test_unknown_market_is_an_error_for_admin_commands(ex):
    with pytest.raises(ValueError, match="unknown market"):
        ex.halt_market("nope")


def test_open_only_works_on_a_created_market(ex):
    ex.halt_market("cars")

    with pytest.raises(ValueError, match="cannot open"):
        ex.open_market("cars")  # resuming a halted market is resume_market's job


# --- Halt and resume ------------------------------------------------------------------------


def test_halt_rejects_new_orders_and_takes(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", alice, SELL, 40, 10)

    ex.halt_market("cars")

    assert status(ex) is MarketStatus.HALTED
    (limit,) = ex.place_limit("cars", bob, BUY, 35, 1)
    (take,) = ex.take("cars", bob, BUY, 40, 1)
    assert isinstance(limit, Rejected) and "not open" in limit.reason
    assert isinstance(take, Rejected)
    assert asks(ex) == [(40, [(alice, 10)])]


def test_orders_can_be_cancelled_while_halted(ex):
    (alice,) = join_all(ex, "Alice")
    order_id = accepted_id(ex.place_limit("cars", alice, SELL, 40, 10))
    ex.place_limit("cars", alice, BUY, 30, 10)
    ex.halt_market("cars")

    (cancelled,) = ex.cancel("cars", alice, order_id)
    ex.cancel_all(alice)

    assert isinstance(cancelled, OrderCancelled)
    assert bids(ex) == [] and asks(ex) == []


def test_resume_brings_back_trading_with_the_book_unchanged(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", alice, SELL, 40, 10)
    ex.halt_market("cars")

    ex.resume_market("cars")

    assert status(ex) is MarketStatus.OPEN
    assert asks(ex) == [(40, [(alice, 10)])]
    ex.take("cars", bob, BUY, 40, 3)
    assert ex.position("cars", bob) == 3


def test_halt_and_resume_only_from_the_right_status(ex):
    with pytest.raises(ValueError, match="cannot resume"):
        ex.resume_market("cars")
    ex.halt_market("cars")
    with pytest.raises(ValueError, match="cannot halt"):
        ex.halt_market("cars")


# --- Settle ---------------------------------------------------------------------------------


def trade_and_leave_orders(ex):
    """Alice buys 10 @ 40 from Bob; Bob is left with a resting offer at 45."""
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", bob, SELL, 40, 10)
    ex.take("cars", alice, BUY, 40, 10)
    order_id = accepted_id(ex.place_limit("cars", bob, SELL, 45, 5))
    return alice, bob, order_id


def test_settle_straight_from_open(ex):
    trade_and_leave_orders(ex)

    ex.settle_market("cars", 50)

    assert status(ex) is MarketStatus.SETTLED


def test_settle_from_halted(ex):
    trade_and_leave_orders(ex)
    ex.halt_market("cars")

    ex.settle_market("cars", 50)

    assert status(ex) is MarketStatus.SETTLED


def test_settlement_value_is_the_mark_and_gives_final_pnl(ex):
    alice, bob, _ = trade_and_leave_orders(ex)

    ex.settle_market("cars", 52)

    assert ex.mark("cars") == 52
    assert ex.pnl("cars", alice) == PnL(realized=0, unrealized=120)  # (52 - 40) x 10
    assert ex.pnl("cars", bob) == PnL(realized=0, unrealized=-120)


def test_settlement_value_can_be_off_the_tick_zero_or_negative(clock):
    for value in (118_437, 0, -3):
        ex = Exchange(clock=clock)
        open_market(ex, tick_size=500)
        ex.settle_market("cars", value)
        assert ex.mark("cars") == value


@pytest.mark.parametrize("value", [52.5, "52", None, True])
def test_settlement_value_must_be_a_whole_number(ex, value):
    with pytest.raises(ValueError, match="whole number"):
        ex.settle_market("cars", value)
    assert status(ex) is MarketStatus.OPEN


def test_cannot_settle_before_opening_or_twice(clock):
    ex = Exchange(clock=clock)
    ex.create_market(make_config())
    with pytest.raises(ValueError, match="cannot settle"):
        ex.settle_market("cars", 50)

    ex.open_market("cars")
    ex.settle_market("cars", 50)
    with pytest.raises(ValueError, match="cannot settle"):
        ex.settle_market("cars", 60)


def test_resting_orders_stay_frozen_after_settlement(ex):
    alice, bob, order_id = trade_and_leave_orders(ex)
    ex.settle_market("cars", 50)

    (cancel,) = ex.cancel("cars", bob, order_id)
    cancel_all = ex.cancel_all(bob)
    (take,) = ex.take("cars", alice, BUY, 45, 1)

    assert isinstance(cancel, Rejected) and cancel.reason == "market is settled"
    assert cancel_all == []
    assert isinstance(take, Rejected)
    assert asks(ex) == [(45, [(bob, 5)])]


def test_cancel_all_everywhere_skips_settled_markets(ex):
    open_market(ex, market_id="homes")
    (alice,) = join_all(ex, "Alice")
    ex.place_limit("cars", alice, BUY, 30, 1)
    ex.place_limit("homes", alice, BUY, 30, 1)
    ex.settle_market("cars", 50)

    ex.cancel_all(alice)

    assert bids(ex, "cars") == [(30, [(alice, 1)])]
    assert bids(ex, "homes") == []


def test_market_view_shows_the_settlement_value(ex):
    assert market_view(ex, "cars")["settlement_value"] is None
    ex.settle_market("cars", 50)
    assert market_view(ex, "cars")["settlement_value"] == 50


# --- Info drops -----------------------------------------------------------------------------


def test_info_drop_is_logged_for_the_whole_room(ex, clock):
    clock.set("09:45:00")

    (event,) = ex.mark_info_drop("  Hint: more than 100,000  ")

    assert isinstance(event, InfoDropMarked)
    assert event.note == "Hint: more than 100,000"
    assert event.market_id is None
    assert event.ts == clock.now
    assert ex.info_drops == [event]


def test_info_drop_note_is_optional(ex):
    (event,) = ex.mark_info_drop()

    assert event.note == ""


# --- Kick -----------------------------------------------------------------------------------


def test_kick_cancels_orders_in_unsettled_markets_and_keeps_the_position(ex):
    open_market(ex, market_id="homes")
    open_market(ex, market_id="done")
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", bob, SELL, 40, 10)
    ex.take("cars", alice, BUY, 40, 4)  # Alice long 4, Bob has 6 left at 40
    ex.place_limit("homes", bob, BUY, 30, 1)
    ex.place_limit("done", bob, BUY, 30, 1)
    ex.settle_market("done", 50)
    ex.halt_market("homes")

    events = ex.kick(bob)

    assert [type(event) for event in events] == [OrderCancelled, OrderCancelled, TraderKicked]
    assert all(event.reason == "removed by admin" for event in events[:2])
    assert asks(ex, "cars") == [] and bids(ex, "homes") == []
    assert bids(ex, "done") == [(30, [(bob, 1)])]  # settled: frozen
    assert ex.position("cars", bob) == -4
    assert bob in ex.kicked
    assert ex.traders[bob] == "Bob"  # still listed; the name stays taken


def test_a_kicked_trader_cannot_trade_or_cancel(ex):
    (alice,) = join_all(ex, "Alice")
    ex.kick(alice)

    for events in (ex.place_limit("cars", alice, BUY, 30, 1),
                   ex.take("cars", alice, BUY, 30, 1),
                   ex.place_quote("cars", alice, 30, 40, 1),
                   ex.cancel("cars", alice, 1),
                   ex.cancel_all(alice)):
        (event,) = events
        assert isinstance(event, Rejected)
        assert event.reason == "you were removed from the game"


def test_a_kicked_name_stays_taken(ex):
    (alice,) = join_all(ex, "Alice")
    ex.kick(alice)

    with pytest.raises(ValueError, match="taken"):
        ex.join("alice")


def test_kick_needs_a_known_trader_who_is_still_here(ex):
    with pytest.raises(ValueError, match="unknown trader"):
        ex.kick("t99")
    (alice,) = join_all(ex, "Alice")
    ex.kick(alice)
    with pytest.raises(ValueError, match="already removed"):
        ex.kick(alice)


# --- Rename ---------------------------------------------------------------------------------


def test_rename_logs_old_and_new_name(ex):
    (alice,) = join_all(ex, "Alice")

    (event,) = ex.rename(alice, "  Alicia ")

    assert isinstance(event, TraderRenamed)
    assert (event.trader_id, event.old_name, event.new_name) == (alice, "Alice", "Alicia")
    assert ex.traders[alice] == "Alicia"
    assert ex.find_trader("alicia") == alice
    assert ex.find_trader("alice") is None


def test_rename_follows_the_join_rules(ex):
    alice, _ = join_all(ex, "Alice", "Bob")

    with pytest.raises(ValueError, match="taken"):
        ex.rename(alice, "BOB")
    with pytest.raises(ValueError, match="1 to 20"):
        ex.rename(alice, "   ")
    with pytest.raises(ValueError, match="1 to 20"):
        ex.rename(alice, "x" * 21)
    with pytest.raises(ValueError, match="unknown trader"):
        ex.rename("t99", "Zed")


def test_a_trader_may_change_the_case_of_their_own_name(ex):
    (alice,) = join_all(ex, "alice")

    ex.rename(alice, "Alice")

    assert ex.traders[alice] == "Alice"


def test_old_trades_and_the_book_show_the_new_name(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", bob, SELL, 40, 10)
    (trade,) = trades_in(ex.take("cars", alice, BUY, 40, 4))

    ex.rename(bob, "Robert")

    assert trade_view(ex, trade)["seller"] == "Robert"
    assert market_view(ex, "cars")["asks"][0]["orders"][0]["name"] == "Robert"


# --- Locking joins --------------------------------------------------------------------------


def test_locked_joining_refuses_new_traders_until_unlocked(ex):
    ex.lock_joining()

    assert ex.joining_locked
    with pytest.raises(ValueError, match="joining is locked"):
        ex.join("Alice")

    ex.unlock_joining()
    assert not ex.joining_locked
    ex.join("Alice")


def test_lock_and_unlock_must_change_something(ex):
    with pytest.raises(ValueError, match="already unlocked"):
        ex.unlock_joining()
    ex.lock_joining()
    with pytest.raises(ValueError, match="already locked"):
        ex.lock_joining()
