"""The delta-hedging round in the engine (SPEC Q160-Q185).

A round is one underlying whose fair price moves by itself (geometric Brownian motion, one move
per step) plus options on it that expire when the round ends. This file covers the engine's
part: creating, starting, moving, halting and settling a round, the greeks, each trader's net
delta, and the hedging error. The house market maker comes later.
"""

import math
from dataclasses import replace

import pytest

from exchange.engine import (
    Exchange,
    GreeksHidden,
    GreeksShown,
    MarketStatus,
    RoundConfig,
    Side,
    UnderlyingMoved,
    from_dict,
    to_dict,
)
from exchange.engine.options import black_scholes
from helpers import FakeClock, make_config, open_market

BUY, SELL = Side.BUY, Side.SELL


class ScriptedNormal:
    """Stands in for random.Random: each gauss() gives the next number in the script, so a test
    knows exactly where the price goes."""

    def __init__(self, *draws):
        self.draws = list(draws)

    def gauss(self, mu, sigma):
        assert (mu, sigma) == (0, 1)
        assert self.draws, "the test didn't script enough draws"
        return self.draws.pop(0)


def make_round(round_id="SPX", **changes):
    settings = dict(
        round_id=round_id, title="S&P", start_price=100, tick_size=1,
        realized_vol=0.2, implied_vol=0.2, minutes=1,
        options=(("call", 100), ("put", 95)),
        max_position=100, dollars_per_point=1, house_half_spread=1, house_size=10,
    )
    settings.update(changes)
    return RoundConfig(**settings)


def started(rng=None, **changes):
    exchange = Exchange(clock=FakeClock(), rng=rng)
    exchange.create_round(make_round(**changes))
    exchange.start_round("SPX")
    return exchange


def trade(ex, market_id, buyer, seller, price, size):
    ex.place_limit(market_id, seller, SELL, price, size)
    ex.take(market_id, buyer, BUY, price, size)


# --- Creating a round --------------------------------------------------------------------------


def test_a_round_creates_the_underlying_and_one_market_per_option():
    ex = Exchange()
    ex.create_round(make_round())

    assert list(ex.markets) == ["SPX", "SPX-C-100", "SPX-P-95"]
    titles = [market.config.title for market in ex.markets.values()]
    assert titles == ["S&P", "S&P 100 call", "S&P 95 put"]
    for market in ex.markets.values():
        assert market.status is MarketStatus.CREATED
        assert market.config.round_id == "SPX"
        assert market.config.tick_size == 1
        assert market.config.max_position == 100
        assert market.config.dollars_per_point == 1
    assert ex.rounds["SPX"].status == "created"
    assert ex.rounds["SPX"].fair_price == 100


def test_ordinary_markets_belong_to_no_round():
    ex = Exchange()
    open_market(ex)
    assert ex.markets["cars"].config.round_id is None
    assert ex.rounds == {}


def test_option_market_ids_and_kinds():
    ex = Exchange()
    ex.create_round(make_round())
    round_ = ex.rounds["SPX"]
    assert round_.underlying_id == "SPX"
    assert round_.option_ids == ["SPX-C-100", "SPX-P-95"]
    assert round_.option_of("SPX-C-100") == ("call", 100)
    assert round_.option_of("SPX-P-95") == ("put", 95)


@pytest.mark.parametrize("changes, reason", [
    (dict(round_id=""), "name"),
    (dict(round_id="S PX"), "letters and digits"),
    (dict(title="  "), "title"),
    (dict(start_price=0), "starting price"),
    (dict(start_price=101, tick_size=5), "multiple of the tick"),
    (dict(tick_size=0), "tick size"),
    (dict(realized_vol=-0.1), "realized vol"),
    (dict(implied_vol=0), "implied vol"),
    (dict(minutes=0), "minutes"),
    (dict(minutes=1.5), "minutes"),
    (dict(options=()), "at least one option"),
    (dict(options=(("straddle", 100),)), "a call or a put"),
    (dict(options=(("call", 0),)), "strike"),
    (dict(options=(("call", 102),), tick_size=5, start_price=100), "multiple of the tick"),
    (dict(options=(("call", 100), ("call", 100))), "twice"),
    (dict(max_position=0), "max position"),
    (dict(dollars_per_point=0), r"\$ per point"),
    (dict(house_half_spread=-1), "half-spread"),
    (dict(house_half_spread=3, tick_size=2, start_price=100, options=(("call", 100),)),
     "multiple of the tick"),
    (dict(house_size=0), "house size"),
])
def test_bad_round_settings_are_refused(changes, reason):
    ex = Exchange()
    with pytest.raises(ValueError, match=reason):
        ex.create_round(make_round(**changes))
    assert ex.events == []


