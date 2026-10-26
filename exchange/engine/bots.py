"""Maker and taker bots (SPEC Q209-Q218): what each one decides to do.

Bots are ordinary traders; the server places their orders for them, once a second at most. They
use only what every student can see: the book, the last trade, and their own position. They
never know the true value, so they give nothing away.

- A maker keeps one bid and one offer in each open market, a few ticks either side of the
  middle of other people's best bid and offer (or of the last trade). It shades its quote away
  from the position it holds, so it doesn't pile one up.
- A taker now and then buys at the best offer or sells at the best bid: noise flow, which gives
  makers and students something to trade against.

These functions only look at a market; Exchange.step_bots turns their answers into orders.
"""

import math

from .models import Side

MAKER_NAMES = ("Maker Bot 1", "Maker Bot 2")
TAKER_NAMES = ("Taker Bot 1", "Taker Bot 2", "Taker Bot 3")
NAMES = {"maker": MAKER_NAMES, "taker": TAKER_NAMES}

# On each step (one a second) a bot acts with this chance, so a maker re-quotes about every 3
# seconds and a taker trades about every 4, at random moments.
CHANCE_TO_ACT = {"maker": 1 / 3, "taker": 1 / 4}
MAKER_HALF_WIDTH_TICKS = (1, 3)  # each quote is fair +/- a random 1 to 3 ticks
MAKER_SIZE = (1, 5)
TAKER_SIZE = (1, 3)
SKEW_STEPS = 5  # a maker shades its quote 1 tick for every fifth of max position it holds


def maker_quote(market, bot_id, half_width_ticks):
    """(bid, ask) for a maker in this market, or None when there is no public price yet.

    The middle is halfway between the best bid and offer of everyone but this bot, or the last
    trade when the book isn't two-sided. The bid and offer are half_width_ticks either side,
    rounded outwards to the tick, shaded away from the bot's position, and kept inside the
    market's price limits.
    """
    config = market.config
    tick = config.tick_size
    best_bid = best_price_of_others(market, Side.BUY, bot_id)
    best_ask = best_price_of_others(market, Side.SELL, bot_id)
    if best_bid is not None and best_ask is not None:
        middle = (best_bid + best_ask) / 2
    elif market.last_price is not None:
        middle = market.last_price
    else:
        return None

    position = market.position(bot_id).size
    skew_ticks = int(position / (config.max_position / SKEW_STEPS))  # towards zero
    middle -= skew_ticks * tick
    bid = math.floor((middle - half_width_ticks * tick) / tick) * tick
    ask = math.ceil((middle + half_width_ticks * tick) / tick) * tick

    if config.min_price is not None:
        bid, ask = max(bid, config.min_price), max(ask, config.min_price)
    if config.max_price is not None:
        bid, ask = min(bid, config.max_price), min(ask, config.max_price)
    if bid >= ask:
        return None
    return bid, ask


def best_price_of_others(market, side, bot_id):
    """The best bid (side BUY) or offer (SELL) in the book, not counting this bot's orders."""
    for price, orders in market.book.levels(side):
        if any(order.trader_id != bot_id for order in orders):
            return price
    return None


def taker_price(market, side):
    """The price a taker buying (or selling) would trade at: the best offer (or bid)."""
    return market.book.best_ask() if side is Side.BUY else market.book.best_bid()
