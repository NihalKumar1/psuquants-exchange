"""Positions and PnL.

Realized PnL uses FIFO: a position is a list of lots (price, size), oldest first. A trade that
reduces the position closes the oldest lots first and locks in their profit or loss.
"""

from collections import deque
from dataclasses import dataclass

from .models import Side


@dataclass
class Lot:
    price: int
    size: int  # positive = long, negative = short


class Position:
    def __init__(self):
        self.lots = deque()  # oldest first; all lots are on the same side
        self.realized = 0

    @property
    def size(self):
        return sum(lot.size for lot in self.lots)

    def apply_fill(self, side, price, size):
        signed = size if side is Side.BUY else -size

        # Close the oldest lots on the opposite side first.
        while signed != 0 and self.lots and (self.lots[0].size > 0) != (signed > 0):
            lot = self.lots[0]
            closed = min(abs(signed), abs(lot.size))
            direction = 1 if lot.size > 0 else -1  # +1 closing a long, -1 closing a short
            self.realized += (price - lot.price) * closed * direction
            lot.size -= closed * direction
            signed += closed * direction
            if lot.size == 0:
                self.lots.popleft()

        # Anything left opens (or adds to) a position at this price.
        if signed != 0:
            self.lots.append(Lot(price, signed))

    def unrealized(self, mark):
        """Mark-to-market PnL of the open lots. Zero when flat (even if there is no mark)."""
        if not self.lots:
            return 0
        return sum((mark - lot.price) * lot.size for lot in self.lots)


@dataclass(frozen=True)
class PnL:
    realized: float
    unrealized: float

    @property
    def total(self):
        return self.realized + self.unrealized

    def __add__(self, other):
        return PnL(self.realized + other.realized, self.unrealized + other.unrealized)


def mark_price(best_bid, best_ask, last_price, settlement_value=None):
    """The price used to value open positions.

    Settlement value once settled; otherwise the mid of best bid and best offer; if either side
    is empty, the last traded price; if nothing has traded yet, None (shown as blank).
    """
    if settlement_value is not None:
        return settlement_value
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / 2
    return last_price
