import pytest

from exchange.engine import Exchange
from helpers import FakeClock, open_market


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def ex(clock):
    """An exchange with one open market, "cars" (tick 1, max position 1000)."""
    exchange = Exchange(clock=clock)
    open_market(exchange)
    return exchange
