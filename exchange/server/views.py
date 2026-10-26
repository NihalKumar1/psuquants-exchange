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

BOOK_DEPTH = 10         # price levels shown on each side of the book...
SHORT_BOOK_DEPTH = 5    # ...or this many when two markets share the trader's screen...
NARROW_BOOK_DEPTH = 4   # ...or this many when three or more do (so it all fits a laptop screen)
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
    """The full book when one market has the screen to itself, shorter ones when several share it."""
    running = len(running_market_ids(exchange))
    if running <= 1:
        return BOOK_DEPTH
    if running == 2:
        return SHORT_BOOK_DEPTH
    return NARROW_BOOK_DEPTH


# --- Public ---------------------------------------------------------------------------------


def market_view(exchange, market_id, depth=BOOK_DEPTH):
    market = exchange.markets[market_id]
    return {
        "market_id": market_id,
        "title": market.config.title,
        "tick_size": market.config.tick_size,
        "max_position": market.config.max_position,
        "dollars_per_point": market.config.dollars_per_point,
        "min_price": market.config.min_price,  # price limits: None = no limit
        "max_price": market.config.max_price,
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
        # Ids too (they are already public in the book), so a page can pick out its own trades
        # even after a rename.
        "buyer_id": trade.buyer_id,
        "seller_id": trade.seller_id,
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
        "average_price": average_price_text(market.position(trader_id)),
        "is_mm": market.mm_id == trader_id,
        "my_choice": choice.value if choice else None,
    }


def average_price_text(position):
    """The average price of the open position, e.g. "36.2", or None when flat (Q124-Q126).

    It is the average of the open lots, so a sale closes the oldest buys first (FIFO, Q9) and
    the MTM tile is always (mark - average) x position. Cut to 1 decimal like the fill alerts.
    """
    if position.size == 0:
        return None
    value = sum(lot.price * abs(lot.size) for lot in position.lots)
    return average_text(value, abs(position.size))


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
    """One trader's PnL added up over every market, and their equity, in dollars (each market's
    PnL times its dollars per point). Rounded to cents, so 0.30000000000000004 is sent as 0.3."""
    pnl = exchange.total_pnl(trader_id)
    return {
        "realized": cents(pnl.realized),
        "unrealized": cents(pnl.unrealized),
        "total": cents(pnl.total),
        "equity": cents(exchange.equity(trader_id)),
    }


def cents(dollars):
    """Dollars rounded to the cent; a whole number of dollars stays a whole number (5, not 5.0)."""
    rounded = round(dollars, 2)
    return int(rounded) if rounded == int(rounded) else rounded


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
        "my_fills": my_fills(exchange, trader_id, events),
    }


# --- Fill alerts ----------------------------------------------------------------------------


def my_fills(exchange, trader_id, events):
    """One alert per market where this trader just got filled, ready for the page to show:
    {"market_id", "text": "You bought 5 of Cars at 36, now LONG 5", "colour": "buy"}.

    All of a trader's fills in one command make one alert (Q114). The colour follows the net
    amount: more bought is "buy" (green), more sold is "sell" (red), equal is "even" (Q115, Q117).
    """
    bought = {}  # market_id -> the prices and sizes this trader bought, e.g. [(36, 20), (37, 5)]
    sold = {}
    for event in events:
        if not isinstance(event, TradeExecuted):
            continue
        if event.buyer_id == trader_id:
            bought.setdefault(event.market_id, []).append((event.price, event.size))
        if event.seller_id == trader_id:
            sold.setdefault(event.market_id, []).append((event.price, event.size))

    alerts = []
    for market_id, market in exchange.markets.items():
        buys = bought.get(market_id, [])
        sells = sold.get(market_id, [])
        if not buys and not sells:
            continue
        title = market.config.title
        if buys and sells:
            what = f"bought {fills_text(buys, title)} and sold {fills_text(sells)}"
        elif buys:
            what = f"bought {fills_text(buys, title)}"
        else:
            what = f"sold {fills_text(sells, title)}"
        net = total_size(buys) - total_size(sells)
        alerts.append({
            "market_id": market_id,
            "text": f"You {what}, now {position_text(market.position(trader_id).size)}",
            "colour": "buy" if net > 0 else "sell" if net < 0 else "even",
        })
    return alerts


def total_size(fills):
    return sum(size for _, size in fills)


def fills_text(fills, title=None):
    """"25 of Cars at avg 36.2": "avg" only when the prices differ (Q118). The title is left out
    for the second side of a two-sided alert ("… and sold 10 at 110")."""
    size = total_size(fills)
    of_title = f" of {title}" if title is not None else ""
    prices = {price for price, _ in fills}
    if len(prices) == 1:
        return f"{size:,}{of_title} at {prices.pop():,}"
    value = sum(price * size for price, size in fills)
    return f"{size:,}{of_title} at avg {average_text(value, size)}"


def average_text(value, size):
    """value / size with at most 1 decimal, cut towards zero rather than rounded (Q116):
    905 / 25 -> "36.2", 145 / 4 (36.25) -> "36.2", -143 / 4 (-35.75) -> "-35.7", 72 / 2 -> "36".

    Whole numbers only, so there is no floating-point rounding. Python's // rounds down (-35.75
    would become -35.8), so the value is divided without its sign and the sign is put back.
    """
    tenths = abs(10 * value) // size  # e.g. 362 for 36.25
    sign = "-" if value < 0 and tenths > 0 else ""
    whole, tenth = divmod(tenths, 10)
    return f"{sign}{whole:,}" + (f".{tenth}" if tenth else "")


def position_text(position):
    if position > 0:
        return f"LONG {position:,}"
    if position < 0:
        return f"SHORT {-position:,}"
    return "FLAT"


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
        "bots_on": exchange.bots_on,
        "starting_equity": exchange.starting_equity,
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
                "bot": exchange.bots.get(trader_id),  # "maker", "taker" or None
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
    return {"type": "review_state", "options": review_options(exchange), "review": review,
            "leaderboard": leaderboard_view(exchange)}


def leaderboard_view(exchange):
    """Every trader ranked by equity, highest first (SPEC Q153-Q158), for the projector only:
    it has everyone's PnL, so it never goes to a trader's browser (Q10).

    Each row has the equity and total PnL in dollars, and each started market's PnL in that
    market's points. Ties share a rank (1, 2, 2, 4) and are listed in name order. Removed
    traders are ranked too, marked "kicked".
    """
    market_ids = started_market_ids(exchange)
    rows = [
        {
            "trader_id": trader_id,
            "name": name,
            "kicked": trader_id in exchange.kicked,
            "equity": cents(exchange.equity(trader_id)),
            "total_pnl": cents(exchange.total_pnl(trader_id).total),
            "markets": {market_id: exchange.markets[market_id].pnl(trader_id).total
                        for market_id in market_ids},
        }
        for trader_id, name in exchange.traders.items()
    ]
    rows.sort(key=lambda row: (-row["equity"], row["name"].casefold()))
    for row in rows:
        # Rank = 1 + how many have more equity, so equal equity (to the cent) shares a rank.
        row["rank"] = 1 + sum(other["equity"] > row["equity"] for other in rows)
    return {
        "markets": [
            {"market_id": market_id, "title": exchange.markets[market_id].config.title,
             "dollars_per_point": exchange.markets[market_id].config.dollars_per_point}
            for market_id in market_ids
        ],
        "rows": rows,
    }


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
