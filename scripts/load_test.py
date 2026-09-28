"""Load test: 60 bot traders play a whole game against a running server, like a busy meeting.

Run this before a meeting, against the real server on Render (or a local one). The script logs in
as the admin and runs everything itself:

  1. 60 bots join ("Bot 01" ... "Bot 60"), and an admin connection watches throughout.
  2. It creates a market and runs Trade or Tighten: bots bid widths, the winner quotes, and
     about 2/3 of the rest choose a side; the timer assigns the others a random side.
  3. Continuous trading: each bot acts about once every 2 seconds for 2 minutes (limit orders,
     two-sided quotes, clicking the best bid/offer, cancels).
  4. It settles the market and checks the results.

It passes when 95% of commands get their reply within 750 ms, nothing errors or disconnects,
every forced trade printed, positions add up to 0 and, after settlement, PnL adds up to 0.

Usage (PowerShell, from the project folder with the venv active):

    $env:ADMIN_SECRET="the-server's-admin-password"
    python scripts/load_test.py https://<your-app>.onrender.com
    python scripts/load_test.py http://127.0.0.1:8000          (a server on this laptop)

It exits with code 0 on a pass and 1 on a fail. It does NOT reset the game afterwards: look at
the results on the admin page, then STOP this script before pressing Reset (bots still connected
at a reset are carried into the new game).

How commands are timed: every bot command carries a "ref", and the server replies "done" once it
has fully handled the command, including telling every trader and the admin. So a round trip
here is the longest a student could wait to see the result of any command.
"""

import argparse
import asyncio
import json
import math
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import websockets

MAX_P95_MS = 750
REPLY_TIMEOUT_SECONDS = 30  # a command with no reply after this long counts as an error
CONNECT_TIMEOUT_SECONDS = 120  # generous: a sleeping Render service takes about a minute to wake
WIDTH_BIDDERS = 20  # how many bots bid a width in the auction
ACTION_MIX = {"limit": 35, "quote": 15, "take": 25, "cancel": 15, "cancel_all": 10}  # percent


# --- Small helpers --------------------------------------------------------------------------


def websocket_url(base, path):
    """ "https://x.onrender.com" + "/ws" -> "wss://x.onrender.com/ws" (ws:// for http://)."""
    base = base.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base.removeprefix("https://") + path
    return "ws://" + base.removeprefix("http://") + path


def percentile(values, fraction):
    """Nearest-rank percentile: the smallest value that at least `fraction` of values are <=."""
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def snap_to_tick(price, tick):
    """The nearest price on the tick."""
    return round(price / tick) * tick


# --- Results --------------------------------------------------------------------------------


@dataclass
class Results:
    latencies_ms: list = field(default_factory=list)  # one per bot command: send -> "done"
    actions: Counter = field(default_factory=Counter)  # command type -> count
    rejections: Counter = field(default_factory=Counter)  # reason -> count (normal refusals)
    errors: list = field(default_factory=list)  # anything that should never happen
    messages: int = 0
    message_bytes: int = 0
    trading_seconds: float = 0.0
    # Filled in at the end of the run (None if the run stopped before getting there):
    bots_disconnected: list = None  # bot names the admin page shows as disconnected
    forced_printed: int = None
    forced_expected: int = None
    position_sum: int = None
    pnl_sum: float = None


def checks(results):
    """[(label, passed), ...]: the load test passes only if every check passes."""
    latencies = results.latencies_ms
    p95 = percentile(latencies, 0.95) if latencies else None
    p95_text = f"{p95:.0f} ms" if p95 is not None else "no commands timed"
    return [
        (f"p95 round trip under {MAX_P95_MS} ms ({p95_text})",
         p95 is not None and p95 < MAX_P95_MS),
        (f"no errors ({len(results.errors)})", not results.errors),
        ("every bot still connected",
         results.bots_disconnected is not None and not results.bots_disconnected),
        (f"forced trades printed ({results.forced_printed} of {results.forced_expected})",
         results.forced_expected is not None
         and results.forced_printed == results.forced_expected),
        (f"positions sum to 0 ({results.position_sum})", results.position_sum == 0),
        (f"total PnL sums to 0 after settlement ({results.pnl_sum})",
         results.pnl_sum is not None and abs(results.pnl_sum) < 1e-6),
    ]


