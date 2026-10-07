"""The export: the whole game as one zip, for keeping after the meeting.

    trades.csv   one row per trade
    orders.csv   one row per accepted order, and how it ended
    events.csv   one row per event in the log (rejections and info drops included)
    events.json  the full event log

Times are US Eastern (the meeting's clock). Traders appear by their current names everywhere,
also in events.json, where each trader id is replaced by the name.
The CSVs start with a byte-order mark so Excel reads names with accents correctly.
"""

import csv
import io
import json
import zipfile
from datetime import timezone
from zoneinfo import ZoneInfo

from exchange.engine import (
    MarketStatus,
    OrderAccepted,
    OrderCancelled,
    TradeExecuted,
    to_dict,
)

EASTERN = ZoneInfo("America/New_York")
TRADER_ID_FIELDS = ("trader_id", "buyer_id", "seller_id")  # replaced by names in the export


def export_zip(exchange):
    """The zip file's bytes."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("trades.csv", to_csv(*trades_table(exchange)))
        archive.writestr("orders.csv", to_csv(*orders_table(exchange)))
        archive.writestr("events.csv", to_csv(*events_table(exchange)))
        archive.writestr("events.json", json.dumps(events_json(exchange), indent=2))
    return data.getvalue()


def export_filename(now):
    """e.g. "psuquants-game-2026-10-06-1931.zip" (Eastern time)."""
    return f"psuquants-game-{eastern(now):%Y-%m-%d-%H%M}.zip"


# --- Times and names ------------------------------------------------------------------------


def eastern(ts):
    """A timestamp in US Eastern time. One with no time zone is taken to be UTC (the tests'
    clock); the server's own clock is always UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(EASTERN)


def csv_time(ts):
    """e.g. "2026-10-06 19:31:04.123", which Excel reads as a date and time."""
    local = eastern(ts)
    return f"{local:%Y-%m-%d %H:%M:%S}.{local.microsecond // 1000:03d}"


def name_of(exchange, trader_id):
    return exchange.traders.get(trader_id, trader_id)


def title_of(exchange, market_id):
    """The market's (current) title, or "" for room-wide events like info drops."""
    market = exchange.markets.get(market_id)
    return market.config.title if market else ""


def to_csv(header, rows):
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(header)
    writer.writerows(rows)
    return text.getvalue().encode("utf-8-sig")


# --- The tables -----------------------------------------------------------------------------


def trades_table(exchange):
    header = ["trade_id", "time", "market_id", "market", "price", "size", "buyer", "seller",
              "aggressor", "forced"]
    rows = [
        [trade.trade_id, csv_time(trade.ts), trade.market_id,
         title_of(exchange, trade.market_id), trade.price, trade.size,
         name_of(exchange, trade.buyer_id), name_of(exchange, trade.seller_id),
         trade.aggressor_side.value, "yes" if trade.forced else "no"]
        for trade in exchange.events if isinstance(trade, TradeExecuted)
    ]
    return header, rows


def orders_table(exchange):
    """Walk the log: each accepted order starts a row; its trades and cancel fill it in."""
    orders = {}  # order_id -> the facts so far
    for event in exchange.events:
        if isinstance(event, OrderAccepted):
            orders[event.order_id] = {"accepted": event, "filled": 0, "cancelled": None}
        elif isinstance(event, TradeExecuted):
            for order_id in (event.buy_order_id, event.sell_order_id):
                if order_id is not None:  # forced trades have no orders behind them
                    orders[order_id]["filled"] += event.size
        elif isinstance(event, OrderCancelled):
            orders[event.order_id]["cancelled"] = event

    header = ["order_id", "time", "market_id", "market", "trader", "kind", "side", "price",
              "requested_size", "accepted_size", "filled", "status", "cancelled_at",
              "cancel_reason"]
    rows = []
    for facts in orders.values():
        order, cancelled = facts["accepted"], facts["cancelled"]
        rows.append([
            order.order_id, csv_time(order.ts), order.market_id,
            title_of(exchange, order.market_id), name_of(exchange, order.trader_id),
            order.kind, order.side.value, order.price, order.requested_size, order.size,
            facts["filled"], order_status(exchange, order, facts["filled"], cancelled),
            csv_time(cancelled.ts) if cancelled else "",
            cancelled.reason if cancelled else "",
        ])
    return header, rows


def order_status(exchange, order, filled, cancelled):
    """How the order ended: filled, cancelled, still resting, or frozen in a settled market."""
    if cancelled is not None:
        return "cancelled"
    if filled == order.size:
        return "filled"
    if exchange.markets[order.market_id].status is MarketStatus.SETTLED:
        return "frozen"
    return "resting"


def events_table(exchange):
    """One row per event. The columns every event shares come first; the rest of its fields
    go in "details" as "key=value; key=value"."""
    header = ["seq", "time", "type", "market_id", "market", "trader", "details"]
    rows = []
    for event in exchange.events:
        data = with_names(exchange, to_dict(event))
        details = "; ".join(
            f"{key}={'' if value is None else value}"
            for key, value in data.items()
            if key not in ("seq", "ts", "type", "market_id", "trader_id")
        )
        rows.append([event.seq, csv_time(event.ts), data["type"], event.market_id or "",
                     title_of(exchange, event.market_id), data.get("trader_id", ""), details])
    return header, rows


def events_json(exchange):
    """The full log as a list of events, with names for trader ids and Eastern times."""
    events = []
    for event in exchange.events:
        data = with_names(exchange, to_dict(event))
        data["ts"] = eastern(event.ts).isoformat()
        events.append(data)
    return events


def with_names(exchange, data):
    """An event dict (from to_dict) with each trader id replaced by the trader's name."""
    return {
        key: name_of(exchange, value) if key in TRADER_ID_FIELDS else value
        for key, value in data.items()
    }
