"""The delta-hedging round (SPEC Q160-Q185).

A round is one underlying whose fair price moves at random by itself, plus European calls and
puts on it that all expire when the round ends. Each is an ordinary Market (book, trades,
positions); this class adds what ties them together:

- the fair price, which takes one geometric Brownian motion step per second (each step is
  drawn by the server and logged as an UnderlyingMoved event, so replay never draws),
- the time left, counted in steps, so halting the round stops the clock too,
- Black-Scholes values and greeks at the implied vol, each trader's net delta and gamma,
- the hedging error: the average |net delta| each trader carried into each move.

Like Market, decide_* methods return events without changing anything, and apply(event) is the
only place state changes.
"""

import math
from dataclasses import dataclass

from .events import (
    MarketCreated,
    MarketHalted,
    MarketOpened,
    MarketResumed,
    MarketSettled,
    RoundHalted,
    RoundResumed,
    RoundSettled,
    RoundStarted,
    UnderlyingMoved,
)
from .house import HOUSE_ID
from .models import is_whole_number
from .options import Greeks, black_scholes

STEPS_PER_MINUTE = 60  # the underlying moves once a second (SPEC Q169)


@dataclass(frozen=True)
class RoundConfig:
    """The settings the admin chooses for a round (SPEC Q167)."""

    round_id: str  # also the underlying's market id, e.g. "SPX"
    title: str  # the underlying's name, e.g. "S&P"
    start_price: int
    tick_size: int  # for the underlying and every option
    realized_vol: float  # e.g. 0.3 = the log price at expiry has a 30% standard deviation
    implied_vol: float  # what the house and the greeks use
    minutes: int
    options: tuple  # (("call", 100), ("put", 95), ...): kind and strike
    max_position: int  # per market, as in any market
    dollars_per_point: float  # one for the whole round, so deltas add up
    house_half_spread: int  # the house's quote: fair +/- this, when nothing is happening
    house_size: int  # the house's size on each side


def option_market_id(round_id, kind, strike):
    """ "SPX", "call", 100 -> "SPX-C-100"."""
    return f"{round_id}-{kind[0].upper()}-{strike}"


def round_markets(config):
    """The MarketCreated events for a round: the underlying first, then each option."""
    common = dict(tick_size=config.tick_size, max_position=config.max_position,
                  forced_trade_size=1, forced_trade_seconds=1,  # unused: no Trade or Tighten
                  dollars_per_point=config.dollars_per_point, round_id=config.round_id)
    events = [MarketCreated(market_id=config.round_id, title=config.title, **common)]
    for kind, strike in config.options:
        events.append(MarketCreated(market_id=option_market_id(config.round_id, kind, strike),
                                    title=f"{config.title} {strike} {kind}", **common))
    return events


