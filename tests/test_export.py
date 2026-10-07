"""The export: one zip with trades.csv, orders.csv, events.csv and events.json.

Times are US Eastern, and traders appear by their current names everywhere (also in
events.json, where names replace the ids). The test clock has no time zone, so its times are
read as UTC: 09:30 UTC in September is 05:30 in the morning Eastern (EDT, UTC-4).
"""

import csv
import io
import json
import zipfile
from datetime import datetime

from exchange.engine import Exchange, Side
from exchange.server.export import export_filename, export_zip
from helpers import ScriptedCoin, make_config, open_market

BUY, SELL = Side.BUY, Side.SELL


def join_all(ex, *names):
    return [ex.join(name)[0].trader_id for name in names]


def unzip(ex):
    """The export as {file name: contents}. ("utf-8-sig" drops the CSVs' byte-order mark.)"""
    with zipfile.ZipFile(io.BytesIO(export_zip(ex))) as archive:
        return {name: archive.read(name).decode("utf-8-sig") for name in archive.namelist()}


def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_the_zip_holds_the_four_files(ex):
    assert sorted(unzip(ex)) == ["events.csv", "events.json", "orders.csv", "trades.csv"]


def test_the_file_name_has_the_eastern_date_and_time():
    assert export_filename(datetime(2026, 10, 6, 23, 31, 4)) == "psuquants-game-2026-10-06-1931.zip"


# --- trades.csv -----------------------------------------------------------------------------


def test_trades_csv_has_one_row_per_trade_with_names(ex, clock):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", bob, SELL, 40, 10)
    clock.set("09:31:04")
    ex.take("cars", alice, BUY, 40, 4)
    ex.rename(alice, "Alicia")

    (row,) = csv_rows(unzip(ex)["trades.csv"])

    assert row == {
        "trade_id": "1", "time": "2026-09-27 05:31:04.000", "market_id": "cars",
        "market": "Test market cars", "price": "40", "size": "4", "buyer": "Alicia",
        "seller": "Bob", "aggressor": "buy", "forced": "no",
    }


def test_forced_trades_are_marked_in_trades_csv(clock):
    ex = Exchange(clock=clock, rng=ScriptedCoin(SELL))
    ex.create_market(make_config(tick_size=5))
    mm, alice = join_all(ex, "Mm", "Alice")
    ex.start_auction("cars")
    ex.submit_width("cars", mm, 10)
    ex.close_auction("cars")
    ex.submit_mm_quote("cars", mm, 100, 110)
    ex.start_forced_trade("cars")
    ex.end_forced_trade("cars", ended_by="timer")

    (row,) = csv_rows(unzip(ex)["trades.csv"])

    assert (row["buyer"], row["seller"], row["price"], row["forced"]) == ("Mm", "Alice", "100", "yes")


# --- orders.csv -----------------------------------------------------------------------------


def test_orders_csv_shows_how_each_order_ended(ex, clock):
    alice, bob, carol = join_all(ex, "Alice", "Bob", "Carol")
    ex.place_limit("cars", bob, SELL, 40, 10)        # 1: partly filled, then cancelled
    ex.place_limit("cars", carol, BUY, 30, 5)        # 2: still resting
    ex.take("cars", alice, BUY, 40, 4)               # 3: filled
    clock.set("09:32:00")
    ex.cancel("cars", bob, 1)
    ex.place_limit("cars", carol, SELL, 45, 2)       # 4: resting...
    ex.take("cars", alice, BUY, 45, 5)               # 5: take of 5, only 2 there: rest cancelled

    rows = {row["order_id"]: row for row in csv_rows(unzip(ex)["orders.csv"])}

    assert rows["1"] == {
        "order_id": "1", "time": "2026-09-27 05:30:00.000", "market_id": "cars",
        "market": "Test market cars", "trader": "Bob", "kind": "limit", "side": "sell",
        "price": "40", "requested_size": "10", "accepted_size": "10", "filled": "4",
        "status": "cancelled", "cancelled_at": "2026-09-27 05:32:00.000",
        "cancel_reason": "cancelled by trader",
    }
    assert (rows["2"]["status"], rows["2"]["filled"], rows["2"]["cancelled_at"]) == ("resting", "0", "")
    assert (rows["3"]["kind"], rows["3"]["status"], rows["3"]["filled"]) == ("take", "filled", "4")
    assert (rows["4"]["status"], rows["4"]["filled"]) == ("filled", "2")
    assert (rows["5"]["status"], rows["5"]["filled"], rows["5"]["cancel_reason"]) == (
        "cancelled", "2", "unfilled part of take")


