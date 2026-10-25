"""What the pages and the export get from a delta-hedging round (SPEC Q177-Q178, Q196-Q203): the
review chart's fair-value and net-delta lines, the export's rounds.csv and moves.csv, and the
round controls on each page."""

import csv
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from exchange.engine import Exchange, Side
from exchange.engine.house import HOUSE_ID
from exchange.engine.options import black_scholes
from exchange.server.app import create_app
from exchange.server.export import export_zip
from exchange.server.room import Room
from exchange.server.views import review_message
from helpers import FakeClock, open_market
from test_delta_round import ScriptedNormal, make_round, trade


def started(draws=(), **changes):
    clock = FakeClock()
    exchange = Exchange(clock=clock, rng=ScriptedNormal(*draws))
    for name in ("Alice", "Bob"):
        exchange.join(name)
    exchange.create_round(make_round(**changes))
    exchange.start_round("SPX")
    return exchange, clock


def step(exchange, clock, seconds):
    clock.now = clock.now.replace(second=seconds)
    exchange.step_round("SPX")


# --- The review chart -------------------------------------------------------------------------


def test_an_ordinary_market_has_no_round_lines(ex):
    alice = ex.join("Alice")[0].trader_id
    assert review_message(ex, "cars", alice)["round"] is None


def test_the_underlyings_chart_has_its_fair_price_after_every_move():
    ex, clock = started(draws=(0.5, -1.0))
    step(ex, clock, 1)
    step(ex, clock, 2)
    moves = ex.rounds["SPX"].history

    fair = review_message(ex, "SPX", "t1")["round"]["fair"]

    assert fair == [
        {"time": "2026-09-27T09:30:00", "value": 100},  # the start, when the round opened
        {"time": "2026-09-27T09:30:01", "value": moves[0].price},
        {"time": "2026-09-27T09:30:02", "value": moves[1].price},
    ]


def test_an_options_chart_has_its_black_scholes_value_at_each_move():
    ex, clock = started(draws=(0.5,))
    step(ex, clock, 1)
    price = ex.rounds["SPX"].history[0].price

    fair = review_message(ex, "SPX-C-100", "t1")["round"]["fair"]

    assert [point["value"] for point in fair] == [
        pytest.approx(black_scholes("call", 100, 100, 0.2, 1).value),
        pytest.approx(black_scholes("call", price, 100, 0.2, 59 / 60).value),
    ]


def test_the_net_delta_line_holds_what_the_trader_carried_through_each_second():
    ex, clock = started(draws=(0.5, 0.5))
    step(ex, clock, 1)                       # Alice holds nothing through the first second
    trade(ex, "SPX-C-100", "t1", "t2", 8, 10)
    carried = ex.net_delta("SPX", "t1")
    step(ex, clock, 2)

    line = review_message(ex, "SPX", "t1")["round"]["net_delta"]

    assert line == [
        {"time": "2026-09-27T09:30:00", "value": 0},
        {"time": "2026-09-27T09:30:01", "value": pytest.approx(carried)},
        # and from the last move until now, what she holds at the moment
        {"time": "2026-09-27T09:30:02", "value": pytest.approx(ex.net_delta("SPX", "t1"))},
    ]


def test_every_market_in_the_round_shows_the_traders_net_delta_and_hedging_error():
    ex, clock = started(draws=(0.5,))
    trade(ex, "SPX-C-100", "t1", "t2", 8, 10)
    step(ex, clock, 1)

    for market_id in ("SPX", "SPX-C-100", "SPX-P-95"):
        round_lines = review_message(ex, market_id, "t1")["round"]
        assert round_lines["title"] == "S&P"
        assert len(round_lines["net_delta"]) == 2
        assert round_lines["hedging_error"] == pytest.approx(ex.hedging_error("SPX", "t1"))


def test_a_settled_round_has_no_live_point_after_the_last_move():
    ex, clock = started(draws=(0.0,) * 60)
    for second in range(1, 60):
        step(ex, clock, second)
    clock.now = clock.now.replace(minute=31, second=0)
    ex.step_round("SPX")                     # the 60th move settles the round

    round_lines = review_message(ex, "SPX", "t1")["round"]
    assert len(round_lines["fair"]) == 61
    assert len(round_lines["net_delta"]) == 60


