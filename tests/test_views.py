"""What traders are shown: the public market view, a trader's private view, and the tape."""

from exchange.engine import Exchange, Side
from exchange.server.room import Room
from exchange.server.views import (
    admin_message,
    market_view,
    snapshot_message,
    trade_view,
    trader_view,
    update_message,
)
from helpers import ScriptedCoin, make_config, open_market, trades_in

BUY, SELL = Side.BUY, Side.SELL


def join_all(ex, *names):
    """Join each name and return their trader ids in the same order."""
    return [ex.join(name)[0].trader_id for name in names]


def test_book_shows_owner_names_and_sizes_best_price_first(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", alice, BUY, 36, 20)
    ex.place_limit("cars", bob, BUY, 36, 15)
    ex.place_limit("cars", bob, BUY, 35, 5)
    ex.place_limit("cars", alice, SELL, 38, 7)

    view = market_view(ex, "cars")

    assert [level["price"] for level in view["bids"]] == [36, 35]
    assert [(o["name"], o["size"]) for o in view["bids"][0]["orders"]] == [("Alice", 20), ("Bob", 15)]
    assert [(o["name"], o["size"]) for o in view["asks"][0]["orders"]] == [("Alice", 7)]
    assert view["best_bid"] == 36
    assert view["best_ask"] == 38
    assert view["mark"] == 37


def test_book_shows_only_the_top_10_levels_each_side(ex):
    (alice,) = join_all(ex, "Alice")
    for price in range(1, 13):
        ex.place_limit("cars", alice, BUY, price, 1)
        ex.place_limit("cars", alice, SELL, 100 + price, 1)

    view = market_view(ex, "cars")

    assert [level["price"] for level in view["bids"]] == list(range(12, 2, -1))
    assert [level["price"] for level in view["asks"]] == list(range(101, 111))


def test_book_shows_remaining_size_after_a_partial_fill(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", alice, SELL, 40, 10)
    ex.take("cars", bob, BUY, 40, 4)

    view = market_view(ex, "cars")

    assert view["asks"][0]["orders"][0]["size"] == 6


def test_market_view_has_settings_status_and_blank_mark(ex):
    view = market_view(ex, "cars")

    assert view["market_id"] == "cars"
    assert view["title"] == "Test market cars"
    assert view["tick_size"] == 1
    assert view["max_position"] == 1000
    assert view["status"] == "open"
    assert view["mark"] is None
    assert view["last_price"] is None


def test_positions_table_lists_every_joined_trader_including_flat_ones(ex):
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    ex.place_limit("cars", alice, SELL, 40, 10)
    ex.take("cars", bob, BUY, 40, 4)

    view = market_view(ex, "cars")

    assert [(row["name"], row["position"]) for row in view["positions"]] == [
        ("Alice", -4), ("Bob", 4), ("Carol", 0),
    ]


def test_market_view_contains_no_pnl(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", alice, SELL, 40, 10)
    ex.take("cars", bob, BUY, 40, 4)

    assert "pnl" not in str(market_view(ex, "cars")).lower()


def test_trader_view_has_only_my_resting_orders_and_my_pnl(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", alice, SELL, 40, 10)
    ex.place_limit("cars", bob, BUY, 30, 3)
    ex.take("cars", bob, BUY, 40, 4)  # bob long 4 @ 40; alice short 4

    view = trader_view(ex, "cars", alice)

    assert view["orders"] == [
        {"order_id": 1, "side": "sell", "price": 40, "size": 6},
    ]
    assert view["position"] == -4
    # Mark is the mid (30 + 40) / 2 = 35, so alice's short from 40 is up 20.
    assert view["realized"] == 0
    assert view["unrealized"] == 20
    assert view["total"] == 20


def test_trade_view_uses_names(ex, clock):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", alice, SELL, 40, 10)
    (trade,) = trades_in(ex.take("cars", bob, BUY, 40, 4))

    assert trade_view(ex, trade) == {
        "trade_id": 1,
        "market_id": "cars",
        "time": clock.now.isoformat(),
        "price": 40,
        "size": 4,
        "buyer": "Bob",
        "seller": "Alice",
        "aggressor_side": "buy",
        "forced": False,
    }


def test_trader_messages_carry_my_current_name(ex):
    (alice,) = join_all(ex, "Alice")
    ex.rename(alice, "Alicia")

    assert snapshot_message(ex, alice)["name"] == "Alicia"
    assert update_message(ex, alice, [])["name"] == "Alicia"


# --- Admin ----------------------------------------------------------------------------------


def test_admin_view_has_everyones_positions_and_pnl(ex, clock):
    room = Room(code="1234", exchange=ex)
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    ex.place_limit("cars", alice, SELL, 40, 10)
    ex.take("cars", bob, BUY, 40, 4)
    ex.place_limit("cars", bob, BUY, 30, 1)  # mark = mid 35
    room.connect(alice, "alice-socket")
    ex.kick(carol)
    ex.lock_joining()
    clock.set("09:45:00")
    ex.mark_info_drop("Over 100k")

    view = admin_message(room)

    assert view["type"] == "admin_state"
    assert view["room_code"] == "1234"
    assert view["joining_locked"] is True
    assert view["info_drops"] == [{"time": clock.now.isoformat(), "note": "Over 100k"}]
    assert view["markets"]["cars"]["forced_trade_size"] == 10
    assert view["markets"]["cars"]["forced_trade_seconds"] == 30
    assert view["markets"]["cars"]["title"] == "Test market cars"

    rows = {row["name"]: row for row in view["traders"]}
    assert list(rows) == ["Alice", "Bob", "Carol"]
    assert (rows["Alice"]["connected"], rows["Alice"]["kicked"]) == (True, False)
    assert (rows["Bob"]["connected"], rows["Bob"]["kicked"]) == (False, False)
    assert rows["Carol"]["kicked"] is True
    assert rows["Alice"]["markets"]["cars"] == {
        "position": -4, "realized": 0, "unrealized": 20, "total": 20,
    }
    assert rows["Bob"]["markets"]["cars"]["total"] == -20
    assert rows["Bob"]["total"] == {"realized": 0, "unrealized": -20, "total": -20}


# --- Trade or Tighten -----------------------------------------------------------------------


def tot_exchange(clock, *coin_flips):
    """An exchange with one CREATED market, "cars" (tick 5, forced size 10, 30 s timer)."""
    ex = Exchange(clock=clock, rng=ScriptedCoin(*coin_flips))
    ex.create_market(make_config(tick_size=5))
    return ex


def run_to_forced_trade(ex, mm, bid=100, ask=110):
    ex.start_auction("cars")
    ex.submit_width("cars", mm, 10)
    ex.close_auction("cars")
    ex.submit_mm_quote("cars", mm, bid, ask)
    ex.start_forced_trade("cars")


def test_there_is_no_trade_or_tighten_block_outside_it(ex):
    assert market_view(ex, "cars")["tot"] is None


def test_the_auction_shows_the_best_width_and_who_holds_it(clock):
    ex = tot_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.start_auction("cars")
    assert market_view(ex, "cars")["tot"]["best_width"] is None

    ex.submit_width("cars", alice, 50)
    ex.submit_width("cars", bob, 20)

    tot = market_view(ex, "cars")["tot"]
    assert (tot["best_width"], tot["best_holder"]) == (20, "Bob")


def test_everyone_sees_the_market_maker_and_their_quote_once_submitted(clock):
    ex = tot_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.start_auction("cars")
    ex.submit_width("cars", alice, 10)
    ex.close_auction("cars")

    tot = market_view(ex, "cars")["tot"]
    assert (tot["mm"], tot["width"], tot["bid"], tot["ask"]) == ("Alice", 10, None, None)
    assert trader_view(ex, "cars", alice)["is_mm"] is True
    assert trader_view(ex, "cars", bob)["is_mm"] is False

    ex.submit_mm_quote("cars", alice, 100, 105)
    tot = market_view(ex, "cars")["tot"]
    assert (tot["bid"], tot["ask"]) == (100, 105)


def test_the_window_shows_the_quote_size_and_time_left_but_only_my_own_choice(clock):
    ex = tot_exchange(clock)
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    clock.set("09:30:00")
    run_to_forced_trade(ex, alice, bid=100, ask=110)
    clock.set("09:30:10")
    ex.choose_side("cars", bob, BUY)

    tot = market_view(ex, "cars")["tot"]
    assert (tot["mm"], tot["bid"], tot["ask"]) == ("Alice", 100, 110)
    assert (tot["forced_trade_size"], tot["seconds_left"]) == (10, 20)
    assert trader_view(ex, "cars", bob)["my_choice"] == "buy"
    assert trader_view(ex, "cars", carol)["my_choice"] is None
    # Bob's choice is nowhere in what Carol is sent.
    assert "buy" not in str(update_message(ex, carol, [])["markets"])


def test_forced_trades_are_tagged_on_the_tape(clock):
    ex = tot_exchange(clock)
    alice, bob = join_all(ex, "Alice", "Bob")
    run_to_forced_trade(ex, alice)
    ex.choose_side("cars", bob, BUY)

    (trade,) = trades_in(ex.end_forced_trade("cars", ended_by="timer"))

    view = trade_view(ex, trade)
    assert (view["buyer"], view["seller"], view["price"], view["forced"]) == ("Bob", "Alice", 110, True)


def test_the_admin_sees_how_many_have_chosen_each_side(clock):
    ex = tot_exchange(clock)
    room = Room(code="1234", exchange=ex)
    alice, bob, carol, dave, erin = join_all(ex, "Alice", "Bob", "Carol", "Dave", "Erin")
    assert admin_message(room)["markets"]["cars"]["choices"] is None
    run_to_forced_trade(ex, alice)
    ex.choose_side("cars", bob, BUY)
    ex.choose_side("cars", carol, SELL)
    ex.choose_side("cars", dave, BUY)

    choices = admin_message(room)["markets"]["cars"]["choices"]

    assert choices == {"buy": 2, "sell": 1, "undecided": 1}  # Erin hasn't chosen


# --- Several markets at once (milestone 5) ----------------------------------------------------


def test_only_running_markets_get_a_column_oldest_first(ex):
    # "cars" (created first) is open. Then one market in every other status.
    ex.create_market(make_config("created"))
    ex.create_market(make_config("auction"))
    ex.start_auction("auction")
    open_market(ex, market_id="halted")
    ex.halt_market("halted")
    open_market(ex, market_id="settled")
    ex.settle_market("settled", 100)
    (alice,) = join_all(ex, "Alice")

    state = update_message(ex, alice, [])

    assert state["columns"] == ["cars", "auction", "halted"]


def test_the_positions_and_tape_tables_keep_settled_markets_but_not_created_ones(ex):
    ex.create_market(make_config("created"))
    open_market(ex, market_id="settled")
    ex.settle_market("settled", 100)
    (alice,) = join_all(ex, "Alice")

    assert update_message(ex, alice, [])["table_markets"] == ["cars", "settled"]


def test_one_running_market_shows_10_levels_each_side(ex):
    (alice,) = join_all(ex, "Alice")
    for price in range(1, 13):
        ex.place_limit("cars", alice, BUY, price, 1)
    open_market(ex, market_id="settled")
    ex.settle_market("settled", 100)  # settled markets don't count

    state = update_message(ex, alice, [])

    assert state["book_depth"] == 10
    assert len(state["markets"]["cars"]["bids"]) == 10


def test_several_running_markets_show_5_levels_each_side(ex):
    (alice,) = join_all(ex, "Alice")
    for price in range(1, 13):
        ex.place_limit("cars", alice, BUY, price, 1)
        ex.place_limit("cars", alice, SELL, 100 + price, 1)
    open_market(ex, market_id="homes")

    state = snapshot_message(ex, alice)

    assert state["book_depth"] == 5
    assert [level["price"] for level in state["markets"]["cars"]["bids"]] == [12, 11, 10, 9, 8]
    assert [level["price"] for level in state["markets"]["cars"]["asks"]] == [101, 102, 103, 104, 105]


def test_no_running_market_means_no_columns_and_the_full_depth(ex):
    ex.settle_market("cars", 100)
    (alice,) = join_all(ex, "Alice")

    state = update_message(ex, alice, [])

    assert state["columns"] == []
    assert state["book_depth"] == 10


def test_the_admin_always_gets_10_levels(ex):
    room = Room(code="1234", exchange=ex)
    (alice,) = join_all(ex, "Alice")
    for price in range(1, 13):
        ex.place_limit("cars", alice, BUY, price, 1)
    open_market(ex, market_id="homes")

    assert len(admin_message(room)["markets"]["cars"]["bids"]) == 10
