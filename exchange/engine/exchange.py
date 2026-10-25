"""The Exchange: all markets plus the append-only event log.

Every command follows the same three steps:
1. a market decides which events the command produces (without changing anything),
2. each event is numbered, timestamped and appended to self.events,
3. each event is applied, which is the only way state changes.

Because of this, Exchange.replay(events) rebuilds exactly the same state from a saved log.
"""

import math
import random
from dataclasses import replace
from datetime import datetime, timezone

from . import bots
from .events import (
    AuctionStarted,
    BotsStarted,
    BotsStopped,
    ForcedTradeStarted,
    InfoDropMarked,
    JoiningLocked,
    JoiningUnlocked,
    MarketCreated,
    MarketEdited,
    MarketHalted,
    MarketMakerChosen,
    MarketOpened,
    MarketResumed,
    MarketSettled,
    OrderAccepted,
    Rejected,
    StartingEquitySet,
    TradeExecuted,
    TraderJoined,
    TraderKicked,
    TraderRenamed,
)
from .market import Market
from .models import MarketConfig, MarketStatus, Side, is_whole_number
from .positions import PnL

MAX_NAME_LENGTH = 20
DEFAULT_STARTING_EQUITY = 100_000  # dollars, until the admin sets another amount (SPEC Q148)
REMOVED = "you were removed from the game"


def utc_now():
    return datetime.now(timezone.utc)


