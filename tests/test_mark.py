"""Mark price: mid, falling back to the last traded price, overridden by settlement."""

from exchange.engine import PnL, Side, mark_price

BUY, SELL = Side.BUY, Side.SELL


def test_mark_is_the_mid_when_both_sides_exist():
    assert mark_price(best_bid=36, best_ask=38, last_price=None) == 37


def test_mid_can_fall_between_ticks():
    assert mark_price(best_bid=36, best_ask=37, last_price=None) == 36.5


def test_one_side_empty_uses_the_last_traded_price():
    assert mark_price(best_bid=36, best_ask=None, last_price=40) == 40
    assert mark_price(best_bid=None, best_ask=38, last_price=40) == 40


def test_no_mid_and_no_trade_means_no_mark():
    assert mark_price(best_bid=None, best_ask=None, last_price=None) is None
    assert mark_price(best_bid=36, best_ask=None, last_price=None) is None


def test_settlement_value_overrides_everything():
    assert mark_price(best_bid=36, best_ask=38, last_price=40, settlement_value=50) == 50


# --- Through the exchange ------------------------------------------------------------------


def test_new_market_has_no_mark_and_zero_pnl(ex):
    assert ex.mark("cars") is None
    assert ex.pnl("cars", "alice") == PnL(realized=0, unrealized=0)


def test_mark_falls_back_to_last_trade_when_a_side_empties(ex):
    ex.place_limit("cars", "alice", SELL, 38, 5)
    ex.place_limit("cars", "bob", BUY, 36, 5)
    assert ex.mark("cars") == 37

    ex.take("cars", "carol", BUY, 38, 5)

    assert ex.mark("cars") == 38


def test_pnl_is_marked_to_the_mid(ex):
    ex.place_limit("cars", "bob", SELL, 38, 10)
    ex.take("cars", "alice", BUY, 38, 10)
    ex.place_limit("cars", "carol", BUY, 36, 1)
    ex.place_limit("cars", "dave", SELL, 42, 1)  # mid = 39

    assert ex.pnl("cars", "alice") == PnL(realized=0, unrealized=10)
    assert ex.pnl("cars", "bob") == PnL(realized=0, unrealized=-10)
    assert ex.pnl("cars", "alice").total == 10
