"""The load-test script (scripts/load_test.py): its small helpers, its pass/fail checks, and one
short run against a real server."""

import asyncio
import socket
import threading
import time

import pytest
import uvicorn

from exchange.engine import Exchange
from exchange.server.app import create_app
from exchange.server.room import Room
from scripts import load_test

SECRET = "load-test-secret"


# --- Helpers --------------------------------------------------------------------------------


@pytest.mark.parametrize("base, expected", [
    ("https://psuq.onrender.com", "wss://psuq.onrender.com/ws"),
    ("https://psuq.onrender.com/", "wss://psuq.onrender.com/ws"),
    ("http://127.0.0.1:8000", "ws://127.0.0.1:8000/ws"),
])
def test_websocket_url_follows_the_page_scheme(base, expected):
    assert load_test.websocket_url(base, "/ws") == expected


def test_percentile_uses_the_nearest_rank():
    values = list(range(1, 101))  # 1..100

    assert load_test.percentile(values, 0.50) == 50
    assert load_test.percentile(values, 0.95) == 95
    assert load_test.percentile(values, 1.00) == 100
    assert load_test.percentile([7], 0.95) == 7


def test_prices_snap_to_the_nearest_tick():
    assert load_test.snap_to_tick(50_049, 100) == 50_000
    assert load_test.snap_to_tick(50_051, 100) == 50_100
    assert load_test.snap_to_tick(50_025.0, 50) == 50_000


# --- Pass / fail ----------------------------------------------------------------------------


def good_results():
    results = load_test.Results()
    results.latencies_ms = [10.0] * 99 + [400.0]
    results.bots_disconnected = []
    results.forced_printed = results.forced_expected = 59
    results.position_sum = 0
    results.pnl_sum = 0
    return results


def failed_checks(results):
    return [label for label, passed in load_test.checks(results) if not passed]


def test_good_results_pass_every_check():
    assert failed_checks(good_results()) == []


def test_a_slow_p95_fails():
    results = good_results()
    results.latencies_ms = [10.0] * 90 + [750.0] * 10

    (failure,) = failed_checks(results)
    assert "p95" in failure


@pytest.mark.parametrize("change, wanted", [
    (lambda r: r.errors.append("Bot 03: bad message"), "no errors"),
    (lambda r: r.bots_disconnected.append("Bot 07"), "connected"),
    (lambda r: setattr(r, "forced_printed", 58), "forced trades"),
    (lambda r: setattr(r, "position_sum", 1), "positions"),
    (lambda r: setattr(r, "pnl_sum", 100), "PnL"),
])
def test_each_problem_fails_its_check(change, wanted):
    results = good_results()
    change(results)

    (failure,) = failed_checks(results)
    assert wanted in failure


def test_a_run_that_stopped_early_fails():
    # e.g. the admin was refused: the end-of-run numbers were never filled in.
    results = load_test.Results()
    results.errors.append("admin: wrong admin secret")

    assert len(failed_checks(results)) == len(load_test.checks(results))


# --- A short run against a real server ------------------------------------------------------


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def server_url():
    """A real uvicorn server (like the one on Render) running in a background thread."""
    app = create_app(Room(code="1234", exchange=Exchange()), admin_secret=SECRET)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the test server didn't start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_a_short_load_test_passes(server_url):
    args = load_test.parse_args([server_url, "--bots", "5", "--seconds", "4",
                                 "--interval", "0.2", "--forced-seconds", "2", "--seed", "1"])

    results = asyncio.run(load_test.run(args, SECRET))

    assert failed_checks(results) == [], results.errors
    assert results.forced_expected == 4  # every bot except the market maker
    assert sum(results.actions.values()) > 20
