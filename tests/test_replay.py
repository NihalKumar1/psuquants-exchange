"""The event log: state is a replay of the events, the log is JSON-friendly, markets are isolated."""

import json
from dataclasses import replace

from exchange.engine import Exchange, PnL, Side, from_dict, to_dict
from helpers import accepted_id, asks, bids, make_config, open_market

BUY, SELL = Side.BUY, Side.SELL


def run_session(ex, clock):
    """A little two-market game with trades, clipping, rejects, quotes, takes and cancels,
    plus every admin command."""
    open_market(ex, market_id="cars", tick_size=1, max_position=100)
    open_market(ex, market_id="homes", tick_size=100, max_position=50)

    clock.set("09:31:00")
    ex.place_quote("cars", "mm", 35, 38, 20)
    ex.take("cars", "alice", BUY, 38, 10)
    ex.take("cars", "bob", SELL, 35, 5)
    ex.place_limit("cars", "alice", BUY, 36, 200)  # clipped
    ex.place_limit("cars", "bob", SELL, 36, 3)
    order_id = accepted_id(ex.place_limit("cars", "bob", SELL, 37, 4))
    ex.cancel("cars", "bob", order_id)
    ex.place_limit("cars", "bob", BUY, 36.5, 1)  # rejected: off tick

    clock.set("09:32:00")
    ex.place_limit("homes", "alice", SELL, 250_000, 30)
    ex.take("homes", "bob", BUY, 250_000, 60)  # clipped to 50 room, 30 available
    ex.place_limit("homes", "mm", BUY, 240_000, 10)
    ex.place_limit("homes", "mm", SELL, 240_000, 2)  # rejected: would trade with own bid
    ex.cancel_all("mm")

    clock.set("09:33:00")
    ex.mark_info_drop("Hint: under 300,000")
    ex.halt_market("cars")
    ex.place_limit("cars", "alice", BUY, 30, 1)  # rejected: halted
    ex.resume_market("cars")
    carol = ex.join("Carol")[0].trader_id
    dave = ex.join("Dave")[0].trader_id
    ex.place_limit("cars", carol, SELL, 39, 2)
    ex.place_limit("cars", carol, BUY, 20, 2)
    ex.take("cars", dave, BUY, 39, 1)
    ex.rename(carol, "Caroline")
    ex.kick(carol)  # cancels her resting orders
    ex.lock_joining()
    ex.settle_market("homes", 245_123)
    ex.create_market(make_config(market_id="later"))
    ex.edit_market(replace(make_config(market_id="later"), title="Edited", tick_size=5))


def snapshot(ex):
    """Everything a trader or the admin could see, as plain values."""
    state = {
        "next_ids": (ex.next_order_id, ex.next_trade_id, ex.next_trader_id),
        "traders": ex.traders,
        "kicked": ex.kicked,
        "info_drops": ex.info_drops,
        "joining_locked": ex.joining_locked,
    }
    for market_id, market in ex.markets.items():
        traders = sorted(market.positions)
        state[market_id] = {
            "config": market.config,
            "status": market.status,
            "settlement_value": market.settlement_value,
            "bids": bids(ex, market_id),
            "asks": asks(ex, market_id),
            "trades": ex.trades(market_id),
            "mark": ex.mark(market_id),
            "positions": {t: ex.position(market_id, t) for t in traders},
            "pnl": {t: ex.pnl(market_id, t) for t in traders},
        }
    return state


def test_replaying_the_log_rebuilds_the_same_state(clock):
    live = Exchange(clock=clock)
    run_session(live, clock)

    replayed = Exchange.replay(live.events)

    assert snapshot(replayed) == snapshot(live)


def test_events_survive_a_json_round_trip(clock):
    live = Exchange(clock=clock)
    run_session(live, clock)

    text = json.dumps([to_dict(event) for event in live.events])
    events = [from_dict(data) for data in json.loads(text)]

    assert events == live.events
    assert snapshot(Exchange.replay(events)) == snapshot(live)


def test_log_is_numbered_in_order_and_timestamped_by_the_clock(clock):
    ex = Exchange(clock=clock)
    run_session(ex, clock)

    assert [event.seq for event in ex.events] == list(range(1, len(ex.events) + 1))
    assert ex.events[-1].ts == clock.now


def test_replayed_exchange_carries_on_with_fresh_ids(clock):
    live = Exchange(clock=clock)
    run_session(live, clock)
    replayed = Exchange.replay(live.events, clock=clock)

    new_id = accepted_id(replayed.place_limit("cars", "carol", BUY, 30, 1))

    used_ids = {event.order_id for event in live.events if hasattr(event, "order_id")}
    assert new_id not in used_ids


def test_markets_are_isolated(ex):
    open_market(ex, market_id="homes")
    ex.place_limit("cars", "alice", SELL, 37, 5)

    ex.place_limit("homes", "bob", BUY, 40, 5)  # would cross in "cars", but this is "homes"

    assert ex.trades("homes") == []
    assert asks(ex, "cars") == [(37, [("alice", 5)])]
    assert bids(ex, "homes") == [(40, [("bob", 5)])]


def test_total_pnl_sums_every_market(ex):
    open_market(ex, market_id="homes")
    ex.place_limit("cars", "bob", SELL, 40, 10)
    ex.take("cars", "alice", BUY, 40, 10)
    ex.place_limit("cars", "bob", BUY, 45, 10)
    ex.place_limit("cars", "alice", SELL, 45, 10)  # alice realizes +50 in cars
    ex.place_limit("homes", "bob", BUY, 20, 4)
    ex.take("homes", "alice", SELL, 20, 4)  # alice short 4 @ 20, last price 20

    assert ex.pnl("cars", "alice") == PnL(realized=50, unrealized=0)
    assert ex.pnl("homes", "alice") == PnL(realized=0, unrealized=0)
    ex.place_limit("homes", "carol", BUY, 17, 1)
    ex.place_limit("homes", "dave", SELL, 19, 1)  # mid 18: alice +8 unrealized
    assert ex.total_pnl("alice") == PnL(realized=50, unrealized=8)
