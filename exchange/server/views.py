"""Turn engine state into the plain dicts sent to traders' browsers as JSON.

The engine knows traders by id ("t1"); everything shown on screen uses their names.

Four kinds of view:
- public (market_view, trade_view): the same for everyone, so no one's PnL is in them;
- private (trader_view, total_view): one trader's own orders and PnL, sent only to them;
- admin (admin_message): everyone's positions and PnL, sent only to the admin page;
- review (review_state_message): one trader's fills and PnL for the projected review page,
  which needs the admin password.
"""

from exchange.engine import MarketStatus, Side, TradeExecuted
from exchange.engine.review import total_edge, trader_fills

BOOK_DEPTH = 10        # price levels shown on each side of the book...
SHORT_BOOK_DEPTH = 5   # ...or this many when several markets share the trader's screen
TRADE_OR_TIGHTEN = (MarketStatus.AUCTION, MarketStatus.MM_QUOTING, MarketStatus.FORCED_TRADE)
# Markets that get their own column on the trader page (not CREATED, not SETTLED).
RUNNING = (*TRADE_OR_TIGHTEN, MarketStatus.OPEN, MarketStatus.HALTED)


def name_of(exchange, trader_id):
    return exchange.traders.get(trader_id, trader_id)


# --- Which markets the trader page shows ----------------------------------------------------
# Markets are listed in the order they were created (oldest first), which is the order of
# exchange.markets.


def running_market_ids(exchange):
    """The markets that get a column on the trader page, side by side."""
    return [market_id for market_id, market in exchange.markets.items()
            if market.status in RUNNING]


def started_market_ids(exchange):
    """The markets in the positions table and the trade tape: every one that has started,
    settled ones included."""
    return [market_id for market_id, market in exchange.markets.items()
            if market.status is not MarketStatus.CREATED]


def book_depth(exchange):
    """The full book when one market has the screen to itself, a shorter one when several share it."""
    return BOOK_DEPTH if len(running_market_ids(exchange)) <= 1 else SHORT_BOOK_DEPTH


# --- Public ---------------------------------------------------------------------------------


def market_view(exchange, market_id, depth=BOOK_DEPTH):
    market = exchange.markets[market_id]
    return {
        "market_id": market_id,
        "title": market.config.title,
        "tick_size": market.config.tick_size,
        "max_position": market.config.max_position,
        "status": market.status.value,
        "bids": levels_view(exchange, market, Side.BUY, depth),
        "asks": levels_view(exchange, market, Side.SELL, depth),
        "best_bid": market.book.best_bid(),
        "best_ask": market.book.best_ask(),
        "last_price": market.last_price,
        "mark": market.mark(),
        "settlement_value": market.settlement_value,
        # Everyone's position is public; everyone who joined is listed, even at 0.
        "positions": [
            {"trader_id": trader_id, "name": name, "position": market.position(trader_id).size}
            for trader_id, name in exchange.traders.items()
        ],
        "tot": tot_view(exchange, market),
    }


def tot_view(exchange, market):
    """The public side of Trade or Tighten, or None when the market isn't in it.

    Values that don't apply yet are None (e.g. the MM's bid before they quote). No one's
    forced-trade choice is in here: each trader sees only their own (trader_view).
    """
    if market.status not in TRADE_OR_TIGHTEN:
        return None
    best = market.best_width()
    mm_id = market.mm_id if market.status is not MarketStatus.AUCTION else None
    return {
        "best_width": best[1] if best else None,
        "best_holder": name_of(exchange, best[0]) if best else None,
        "mm": name_of(exchange, mm_id) if mm_id else None,
        "width": market.mm_width if mm_id else None,
        "bid": market.mm_bid if mm_id else None,
        "ask": market.mm_ask if mm_id else None,
        "forced_trade_size": market.config.forced_trade_size,
        "seconds_left": exchange.forced_trade_seconds_left(market.market_id),
    }


