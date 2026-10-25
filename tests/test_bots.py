"""Maker and taker bots in the engine (SPEC Q209-Q218).

Bots are ordinary traders whose orders the server places for them. They see only what every
student sees (the book, the last trade, their own position) and follow every normal rule.
"""

from dataclasses import replace

import pytest

from exchange.engine import (
    BotsStarted,
    BotsStopped,
    Exchange,
    Rejected,
    Side,
    TraderJoined,
    from_dict,
    to_dict,
)
from exchange.engine.bots import maker_quote, taker_price
from helpers import asks, bids, make_config, open_market

BUY, SELL = Side.BUY, Side.SELL


class EagerRandom:
    """Stands in for random.Random: every bot acts on every step and picks the first choice and
    the smallest number, so a test knows exactly what the bots do."""

    def random(self):
        return 0.0

    def randint(self, low, high):
        return low

    def choice(self, options):
        return options[0]


def with_bots(*kinds, **config):
    ex = Exchange(rng=EagerRandom())
    open_market(ex, **config)
    for kind in kinds:
        ex.start_bots(kind)
    return ex


def bot_ids(ex, kind):
    return [trader_id for trader_id, bot in ex.bots.items() if bot == kind]


# --- Turning bots on and off -----------------------------------------------------------------


def test_turning_makers_on_brings_in_two_labelled_makers():
    ex = with_bots("maker")
    makers = bot_ids(ex, "maker")
    assert [ex.traders[trader_id] for trader_id in makers] == ["Maker Bot 1", "Maker Bot 2"]
    assert ex.bots_on == {"maker": True, "taker": False}
    assert isinstance(ex.events[-1], BotsStarted)


def test_turning_takers_on_brings_in_three_takers():
    ex = with_bots("taker")
    assert [ex.traders[t] for t in bot_ids(ex, "taker")] == \
        ["Taker Bot 1", "Taker Bot 2", "Taker Bot 3"]


def test_bots_join_even_when_joining_is_locked():
    ex = Exchange()
    ex.lock_joining()
    ex.start_bots("maker")
    assert len(bot_ids(ex, "maker")) == 2


def test_turning_bots_off_cancels_their_orders_and_on_again_reuses_them():
    ex = with_bots("maker")
    market_with(ex, last=45)  # a price to quote around
    ex.step_bots("maker")
    assert bids(ex) and asks(ex)

    events = ex.stop_bots("maker")
    assert isinstance(events[-1], BotsStopped)
    assert bids(ex) == [] and asks(ex) == []
    assert ex.bots_on["maker"] is False

    ex.start_bots("maker")
    assert len(bot_ids(ex, "maker")) == 2  # the same two, with their positions


@pytest.mark.parametrize("command, wanted", [
    ("start", "the makers are already on"),
    ("stop", "the takers are already off"),
])
def test_switching_to_the_state_its_already_in_is_refused(command, wanted):
    ex = with_bots("maker")
    with pytest.raises(ValueError, match=wanted):
        if command == "start":
            ex.start_bots("maker")
        else:
            ex.stop_bots("taker")


def test_an_unknown_kind_of_bot_is_refused():
    with pytest.raises(ValueError, match="a maker or a taker"):
        Exchange().start_bots("spoofer")


def test_bots_sit_out_the_forced_trade():
    ex = Exchange()
    ex.create_market(make_config())
    ex.join("Alice")
    ex.join("Bob")
    ex.start_bots("maker")
    ex.start_bots("taker")
    ex.start_auction("cars")
    ex.submit_width("cars", "t1", 10)
    ex.close_auction("cars")

    assert ex.forced_trade_participants("cars") == ["t2"]  # Bob only: Alice is the MM


def test_replay_rebuilds_the_bots_and_their_switches():
    ex = with_bots("maker", "taker")
    ex.stop_bots("taker")
    copy = Exchange.replay([from_dict(to_dict(event)) for event in ex.events])
    assert copy.bots == ex.bots
    assert copy.bots_on == {"maker": True, "taker": False}


def test_a_student_cant_take_a_bots_name():
    ex = with_bots("maker")
    with pytest.raises(ValueError, match="taken"):
        ex.join("maker bot 1")


def test_logs_from_before_bots_still_replay():
    data = to_dict(TraderJoined(trader_id="t1", name="Alice"))
    del data["bot"]
    assert Exchange.replay([from_dict(data)]).bots == {}


# --- What a maker quotes (only public information) --------------------------------------------


def market_with(ex, *orders, last=None):
    """Put students' orders in "cars" (and a trade first, to set the last price)."""
    if last is not None:
        ex.place_limit("cars", "carol", SELL, last, 1)
        ex.take("cars", "dave", BUY, last, 1)
    for trader_id, side, price in orders:
        ex.place_limit("cars", trader_id, side, price, 1)
    return ex.markets["cars"]


def test_a_maker_quotes_around_the_middle_of_other_peoples_best_prices():
    ex = Exchange()
    open_market(ex)
    market = market_with(ex, ("alice", BUY, 40), ("bob", SELL, 50))
    assert maker_quote(market, "m1", half_width_ticks=2) == (43, 47)


def test_a_maker_ignores_its_own_orders():
    ex = Exchange()
    open_market(ex)
    market = market_with(ex, ("alice", BUY, 40), ("bob", SELL, 50), ("m1", BUY, 49))
    assert maker_quote(market, "m1", half_width_ticks=1) == (44, 46)


