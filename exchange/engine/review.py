"""The numbers on the review screen: one trader's fills in one market, in order.

For each fill: the trader's position, realized PnL and MTM PnL right after it, and once the
market is settled, the fill's edge against the true value.
"""

from dataclasses import dataclass

from .events import TradeExecuted
from .models import Side
from .positions import Position


@dataclass(frozen=True)
class FillRow:
    trade: TradeExecuted
    side: Side  # the reviewed trader's side of this trade
    counterparty_id: str
    position: int  # after this fill
    realized: float  # after this fill (FIFO)
    unrealized: float  # MTM after this fill, at the mark right after it
    edge: int | None  # (settlement - price) x signed size; None until the market settles


def mark_at(price_history, seq):
    """The mark right after event number `seq`: the latest PricePoint at or before it."""
    mark = None
    for point in price_history:
        if point.seq > seq:
            break
        mark = point.mark
    return mark


def trader_fills(market, trader_id):
    """One FillRow per trade this trader was in (buyer or seller), oldest first."""
    position = Position()  # rebuilt fill by fill, so each row shows the numbers at that moment
    rows = []
    for trade in market.trades:
        if trade.buyer_id == trader_id:
            side, counterparty_id = Side.BUY, trade.seller_id
        elif trade.seller_id == trader_id:
            side, counterparty_id = Side.SELL, trade.buyer_id
        else:
            continue
        position.apply_fill(side, trade.price, trade.size)
        rows.append(FillRow(
            trade=trade,
            side=side,
            counterparty_id=counterparty_id,
            position=position.size,
            realized=position.realized,
            unrealized=position.unrealized(mark_at(market.price_history, trade.seq)),
            edge=edge(market.settlement_value, side, trade.price, trade.size),
        ))
    return rows


def edge(settlement_value, side, price, size):
    """What a fill was worth against the true value: (settlement - price) x signed size.
    A buy below the true value and a sell above it both have positive edge."""
    if settlement_value is None:
        return None
    signed_size = size if side is Side.BUY else -size
    return (settlement_value - price) * signed_size


def total_edge(market, rows):
    """The edge of all the rows added up, or None if the market hasn't settled."""
    if market.settlement_value is None:
        return None
    return sum(row.edge for row in rows)
