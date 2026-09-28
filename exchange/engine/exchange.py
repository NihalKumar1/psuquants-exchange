"""The Exchange: all markets plus the append-only event log.

Every command follows the same three steps:
1. a market decides which events the command produces (without changing anything),
2. each event is numbered, timestamped and appended to self.events,
3. each event is applied, which is the only way state changes.

Because of this, Exchange.replay(events) rebuilds exactly the same state from a saved log.
"""

import random
from dataclasses import replace
from datetime import datetime, timezone

from .events import (
    AuctionStarted,
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
    TradeExecuted,
    TraderJoined,
    TraderKicked,
    TraderRenamed,
)
from .market import Market
from .models import MarketConfig, MarketStatus, Side, is_whole_number
from .positions import PnL

MAX_NAME_LENGTH = 20
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

    def lock_joining(self):
        if self.joining_locked:
            raise ValueError("joining is already locked")
        return self._record([JoiningLocked()])

    def unlock_joining(self):
        if not self.joining_locked:
            raise ValueError("joining is already unlocked")
        return self._record([JoiningUnlocked()])

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
        order. Late joiners and traders who are offline are included."""
        mm_id = self.markets[market_id].mm_id
        return [
            trader_id for trader_id in self.traders
            if trader_id not in self.kicked and trader_id != mm_id
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
            )
            self.markets[event.market_id] = Market(config)
        elif isinstance(event, TraderJoined):
            self.traders[event.trader_id] = event.name
            self.next_trader_id += 1
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
        total = PnL(realized=0, unrealized=0)
        for market in self.markets.values():
            total = total + market.pnl(trader_id)
        return total


def check_settings(config):
    """A market's five settings as a dict, with the title trimmed.

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
    return {
        "title": title,
        "tick_size": config.tick_size,
        "max_position": config.max_position,
        "forced_trade_size": config.forced_trade_size,
        "forced_trade_seconds": config.forced_trade_seconds,
    }