# --- Connections ----------------------------------------------------------------------------


class Connection:
    """One WebSocket that keeps reading everything the server sends, so the server never waits
    on us. Subclasses decide what to do with each message."""

    def __init__(self, name, results):
        self.name = name
        self.results = results
        self.socket = None
        self.closing = False  # True once we close it ourselves (so it isn't an error)
        self.connected = False

    async def open(self, url, first_message):
        self.socket = await websockets.connect(url, max_size=None,
                                               open_timeout=CONNECT_TIMEOUT_SECONDS)
        self.connected = True
        await self.socket.send(json.dumps(first_message))

    async def read_forever(self):
        try:
            async for text in self.socket:
                self.results.messages += 1
                self.results.message_bytes += len(text)
                self.handle(json.loads(text))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.connected = False
            if not self.closing:
                self.results.errors.append(f"{self.name}: disconnected")
            self.on_disconnect()

    async def close(self):
        self.closing = True
        await self.socket.close()

    def handle(self, message):
        raise NotImplementedError

    def on_disconnect(self):
        pass


class Bot(Connection):
    """A simulated trader. `state` is the latest snapshot/update the server sent it."""

    def __init__(self, name, results):
        super().__init__(name, results)
        self.trader_id = None
        self.state = None
        self.forced_trades = Counter()  # market_id -> forced trades seen on the tape
        self.next_ref = 1
        self.waiting = {}  # ref -> future resolved when "done" arrives

    async def join(self, url, code):
        await self.open(url, {"type": "join", "code": code, "name": self.name})
        welcome = json.loads(await self.socket.recv())
        if welcome["type"] != "welcome":
            raise RuntimeError(f"{self.name} could not join: {welcome.get('reason')}")
        self.trader_id = welcome["trader_id"]
        self.handle(json.loads(await self.socket.recv()))  # the snapshot
        asyncio.create_task(self.read_forever())

    async def do(self, **command):
        """Send one command and wait until the server says it is done. Records the round trip."""
        if not self.connected:
            return
        ref = self.next_ref
        self.next_ref += 1
        reply = asyncio.get_running_loop().create_future()
        self.waiting[ref] = reply
        self.results.actions[command["type"]] += 1
        started = time.perf_counter()
        try:
            await self.socket.send(json.dumps({**command, "ref": ref}))
            await asyncio.wait_for(reply, REPLY_TIMEOUT_SECONDS)
        except websockets.ConnectionClosed:
            return  # already recorded as a disconnect
        except TimeoutError:
            self.results.errors.append(f"{self.name}: no reply to {command['type']}")
            return
        finally:
            self.waiting.pop(ref, None)
        if self.connected:
            self.results.latencies_ms.append((time.perf_counter() - started) * 1000)

    def handle(self, message):
        kind = message["type"]
        if kind == "snapshot":
            self.state = message
            for market_id, trades in message["tape"].items():
                self.forced_trades[market_id] = sum(1 for trade in trades if trade["forced"])
        elif kind == "update":
            self.state = message
            for trade in message["new_trades"]:
                if trade["forced"]:
                    self.forced_trades[trade["market_id"]] += 1
        elif kind == "done":
            reply = self.waiting.get(message["ref"])
            if reply is not None and not reply.done():
                reply.set_result(None)
        elif kind == "rejected":
            if message["reason"] == "bad message":  # the script sent something malformed
                self.results.errors.append(f"{self.name}: bad message ({message['command']})")
            else:  # a normal trading refusal, e.g. "would trade with your own order"
                self.results.rejections[message["reason"]] += 1
        elif kind in ("error", "kicked", "opened_elsewhere"):
            self.results.errors.append(f"{self.name}: {kind} {message.get('reason', '')}")

    def on_disconnect(self):
        for reply in self.waiting.values():  # nobody will answer these now
            if not reply.done():
                reply.set_result(None)

    # What this bot can see of one market.
    def market(self, market_id):
        return self.state["markets"][market_id]

    def my_orders(self, market_id):
        return self.state["me"][market_id]["orders"]