def test_a_round_name_cant_clash_with_a_market():
    ex = Exchange()
    open_market(ex, market_id="SPX")
    with pytest.raises(ValueError, match="already exists"):
        ex.create_round(make_round())
    ex2 = Exchange()
    ex2.create_market(make_config(market_id="SPX-C-100"))
    with pytest.raises(ValueError, match="already exists"):
        ex2.create_round(make_round())


# --- Starting, halting, resuming ---------------------------------------------------------------


def test_starting_opens_every_market_in_the_round():
    ex = started()
    assert ex.rounds["SPX"].status == "running"
    assert all(market.status is MarketStatus.OPEN for market in ex.markets.values())
    assert ex.rounds["SPX"].steps_taken == 0
    assert ex.rounds["SPX"].total_steps == 60  # one move a second for one minute


def test_cant_trade_before_the_round_starts():
    ex = Exchange()
    ex.create_round(make_round())
    events = ex.place_limit("SPX", "alice", BUY, 99, 1)
    assert events[0].reason == "market is not open"


def test_cant_start_twice_or_start_an_unknown_round():
    ex = started()
    with pytest.raises(ValueError, match="cannot start"):
        ex.start_round("SPX")
    with pytest.raises(ValueError, match="unknown round"):
        ex.start_round("NOPE")


def test_halting_the_round_halts_every_market_and_resuming_reopens_them():
    ex = started()
    ex.halt_round("SPX")
    assert ex.rounds["SPX"].status == "halted"
    assert all(market.status is MarketStatus.HALTED for market in ex.markets.values())
    with pytest.raises(ValueError, match="cannot halt"):
        ex.halt_round("SPX")

    ex.resume_round("SPX")
    assert ex.rounds["SPX"].status == "running"
    assert all(market.status is MarketStatus.OPEN for market in ex.markets.values())
    with pytest.raises(ValueError, match="cannot resume"):
        ex.resume_round("SPX")


def test_a_rounds_markets_are_opened_halted_and_settled_only_as_a_round():
    ex = Exchange()
    ex.create_round(make_round())
    for command in (ex.open_market, ex.start_auction):
        with pytest.raises(ValueError, match="part of round SPX"):
            command("SPX-C-100")
    ex.start_round("SPX")
    with pytest.raises(ValueError, match="part of round SPX"):
        ex.halt_market("SPX")
    with pytest.raises(ValueError, match="part of round SPX"):
        ex.settle_market("SPX", 100)
    ex.halt_round("SPX")
    with pytest.raises(ValueError, match="part of round SPX"):
        ex.resume_market("SPX")
    with pytest.raises(ValueError, match="part of round SPX"):
        ex.edit_market(make_config(market_id="SPX"))


# --- The price moves ---------------------------------------------------------------------------


def test_each_step_moves_the_fair_price_by_a_logged_random_draw():
    ex = started(rng=ScriptedNormal(1.5, -0.5))
    dt = 1 / 60
    sigma = 0.2

    first = ex.step_round("SPX")
    expected = 100 * math.exp(-sigma ** 2 * dt / 2 + sigma * math.sqrt(dt) * 1.5)
    assert first[0] == UnderlyingMoved(seq=first[0].seq, ts=first[0].ts, round_id="SPX",
                                       step=1, price=expected, draw=1.5)
    assert ex.rounds["SPX"].fair_price == pytest.approx(expected)

    ex.step_round("SPX")
    expected *= math.exp(-sigma ** 2 * dt / 2 + sigma * math.sqrt(dt) * -0.5)
    assert ex.rounds["SPX"].fair_price == pytest.approx(expected)
    assert ex.rounds["SPX"].steps_taken == 2


def test_with_no_realized_vol_the_price_stays_put():
    ex = started(rng=ScriptedNormal(2.0, -3.0), realized_vol=0)
    ex.step_round("SPX")
    ex.step_round("SPX")
    assert ex.rounds["SPX"].fair_price == 100


def test_the_price_moves_only_while_the_round_is_running():
    ex = Exchange(rng=ScriptedNormal())
    ex.create_round(make_round())
    with pytest.raises(ValueError, match="cannot move"):
        ex.step_round("SPX")
    ex.start_round("SPX")
    ex.halt_round("SPX")
    with pytest.raises(ValueError, match="cannot move"):
        ex.step_round("SPX")


def test_the_time_left_counts_steps_not_the_wall_clock():
    ex = started(rng=ScriptedNormal(*[0.0] * 30))
    assert ex.rounds["SPX"].time_left == 1
    for _ in range(30):
        ex.step_round("SPX")
    assert ex.rounds["SPX"].time_left == pytest.approx(0.5)


# --- Expiry ------------------------------------------------------------------------------------