class Exchange:
    def __init__(self, clock=utc_now, rng=None):
        self.clock = clock
        # Flips the coin for traders who don't choose in a forced trade. Replay never uses it:
        # each flip is recorded in the log as a SideAssigned event.
        self.rng = random.Random() if rng is None else rng
        self.events = []
        self.markets = {}  # market_id -> Market
        self.traders = {}  # trader_id -> display name, in the order they joined
        self.kicked = set()  # trader_ids the admin removed (they stay in self.traders)
        self.info_drops = []  # InfoDropMarked events, oldest first
        self.joining_locked = False
        self.bots = {}  # trader_id -> "maker" or "taker", for the traders that are bots
        self.bots_on = {"maker": False, "taker": False}  # the admin's two switches
        self.starting_equity = DEFAULT_STARTING_EQUITY
        self.next_trader_id = 1
        self.next_order_id = 1
        self.next_trade_id = 1

    @classmethod
    def replay(cls, events, clock=utc_now):
        """Build an exchange by applying saved events in order. Nothing is re-matched."""
        exchange = cls(clock=clock)
        for event in events:
            exchange.events.append(event)
            exchange.apply(event)
        return exchange

    # --- Admin commands (a mistake raises ValueError with a reason to show the admin) ------

    def create_market(self, config):
        if config.market_id in self.markets:
            raise ValueError(f"market {config.market_id!r} already exists")
        settings = check_settings(config)
        return self._record([MarketCreated(market_id=config.market_id, **settings)])

    def edit_market(self, config):
        """Change the settings of a market that hasn't opened yet."""
        self._check_status(config.market_id, {MarketStatus.CREATED},
                           "a market can be edited only before it opens")
        settings = check_settings(config)
        return self._record([MarketEdited(market_id=config.market_id, **settings)])

    def open_market(self, market_id):
        """Start continuous trading directly (skipping Trade or Tighten)."""
        self._check_status(market_id, {MarketStatus.CREATED}, "cannot open")
        return self._record([MarketOpened(market_id=market_id)])

    def halt_market(self, market_id):
        self._check_status(market_id, {MarketStatus.OPEN}, "cannot halt")
        return self._record([MarketHalted(market_id=market_id)])

    def resume_market(self, market_id):
        self._check_status(market_id, {MarketStatus.HALTED}, "cannot resume")
        return self._record([MarketResumed(market_id=market_id)])

    def settle_market(self, market_id, value):
        """Reveal the true value. It can be any whole number, even one off the tick."""
        self._check_status(market_id, {MarketStatus.OPEN, MarketStatus.HALTED}, "cannot settle")
        if not is_whole_number(value):
            raise ValueError("the settlement value must be a whole number")
        return self._record([MarketSettled(market_id=market_id, settlement_value=value)])

    def mark_info_drop(self, note=""):
        """Record that the instructor gave out information, for the review timeline."""
        if not isinstance(note, str):
            raise ValueError("the note must be text")
        return self._record([InfoDropMarked(note=note.strip())])

    def kick(self, trader_id):
        """Remove a trader for the rest of the game.

        Their resting orders are cancelled, except in settled markets (whose books are frozen).
        Their position stays and settles like anyone's. In Trade or Tighten their widths stop
        counting, and a kicked MM is replaced (see Market.decide_kick).
        """
        self._check_trader(trader_id)
        if trader_id in self.kicked:
            raise ValueError(f"{self.traders[trader_id]} was already removed")
        events = [
            event for market in self.markets.values() for event in market.decide_kick(trader_id)
        ]
        events.append(TraderKicked(trader_id=trader_id))
        return self._record(events)

    def rename(self, trader_id, new_name):
        self._check_trader(trader_id)
        new_name = self._check_name(new_name, renaming=trader_id)
        return self._record([TraderRenamed(trader_id=trader_id,
                                           old_name=self.traders[trader_id], new_name=new_name)])

    def set_starting_equity(self, amount):
        """The dollars everyone's equity starts at. It only changes the score: equity is this
        plus total PnL, so it can be changed at any time."""
        if not is_whole_number(amount) or amount < 0:
            raise ValueError("starting equity must be a whole number of dollars, 0 or more")
        return self._record([StartingEquitySet(amount=amount)])

    def lock_joining(self):
        if self.joining_locked:
            raise ValueError("joining is already locked")
        return self._record([JoiningLocked()])

    def unlock_joining(self):
        if not self.joining_locked:
            raise ValueError("joining is already unlocked")
        return self._record([JoiningUnlocked()])

    # --- Bots: admin commands (SPEC Q209-Q218) ---------------------------------------------

    def start_bots(self, kind):
        """Turn the makers (or takers) on. The first time, they join the game as traders."""
        self._check_bot_kind(kind)
        if self.bots_on[kind]:
            raise ValueError(f"the {kind}s are already on")
        events = []
        if kind not in self.bots.values():
            next_id = self.next_trader_id
            for name in bots.NAMES[kind]:
                self._check_name(name)  # a student may have taken it
                events.append(TraderJoined(trader_id=f"t{next_id}", name=name, bot=kind))
                next_id += 1
        events.append(BotsStarted(kind=kind))
        return self._record(events)

    def stop_bots(self, kind):
        """Turn the makers (or takers) off: their resting orders are cancelled. They stay in the
        game with their positions, and come back if turned on again."""
        self._check_bot_kind(kind)
        if not self.bots_on[kind]:
            raise ValueError(f"the {kind}s are already off")
        events = [
            event
            for trader_id in self._bot_ids(kind)
            for market in self.markets.values()
            for event in market.decide_cancel_all(trader_id, reason="bots turned off")
        ]
        events.append(BotsStopped(kind=kind))
        return self._record(events)

    def step_bots(self, kind):
        """One step of the makers (or takers), called by the server once a second while they are
        on. Each bot acts with a chance (bots.CHANCE_TO_ACT), using self.rng. What they do is
        logged as ordinary orders, so replay never runs a bot."""
        if not self.bots_on[kind]:
            return []
        events = []
        for bot_id in self._bot_ids(kind):
            if bot_id in self.kicked or self.rng.random() >= bots.CHANCE_TO_ACT[kind]:
                continue
            if kind == "maker":
                events += self._maker_requotes(bot_id)
            else:
                events += self._taker_trades(bot_id)
        return events

    def _maker_requotes(self, bot_id):
        """Replace the maker's quote in every open market it can price. A side it has no room
        for (max position) is left out, so it never sends an order that would be rejected."""
        events = []
        for market in self.markets.values():
            if market.status is not MarketStatus.OPEN:
                continue
            events += self._record(market.decide_cancel_all(bot_id, reason="re-quote"))
            quote = bots.maker_quote(market, bot_id, self.rng.randint(*bots.MAKER_HALF_WIDTH_TICKS))
            if quote is None:
                continue
            size = self.rng.randint(*bots.MAKER_SIZE)
            for side, price in zip((Side.BUY, Side.SELL), quote):
                if market.room(bot_id, side) >= 1:
                    events += self.place_limit(market.market_id, bot_id, side, price,
                                               min(size, market.room(bot_id, side)))
        return events

    def _taker_trades(self, bot_id):
        """Buy at the best offer or sell at the best bid in a random open market."""
        markets = [market for market in self.markets.values()
                   if market.status is MarketStatus.OPEN]
        if not markets:
            return []
        market = self.rng.choice(markets)
        side = self.rng.choice([Side.BUY, Side.SELL])
        price = bots.taker_price(market, side)
        size = min(self.rng.randint(*bots.TAKER_SIZE), market.room(bot_id, side))
        if price is None or size < 1:
            return []
        return self.take(market.market_id, bot_id, side, price, size)

    def _bot_ids(self, kind):
        return [trader_id for trader_id, bot in self.bots.items() if bot == kind]

    def _check_bot_kind(self, kind):
        if kind not in self.bots_on:
            raise ValueError("a bot is a maker or a taker")

    # --- Trade or Tighten: admin commands --------------------------------------------------
    # CREATED -> AUCTION -> MM_QUOTING -> FORCED_TRADE -> OPEN (or open_market to skip it all)

    def start_auction(self, market_id):
        """Start Trade or Tighten: traders may now submit widths."""
        self._check_status(market_id, {MarketStatus.CREATED}, "cannot start the auction")
        return self._record([AuctionStarted(market_id=market_id)])

    def close_auction(self, market_id):
        """The narrowest width holder becomes the market maker."""
        self._check_status(market_id, {MarketStatus.AUCTION}, "cannot close the auction")
        best = self.markets[market_id].best_width()
        if best is None:
            raise ValueError("no widths yet, so there is no one to make the market")
        trader_id, width = best
        return self._record([MarketMakerChosen(market_id=market_id, trader_id=trader_id,
                                               width=width)])

    def start_forced_trade(self, market_id):
        """Open the forced-trade window. The server ends it when the timer runs out."""
        self._check_status(market_id, {MarketStatus.MM_QUOTING}, "cannot start the forced trade")
        if self.markets[market_id].mm_bid is None:
            raise ValueError("the market maker hasn't quoted yet")
        return self._record([ForcedTradeStarted(market_id=market_id)])

    def end_forced_trade(self, market_id, ended_by):
        """Close the window ("timer" ran out, or "admin" pressed End now): unchosen traders get
        a random side, every forced trade prints, and continuous trading starts."""
        self._check_status(market_id, {MarketStatus.FORCED_TRADE}, "cannot end the forced trade")
        market = self.markets[market_id]
        return self._record(market.decide_forced_trades(
            self.forced_trade_participants(market_id), self.rng,
            trade_id=self.next_trade_id, ended_by=ended_by,
        ))

    def forced_trade_participants(self, market_id):
        """Who must trade in the forced trade: every trader not kicked, except the MM, in join
        order. Late joiners and traders who are offline are included; bots are not (Q212)."""
        mm_id = self.markets[market_id].mm_id
        return [
            trader_id for trader_id in self.traders
            if trader_id not in self.kicked and trader_id != mm_id and trader_id not in self.bots
        ]

    def _check_status(self, market_id, allowed, refusal):
        """Raise unless the market exists and its status is one of `allowed`."""
        market = self.markets.get(market_id)
        if market is None:
            raise ValueError(f"unknown market {market_id!r}")
        if market.status not in allowed:
            raise ValueError(f"{refusal} (the market is {market.status.value})")

    def _check_trader(self, trader_id):
        if trader_id not in self.traders:
            raise ValueError(f"unknown trader {trader_id!r}")

    # --- Joining --------------------------------------------------------------------------

    def join(self, name):
        """Add a trader to the room. A bad or taken name raises ValueError with the reason."""
        if self.joining_locked:
            raise ValueError("joining is locked")
        name = self._check_name(name)
        return self._record([TraderJoined(trader_id=f"t{self.next_trader_id}", name=name)])

    def find_trader(self, name):
        """The id of the trader with this name (ignoring case and outer spaces), or None."""
        wanted = name.strip().casefold()
        for trader_id, existing in self.traders.items():
            if existing.casefold() == wanted:
                return trader_id
        return None

    def _check_name(self, name, renaming=None):
        """The name with outer spaces trimmed. Raises ValueError if it is too short or too long,
        or someone else has it. When renaming, the trader's own current name doesn't count."""
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= MAX_NAME_LENGTH:
            raise ValueError(f"name must be 1 to {MAX_NAME_LENGTH} characters")
        owner = self.find_trader(name)
        if owner is not None and owner != renaming:
            raise ValueError(f"the name {name!r} is taken")
        return name

    # --- Trader commands (bad input is normal, so these return a Rejected event) ----------
    # A kicked trader's connection is closed, but a command may already have been on its
    # way, so each command also refuses kicked traders.

    def place_limit(self, market_id, trader_id, side, price, size):
        """ "I'm 36 bid for 20." Trades whatever it crosses; the rest rests in the book."""
        return self._place(market_id, trader_id, "limit", side, price, size)

    def take(self, market_id, trader_id, side, price, size):
        """A click on the best bid or offer: trade at that price or better, never rest."""
        return self._place(market_id, trader_id, "take", side, price, size)

    def place_quote(self, market_id, trader_id, bid_price, ask_price, size):
        """ "35 at 38, 100 up": a bid and an offer, each handled like a separate limit order."""
        command = f"quote {bid_price} @ {ask_price} x {size}"
        if trader_id in self.kicked:
            return self._reject(market_id, trader_id, command, REMOVED)
        if market_id not in self.markets:
            return self._reject(market_id, trader_id, command, "unknown market")
        if not bid_price < ask_price:
            return self._reject(market_id, trader_id, command, "bid must be below offer")
        bid_events = self.place_limit(market_id, trader_id, Side.BUY, bid_price, size)
        ask_events = self.place_limit(market_id, trader_id, Side.SELL, ask_price, size)
        return bid_events + ask_events

    def cancel(self, market_id, trader_id, order_id):
        command = f"cancel {order_id}"
        if trader_id in self.kicked:
            return self._reject(market_id, trader_id, command, REMOVED)
        if market_id not in self.markets:
            return self._reject(market_id, trader_id, command, "unknown market")
        return self._record(self.markets[market_id].decide_cancel(trader_id, order_id))

    def amend(self, market_id, trader_id, order_id, price, size):
        """Change a resting order's price or size. Shrinking it keeps its place in line; any
        other change sends it to the back (see Market.decide_amend)."""
        refusal = self._refusal(market_id, trader_id)
        if refusal:
            return self._reject(market_id, trader_id, f"amend {order_id} to {size} @ {price}",
                                refusal)
        return self._record(self.markets[market_id].decide_amend(
            trader_id, order_id, price, size,
            new_order_id=self.next_order_id, trade_id=self.next_trade_id,
        ))

    def cancel_all(self, trader_id, market_id=None):
        """Cancel the trader's resting orders in one market, or in every market if None."""
        if trader_id in self.kicked:
            return self._reject(market_id, trader_id, "cancel all", REMOVED)
        if market_id is None:
            markets = list(self.markets.values())
        elif market_id in self.markets:
            markets = [self.markets[market_id]]
        else:
            return self._reject(market_id, trader_id, "cancel all", "unknown market")
        events = [event for market in markets for event in market.decide_cancel_all(trader_id)]
        return self._record(events)

    def _place(self, market_id, trader_id, kind, side, price, size):
        command = f"{kind} {side.value} {size} @ {price}"
        if trader_id in self.kicked:
            return self._reject(market_id, trader_id, command, REMOVED)
        market = self.markets.get(market_id)
        if market is None:
            return self._reject(market_id, trader_id, command, "unknown market")
        events = market.decide_order(trader_id, kind, side, price, size,
                                     order_id=self.next_order_id, trade_id=self.next_trade_id)
        return self._record(events)

    # --- Trade or Tighten: trader commands -------------------------------------------------

    def submit_width(self, market_id, trader_id, width):
        """ "I'll make it 2,000 wide." Must be narrower than the best width so far."""
        refusal = self._refusal(market_id, trader_id)
        if refusal:
            return self._reject(market_id, trader_id, f"width {width}", refusal)
        return self._record(self.markets[market_id].decide_width(trader_id, width))

    def submit_mm_quote(self, market_id, trader_id, bid, ask):
        """The market maker's bid and offer for the forced trade."""
        refusal = self._refusal(market_id, trader_id)
        if refusal:
            return self._reject(market_id, trader_id, f"mm quote {bid} @ {ask}", refusal)
        return self._record(self.markets[market_id].decide_mm_quote(trader_id, bid, ask))

    def choose_side(self, market_id, trader_id, side):
        """Buy at the MM's offer or sell at the MM's bid; can be changed until the window ends."""
        refusal = self._refusal(market_id, trader_id)
        if refusal:
            return self._reject(market_id, trader_id, f"choose {side.value}", refusal)
        return self._record(self.markets[market_id].decide_choice(trader_id, side))

    def _refusal(self, market_id, trader_id):
        """Why this trader can't act in this market at all, or None if they can."""
        if trader_id in self.kicked:
            return REMOVED
        if market_id not in self.markets:
            return "unknown market"
        return None

    def _reject(self, market_id, trader_id, command, reason):
        return self._record([Rejected(market_id=market_id, trader_id=trader_id,
                                      command=command, reason=reason)])

    # --- The log --------------------------------------------------------------------------

    def _record(self, events):
        """Number, timestamp, append and apply events. Returns them as recorded."""
        now = self.clock()
        recorded = []
        for event in events:
            event = replace(event, seq=len(self.events) + 1, ts=now)
            self.events.append(event)
            self.apply(event)
            recorded.append(event)
        return recorded

    def apply(self, event):
        if isinstance(event, MarketCreated):
            config = MarketConfig(
                market_id=event.market_id, title=event.title, tick_size=event.tick_size,
                max_position=event.max_position, forced_trade_size=event.forced_trade_size,
                forced_trade_seconds=event.forced_trade_seconds,
                dollars_per_point=event.dollars_per_point,
                min_price=event.min_price, max_price=event.max_price,
            )
            self.markets[event.market_id] = Market(config)
        elif isinstance(event, TraderJoined):
            self.traders[event.trader_id] = event.name
            self.next_trader_id += 1
            if event.bot is not None:
                self.bots[event.trader_id] = event.bot
        elif isinstance(event, BotsStarted):
            self.bots_on[event.kind] = True
        elif isinstance(event, BotsStopped):
            self.bots_on[event.kind] = False
        elif isinstance(event, TraderKicked):
            self.kicked.add(event.trader_id)
        elif isinstance(event, TraderRenamed):
            self.traders[event.trader_id] = event.new_name
        elif isinstance(event, InfoDropMarked):
            self.info_drops.append(event)
        elif isinstance(event, JoiningLocked):
            self.joining_locked = True
        elif isinstance(event, JoiningUnlocked):
            self.joining_locked = False
        elif isinstance(event, StartingEquitySet):
            self.starting_equity = event.amount
        elif isinstance(event, Rejected):
            pass  # recorded in the log for review; changes nothing
        else:
            self.markets[event.market_id].apply(event)

        if isinstance(event, OrderAccepted):
            self.next_order_id = event.order_id + 1
        elif isinstance(event, TradeExecuted):
            self.next_trade_id = event.trade_id + 1

    # --- Queries --------------------------------------------------------------------------

    def book(self, market_id):
        return self.markets[market_id].book

    def trades(self, market_id):
        return list(self.markets[market_id].trades)

    def mark(self, market_id):
        return self.markets[market_id].mark()

    def position(self, market_id, trader_id):
        return self.markets[market_id].position(trader_id).size

    def pnl(self, market_id, trader_id):
        return self.markets[market_id].pnl(trader_id)

    def forced_trade_seconds_left(self, market_id):
        """Seconds until the forced-trade window closes (0 once it's due), or None if it isn't
        running."""
        market = self.markets[market_id]
        if market.status is not MarketStatus.FORCED_TRADE:
            return None
        return max(0, (market.forced_trade_deadline() - self.clock()).total_seconds())

    def total_pnl(self, trader_id):
        """PnL in dollars over every market: each market's PnL (in its own price units, or
        points) times that market's dollars per point."""
        total = PnL(realized=0, unrealized=0)
        for market in self.markets.values():
            total = total + market.pnl(trader_id).times(market.config.dollars_per_point)
        return total

    def equity(self, trader_id):
        """Starting equity plus total PnL, in dollars."""
        return self.starting_equity + self.total_pnl(trader_id).total