def test_orders_resting_in_a_settled_market_are_frozen(ex):
    (alice,) = join_all(ex, "Alice")
    ex.place_limit("cars", alice, BUY, 30, 5)
    ex.settle_market("cars", 35)

    (row,) = csv_rows(unzip(ex)["orders.csv"])

    assert row["status"] == "frozen"


def test_a_clipped_order_shows_requested_and_accepted_size(clock):
    ex = Exchange(clock=clock)
    open_market(ex, max_position=5)
    (alice,) = join_all(ex, "Alice")
    ex.place_limit("cars", alice, BUY, 30, 8)

    (row,) = csv_rows(unzip(ex)["orders.csv"])

    assert (row["requested_size"], row["accepted_size"]) == ("8", "5")


# --- events.csv and events.json -------------------------------------------------------------


def test_events_csv_lists_every_event_with_names_in_place_of_ids(ex):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", bob, SELL, 40, 10)
    ex.take("cars", alice, BUY, 40, 4)
    ex.place_limit("cars", alice, BUY, 0, 1)   # rests
    ex.place_limit("cars", alice, SELL, 0, 1)  # rejected: would trade with her own bid
    ex.mark_info_drop("over 100k")

    rows = csv_rows(unzip(ex)["events.csv"])

    assert [row["seq"] for row in rows] == [str(n) for n in range(1, len(ex.events) + 1)]
    assert rows[0] == {
        "seq": "1", "time": "2026-09-27 05:30:00.000", "type": "MarketCreated",
        "market_id": "cars", "market": "Test market cars", "trader": "",
        "details": "title=Test market cars; tick_size=1; max_position=1000; "
                   "forced_trade_size=10; forced_trade_seconds=30",
    }
    trade = next(row for row in rows if row["type"] == "TradeExecuted")
    assert "buyer_id=Alice; seller_id=Bob" in trade["details"]
    rejected = next(row for row in rows if row["type"] == "Rejected")
    assert (rejected["trader"], rejected["details"]) == (
        "Alice", "command=limit sell 1 @ 0; reason=would trade with your own order")
    drop = rows[-1]
    assert (drop["type"], drop["market_id"], drop["market"], drop["details"]) == (
        "InfoDropMarked", "", "", "note=over 100k")


def test_events_json_is_the_full_log_with_names_and_eastern_times(ex, clock):
    alice, bob = join_all(ex, "Alice", "Bob")
    ex.place_limit("cars", bob, SELL, 40, 10)
    ex.take("cars", alice, BUY, 40, 4)
    ex.rename(alice, "Alicia")

    events = json.loads(unzip(ex)["events.json"])

    assert len(events) == len(ex.events)
    assert events[0]["ts"] == "2026-09-27T05:30:00-04:00"
    joined = [event for event in events if event["type"] == "TraderJoined"]
    assert [(event["trader_id"], event["name"]) for event in joined] == [
        ("Alicia", "Alice"), ("Bob", "Bob")]  # the name the trader joined with stays as logged
    trade = next(event for event in events if event["type"] == "TradeExecuted")
    assert (trade["buyer_id"], trade["seller_id"], trade["aggressor_side"]) == ("Alicia", "Bob", "buy")
    renamed = events[-1]
    assert (renamed["trader_id"], renamed["old_name"], renamed["new_name"]) == (
        "Alicia", "Alice", "Alicia")


def test_winter_times_are_eastern_standard_time(clock):
    clock.now = datetime(2026, 12, 3, 0, 15, 0)  # 00:15 UTC = 19:15 the evening before, EST
    ex = Exchange(clock=clock)
    open_market(ex)

    files = unzip(ex)

    assert json.loads(files["events.json"])[0]["ts"] == "2026-12-02T19:15:00-05:00"
    assert csv_rows(files["events.csv"])[0]["time"] == "2026-12-02 19:15:00.000"
