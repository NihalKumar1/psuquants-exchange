"""The room: one game on this server.

The room holds the Exchange plus the things that are *not* part of the game itself, so they are
never put in the event log:
- the room code students type to join,
- secret tokens that let a browser rejoin as the same trader after a refresh or Wi-Fi drop,
- the live connection for each trader (at most one; the newest tab wins),
- the live admin connections (any number of admin tabs),
- the live review page connections, each with the trader and market it is showing,
- how much of the log has been exported (so Reset can warn about a game not yet exported).

Nothing here knows about WebSockets: a "connection" is whatever object the caller passes in.
"""

import secrets

from exchange.engine import Exchange, MarketConfig, Side, TraderJoined


def new_room_code():
    """Four random digits, e.g. "0482"."""
    return f"{secrets.randbelow(10_000):04d}"


class Room:
    def __init__(self, code, exchange):
        self.code = code
        self.exchange = exchange
        self.tokens = {}  # token -> trader_id
        self.connections = {}  # trader_id -> live connection
        self.admins = set()  # live admin connections
        self.reviewers = {}  # live review page connection -> (market_id, trader_id) or None
        self.exported_seq = 0  # how many events the last export had

    # --- Export ---------------------------------------------------------------------------

    def mark_exported(self):
        """Remember that everything in the log so far has been exported."""
        self.exported_seq = len(self.exchange.events)

    def has_unexported_changes(self):
        """True if something happened since the last export. People joining doesn't count:
        a game where nothing else happened has nothing worth keeping."""
        return any(not isinstance(event, TraderJoined)
                   for event in self.exchange.events[self.exported_seq:])

    # --- Joining --------------------------------------------------------------------------

    def join(self, code, name):
        """Join with the room code and a name. Returns (trader_id, token, events).

        A name that already exists is reclaimed if nobody is connected as that trader, so a
        student on a new laptop (or after a server restart) can get back in by typing their
        name, even while joining is locked. A kicked trader can't come back. Raises ValueError
        with a reason to show the student.
        """
        if str(code).strip() != self.code:
            raise ValueError("wrong room code")

        existing = self.exchange.find_trader(str(name))
        if existing is None:
            events = self.exchange.join(name)
            trader_id = events[0].trader_id
        elif existing in self.exchange.kicked:
            raise ValueError("you were removed from this game")
        elif existing in self.connections:
            raise ValueError("that name is taken by someone who is connected")
        else:
            trader_id, events = existing, []

        return trader_id, self.new_token(trader_id), events

    def new_token(self, trader_id):
        token = secrets.token_urlsafe(16)
        self.tokens[token] = trader_id
        return token

    def rejoin(self, token):
        """The trader this browser's token belongs to, or None (also None once kicked)."""
        trader_id = self.tokens.get(token)
        if trader_id in self.exchange.kicked:
            return None
        return trader_id

    # --- Connections ----------------------------------------------------------------------

    def connect(self, trader_id, connection):
        """Make this the trader's live connection. Returns the one it replaced, if any."""
        replaced = self.connections.get(trader_id)
        self.connections[trader_id] = connection
        return replaced

    def disconnect(self, trader_id, connection):
        """Forget the trader's connection. Returns True if it was their current one.

        An old tab closing must not disconnect the new tab that replaced it.
        """
        if self.connections.get(trader_id) is connection:
            del self.connections[trader_id]
            return True
        return False

    def trader_of(self, connection):
        """The trader this connection currently belongs to, or None if it no longer belongs to
        anyone (replaced by a newer tab, or kicked). A reset can change a trader's id."""
        for trader_id, current in self.connections.items():
            if current is connection:
                return trader_id
        return None

    # --- Reset ----------------------------------------------------------------------------

    def reset(self):
        """End this game and start a new, empty one with the same room code.

        Traders connected right now are carried into the new game under the same names, in
        their old join order, starting flat. Each gets a new id and a new token; every old
        token stops working. Offline traders are dropped, and so are kicked ones (they are
        never connected), so both can join the new game like anyone.

        The old game's event log is discarded: the new game's log starts with the carried-over
        traders joining. Returns [(new trader_id, new token), ...] for those traders.
        """
        old = self.exchange
        old_connections = self.connections
        self.exchange = Exchange(clock=old.clock, rng=old.rng)
        self.connections = {}
        self.tokens = {}
        self.exported_seq = 0
        for reviewer in self.reviewers:
            self.reviewers[reviewer] = None  # the picked market and trader are gone

        carried = []
        for old_id, name in old.traders.items():  # join order
            if old_id not in old_connections:
                continue
            (joined,) = self.exchange.join(name)
            self.connections[joined.trader_id] = old_connections[old_id]
            carried.append((joined.trader_id, self.new_token(joined.trader_id)))
        return carried

    # --- Commands -------------------------------------------------------------------------

    def handle(self, trader_id, message):
        """Run one trader command and return its events.

        A malformed message (missing field, unknown type or side) raises KeyError or
        ValueError; that is a bug or a tampered page, not a trading decision, so it is not
        logged. Normal trading mistakes come back from the engine as Rejected events.
        """
        exchange = self.exchange
        kind = message["type"]
        if kind == "limit":
            return exchange.place_limit(message["market_id"], trader_id, Side(message["side"]),
                                        message["price"], message["size"])
        if kind == "take":
            return exchange.take(message["market_id"], trader_id, Side(message["side"]),
                                 message["price"], message["size"])
        if kind == "quote":
            return exchange.place_quote(message["market_id"], trader_id, message["bid_price"],
                                        message["ask_price"], message["size"])
        if kind == "cancel":
            return exchange.cancel(message["market_id"], trader_id, message["order_id"])
        if kind == "cancel_all":
            # No market_id means cancel in every market.
            return exchange.cancel_all(trader_id, message.get("market_id"))
        # Trade or Tighten
        if kind == "width":
            return exchange.submit_width(message["market_id"], trader_id, message["width"])
        if kind == "mm_quote":
            return exchange.submit_mm_quote(message["market_id"], trader_id, message["bid"],
                                            message["ask"])
        if kind == "choose_side":
            return exchange.choose_side(message["market_id"], trader_id, Side(message["side"]))
        raise ValueError(f"unknown command {kind!r}")

    def handle_admin(self, message):
        """Run one admin command and return its events.

        An admin mistake (e.g. settling a market that isn't open) raises ValueError with a
        reason to show the admin. A malformed message raises KeyError or ValueError.
        """
        exchange = self.exchange
        kind = message["type"]
        if kind == "create_market":
            market_id = f"m{len(exchange.markets) + 1}"  # markets are never deleted
            return exchange.create_market(market_config(market_id, message))
        if kind == "edit_market":
            return exchange.edit_market(market_config(message["market_id"], message))
        if kind == "open_market":  # open directly, skipping Trade or Tighten
            return exchange.open_market(message["market_id"])
        if kind == "start_auction":
            return exchange.start_auction(message["market_id"])
        if kind == "close_auction":
            return exchange.close_auction(message["market_id"])
        if kind == "start_forced_trade":
            return exchange.start_forced_trade(message["market_id"])
        if kind == "end_forced_trade":  # the admin's "End now"
            return exchange.end_forced_trade(message["market_id"], ended_by="admin")
        if kind == "halt":
            return exchange.halt_market(message["market_id"])
        if kind == "resume":
            return exchange.resume_market(message["market_id"])
        if kind == "settle":
            return exchange.settle_market(message["market_id"], message["value"])
        if kind == "info_drop":
            return exchange.mark_info_drop(message.get("note", ""))
        if kind == "kick":
            return exchange.kick(message["trader_id"])
        if kind == "rename":
            return exchange.rename(message["trader_id"], message["name"])
        if kind == "lock_joining":
            return exchange.lock_joining()
        if kind == "unlock_joining":
            return exchange.unlock_joining()
        raise ValueError(f"unknown command {kind!r}")


def market_config(market_id, message):
    """The five market settings from an admin message."""
    return MarketConfig(
        market_id=market_id,
        title=message["title"],
        tick_size=message["tick_size"],
        max_position=message["max_position"],
        forced_trade_size=message["forced_trade_size"],
        forced_trade_seconds=message["forced_trade_seconds"],
    )