def test_with_no_two_sided_market_a_maker_uses_the_last_trade():
    ex = Exchange()
    open_market(ex)
    market = market_with(ex, ("alice", BUY, 40), last=70)
    assert maker_quote(market, "m1", half_width_ticks=3) == (67, 73)


def test_with_no_price_at_all_a_maker_doesnt_quote():
    ex = Exchange()
    open_market(ex)
    market = market_with(ex, ("alice", BUY, 40))  # one bid, no trade yet
    assert maker_quote(market, "m1", half_width_ticks=1) is None


def test_quotes_round_outwards_to_the_tick():
    ex = Exchange()
    open_market(ex, tick_size=5)
    market = market_with(ex, ("alice", BUY, 40), ("bob", SELL, 55))  # middle 47.5
    assert maker_quote(market, "m1", half_width_ticks=1) == (40, 55)


def test_a_long_maker_shades_its_quote_down_one_tick_per_fifth_of_max_position():
    ex = Exchange()
    open_market(ex, max_position=50)
    market = market_with(ex, ("alice", BUY, 40), ("bob", SELL, 50))
    ex.place_limit("cars", "carol", SELL, 45, 20)
    ex.take("cars", "m1", BUY, 45, 20)  # the maker is long 20: two fifths of 50
    assert maker_quote(market, "m1", half_width_ticks=1) == (42, 44)


def test_a_makers_quote_stays_inside_the_price_limits():
    ex = Exchange()
    ex.create_market(replace(make_config(), min_price=0, max_price=100))
    ex.open_market("cars")
    market = market_with(ex, ("alice", BUY, 97), ("bob", SELL, 100))
    assert maker_quote(market, "m1", half_width_ticks=3) == (95, 100)


# --- What a taker does ------------------------------------------------------------------------


def test_a_taker_buys_at_the_best_offer_and_sells_at_the_best_bid():
    ex = Exchange()
    open_market(ex)
    market = market_with(ex, ("alice", BUY, 40), ("bob", SELL, 50))
    assert taker_price(market, BUY) == 50
    assert taker_price(market, SELL) == 40


def test_a_taker_has_nothing_to_hit_on_an_empty_side():
    ex = Exchange()
    open_market(ex)
    market = market_with(ex, ("alice", BUY, 40))
    assert taker_price(market, BUY) is None


# --- Each step of the bots --------------------------------------------------------------------


def test_each_maker_quotes_every_open_market_it_can_price():
    ex = with_bots("maker")
    market_with(ex, ("alice", BUY, 40), ("bob", SELL, 50))
    ex.create_market(make_config("bikes"))  # not open: no quotes there

    ex.step_bots("maker")

    makers = bot_ids(ex, "maker")
    quoted = {order.trader_id for order in ex.book("cars").all_orders()}
    assert set(makers) <= quoted
    assert ex.book("bikes").all_orders() == []


def test_a_maker_replaces_its_old_quote_each_time():
    ex = with_bots("maker")
    market_with(ex, ("alice", BUY, 40), ("bob", SELL, 50))
    ex.step_bots("maker")
    ex.step_bots("maker")
    maker = bot_ids(ex, "maker")[0]
    mine = [order for order in ex.book("cars").all_orders() if order.trader_id == maker]
    assert len(mine) == 2  # one bid and one offer, not four


def test_a_maker_at_max_position_only_quotes_the_side_that_reduces_it():
    ex = with_bots("maker", max_position=5)
    market_with(ex, ("alice", BUY, 40), ("bob", SELL, 50))
    maker = bot_ids(ex, "maker")[0]
    ex.place_limit("cars", "carol", SELL, 45, 5)
    ex.take("cars", maker, BUY, 45, 5)

    events = ex.step_bots("maker")

    assert not any(isinstance(event, Rejected) for event in events)
    sides = {order.side for order in ex.book("cars").all_orders() if order.trader_id == maker}
    assert sides == {SELL}


def test_a_taker_trades_at_the_best_price():
    ex = with_bots("taker")
    market_with(ex, ("alice", BUY, 40), ("bob", SELL, 50))
    taker = bot_ids(ex, "taker")[0]

    ex.step_bots("taker")

    assert ex.position("cars", taker) == 1  # EagerRandom: buys, size 1, at the offer
    assert ex.markets["cars"].last_price == 50


def test_bots_that_are_off_do_nothing():
    ex = with_bots("maker")
    market_with(ex, ("alice", BUY, 40), ("bob", SELL, 50))
    ex.stop_bots("maker")
    assert ex.step_bots("maker") == []


def test_a_kicked_bot_stops_trading():
    ex = with_bots("taker")
    market_with(ex, ("alice", BUY, 40), ("bob", SELL, 500))
    for taker in bot_ids(ex, "taker"):
        ex.kick(taker)
    assert ex.step_bots("taker") == []


def test_bots_act_only_now_and_then():
    class NeverActs(EagerRandom):
        def random(self):
            return 0.99

    ex = with_bots("maker", "taker")
    market_with(ex, ("alice", BUY, 40), ("bob", SELL, 50))
    ex.rng = NeverActs()
    assert ex.step_bots("maker") == [] and ex.step_bots("taker") == []
