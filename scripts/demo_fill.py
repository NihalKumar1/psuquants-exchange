"""Fill the order book with bot traders, for screenshots and demos.

This is not part of the app: it connects to a running server over WebSockets, the same way a
browser does, joins 8 bots, posts a book around a centre price, and makes a few trades.

Screenshot setup (PowerShell, from the project folder with the venv active):

    $env:ADMIN_SECRET="choose-a-password"
    uvicorn exchange.server.main:app --workers 1

Open http://localhost:8000/admin, log in, create a market (e.g. "Cars registered in Centre
County, PA", tick 500, max position 50) and click Open. The bots trade in the same market the
trader page shows (the newest one that isn't settled). Then, in a second terminal:

    python scripts/demo_fill.py <room code> --center 120000

Then open http://localhost:8000, join as yourself, and take the screenshot.
The bots stay connected (so their names stay in the book) until you press Ctrl+C.
"""

import argparse
import asyncio
import json
import random

import websockets

# Bidders only ever bid and lift offers; offerers only ever offer and hit bids.
# Keeping the two groups apart means no bot can ever trade with its own order.
BIDDER_NAMES = ["Nihal Kumar", "Ahnaf Iftikhar", "Jeremy Hsu", "Akshay Honawar"]
OFFERER_NAMES = ["Pranay Mahesh", "Tarun Shetru", "Jaden Jeon", "Preston Jones"]
LEVELS = 6   # price levels on each side
TRADES = 6   # trades made after the book is posted


class Bot:
    def __init__(self, name):
        self.name = name
        self.socket = None
        self.reader = None

    async def join(self, url, code):
        """Join the room and return the snapshot the server sends back."""
        self.socket = await websockets.connect(url)
        await self.send(type="join", code=code, name=self.name)
        welcome = json.loads(await self.socket.recv())
        if welcome["type"] != "welcome":
            raise SystemExit(f"{self.name} could not join: {welcome.get('reason')}")
        snapshot = json.loads(await self.socket.recv())
        self.reader = asyncio.create_task(self.read_forever())
        return snapshot

    async def send(self, **message):
        await self.socket.send(json.dumps(message))

    async def read_forever(self):
        # Keep reading so the server never waits on us; mention anything rejected.
        async for text in self.socket:
            message = json.loads(text)
            if message["type"] == "rejected":
                print(f"  ({self.name}: rejected, {message['reason']})")


async def main(args):
    rng = random.Random(args.seed)
    bidders = [Bot(name) for name in BIDDER_NAMES]
    offerers = [Bot(name) for name in OFFERER_NAMES]

    snapshot = None
    for bot in bidders + offerers:
        snapshot = await bot.join(args.url, args.code)

    # The same market the trader page shows: the newest one that isn't settled.
    markets = list(snapshot["markets"].values())
    unsettled = [m for m in markets if m["status"] != "settled"]
    market = (unsettled or markets)[-1]
    market_id = market["market_id"]
    tick = market["tick_size"]
    center = round(args.center / tick) * tick
    print(f"Filling '{market['title']}' around {center:,} (tick {tick:,})")

    # The book: LEVELS bids below the centre and LEVELS offers above it.
    # The best level on each side is thicker, so the trades below don't use it up.
    for k in range(1, LEVELS + 1):
        traders, sizes = (2, (5, 8)) if k == 1 else (rng.randint(1, 2), (1, 5))
        for bot in rng.sample(bidders, traders):
            await bot.send(type="limit", market_id=market_id, side="buy",
                           price=center - k * tick, size=rng.randint(*sizes))
        for bot in rng.sample(offerers, traders):
            await bot.send(type="limit", market_id=market_id, side="sell",
                           price=center + k * tick, size=rng.randint(*sizes))
        await asyncio.sleep(0.1)

    # A few trades, spread out in time: bidders lift the best offer, offerers hit the best bid.
    for i in range(TRADES):
        await asyncio.sleep(0.8)
        if i % 2 == 0:
            await rng.choice(bidders).send(type="take", market_id=market_id, side="buy",
                                           price=center + tick, size=rng.randint(1, 3))
        else:
            await rng.choice(offerers).send(type="take", market_id=market_id, side="sell",
                                            price=center - tick, size=rng.randint(1, 3))

    print("Book filled. The bots stay connected; press Ctrl+C to stop them.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fill the order book with bot traders.")
    parser.add_argument("code", help="the room code printed by the server")
    # 127.0.0.1 rather than "localhost": on Windows, "localhost" tries IPv6 first and each bot
    # waits about 2 seconds for that to fail, because uvicorn only listens on IPv4.
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    parser.add_argument("--center", type=int, default=120_000, help="price to build the book around")
    parser.add_argument("--seed", type=int, default=7, help="change for a different-looking book")
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        print("Bots disconnected.")