def run_to_expiry(ex, draws):
    for _ in draws[:-1]:
        ex.step_round("SPX")
    return ex.step_round("SPX")


def test_the_last_step_settles_every_market_at_its_payoff():
    draws = [0.3] * 60  # steadily up
    ex = started(rng=ScriptedNormal(*draws))
    run_to_expiry(ex, draws)

    final = ex.rounds["SPX"].fair_price
    settle = round(final)  # tick 1
    assert ex.rounds["SPX"].status == "settled"
    assert ex.markets["SPX"].settlement_value == settle
    assert ex.markets["SPX-C-100"].settlement_value == max(settle - 100, 0)
    assert ex.markets["SPX-P-95"].settlement_value == max(95 - settle, 0)
    assert all(market.status is MarketStatus.SETTLED for market in ex.markets.values())
    with pytest.raises(ValueError, match="cannot move"):
        ex.step_round("SPX")


def test_the_underlying_settles_at_the_nearest_tick():
    draws = [-0.4] * 60
    ex = started(rng=ScriptedNormal(*draws), tick_size=5, start_price=100,
                 options=(("call", 100), ("put", 90)), house_half_spread=5)
    run_to_expiry(ex, draws)
    final = ex.rounds["SPX"].fair_price
    settle = ex.markets["SPX"].settlement_value
    assert settle % 5 == 0
    assert abs(settle - final) <= 2.5
    assert ex.markets["SPX-P-90"].settlement_value == max(90 - settle, 0)


def test_a_halted_round_can_still_be_settled_by_resuming_and_stepping():
    draws = [0.0] * 60
    ex = started(rng=ScriptedNormal(*draws))
    for _ in range(59):
        ex.step_round("SPX")
    ex.halt_round("SPX")
    ex.resume_round("SPX")
    ex.step_round("SPX")
    assert ex.rounds["SPX"].status == "settled"


# --- Greeks and net delta ----------------------------------------------------------------------


def test_option_greeks_are_black_scholes_at_the_implied_vol():
    ex = started(rng=ScriptedNormal(*[0.5] * 15), implied_vol=0.3, realized_vol=0.1)
    for _ in range(15):
        ex.step_round("SPX")
    round_ = ex.rounds["SPX"]
    expected = black_scholes("call", round_.fair_price, 100, 0.3, 45 / 60)
    assert round_.greeks("SPX-C-100") == expected
    assert round_.greeks("SPX-P-95") == black_scholes("put", round_.fair_price, 95, 0.3, 45 / 60)


def test_the_underlying_has_delta_1_and_gamma_0():
    ex = started()
    greeks = ex.rounds["SPX"].greeks("SPX")
    assert (greeks.value, greeks.delta, greeks.gamma) == (100, 1, 0)


def test_net_delta_adds_the_underlying_and_each_option_times_its_delta():
    ex = started()
    trade(ex, "SPX-C-100", "alice", "bob", 8, 10)   # alice long 10 calls
    trade(ex, "SPX-P-95", "alice", "bob", 5, 4)     # and long 4 puts
    trade(ex, "SPX", "bob", "alice", 100, 3)        # and short 3 of the underlying
    round_ = ex.rounds["SPX"]
    call_delta = round_.greeks("SPX-C-100").delta
    put_delta = round_.greeks("SPX-P-95").delta

    assert ex.net_delta("SPX", "alice") == pytest.approx(10 * call_delta + 4 * put_delta - 3)
    assert ex.net_delta("SPX", "bob") == pytest.approx(-10 * call_delta - 4 * put_delta + 3)
    gamma = 10 * round_.greeks("SPX-C-100").gamma + 4 * round_.greeks("SPX-P-95").gamma
    assert ex.net_gamma("SPX", "alice") == pytest.approx(gamma)
    assert ex.net_delta("SPX", "carol") == 0


# --- Hedging error -----------------------------------------------------------------------------


def test_hedging_error_is_the_average_abs_net_delta_held_into_each_move():
    ex = started(rng=ScriptedNormal(0.0, 0.0, 0.0, 0.0))
    trade(ex, "SPX-C-100", "alice", "bob", 8, 10)
    delta_before_1 = ex.net_delta("SPX", "alice")
    ex.step_round("SPX")                     # alice carried 10 calls, unhedged, into move 1
    delta_before_2 = ex.net_delta("SPX", "alice")
    ex.step_round("SPX")                     # and into move 2
    trade(ex, "SPX", "bob", "alice", 100, 5)  # now she hedges with 5 short
    delta_before_3 = ex.net_delta("SPX", "alice")
    ex.step_round("SPX")
    delta_before_4 = ex.net_delta("SPX", "alice")
    ex.step_round("SPX")

    expected = (abs(delta_before_1) + abs(delta_before_2)
                + abs(delta_before_3) + abs(delta_before_4)) / 4
    assert ex.hedging_error("SPX", "alice") == pytest.approx(expected)
    assert ex.hedging_error("SPX", "bob") == pytest.approx(expected)  # the mirror image


