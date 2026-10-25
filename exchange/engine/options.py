"""Black-Scholes option values and greeks, for the delta-hedging round (SPEC Q164, Q168, Q175).

No interest rate and no dividends. `vol` is the volatility over the whole round and `time_left`
is the fraction of the round still to go (1 at the start, 0 at expiry), so the usual
vol * sqrt(years) becomes vol * sqrt(time_left).
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Greeks:
    value: float  # what the option is worth now
    delta: float  # how much the value moves per 1 point of the underlying
    gamma: float  # how much delta moves per 1 point of the underlying


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def normal_pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def black_scholes(kind, price, strike, vol, time_left):
    """Value, delta and gamma of one "call" or "put" on the underlying at `price`."""
    if kind not in ("call", "put"):
        raise ValueError(f"an option is a call or a put, not {kind!r}")

    spread = vol * math.sqrt(time_left)  # the standard deviation of the log price at expiry
    if spread == 0:
        # At expiry: worth its payoff, and delta jumps from 0 to 1 at the strike.
        if kind == "call":
            value = max(price - strike, 0)
        else:
            value = max(strike - price, 0)
        call_delta = 1 if price > strike else 0 if price < strike else 0.5
        delta = call_delta if kind == "call" else call_delta - 1
        return Greeks(value=value, delta=delta, gamma=0)

    d1 = (math.log(price / strike) + spread ** 2 / 2) / spread
    d2 = d1 - spread
    call_value = price * normal_cdf(d1) - strike * normal_cdf(d2)
    call_delta = normal_cdf(d1)
    gamma = normal_pdf(d1) / (price * spread)
    if kind == "call":
        return Greeks(value=call_value, delta=call_delta, gamma=gamma)
    # Put-call parity with no interest rate: put = call - (price - strike).
    return Greeks(value=call_value - price + strike, delta=call_delta - 1, gamma=gamma)