class DeltaRound:
    def __init__(self, config, markets):
        self.config = config
        self.markets = markets  # the Exchange's market_id -> Market (shared, not copied)
        self.status = "created"  # created -> running <-> halted -> settled
        self.fair_price = config.start_price
        self.steps_taken = 0
        self.last_draw = 0  # the last move's size in standard deviations (the house widens)
        self.history = []  # UnderlyingMoved events, oldest first (for the review chart)
        # One {trader_id: net delta} per move: what each trader carried into it (review chart).
        self.carried = []
        self.abs_delta_sums = {}  # trader_id -> sum of |net delta| carried into each move

    # --- Queries --------------------------------------------------------------------------

    @property
    def round_id(self):
        return self.config.round_id

    @property
    def underlying_id(self):
        return self.config.round_id

    @property
    def option_ids(self):
        return [option_market_id(self.round_id, kind, strike)
                for kind, strike in self.config.options]

    @property
    def market_ids(self):
        return [self.underlying_id, *self.option_ids]

    @property
    def total_steps(self):
        return self.config.minutes * STEPS_PER_MINUTE

    @property
    def time_left(self):
        """The fraction of the round still to go: 1 at the start, 0 at expiry."""
        return (self.total_steps - self.steps_taken) / self.total_steps

    def option_of(self, market_id):
        """(kind, strike) of an option market in this round."""
        for kind, strike in self.config.options:
            if option_market_id(self.round_id, kind, strike) == market_id:
                return kind, strike
        raise KeyError(market_id)

    def greeks(self, market_id):
        """Value, delta and gamma of one unit of this market at the fair price. Options use
        Black-Scholes at the implied vol (the vol everyone is told, SPEC Q175)."""
        return self.greeks_at(market_id, self.fair_price, self.steps_taken)

    def greeks_at(self, market_id, price, step):
        """The same, as they were after `step` moves with the underlying at `price` (the review
        chart draws an option's fair value over time from these)."""
        if market_id == self.underlying_id:
            return Greeks(value=price, delta=1, gamma=0)
        kind, strike = self.option_of(market_id)
        time_left = (self.total_steps - step) / self.total_steps
        return black_scholes(kind, price, strike, self.config.implied_vol, time_left)

    def net_delta(self, trader_id):
        """Underlying position + each option position times that option's delta."""
        return sum(self.markets[market_id].position(trader_id).size * self.greeks(market_id).delta
                   for market_id in self.market_ids)

    def net_gamma(self, trader_id):
        return sum(self.markets[market_id].position(trader_id).size * self.greeks(market_id).gamma
                   for market_id in self.option_ids)

    def held_an_option(self, trader_id):
        return any(trader_id in (trade.buyer_id, trade.seller_id)
                   for market_id in self.option_ids
                   for trade in self.markets[market_id].trades)

    def hedging_error(self, trader_id):
        """The average |net delta| carried into each move so far (0 = always hedged), or None
        for a trader who never held an option, so doing nothing doesn't score best (Q176)."""
        if not self.held_an_option(trader_id):
            return None
        if self.steps_taken == 0:
            return 0
        return self.abs_delta_sums.get(trader_id, 0) / self.steps_taken

    def traders(self):
        """Every student who has ever traded in this round's markets (not the house)."""
        return {trader_id for market_id in self.market_ids
                for trader_id in self.markets[market_id].positions if trader_id != HOUSE_ID}

    # --- Decide: command -> events ---------------------------------------------------------

    def decide_start(self):
        return [RoundStarted(round_id=self.round_id),
                *[MarketOpened(market_id=market_id) for market_id in self.market_ids]]

    def decide_halt(self):
        return [RoundHalted(round_id=self.round_id),
                *[MarketHalted(market_id=market_id) for market_id in self.market_ids]]

    def decide_resume(self):
        return [RoundResumed(round_id=self.round_id),
                *[MarketResumed(market_id=market_id) for market_id in self.market_ids]]

    def decide_step(self, rng):
        """One geometric Brownian motion step at the realized vol. Over the whole round the
        log price drifts by -vol^2/2 (so the expected price stays the same) with a standard
        deviation of vol. The last step also settles every market."""
        dt = 1 / self.total_steps
        vol = self.config.realized_vol
        draw = rng.gauss(0, 1)
        price = self.fair_price * math.exp(-vol ** 2 * dt / 2 + vol * math.sqrt(dt) * draw)
        step = self.steps_taken + 1
        events = [UnderlyingMoved(round_id=self.round_id, step=step, price=price, draw=draw)]
        if step == self.total_steps:
            events += self._settlement(price)
        return events

    def _settlement(self, final_price):
        """The underlying settles at the nearest tick; each option at its payoff from that."""
        tick = self.config.tick_size
        settle = round(final_price / tick) * tick
        events = [RoundSettled(round_id=self.round_id),
                  MarketSettled(market_id=self.underlying_id, settlement_value=settle)]
        for kind, strike in self.config.options:
            payoff = max(settle - strike, 0) if kind == "call" else max(strike - settle, 0)
            events.append(MarketSettled(market_id=option_market_id(self.round_id, kind, strike),
                                        settlement_value=payoff))
        return events

    # --- Apply: event -> new state ---------------------------------------------------------

    def apply(self, event):
        if isinstance(event, RoundStarted):
            self.status = "running"
        elif isinstance(event, RoundHalted):
            self.status = "halted"
        elif isinstance(event, RoundResumed):
            self.status = "running"
        elif isinstance(event, UnderlyingMoved):
            # Score the delta each trader carried into this move, before the price changes.
            carried = {trader_id: self.net_delta(trader_id) for trader_id in self.traders()}
            for trader_id, delta in carried.items():
                self.abs_delta_sums[trader_id] = self.abs_delta_sums.get(trader_id, 0) + abs(delta)
            self.carried.append(carried)
            self.fair_price = event.price
            self.steps_taken = event.step
            self.last_draw = event.draw
            self.history.append(event)
        elif isinstance(event, RoundSettled):
            self.status = "settled"


def check_round_settings(config):
    """Raise ValueError with the reason if any of a round's settings is bad."""
    round_id = config.round_id
    if not isinstance(round_id, str) or not round_id:
        raise ValueError("the round needs a name (its underlying's market id, e.g. SPX)")
    if not (round_id.isascii() and round_id.isalnum()):
        raise ValueError("the round's name must be letters and digits only")
    if not isinstance(config.title, str) or not config.title.strip():
        raise ValueError("the title can't be empty")

    tick = config.tick_size
    if not is_whole_number(tick) or tick < 1:
        raise ValueError("tick size must be a whole number of at least 1")
    if not is_whole_number(config.start_price) or config.start_price < 1:
        raise ValueError("the starting price must be a whole number of at least 1")
    if config.start_price % tick != 0:
        raise ValueError(f"the starting price must be a multiple of the tick size ({tick})")

    if not is_number(config.realized_vol) or config.realized_vol < 0:
        raise ValueError("the realized vol must be a number, 0 or more")
    if not is_number(config.implied_vol) or config.implied_vol <= 0:
        raise ValueError("the implied vol must be a number above 0")
    if not is_whole_number(config.minutes) or config.minutes < 1:
        raise ValueError("minutes must be a whole number of at least 1")

    if not config.options:
        raise ValueError("a round needs at least one option")
    seen = set()
    for option in config.options:
        kind, strike = option
        if kind not in ("call", "put"):
            raise ValueError(f"an option is a call or a put, not {kind!r}")
        if not is_whole_number(strike) or strike < 1:
            raise ValueError("a strike must be a whole number of at least 1")
        if strike % tick != 0:
            raise ValueError(f"strike {strike} must be a multiple of the tick size ({tick})")
        if (kind, strike) in seen:
            raise ValueError(f"the {strike} {kind} is listed twice")
        seen.add((kind, strike))

    if not is_whole_number(config.max_position) or config.max_position < 1:
        raise ValueError("max position must be a whole number of at least 1")
    if not is_number(config.dollars_per_point) or config.dollars_per_point <= 0:
        raise ValueError("$ per point must be a number above 0")
    half_spread = config.house_half_spread
    if not is_whole_number(half_spread) or half_spread < tick or half_spread % tick != 0:
        raise ValueError(f"the house's half-spread must be a multiple of the tick size ({tick}),"
                         " at least one tick")
    if not is_whole_number(config.house_size) or config.house_size < 1:
        raise ValueError("the house size must be a whole number of at least 1")


def is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))