def check_settings(config):
    """A market's settings as a dict, with the title trimmed.

    Raises ValueError with the reason if any setting is bad.
    """
    title = config.title.strip() if isinstance(config.title, str) else ""
    if not title:
        raise ValueError("the title can't be empty")
    whole_numbers = {
        "tick size": config.tick_size,
        "max position": config.max_position,
        "forced-trade size": config.forced_trade_size,
        "forced-trade seconds": config.forced_trade_seconds,
    }
    for label, value in whole_numbers.items():
        if not is_whole_number(value) or value < 1:
            raise ValueError(f"{label} must be a whole number of at least 1")
    multiplier = config.dollars_per_point
    is_number = isinstance(multiplier, (int, float)) and not isinstance(multiplier, bool)
    if not is_number or not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError("$ per point must be a number above 0")
    for label, limit in (("min price", config.min_price), ("max price", config.max_price)):
        if limit is not None and not is_whole_number(limit):
            raise ValueError(f"{label} must be a whole number (or left blank for no limit)")
    if None not in (config.min_price, config.max_price) and config.min_price >= config.max_price:
        raise ValueError("min price must be below max price")
    return {
        "title": title,
        "tick_size": config.tick_size,
        "max_position": config.max_position,
        "forced_trade_size": config.forced_trade_size,
        "forced_trade_seconds": config.forced_trade_seconds,
        "dollars_per_point": config.dollars_per_point,
        "min_price": config.min_price,
        "max_price": config.max_price,
    }
