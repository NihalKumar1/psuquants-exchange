"""Position and PnL math with FIFO lots."""

from exchange.engine import Position, Side

BUY, SELL = Side.BUY, Side.SELL


def test_new_position_is_flat():
    position = Position()

    assert position.size == 0
    assert position.realized == 0
    assert position.unrealized(mark=None) == 0


def test_buys_add_lots():
    position = Position()
    position.apply_fill(BUY, 100, 10)
    position.apply_fill(BUY, 110, 10)

    assert position.size == 20
    assert position.realized == 0
    assert position.unrealized(mark=120) == 10 * 20 + 10 * 10


def test_partial_close_realizes_against_the_oldest_lot_first():
    position = Position()
    position.apply_fill(BUY, 100, 10)
    position.apply_fill(BUY, 110, 10)

    position.apply_fill(SELL, 120, 15)

    assert position.size == 5
    assert position.realized == 10 * (120 - 100) + 5 * (120 - 110)
    assert position.unrealized(mark=120) == 5 * (120 - 110)


def test_fifo_differs_from_average_cost():
    position = Position()
    position.apply_fill(BUY, 100, 10)
    position.apply_fill(BUY, 120, 10)

    position.apply_fill(SELL, 130, 10)

    assert position.realized == 300  # average cost would say 10 * (130 - 110) = 200


def test_flip_from_long_to_short():
    position = Position()
    position.apply_fill(BUY, 100, 10)

    position.apply_fill(SELL, 90, 25)

    assert position.size == -15
    assert position.realized == 10 * (90 - 100)
    assert position.unrealized(mark=80) == 15 * (90 - 80)


def test_short_then_cover():
    position = Position()
    position.apply_fill(SELL, 100, 10)

    position.apply_fill(BUY, 90, 4)

    assert position.size == -6
    assert position.realized == 4 * (100 - 90)
    assert position.unrealized(mark=95) == 6 * (100 - 95)


def test_back_to_flat_leaves_only_realized():
    position = Position()
    position.apply_fill(BUY, 100, 10)
    position.apply_fill(SELL, 105, 10)

    assert position.size == 0
    assert position.realized == 50
    assert position.unrealized(mark=None) == 0


def test_unrealized_with_a_half_tick_mark():
    position = Position()
    position.apply_fill(BUY, 36, 3)

    assert position.unrealized(mark=36.5) == 1.5
