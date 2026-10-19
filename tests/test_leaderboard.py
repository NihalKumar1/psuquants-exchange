"""The leaderboard on the review screen (SPEC Q153-Q158).

Every trader ranked by equity (starting equity + total PnL in dollars), highest first, with
their total PnL in dollars and each started market's PnL in that market's points. Ties share
a rank and are listed in name order. Removed traders are on it, marked. It is live: open
positions are valued at the current mark.
"""

from dataclasses import replace

from exchange.engine import Exchange, Side
from exchange.server.room import Room
from exchange.server.views import leaderboard_view, review_state_message, snapshot_message
from helpers import make_config

BUY, SELL = Side.BUY, Side.SELL


def open_with(ex, market_id, dollars_per_point):
    ex.create_market(replace(make_config(market_id), dollars_per_point=dollars_per_point))
    ex.open_market(market_id)


def trade(ex, market_id, buyer, seller, price, size=1):
    """seller offers, buyer lifts: one trade at `price`."""
    ex.place_limit(market_id, seller, SELL, price, size)
    ex.take(market_id, buyer, BUY, price, size)


def join(ex, *names):
    ids = []
    for name in names:
        (event,) = ex.join(name)
        ids.append(event.trader_id)
    return ids


def ranking(board):
    return [(row["rank"], row["name"]) for row in board["rows"]]


def test_traders_are_ranked_by_equity_with_each_markets_multiplier():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.01)  # a ~120,000 market
    open_with(ex, "homes", dollars_per_point=1)
    alice, bob, carol = join(ex, "Alice", "Bob", "Carol")
    trade(ex, "cars", alice, bob, 120_000)
    trade(ex, "cars", carol, alice, 121_000)   # Alice +1,000 points = +$10, Bob -$10 so far
    trade(ex, "homes", bob, carol, 100)
    trade(ex, "homes", carol, bob, 150)        # Bob +50 points = +$50, Carol -$50

    board = leaderboard_view(ex)

    assert ranking(board) == [(1, "Bob"), (2, "Alice"), (3, "Carol")]
    bob_row, alice_row, carol_row = board["rows"]
    assert bob_row["total_pnl"] == 40          # -$10 in cars, +$50 in homes
    assert bob_row["equity"] == 100_040
    assert alice_row["equity"] == 100_010
    assert carol_row["total_pnl"] == -50


def test_each_markets_pnl_is_in_its_own_points():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.01)
    alice, bob = join(ex, "Alice", "Bob")
    trade(ex, "cars", alice, bob, 120_000)
    trade(ex, "cars", bob, alice, 121_000)

    alice_row = leaderboard_view(ex)["rows"][0]

    assert alice_row["markets"] == {"cars": 1_000}   # points, not dollars
    assert alice_row["total_pnl"] == 10              # dollars


def test_ties_share_a_rank_and_are_listed_in_name_order():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=1)
    alice, bob, dave, carol = join(ex, "Alice", "Bob", "dave", "Carol")
    trade(ex, "cars", alice, bob, 100)
    trade(ex, "cars", bob, alice, 110)   # Alice +10, Bob -10; Carol and dave never trade

    assert ranking(leaderboard_view(ex)) == [(1, "Alice"), (2, "Carol"), (2, "dave"), (4, "Bob")]


def test_removed_traders_are_ranked_and_marked():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=1)
    alice, bob = join(ex, "Alice", "Bob")
    trade(ex, "cars", alice, bob, 100)
    trade(ex, "cars", bob, alice, 110)
    ex.kick(alice)

    rows = leaderboard_view(ex)["rows"]

    assert [(row["name"], row["kicked"]) for row in rows] == [("Alice", True), ("Bob", False)]
    assert rows[0]["rank"] == 1


def test_the_columns_are_the_markets_that_have_started():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.01)
    ex.create_market(make_config("homes"))   # created, not started yet
    join(ex, "Alice")

    board = leaderboard_view(ex)

    assert board["markets"] == [
        {"market_id": "cars", "title": "Test market cars", "dollars_per_point": 0.01},
    ]
    assert board["rows"][0]["markets"] == {"cars": 0}


def test_it_is_live_open_positions_are_valued_at_the_mark_then_at_settlement():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=2)
    alice, bob, carol, dave = join(ex, "Alice", "Bob", "Carol", "Dave")
    trade(ex, "cars", alice, bob, 100, size=3)       # Alice long 3, Bob short 3
    ex.place_limit("cars", carol, BUY, 104, 1)
    ex.place_limit("cars", dave, SELL, 106, 1)       # mark 105

    board = leaderboard_view(ex)
    assert board["rows"][0]["name"] == "Alice"
    assert board["rows"][0]["markets"] == {"cars": 15}   # 3 x (105 - 100) points
    assert board["rows"][0]["equity"] == 100_030         # x $2 per point

    ex.settle_market("cars", 90)

    board = leaderboard_view(ex)
    assert ranking(board)[0] == (1, "Bob")
    bob_row = board["rows"][0]
    assert bob_row["markets"] == {"cars": 30}             # short 3 from 100, settled at 90
    assert bob_row["equity"] == 100_060


def test_equity_uses_the_admins_starting_equity():
    ex = Exchange()
    join(ex, "Alice")
    ex.set_starting_equity(25_000)

    assert leaderboard_view(ex)["rows"][0]["equity"] == 25_000


def test_dollar_amounts_are_rounded_to_the_cent():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=0.1)
    alice, bob = join(ex, "Alice", "Bob")
    trade(ex, "cars", alice, bob, 100)
    trade(ex, "cars", bob, alice, 103)   # 3 points x $0.1 = 0.30000000000000004 in floats

    alice_row = leaderboard_view(ex)["rows"][0]

    assert alice_row["total_pnl"] == 0.3
    assert alice_row["equity"] == 100_000.3


# --- Who gets it (Q153: the projector, never a trader's browser) -----------------------------


def test_the_review_state_carries_the_leaderboard():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=1)
    join(ex, "Alice")

    message = review_state_message(Room(code="1234", exchange=ex), None)

    assert message["leaderboard"] == leaderboard_view(ex)


def test_traders_never_get_the_leaderboard():
    ex = Exchange()
    open_with(ex, "cars", dollars_per_point=1)
    (alice,) = join(ex, "Alice")

    assert "leaderboard" not in snapshot_message(ex, alice)
