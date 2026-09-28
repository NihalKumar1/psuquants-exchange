"""Small helpers shared by the tests."""

from datetime import datetime

from exchange.engine import MarketConfig, OrderAccepted, Side, TradeExecuted


class FakeClock:
    """A clock the tests control, so a test can say "this order was placed at 9:30:01"."""

    def __init__(self):
        self.now = datetime(2026, 9, 27, 9, 30, 0)

    def __call__(self):
        return self.now

    def set(self, hh_mm_ss):
        hour, minute, second = (int(part) for part in hh_mm_ss.split(":"))
        self.now = self.now.replace(hour=hour, minute=minute, second=second)


class ScriptedCoin:
    """Stands in for random.Random in the forced trade: each coin flip gives the next side in
    the script, so a test knows exactly who gets which random side."""

    def __init__(self, *sides):
        self.sides = list(sides)

    def choice(self, options):
        assert self.sides, "the test didn't script enough coin flips"
        return self.sides.pop(0)


def make_config(market_id="cars", tick_size=1, max_position=1000):
    return MarketConfig(
        market_id=market_id,
        title=f"Test market {market_id}",
        tick_size=tick_size,
        max_position=max_position,
        forced_trade_size=10,
        forced_trade_seconds=30,
    )


def open_market(exchange, **config):
    """Create a market and open it straight into continuous trading."""
    market_config = make_config(**config)
    exchange.create_market(market_config)
    exchange.open_market(market_config.market_id)


def accepted_id(events):
    """The order id from the OrderAccepted event in a command's events."""
    return next(event.order_id for event in events if isinstance(event, OrderAccepted))


def trades_in(events):
    return [event for event in events if isinstance(event, TradeExecuted)]


def book_summary(exchange, market_id, side):
    """The book as [(price, [(trader, remaining), ...]), ...], best price first."""
    return [
        (price, [(order.trader_id, order.remaining) for order in orders])
        for price, orders in exchange.book(market_id).levels(side)
    ]


def bids(exchange, market_id="cars"):
    return book_summary(exchange, market_id, Side.BUY)


def asks(exchange, market_id="cars"):
    return book_summary(exchange, market_id, Side.SELL)
