"""The order book for one market.

Each side maps a price to a line (a FIFO queue) of resting orders. Best price trades first; at
the same price, whoever joined the line first trades first. That is price-time priority.
"""

from collections import deque

from .models import Side


class OrderBook:
    def __init__(self):
        self.bids = {}  # price -> deque of Orders, oldest first
        self.asks = {}

    def _side(self, side):
        return self.bids if side is Side.BUY else self.asks

    def add(self, order):
        """Put an order at the back of the line at its price."""
        self._side(order.side).setdefault(order.price, deque()).append(order)

    def remove(self, order):
        levels = self._side(order.side)
        levels[order.price].remove(order)
        if not levels[order.price]:
            del levels[order.price]

    def contains(self, order):
        return order in self._side(order.side).get(order.price, ())

    def best_bid(self):
        return max(self.bids) if self.bids else None

    def best_ask(self):
        return min(self.asks) if self.asks else None

    def levels(self, side):
        """[(price, [orders in time order]), ...] with the best price first."""
        levels = self._side(side)
        best_first = sorted(levels, reverse=(side is Side.BUY))
        return [(price, list(levels[price])) for price in best_first]

    def all_orders(self):
        return [order for side in Side for _, orders in self.levels(side) for order in orders]

    def orders_to_match(self, side, limit_price):
        """Resting orders an incoming order could trade with, in the order they would fill.

        An incoming buy trades with offers at or below its price; an incoming sell trades with
        bids at or above its price. This only looks; it doesn't change the book.
        """
        if side is Side.BUY:
            levels = self.levels(Side.SELL)
            acceptable = [orders for price, orders in levels if price <= limit_price]
        else:
            levels = self.levels(Side.BUY)
            acceptable = [orders for price, orders in levels if price >= limit_price]
        return [order for orders in acceptable for order in orders]