def test_hedging_error_counts_every_move_since_the_start_even_before_trading():
    ex = started(rng=ScriptedNormal(0.0, 0.0))
    ex.step_round("SPX")                      # alice holds nothing: |0|
    trade(ex, "SPX-C-100", "alice", "bob", 8, 10)
    delta = ex.net_delta("SPX", "alice")
    ex.step_round("SPX")
    assert ex.hedging_error("SPX", "alice") == pytest.approx(abs(delta) / 2)


def test_no_hedging_error_for_traders_who_never_held_an_option():
    ex = started(rng=ScriptedNormal(0.0))
    trade(ex, "SPX", "alice", "bob", 100, 5)  # underlying only
    ex.step_round("SPX")
    assert ex.hedging_error("SPX", "alice") is None
    assert ex.hedging_error("SPX", "carol") is None


def test_hedging_error_before_the_first_move_is_zero_for_option_holders():
    ex = started()
    trade(ex, "SPX-C-100", "alice", "bob", 8, 10)
    assert ex.hedging_error("SPX", "alice") == 0


# --- The greeks toggle -------------------------------------------------------------------------


def test_greeks_start_hidden_and_the_admin_shows_and_hides_them():
    ex = Exchange()
    assert ex.greeks_shown is False
    assert isinstance(ex.show_greeks()[0], GreeksShown)
    assert ex.greeks_shown is True
    with pytest.raises(ValueError, match="already shown"):
        ex.show_greeks()
    assert isinstance(ex.hide_greeks()[0], GreeksHidden)
    assert ex.greeks_shown is False
    with pytest.raises(ValueError, match="already hidden"):
        ex.hide_greeks()


# --- Replay ------------------------------------------------------------------------------------


def test_replay_rebuilds_the_round_without_drawing_random_numbers():
    draws = [0.7, -1.2, 0.1] + [0.2] * 57
    ex = started(rng=ScriptedNormal(*draws))
    trade(ex, "SPX-C-100", "alice", "bob", 8, 10)
    ex.step_round("SPX")
    ex.show_greeks()
    trade(ex, "SPX", "bob", "alice", 100, 5)
    ex.halt_round("SPX")
    ex.resume_round("SPX")
    run_to_expiry(ex, draws[1:])

    saved = [to_dict(event) for event in ex.events]
    rebuilt = Exchange.replay([from_dict(data) for data in saved])  # its own rng is never used

    assert [to_dict(event) for event in rebuilt.events] == saved
    assert rebuilt.rounds["SPX"].fair_price == ex.rounds["SPX"].fair_price
    assert rebuilt.rounds["SPX"].status == "settled"
    assert rebuilt.greeks_shown is True
    assert rebuilt.hedging_error("SPX", "alice") == ex.hedging_error("SPX", "alice")
    for market_id in ex.markets:
        assert rebuilt.markets[market_id].settlement_value == \
            ex.markets[market_id].settlement_value
        assert rebuilt.pnl(market_id, "alice") == ex.pnl(market_id, "alice")


def test_round_settings_survive_the_json_log():
    ex = Exchange()
    ex.create_round(make_round())
    rebuilt = Exchange.replay([from_dict(to_dict(event)) for event in ex.events])
    assert rebuilt.rounds["SPX"].config == make_round()


def test_rounds_and_ordinary_markets_live_side_by_side():
    ex = started(rng=ScriptedNormal(0.5))
    open_market(ex, market_id="cars")
    trade(ex, "cars", "alice", "bob", 36, 2)
    ex.step_round("SPX")
    assert ex.position("cars", "alice") == 2
    assert ex.markets["cars"].status is MarketStatus.OPEN


def test_total_pnl_includes_the_rounds_markets():
    draws = [0.0] * 60
    ex = started(rng=ScriptedNormal(*draws), dollars_per_point=2, realized_vol=0)
    ex.take("SPX-C-100", "alice", BUY, 9, 10)  # from the house; settles at 0 with no move
    run_to_expiry(ex, draws)
    assert ex.markets["SPX-C-100"].settlement_value == 0
    assert ex.total_pnl("alice").total == pytest.approx(-9 * 10 * 2)


def test_changing_the_config_keeps_the_round_id():
    # Sanity check on the helper: RoundConfig is a frozen dataclass like MarketConfig.
    config = make_round()
    assert replace(config, minutes=5).round_id == "SPX"
