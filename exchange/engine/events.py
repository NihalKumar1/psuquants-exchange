"""The events that make up the append-only log.

Every change to the exchange is one of these. The current state is whatever you get by applying
them in order, so the log doubles as the trade tape, the review timeline, and the export.
"""

from dataclasses import dataclass, fields
from datetime import datetime

from .models import Side


@dataclass(frozen=True, kw_only=True)
class Event:
    seq: int = 0  # position in the log (1, 2, 3, ...), set by the Exchange
    ts: datetime | None = None  # when it happened, set by the Exchange
    market_id: str | None = None


# --- Admin ----------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class MarketCreated(Event):
    title: str
    tick_size: int
    max_position: int
    forced_trade_size: int
    forced_trade_seconds: int


@dataclass(frozen=True, kw_only=True)
class MarketEdited(Event):
    """New settings for a market that hasn't opened yet (all five, even unchanged ones)."""

    title: str
    tick_size: int
    max_position: int
    forced_trade_size: int
    forced_trade_seconds: int


@dataclass(frozen=True, kw_only=True)
class MarketOpened(Event):
    """The market starts continuous trading."""


@dataclass(frozen=True, kw_only=True)
class MarketHalted(Event):
    """Trading stops. Resting orders stay; traders may still cancel them."""


@dataclass(frozen=True, kw_only=True)
class MarketResumed(Event):
    """Trading starts again after a halt, with the book as it was."""


@dataclass(frozen=True, kw_only=True)
class MarketSettled(Event):
    """The true value is revealed. It becomes the mark, which gives everyone's final PnL.
    Resting orders stay in the book but can never trade or be cancelled."""

    settlement_value: int


# Trade or Tighten: CREATED -> AUCTION -> MM_QUOTING -> FORCED_TRADE -> OPEN


@dataclass(frozen=True, kw_only=True)
class AuctionStarted(Event):
    """Trade or Tighten begins: traders may now submit widths."""


@dataclass(frozen=True, kw_only=True)
class MarketMakerChosen(Event):
    """The narrowest width holder becomes the market maker and must quote at that width or
    tighter. Happens when the admin closes the auction, or when a kicked MM is replaced."""

    trader_id: str
    width: int


@dataclass(frozen=True, kw_only=True)
class TradeOrTightenCancelled(Event):
    """The market goes back to CREATED, forgetting its widths and MM. This only happens when the
    MM is kicked and no one else submitted a width."""

    reason: str


@dataclass(frozen=True, kw_only=True)
class ForcedTradeStarted(Event):
    """The forced-trade window opens. It ends forced_trade_seconds after this event's ts."""


@dataclass(frozen=True, kw_only=True)
class ForcedTradeEnded(Event):
    """The window closed, by the timer or by the admin ("End now"). The events that follow are
    the random sides (SideAssigned), the forced trades, and MarketOpened."""

    ended_by: str  # "timer" or "admin"


@dataclass(frozen=True, kw_only=True)
class SideAssigned(Event):
    """A trader who hadn't chosen when the window closed got this side by a coin flip."""

    trader_id: str
    side: Side


@dataclass(frozen=True, kw_only=True)
class InfoDropMarked(Event):
    """The instructor gave out information (out loud or on a slide). For the review timeline.
    Info drops belong to the whole room, so market_id is None."""

    note: str


# --- Traders --------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TraderJoined(Event):
    """A new trader joined the room. The id never changes; the name is what everyone sees."""

    trader_id: str
    name: str


@dataclass(frozen=True, kw_only=True)
class TraderKicked(Event):
    """The admin removed a trader for the rest of the game. Their position stays."""

    trader_id: str


@dataclass(frozen=True, kw_only=True)
class TraderRenamed(Event):
    trader_id: str
    old_name: str
    new_name: str


@dataclass(frozen=True, kw_only=True)
class JoiningLocked(Event):
    """No new traders may join (existing traders can still get back in)."""


@dataclass(frozen=True, kw_only=True)
class JoiningUnlocked(Event):
    pass


# --- Trade or Tighten (the traders' part; the admin's part is above) ------------------------


@dataclass(frozen=True, kw_only=True)
class WidthSubmitted(Event):
    """ "I'll make it 2,000 wide." Strictly narrower than the best width so far."""

    trader_id: str
    width: int


@dataclass(frozen=True, kw_only=True)
class WidthsWithdrawn(Event):
    """A kicked trader's widths stop counting, so the best width may get wider again."""

    trader_id: str


@dataclass(frozen=True, kw_only=True)
class MarketMakerQuoted(Event):
    """The MM's bid and offer for the forced trade. The first quote is final."""

    trader_id: str
    bid: int
    ask: int


@dataclass(frozen=True, kw_only=True)
class SideChosen(Event):
    """A trader's choice in the forced trade: buy at the MM's offer or sell at the MM's bid.
    A later choice replaces an earlier one."""

    trader_id: str
    side: Side


# --- Orders and trades ----------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class OrderAccepted(Event):
    order_id: int
    trader_id: str
    kind: str  # "limit" or "take"
    side: Side
    price: int
    requested_size: int
    size: int  # smaller than requested_size if clipped by max position


@dataclass(frozen=True, kw_only=True)
class TradeExecuted(Event):
    trade_id: int
    price: int
    size: int
    buyer_id: str
    seller_id: str
    buy_order_id: int | None  # None for forced trades, which have no orders behind them
    sell_order_id: int | None
    aggressor_side: Side  # the side of the incoming order (or forced-trade choice)
    forced: bool = False  # True for the forced trades that open a Trade or Tighten market


@dataclass(frozen=True, kw_only=True)
class OrderRested(Event):
    """The unfilled part of a limit order joins the back of the line at its price."""

    order_id: int


@dataclass(frozen=True, kw_only=True)
class OrderCancelled(Event):
    order_id: int
    reason: str


@dataclass(frozen=True, kw_only=True)
class Rejected(Event):
    """A trader's command that was refused. Kept in the log so the review can show it."""

    trader_id: str
    command: str
    reason: str


# --- JSON -----------------------------------------------------------------------------------

EVENT_TYPES = {
    cls.__name__: cls
    for cls in (
        MarketCreated,
        MarketEdited,
        MarketOpened,
        MarketHalted,
        MarketResumed,
        MarketSettled,
        AuctionStarted,
        MarketMakerChosen,
        TradeOrTightenCancelled,
        ForcedTradeStarted,
        ForcedTradeEnded,
        SideAssigned,
        InfoDropMarked,
        TraderJoined,
        TraderKicked,
        TraderRenamed,
        JoiningLocked,
        JoiningUnlocked,
        WidthSubmitted,
        WidthsWithdrawn,
        MarketMakerQuoted,
        SideChosen,
        OrderAccepted,
        TradeExecuted,
        OrderRested,
        OrderCancelled,
        Rejected,
    )
}


def to_dict(event):
    """Turn an event into plain JSON-friendly values."""
    data = {"type": type(event).__name__}
    for field in fields(event):
        value = getattr(event, field.name)
        if isinstance(value, Side):
            value = value.value
        elif isinstance(value, datetime):
            value = value.isoformat()
        data[field.name] = value
    return data


def from_dict(data):
    """The reverse of to_dict."""
    data = dict(data)
    event_class = EVENT_TYPES[data.pop("type")]
    for name in ("side", "aggressor_side"):
        if name in data:
            data[name] = Side(data[name])
    if data.get("ts") is not None:
        data["ts"] = datetime.fromisoformat(data["ts"])
    return event_class(**data)