# --- The export -------------------------------------------------------------------------------


def unzip(exchange):
    with zipfile.ZipFile(io.BytesIO(export_zip(exchange))) as archive:
        return {name: archive.read(name).decode("utf-8-sig") for name in archive.namelist()}


def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_a_game_without_rounds_exports_the_same_four_files(ex):
    assert sorted(unzip(ex)) == ["events.csv", "events.json", "orders.csv", "trades.csv"]


def test_moves_csv_has_every_move_of_the_underlying():
    ex, clock = started(draws=(0.5, -2.0))
    step(ex, clock, 1)
    step(ex, clock, 2)

    rows = csv_rows(unzip(ex)["moves.csv"])

    moves = ex.rounds["SPX"].history
    assert [(row["round_id"], row["step"], row["draw"]) for row in rows] == [
        ("SPX", "1", "0.5"), ("SPX", "2", "-2.0")]
    assert rows[0]["time"] == "2026-09-27 05:30:01.000"
    assert float(rows[1]["price"]) == pytest.approx(moves[1].price)


def test_rounds_csv_has_each_traders_hedging_error_and_the_house():
    ex, clock = started(draws=(0.5,))
    trade(ex, "SPX-C-100", "t1", "t2", 8, 10)
    step(ex, clock, 1)

    rows = csv_rows(unzip(ex)["rounds.csv"])

    by_trader = {row["trader"]: row for row in rows}
    assert list(by_trader) == ["Alice", "Bob", "House"]
    assert float(by_trader["Alice"]["hedging_error"]) == \
        pytest.approx(ex.hedging_error("SPX", "t1"))
    assert float(by_trader["Alice"]["net_delta"]) == pytest.approx(ex.net_delta("SPX", "t1"))
    assert by_trader["House"]["hedging_error"] == ""  # the house isn't scored
    assert by_trader["Alice"]["round"] == "S&P"


def test_a_trader_who_never_held_an_option_has_a_blank_hedging_error():
    ex, clock = started(draws=(0.5,))
    step(ex, clock, 1)
    rows = {row["trader"]: row for row in csv_rows(unzip(ex)["rounds.csv"])}
    assert rows["Bob"]["hedging_error"] == ""


def test_the_house_is_named_house_in_the_export():
    ex, _ = started()
    ask = ex.markets["SPX"].book.best_ask()
    ex.take("SPX", "t1", Side.BUY, ask, 1)

    (row,) = csv_rows(unzip(ex)["trades.csv"])
    assert (row["buyer"], row["seller"]) == ("Alice", "House")
    orders = csv_rows(unzip(ex)["orders.csv"])
    assert {row["trader"] for row in orders} == {"Alice", "House"}
    events = csv_rows(unzip(ex)["events.csv"])
    assert HOUSE_ID not in {row["trader"] for row in events}
    assert "House" in {row["trader"] for row in events}


# --- The pages --------------------------------------------------------------------------------


@pytest.fixture
def client():
    exchange = Exchange()
    open_market(exchange)
    with TestClient(create_app(Room(code="1234", exchange=exchange), admin_secret="x")) as client:
        yield client


def test_the_admin_page_has_the_round_form_and_the_greeks_toggle(client):
    page = client.get("/admin").text
    for element in ('id="round-form"', 'id="r-calls"', 'id="r-puts"', 'id="r-realized-vol"',
                    'id="r-implied-vol"', 'id="rounds"', 'id="toggle-greeks"'):
        assert element in page


def test_the_trader_page_has_a_bar_for_each_round(client):
    page = client.get("/").text
    assert 'id="rounds"' in page
    assert 'id="round-bar"' in page  # the template for one round's bar
    assert 'class="greeks muted"' in page  # each column's fair value and greeks line


def test_the_review_page_has_the_fair_and_net_delta_keys(client):
    page = client.get("/review").text
    assert 'class="key fair"' in page
    assert 'class="key delta"' in page
