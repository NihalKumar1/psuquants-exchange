"""Black-Scholes values and greeks for the delta-hedging round (SPEC Q164, Q168, Q175).

No interest rate and no dividends. The vol is "over the whole round" and the time is the
fraction of the round left, so vol=0.2 with time_left=1 is the textbook 20% vol, 1 year.
"""

import math

import pytest

from exchange.engine.options import black_scholes


def test_textbook_at_the_money_call():
    # S = K = 100, 20% vol, 1 year, r = 0: d1 = 0.1, d2 = -0.1
    greeks = black_scholes("call", 100, 100, 0.2, 1.0)
    assert greeks.value == pytest.approx(7.9656, abs=1e-4)
    assert greeks.delta == pytest.approx(0.5398, abs=1e-4)  # N(0.1)
    assert greeks.gamma == pytest.approx(0.019848, abs=1e-6)  # N'(0.1) / (100 * 0.2)


def test_put_call_parity_with_no_interest_rate():
    call = black_scholes("call", 110, 100, 0.3, 0.5)
    put = black_scholes("put", 110, 100, 0.3, 0.5)
    assert call.value - put.value == pytest.approx(110 - 100)
    assert call.delta - put.delta == pytest.approx(1)
    assert call.gamma == pytest.approx(put.gamma)


def test_less_time_or_vol_is_worth_less():
    full = black_scholes("call", 100, 100, 0.2, 1.0).value
    half_time = black_scholes("call", 100, 100, 0.2, 0.5).value
    half_vol = black_scholes("call", 100, 100, 0.1, 1.0).value
    assert half_time < full
    assert half_vol < full
    # at the money the value is about 0.4 * S * vol * sqrt(time)
    assert half_time == pytest.approx(0.4 * 100 * 0.2 * math.sqrt(0.5), rel=0.01)


def test_deep_in_and_out_of_the_money():
    deep_in = black_scholes("call", 200, 100, 0.2, 1.0)
    deep_out = black_scholes("call", 50, 100, 0.2, 1.0)
    assert deep_in.delta == pytest.approx(1, abs=1e-3)
    assert deep_in.value == pytest.approx(100, abs=0.01)
    assert deep_out.delta == pytest.approx(0, abs=1e-3)
    assert black_scholes("put", 200, 100, 0.2, 1.0).delta == pytest.approx(0, abs=1e-3)
    assert black_scholes("put", 50, 100, 0.2, 1.0).delta == pytest.approx(-1, abs=1e-3)


@pytest.mark.parametrize("price, call_delta", [(105, 1), (95, 0), (100, 0.5)])
def test_at_expiry_value_is_the_payoff_and_delta_is_a_step(price, call_delta):
    call = black_scholes("call", price, 100, 0.2, 0)
    put = black_scholes("put", price, 100, 0.2, 0)
    assert call.value == max(price - 100, 0)
    assert put.value == max(100 - price, 0)
    assert call.delta == call_delta
    assert put.delta == call_delta - 1
    assert call.gamma == put.gamma == 0


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        black_scholes("straddle", 100, 100, 0.2, 1.0)