class Admin(Connection):
    """The admin page's connection. `state` is the latest admin state."""

    def __init__(self, results):
        super().__init__("admin", results)
        self.state = None
        self.changed = asyncio.Event()
        self.refusal = None  # the latest "admin_error" reason, until a command reads it

    async def log_in(self, url, secret):
        await self.open(url, {"type": "admin_login", "secret": secret})
        reply = json.loads(await self.socket.recv())
        if reply["type"] != "admin_welcome":
            raise RuntimeError(f"admin login refused: {reply.get('reason')}")
        self.handle(json.loads(await self.socket.recv()))  # the first admin state
        asyncio.create_task(self.read_forever())

    def handle(self, message):
        if message["type"] == "admin_state":
            self.state = message
        elif message["type"] == "admin_error":
            self.refusal = message["reason"]
        self.changed.set()

    async def do(self, until, timeout=REPLY_TIMEOUT_SECONDS, **command):
        """Send an admin command, then wait until `until(state)` is true."""
        self.refusal = None
        await self.socket.send(json.dumps(command))
        await self.wait_for(until, timeout, what=command["type"])

    async def wait_for(self, until, timeout, what):
        deadline = time.monotonic() + timeout
        while not until(self.state):
            if self.refusal:
                raise RuntimeError(f"admin {what} refused: {self.refusal}")
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), deadline - time.monotonic())
            except TimeoutError:
                raise RuntimeError(f"admin {what}: nothing happened for {timeout} s") from None

    def status(self, market_id):
        return self.state["markets"][market_id]["status"]


# --- The game -------------------------------------------------------------------------------


async def run(args, secret):
    """Play one whole game with bots. Returns the Results (see checks() for pass/fail)."""
    results = Results()
    rng = random.Random(args.seed)
    admin = Admin(results)
    bots = []
    try:
        await admin.log_in(websocket_url(args.url, "/admin/ws"), secret)
        code = admin.state["room_code"]

        print(f"Joining {args.bots} bots to room {code}...")
        for number in range(1, args.bots + 1):
            bot = Bot(f"Bot {number:02d}", results)
            await bot.join(websocket_url(args.url, "/ws"), code)
            bots.append(bot)

        market_id = await create_market(admin, args)
        await trade_or_tighten(admin, bots, market_id, args, rng, results)

        print(f"Continuous trading for {args.seconds} s "
              f"(each bot acts about every {args.interval} s)...")
        started = time.monotonic()
        deadline = started + args.seconds
        await asyncio.gather(*(trade_until(bot, market_id, deadline, args, rng) for bot in bots))
        results.trading_seconds = time.monotonic() - started

        print(f"Settling at {args.center:,}...")
        await admin.do(lambda state: state["markets"][market_id]["status"] == "settled",
                       type="settle", market_id=market_id, value=args.center)
        record_final_checks(admin, bots, market_id, results)
    except RuntimeError as error:
        results.errors.append(str(error))
    finally:
        for connection in [*bots, admin]:
            if connection.connected:
                await connection.close()
    return results


async def create_market(admin, args):
    title = f"Load test {time.strftime('%H:%M:%S')}"
    await admin.do(lambda state: any(m["title"] == title for m in state["markets"].values()),
                   type="create_market", title=title, tick_size=args.tick, max_position=50,
                   forced_trade_size=1, forced_trade_seconds=args.forced_seconds)
    (market_id,) = [m_id for m_id, m in admin.state["markets"].items() if m["title"] == title]
    print(f"Created market {market_id}: {title!r}")
    return market_id


