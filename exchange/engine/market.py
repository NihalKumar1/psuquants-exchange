"""One market: its book, orders, trades and positions.

Two kinds of methods live here:
- decide_* methods look at the current state and return the events a command would produce.
  They never change anything.
- apply(event) is the only place state changes.
"""

from dataclasses import replace
from datetime import timedelta

from .book import OrderBook
from .events import (
    AuctionStarted,
    ForcedTradeEnded,
    ForcedTradeStarted,
    MarketEdited,
    MarketHalted,
    MarketMakerChosen,
    MarketMakerQuoted,
    MarketOpened,
    MarketResumed,
    MarketSettled,
    OrderAccepted,
    OrderCancelled,
    OrderRested,
    Rejected,
    SideAssigned,
    SideChosen,
    TradeExecuted,
    TradeOrTightenCancelled,
    WidthsWithdrawn,
    WidthSubmitted,
)
from .models import MarketStatus, Order, Side, is_whole_number
from .positions import PnL, Position, mark_price


class Market:
    def __init__(self, config):
        self.config = config
        self.status = MarketStatus.CREATED
        self.book = OrderBook()
        self.orders = {}  # order_id -> Order (every order ever accepted, for review)
        self.trades = []  # TradeExecuted events, oldest first (the tape)
        self.positions = {}  # trader_id -> Position
        self.last_price = None  # price of the most recent trade
        self.settlement_value = None

        # Trade or Tighten (only used if the admin starts the auction instead of opening directly)
        self.widths = []  # (trader_id, width) in the order submitted; each narrower than the last
        self.mm_id = None  # the market maker
        self.mm_width = None  # the width the MM won with
        self.mm_bid = None  # the MM's quote for the forced trade
        self.mm_ask = None
        self.forced_started_at = None  # when the forced-trade window opened
        self.choices = {}  # trader_id -> Side, in the order of each trader's final choice
        self.assigned = {}  # trader_id -> Side, for traders given a random side at close

    # --- Queries --------------------------------------------------------------------------

    @property
    def market_id(self):
        return self.config.market_id

    def mark(self):
        return mark_price(
            self.book.best_bid(), self.book.best_ask(), self.last_price, self.settlement_value
        )

    def position(self, trader_id):
        return self.positions.get(trader_id, Position())

    def pnl(self, trader_id):
        position = self.position(trader_id)
        return PnL(realized=position.realized, unrealized=position.unrealized(self.mark()))

    def room(self, trader_id, side):
        """How much more a trader may buy (or sell) without being able to breach max position.

        Worst case: assume every resting order on that side fills.
        """
        position = self.position(trader_id).size
        exposure = position if side is Side.BUY else -position
        resting = sum(
            order.remaining
            for order in self.book.all_orders()
            if order.trader_id == trader_id and order.side is side
        )
        return self.config.max_position - exposure - resting

    def best_width(self):
        """(trader_id, width) for the narrowest width in the auction, or None if there is none."""
        if not self.widths:
            return None
        return min(self.widths, key=lambda entry: entry[1])

    def forced_trade_deadline(self):
        """When the forced-trade window closes by itself."""
        return self.forced_started_at + timedelta(seconds=self.config.forced_trade_seconds)

    # --- Decide: command -> events ---------------------------------------------------------

    def decide_order(self, trader_id, kind, side, price, size, order_id, trade_id):
        """A limit order ("limit") or a click on the best bid/offer ("take")."""
        command = f"{kind} {side.value} {size} @ {price}"

        def reject(reason):
            return [Rejected(market_id=self.market_id, trader_id=trader_id,
                             command=command, reason=reason)]

        if self.status is not MarketStatus.OPEN:
            return reject("market is not open")
        if not is_whole_number(size) or size < 1:
            return reject("size must be a whole number of at least 1")
        if not is_whole_number(price) or price % self.config.tick_size != 0:
            return reject(f"price must be a multiple of the tick size ({self.config.tick_size})")

        matches = self.book.orders_to_match(side, price)
        if kind == "take" and not matches:
            return reject("nothing to take at this price or better")

        accepted = min(size, self.room(trader_id, side))
        if accepted < 1:
            return reject(f"would exceed max position of {self.config.max_position}")

        events = [OrderAccepted(market_id=self.market_id, order_id=order_id, trader_id=trader_id,
                                kind=kind, side=side, price=price,
                                requested_size=size, size=accepted)]

        # Walk the resting orders in priority order, trading at each one's price.
        # No trading with yourself: if the order would reach one of the trader's own resting
        # orders, the whole order is rejected and nothing trades.
        remaining = accepted
        for resting in matches:
            if remaining == 0:
                break
            if resting.trader_id == trader_id:
                return reject("would trade with your own order")
            fill = min(remaining, resting.remaining)
            if side is Side.BUY:
                buyer, seller = (trader_id, order_id), (resting.trader_id, resting.order_id)
            else:
                buyer, seller = (resting.trader_id, resting.order_id), (trader_id, order_id)
            events.append(TradeExecuted(
                market_id=self.market_id, trade_id=trade_id, price=resting.price, size=fill,
                buyer_id=buyer[0], buy_order_id=buyer[1],
                seller_id=seller[0], sell_order_id=seller[1],
                aggressor_side=side,
            ))
            trade_id += 1
            remaining -= fill

        # A limit order's leftover rests in the book; a take's leftover is cancelled.
        if remaining > 0:
            if kind == "limit":
                events.append(OrderRested(market_id=self.market_id, order_id=order_id))
            else:
                events.append(OrderCancelled(market_id=self.market_id, order_id=order_id,
                                             reason="unfilled part of take"))
        return events

    def decide_cancel(self, trader_id, order_id):
        # Cancelling works while halted, but a settled market's book is frozen for good.
        order = self.orders.get(order_id)
        if self.status is MarketStatus.SETTLED:
            reason = "market is settled"
        elif order is None or not self.book.contains(order):
            reason = "no such resting order"
        elif order.trader_id != trader_id:
            reason = "not your order"
        else:
            return [OrderCancelled(market_id=self.market_id, order_id=order_id,
                                   reason="cancelled by trader")]
        return [Rejected(market_id=self.market_id, trader_id=trader_id,
                         command=f"cancel {order_id}", reason=reason)]

    def decide_cancel_all(self, trader_id, reason="cancel all"):
        """Cancel every resting order this trader has here (none once the market is settled)."""
        if self.status is MarketStatus.SETTLED:
            return []
        return [
            OrderCancelled(market_id=self.market_id, order_id=order.order_id, reason=reason)
            for order in self.book.all_orders()
            if order.trader_id == trader_id
        ]

    def decide_kick(self, trader_id):
        """What removing this trader does to this market.

        Their resting orders are cancelled (except once settled). During Trade or Tighten their
        widths stop counting, and if they were the market maker who hasn't finished quoting,
        the next-narrowest width holder takes over, or the market goes back to CREATED if
        there is no one. A kicked MM in the forced-trade window still gets their trades.
        """
        events = self.decide_cancel_all(trader_id, reason="removed by admin")
        in_auction_or_quoting = self.status in (MarketStatus.AUCTION, MarketStatus.MM_QUOTING)
        if in_auction_or_quoting and any(holder == trader_id for holder, _ in self.widths):
            events.append(WidthsWithdrawn(market_id=self.market_id, trader_id=trader_id))

        if self.status is MarketStatus.MM_QUOTING and trader_id == self.mm_id:
            others = [(holder, width) for holder, width in self.widths if holder != trader_id]
            if others:
                next_id, next_width = min(others, key=lambda entry: entry[1])
                events.append(MarketMakerChosen(market_id=self.market_id, trader_id=next_id,
                                                width=next_width))
            else:
                events.append(TradeOrTightenCancelled(
                    market_id=self.market_id,
                    reason="the market maker was removed and no one else submitted a width"))
        return events

    # --- Decide: Trade or Tighten ----------------------------------------------------------

    def _rejected(self, trader_id, command, reason):
        return [Rejected(market_id=self.market_id, trader_id=trader_id,
                         command=command, reason=reason)]

    def decide_width(self, trader_id, width):
        """A width in the auction: in price units, on the tick, and narrower than the best."""
        tick = self.config.tick_size
        best = self.best_width()
        if self.status is not MarketStatus.AUCTION:
            reason = "the width auction is not open"
        elif not is_whole_number(width):
            reason = "width must be a whole number"
        elif width < 0:
            reason = "width can't be negative"
        elif width % tick != 0:
            reason = f"width must be a multiple of the tick size ({tick})"
        elif best is not None and width >= best[1]:
            reason = f"width must be narrower than the best width ({best[1]})"
        else:
            return [WidthSubmitted(market_id=self.market_id, trader_id=trader_id, width=width)]
        return self._rejected(trader_id, f"width {width}", reason)

    def decide_mm_quote(self, trader_id, bid, ask):
        """The MM's bid and offer: on the tick, and no wider than the width they won with."""
        tick = self.config.tick_size
        on_tick = all(is_whole_number(price) and price % tick == 0 for price in (bid, ask))
        if self.status is not MarketStatus.MM_QUOTING:
            reason = "the market maker isn't quoting now"
        elif trader_id != self.mm_id:
            reason = "only the market maker can quote"
        elif self.mm_bid is not None:
            reason = "you already quoted, and the first quote is final"
        elif not on_tick:
            reason = f"prices must be multiples of the tick size ({tick})"
        elif bid > ask:
            reason = "bid can't be above the offer"
        elif ask - bid > self.mm_width:
            reason = f"quote is wider than your width ({self.mm_width})"
        else:
            return [MarketMakerQuoted(market_id=self.market_id, trader_id=trader_id,
                                      bid=bid, ask=ask)]
        return self._rejected(trader_id, f"mm quote {bid} @ {ask}", reason)

    def decide_choice(self, trader_id, side):
        """Buy at the MM's offer or sell at the MM's bid. Nothing trades until the window closes."""
        command = f"choose {side.value}"
        if self.status is not MarketStatus.FORCED_TRADE:
            return self._rejected(trader_id, command, "the forced trade isn't running")
        if trader_id == self.mm_id:
            return self._rejected(trader_id, command,
                                  "the market maker takes the other side of every forced trade")
        return [SideChosen(market_id=self.market_id, trader_id=trader_id, side=side)]

    def decide_forced_trades(self, participants, rng, trade_id, ended_by):
        """Close the forced-trade window and open the market.

        `participants` are everyone who must trade, in join order. Traders who chose trade first,
        in the order of their final choice; then everyone else gets a coin flip, in join order.
        Each trades the forced-trade size against the MM: a buyer pays the MM's offer and a
        seller gets the MM's bid. Max position doesn't apply to forced trades.
        """
        events = [ForcedTradeEnded(market_id=self.market_id, ended_by=ended_by)]
        sides = {
            trader_id: side for trader_id, side in self.choices.items() if trader_id in participants
        }
        for trader_id in participants:
            if trader_id not in sides:
                sides[trader_id] = rng.choice([Side.BUY, Side.SELL])
                events.append(SideAssigned(market_id=self.market_id, trader_id=trader_id,
                                           side=sides[trader_id]))

        for trader_id, side in sides.items():
            if side is Side.BUY:
                buyer_id, seller_id, price = trader_id, self.mm_id, self.mm_ask
            else:
                buyer_id, seller_id, price = self.mm_id, trader_id, self.mm_bid
            events.append(TradeExecuted(
                market_id=self.market_id, trade_id=trade_id, price=price,
                size=self.config.forced_trade_size, buyer_id=buyer_id, seller_id=seller_id,
                buy_order_id=None, sell_order_id=None, aggressor_side=side, forced=True,
            ))
            trade_id += 1

        # The MM's quote is not left in the book: continuous trading starts with it empty.
        events.append(MarketOpened(market_id=self.market_id))
        return events

    # --- Apply: event -> new state ---------------------------------------------------------

    def apply(self, event):
        if isinstance(event, MarketEdited):
            self.config = replace(
                self.config, title=event.title, tick_size=event.tick_size,
                max_position=event.max_position, forced_trade_size=event.forced_trade_size,
                forced_trade_seconds=event.forced_trade_seconds,
            )
        elif isinstance(event, (MarketOpened, MarketResumed)):
            self.status = MarketStatus.OPEN
        elif isinstance(event, MarketHalted):
            self.status = MarketStatus.HALTED
        elif isinstance(event, MarketSettled):
            self.status = MarketStatus.SETTLED
            self.settlement_value = event.settlement_value
        elif isinstance(event, OrderAccepted):
            self.orders[event.order_id] = Order(
                order_id=event.order_id, market_id=event.market_id, trader_id=event.trader_id,
                kind=event.kind, side=event.side, price=event.price,
                size=event.size, remaining=event.size,
            )
        elif isinstance(event, TradeExecuted):
            self._apply_trade(event)
        elif isinstance(event, OrderRested):
            self.book.add(self.orders[event.order_id])
        elif isinstance(event, OrderCancelled):
            order = self.orders[event.order_id]
            if self.book.contains(order):
                self.book.remove(order)
            order.cancelled = True
        # Trade or Tighten
        elif isinstance(event, AuctionStarted):
            self.status = MarketStatus.AUCTION
        elif isinstance(event, WidthSubmitted):
            self.widths.append((event.trader_id, event.width))
        elif isinstance(event, WidthsWithdrawn):
            self.widths = [entry for entry in self.widths if entry[0] != event.trader_id]
        elif isinstance(event, MarketMakerChosen):
            self.status = MarketStatus.MM_QUOTING
            self.mm_id, self.mm_width = event.trader_id, event.width
            self.mm_bid = self.mm_ask = None  # a replacement MM quotes afresh
        elif isinstance(event, TradeOrTightenCancelled):
            self.status = MarketStatus.CREATED
            self.widths = []
            self.mm_id = self.mm_width = None
        elif isinstance(event, MarketMakerQuoted):
            self.mm_bid, self.mm_ask = event.bid, event.ask
        elif isinstance(event, ForcedTradeStarted):
            self.status = MarketStatus.FORCED_TRADE
            self.forced_started_at = event.ts
        elif isinstance(event, SideChosen):
            self.choices.pop(event.trader_id, None)  # a changed choice moves to the back
            self.choices[event.trader_id] = event.side
        elif isinstance(event, SideAssigned):
            self.assigned[event.trader_id] = event.side
        elif isinstance(event, ForcedTradeEnded):
            pass  # the forced trades and MarketOpened that follow do the work

    def _apply_trade(self, trade):
        for order_id in (trade.buy_order_id, trade.sell_order_id):
            if order_id is None:
                continue  # a forced trade: no order behind this side
            order = self.orders[order_id]
            order.remaining -= trade.size
            if order.remaining == 0 and self.book.contains(order):
                self.book.remove(order)

        self.trades.append(trade)
        self.positions.setdefault(trade.buyer_id, Position()).apply_fill(
            Side.BUY, trade.price, trade.size)
        self.positions.setdefault(trade.seller_id, Position()).apply_fill(
            Side.SELL, trade.price, trade.size)
        self.last_price = trade.price
