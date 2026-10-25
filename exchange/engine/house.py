"""The house market maker's pricing rule, for delta-hedging rounds (SPEC Q163, Q172-Q173).

The house keeps one bid and one offer in every market of a running round, so students always
have someone to hedge with. Its quote is fair +/- a half-spread that widens when the market is
moving fast and when the house is carrying a big position, and it shades both prices away from
its position (a house that is short raises its prices to buy back). Exchange decides when to
re-quote; this file only decides the prices.
"""

import math

HOUSE_ID = "house"  # the house's trader id (student ids look like "t1", so it can't clash)
HOUSE_NAME = "House"
SIZES_TO_DOUBLE = 4  # a position of 4 quote sizes doubles the spread and shades it one tick


def house_quote(fair, half_spread, size, tick, position, speed):
    """(bid, ask) for the house. `speed` is the last move in standard deviations (its random
    draw): an ordinary move (under 1) doesn't widen, a 2x move doubles the half-spread. The bid
    is None when it would be 0 or less (nobody should sell an option for nothing)."""
    speed_factor = max(1, abs(speed))
    inventory_factor = 1 + abs(position) / (SIZES_TO_DOUBLE * size)
    half = half_spread * speed_factor * inventory_factor

    # Round out to the tick, so the spread is never narrower than the formula says.
    bid = math.floor((fair - half) / tick) * tick
    ask = math.ceil((fair + half) / tick) * tick

    # Shade away from the position: one tick per 4 quote sizes held (down when long).
    shift = int(position / (SIZES_TO_DOUBLE * size)) * tick
    bid -= shift
    ask = max(ask - shift, tick)
    return (bid if bid > 0 else None), ask