async def trade_or_tighten(admin, bots, market_id, args, rng, results):
    """Width auction, MM quote, forced trade (ended by the server's timer)."""
    def status_is(wanted):
        return lambda state: state["markets"][market_id]["status"] == wanted

    await admin.do(status_is("auction"), type="start_auction", market_id=market_id)
    # Everyone bids at once; widths that aren't narrower than the best are refused (normal).
    bidders = rng.sample(bots, min(WIDTH_BIDDERS, len(bots)))
    await asyncio.gather(*(
        bot.do(type="width", market_id=market_id, width=rng.randint(1, 20) * args.tick)
        for bot in bidders
    ))
    await admin.do(status_is("mm_quoting"), type="close_auction", market_id=market_id)

    tot = admin.state["markets"][market_id]["tot"]
    (mm,) = [bot for bot in bots if bot.name == tot["mm"]]
    center = snap_to_tick(args.center, args.tick)
    bid = center - (tot["width"] // args.tick // 2) * args.tick
    print(f"Trade or Tighten: {mm.name} makes the market {bid:,} @ {bid + tot['width']:,}")
    await mm.do(type="mm_quote", market_id=market_id, bid=bid, ask=bid + tot["width"])

    await admin.do(status_is("forced_trade"), type="start_forced_trade", market_id=market_id)
    # Everyone in the room except the MM must trade (offline and human traders too).
    results.forced_expected = sum(1 for trader in admin.state["traders"]
                                  if not trader["kicked"]) - 1
    others = [bot for bot in bots if bot is not mm]
    choosers = rng.sample(others, round(len(others) * 2 / 3))
    choices = {bot: rng.choice(["buy", "sell"]) for bot in choosers}
    await asyncio.gather(*(bot.do(type="choose_side", market_id=market_id, side=side)
                           for bot, side in choices.items()))
    changers = rng.sample(choosers, len(choosers) // 4)  # some change their mind
    await asyncio.gather(*(
        bot.do(type="choose_side", market_id=market_id,
               side="sell" if choices[bot] == "buy" else "buy")
        for bot in changers
    ))
    print(f"Forced trade: {len(choosers)} bots chose a side; waiting for the "
          f"{args.forced_seconds} s timer to assign the rest...")
    await admin.wait_for(status_is("open"), args.forced_seconds + REPLY_TIMEOUT_SECONDS,
                         what="forced-trade timer")


async def trade_until(bot, market_id, deadline, args, rng):
    """One bot's continuous trading: wait a random while, act, repeat until the deadline."""
    while bot.connected:
        await asyncio.sleep(rng.uniform(0, 2 * args.interval))
        if time.monotonic() >= deadline:
            return
        await bot.do(**random_action(bot, market_id, args, rng))


def random_action(bot, market_id, args, rng):
    """One random command, with prices near the current mark."""
    market = bot.market(market_id)
    mine = bot.my_orders(market_id)
    tick = args.tick
    mark = market["mark"] if market["mark"] is not None else args.center

    kind = rng.choices(list(ACTION_MIX), weights=list(ACTION_MIX.values()))[0]
    if kind == "take" and market["best_bid"] is None and market["best_ask"] is None:
        kind = "limit"  # nothing to click on
    if kind in ("cancel", "cancel_all") and not mine:
        kind = "limit"  # nothing to cancel

    if kind == "take":  # click the best bid (sell) or the best offer (buy)
        sides = [side for side, price in (("sell", market["best_bid"]),
                                          ("buy", market["best_ask"])) if price is not None]
        side = rng.choice(sides)
        price = market["best_bid"] if side == "sell" else market["best_ask"]
        return {"type": "take", "market_id": market_id, "side": side, "price": price,
                "size": rng.randint(1, 3)}
    if kind == "cancel":
        return {"type": "cancel", "market_id": market_id,
                "order_id": rng.choice(mine)["order_id"]}
    if kind == "cancel_all":
        return {"type": "cancel_all", "market_id": market_id}
    if kind == "quote":
        return {"type": "quote", "market_id": market_id,
                "bid_price": snap_to_tick(mark, tick) - rng.randint(1, 5) * tick,
                "ask_price": snap_to_tick(mark, tick) + rng.randint(1, 5) * tick,
                "size": rng.randint(1, 5)}
    return {"type": "limit", "market_id": market_id, "side": rng.choice(["buy", "sell"]),
            "price": snap_to_tick(mark + rng.randint(-5, 5) * tick, tick),
            "size": rng.randint(1, 5)}


def record_final_checks(admin, bots, market_id, results):
    """Read the end-of-game numbers from the admin state (taken after settlement)."""
    bot_names = {bot.name for bot in bots}
    traders = admin.state["traders"]
    results.bots_disconnected = [trader["name"] for trader in traders
                                 if trader["name"] in bot_names and not trader["connected"]]
    results.forced_printed = bots[0].forced_trades[market_id]
    results.position_sum = sum(p["position"] for p in admin.state["markets"][market_id]["positions"])
    results.pnl_sum = sum(trader["markets"][market_id]["total"] for trader in traders)


# --- Report ---------------------------------------------------------------------------------


def print_summary(args, results):
    print()
    print(f"Load test: {args.bots} bots, one action per bot every ~{args.interval} s")
    latencies = results.latencies_ms
    if latencies:
        print(f"Round trip (send -> done): p50 {percentile(latencies, 0.50):.0f} ms, "
              f"p95 {percentile(latencies, 0.95):.0f} ms, max {max(latencies):.0f} ms, "
              f"over {len(latencies):,} commands")
    if results.trading_seconds:
        trading = sum(results.actions[kind] for kind in ACTION_MIX)
        print(f"Trading rate: {trading / results.trading_seconds:.1f} actions/s "
              f"(target {args.bots / args.interval:.1f}/s)")
    print("Commands: " + ", ".join(f"{kind} {count:,}"
                                   for kind, count in results.actions.most_common()))
    if results.rejections:
        print("Normal refusals (not failures):")
        for reason, count in results.rejections.most_common(10):
            print(f"  {count:6,}  {reason}")
    print(f"Received {results.messages:,} messages, {results.message_bytes / 1e6:.1f} MB of JSON "
          "(before compression)")
    for error in results.errors[:20]:
        print(f"  ERROR  {error}")
    if len(results.errors) > 20:
        print(f"  ... and {len(results.errors) - 20} more errors")
    print("Checks:")
    all_passed = True
    for label, passed in checks(results):
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
        all_passed = all_passed and passed
    print(f"RESULT: {'PASS' if all_passed else 'FAIL'}")
    return all_passed


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Load-test the exchange with bot traders.")
    parser.add_argument("url", help="the server, e.g. https://<your-app>.onrender.com or "
                                    "http://127.0.0.1:8000")
    parser.add_argument("--bots", type=int, default=60)
    parser.add_argument("--seconds", type=float, default=120, help="continuous trading time")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="average seconds between one bot's actions")
    parser.add_argument("--center", type=int, default=50_000,
                        help="price the market trades around, and the settlement value")
    parser.add_argument("--tick", type=int, default=100)
    parser.add_argument("--forced-seconds", type=int, default=15,
                        help="the forced-trade timer")
    parser.add_argument("--seed", type=int, default=None, help="repeat the same random choices")
    return parser.parse_args(argv)


def main():
    args = parse_args(sys.argv[1:])
    secret = os.environ.get("ADMIN_SECRET", "").strip()
    if not secret:
        raise SystemExit('Set ADMIN_SECRET to the server\'s admin password first, e.g. in '
                         'PowerShell: $env:ADMIN_SECRET="the-password"')
    results = asyncio.run(run(args, secret))
    sys.exit(0 if print_summary(args, results) else 1)


if __name__ == "__main__":
    main()