def levels_view(exchange, market, side, depth):
    """The best `depth` price levels, each with its orders in time priority."""
    return [
        {
            "price": price,
            "orders": [
                {"order_id": order.order_id, "trader_id": order.trader_id,
                 "name": name_of(exchange, order.trader_id), "size": order.remaining}
                for order in orders
            ],
        }
        for price, orders in market.book.levels(side)[:depth]
    ]


def trade_view(exchange, trade):
    return {
        "trade_id": trade.trade_id,
        "market_id": trade.market_id,
        "time": trade.ts.isoformat(),
        "price": trade.price,
        "size": trade.size,
        "buyer": name_of(exchange, trade.buyer_id),
        "seller": name_of(exchange, trade.seller_id),
        "aggressor_side": trade.aggressor_side.value,
        "forced": trade.forced,
    }


# --- Private --------------------------------------------------------------------------------


def trader_view(exchange, market_id, trader_id):
    """One trader's resting orders, position and PnL in one market, plus whether they are the
    market maker and their own forced-trade choice (if any)."""
    market = exchange.markets[market_id]
    choice = market.choices.get(trader_id)
    return {
        "orders": [
            {"order_id": order.order_id, "side": order.side.value,
             "price": order.price, "size": order.remaining}
            for order in market.book.all_orders()
            if order.trader_id == trader_id
        ],
        **position_view(market, trader_id),
        "is_mm": market.mm_id == trader_id,
        "my_choice": choice.value if choice else None,
    }


def position_view(market, trader_id):
    """One trader's position and PnL in one market."""
    pnl = market.pnl(trader_id)
    return {
        "position": market.position(trader_id).size,
        "realized": pnl.realized,
        "unrealized": pnl.unrealized,
        "total": pnl.total,
    }


def total_view(exchange, trader_id):
    """One trader's PnL added up over every market."""
    pnl = exchange.total_pnl(trader_id)
    return {"realized": pnl.realized, "unrealized": pnl.unrealized, "total": pnl.total}


# --- Whole messages -------------------------------------------------------------------------


def snapshot_message(exchange, trader_id):
    """Everything a trader's screen needs, sent once when they (re)connect."""
    return {
        "type": "snapshot",
        **state_for(exchange, trader_id),
        "tape": {
            market_id: [trade_view(exchange, trade) for trade in market.trades]
            for market_id, market in exchange.markets.items()
        },
    }


def update_message(exchange, trader_id, events):
    """Sent after every command: the current state plus only the trades that just happened.

    The full tape is only in the snapshot, so updates stay small as the tape grows.
    """
    return {
        "type": "update",
        **state_for(exchange, trader_id),
        "new_trades": [
            trade_view(exchange, event) for event in events if isinstance(event, TradeExecuted)
        ],
    }


def state_for(exchange, trader_id):
    depth = book_depth(exchange)
    return {
        "name": exchange.traders[trader_id],  # can change if the admin renames them
        # What the page lays out: one column per running market, and the markets that get a
        # column in the positions table. The page draws exactly this, so the rules live here.
        "columns": running_market_ids(exchange),
        "table_markets": started_market_ids(exchange),
        "book_depth": depth,
        "markets": {
            market_id: market_view(exchange, market_id, depth) for market_id in exchange.markets
        },
        "me": {
            market_id: trader_view(exchange, market_id, trader_id)
            for market_id in exchange.markets
        },
        "total": total_view(exchange, trader_id),
    }


# --- Admin ----------------------------------------------------------------------------------


