"""Basic building blocks: sides, market status, market settings, and orders."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class MarketStatus(Enum):
    """Where a market is in its life (see SPEC.md "Markets"):

        CREATED -> AUCTION -> MM_QUOTING -> FORCED_TRADE -> OPEN <-> HALTED -> SETTLED

    The admin may also go straight from CREATED to OPEN (skipping Trade or Tighten), and settle
    from OPEN or HALTED. If the MM is kicked while quoting and nobody else submitted a width,
    the market goes back from MM_QUOTING to CREATED.
    Each admin command in exchange.py lists the statuses it works from.
    """

    CREATED = "created"
    AUCTION = "auction"
    MM_QUOTING = "mm_quoting"
    FORCED_TRADE = "forced_trade"
    OPEN = "open"
    HALTED = "halted"
    SETTLED = "settled"


@dataclass(frozen=True)
class MarketConfig:
    """The settings the admin chooses for one market."""

    market_id: str
    title: str
    tick_size: int
    max_position: int
    forced_trade_size: int
    forced_trade_seconds: int


@dataclass(eq=False)  # eq=False: two orders are the same only if they are the same object
class Order:
    order_id: int
    market_id: str
    trader_id: str
    kind: str  # "limit" or "take"
    side: Side
    price: int
    size: int  # size accepted (after any clipping)
    remaining: int
    cancelled: bool = False


@dataclass(frozen=True)
class PricePoint:
    """The market's mark and last price as they were right after event number `seq`.
    A market keeps one each time either of them changes, for the review chart."""

    seq: int
    ts: datetime
    mark: float | None
    last_price: int | None


def is_whole_number(value):
    """True for ints like 5 or -3; False for 2.5, 5.0 and True/False."""
    return isinstance(value, int) and not isinstance(value, bool)