def admin_message(room):
    """Everything the admin page shows, sent after every change. Only admins get this: it has
    everyone's PnL."""
    exchange = room.exchange
    return {
        "type": "admin_state",
        "room_code": room.code,
        "joining_locked": exchange.joining_locked,
        "unexported": room.has_unexported_changes(),  # Reset warns if this is True
        "info_drops": [
            {"time": drop.ts.isoformat(), "note": drop.note} for drop in exchange.info_drops
        ],
        "markets": {
            market_id: {
                **market_view(exchange, market_id),
                "forced_trade_size": market.config.forced_trade_size,
                "forced_trade_seconds": market.config.forced_trade_seconds,
                "choices": choice_counts(exchange, market),
            }
            for market_id, market in exchange.markets.items()
        },
        "traders": [
            {
                "trader_id": trader_id,
                "name": name,
                "connected": trader_id in room.connections,
                "kicked": trader_id in exchange.kicked,
                "markets": {
                    market_id: position_view(market, trader_id)
                    for market_id, market in exchange.markets.items()
                },
                "total": total_view(exchange, trader_id),
            }
            for trader_id, name in exchange.traders.items()
        ],
    }


def choice_counts(exchange, market):
    """During the forced-trade window: how many have chosen buy, sell, or not yet. Else None."""
    if market.status is not MarketStatus.FORCED_TRADE:
        return None
    sides = [market.choices.get(trader_id)
             for trader_id in exchange.forced_trade_participants(market.market_id)]
    return {"buy": sides.count(Side.BUY), "sell": sides.count(Side.SELL),
            "undecided": sides.count(None)}


# --- Review ---------------------------------------------------------------------------------


def review_state_message(room, pick):
    """Everything the review page shows: the pickers, and the review for the current pick
    ((market_id, trader_id), or None if nothing valid is picked)."""
    exchange = room.exchange
    review = None
    if pick is not None:
        market_id, trader_id = pick
        if market_id in exchange.markets and trader_id in exchange.traders:
            review = review_message(exchange, market_id, trader_id)
    return {"type": "review_state", "options": review_options(exchange), "review": review}


def review_options(exchange):
    """What the review page's pickers offer: every market that has started (oldest first), and
    every trader who joined, kicked ones included."""
    return {
        "markets": [
            {"market_id": market_id, "title": exchange.markets[market_id].config.title,
             "status": exchange.markets[market_id].status.value}
            for market_id in started_market_ids(exchange)
        ],
        "traders": [
            {"trader_id": trader_id, "name": name, "kicked": trader_id in exchange.kicked}
            for trader_id, name in exchange.traders.items()
        ],
    }


def review_message(exchange, market_id, trader_id):
    """One trader's fills in one market, the price chart, and the info drops along the way."""
    market = exchange.markets[market_id]
    rows = trader_fills(market, trader_id)
    return {
        "type": "review",
        "market_id": market_id,
        "title": market.config.title,
        "status": market.status.value,
        "trader_id": trader_id,
        "trader": name_of(exchange, trader_id),
        "settlement_value": market.settlement_value,
        "rows": [
            {
                "time": row.trade.ts.isoformat(),
                "side": row.side.value,
                "price": row.trade.price,
                "size": row.trade.size,
                "counterparty": name_of(exchange, row.counterparty_id),
                "position": row.position,
                "realized": row.realized,
                "mtm": row.unrealized,
                "edge": row.edge,
                "forced": row.trade.forced,
            }
            for row in rows
        ],
        "total_edge": total_edge(market, rows),
        **chart_view(exchange, market, rows),
    }


def chart_view(exchange, market, rows):
    """The price chart, from the open to settlement (or now while still trading), and the info
    drops in that time span. No chart before the market opens."""
    if market.opened_at is None:
        return {"chart": None, "info_drops": []}
    start = market.opened_at
    end = market.settled_at or exchange.clock()
    return {
        "chart": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            # The history only has points from the forced trades (at the opening time) up to
            # settlement, so all of it is inside the span.
            "points": [
                {"time": point.ts.isoformat(), "mark": point.mark, "last": point.last_price}
                for point in market.price_history
            ],
            "fills": [
                {"time": row.trade.ts.isoformat(), "price": row.trade.price,
                 "side": row.side.value}
                for row in rows
            ],
        },
        "info_drops": [
            {"time": drop.ts.isoformat(), "note": drop.note}
            for drop in exchange.info_drops
            if start <= drop.ts <= end
        ],
    }
