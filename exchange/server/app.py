"""The web server: serves the trader and admin pages and keeps their WebSockets open.

Trader messages (all JSON), on /ws:
  browser -> server   first:  {"type": "join", "code", "name"}  or  {"type": "rejoin", "token"}
                      then:   {"type": "limit" | "take" | "quote" | "cancel" | "cancel_all", ...}
                              or, in Trade or Tighten, {"type": "width" | "mm_quote" |
                              "choose_side", ...}  (see Room.handle)
                              Any command may carry a "ref"; see "done" below.
  server -> browser   "welcome" + "snapshot" after joining, "error" if joining failed,
                      "update" after every change, "rejected" (to the sender only, with
                      the command's "market_id", "kind" (its "type") and "side", so the page
                      can say which market and outline the form that sent it),
                      "opened_elsewhere" when the same trader connects from a newer tab,
                      "kicked" when the admin removes them,
                      "welcome" + "snapshot" + "reset" when the admin resets the game,
                      "done" (with the command's "ref") once a command that carried a "ref"
                      has been fully handled. Browsers never send a ref; the load test
                      (scripts/load_test.py) uses it to time each command.

Admin messages, on /admin/ws:
  browser -> server   first:  {"type": "admin_login", "secret"}
                      then:   {"type": "create_market" | "halt" | "settle" | "kick" | ..., ...}
                              (see Room.handle_admin), or {"type": "reset"} (see Room.reset)
  server -> browser   "admin_welcome", then "admin_state" after every change,
                      "admin_error" when a command is refused, "error" if the secret is wrong.

Review page messages (the projector), on /review/ws:
  browser -> server   first:  {"type": "admin_login", "secret"}  (the admin password)
                      then:   {"type": "review_pick", "market_id", "trader_id"}
  server -> browser   "review_welcome", then "review_state" (the pickers, and the review of the
                      picked trader and market) right away, after each pick, and after changes,
                      at most once per review_interval seconds: the chart can hold thousands
                      of points, too many to resend after every one of ~30 commands a second.

Export: GET /admin/export with the admin password in an "X-Admin-Secret" header downloads the
whole game as a zip (see export.py).

The forced-trade timer also lives here: when the admin starts a forced-trade window, the server
waits out the timer and then ends the window itself, as if it were one more command.
"""

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from exchange.engine import (
    ForcedTradeStarted,
    InfoDropMarked,
    JoiningLocked,
    JoiningUnlocked,
    MarketStatus,
    Rejected,
    TraderKicked,
)

from .export import export_filename, export_zip
from .views import admin_message, review_state_message, snapshot_message, update_message

STATIC_DIR = Path(__file__).parent / "static"
SEND_TIMEOUT_SECONDS = 5  # a browser this slow to accept a message is treated as gone

# Browsers must re-check the pages and scripts every time they load them (cheap: an unchanged
# file gets a short "not modified" reply). Otherwise a browser can keep running an old
# trader.js for hours after the code changes, e.g. after a redeploy.
NO_CACHE = {"Cache-Control": "no-cache"}

# Events traders are never told about. Info drops are announced out loud, not through the app.
ADMIN_ONLY_EVENTS = (InfoDropMarked, JoiningLocked, JoiningUnlocked)


def create_app(room, admin_secret, review_interval=1.0):
    # Running tasks, kept here so they aren't garbage collected:
    timers = set()  # forced-trade timers
    review_pushes = set()  # a scheduled push to the review pages (at most one at a time)
    review_push_pending = False
    last_review_push = 0.0  # event loop time of the last push

    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in list(timers | review_pushes):  # the server is stopping: leave nothing behind
            task.cancel()

    app = FastAPI(lifespan=lifespan)
    app.state.room = room
    app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")

    # One command at a time: run it, then tell everyone, and only then start the next one.
    # This keeps every browser's updates in the same order as the event log.
    lock = asyncio.Lock()

    @app.get("/")
    def trader_page():
        return FileResponse(STATIC_DIR / "trader.html", headers=NO_CACHE)

    @app.get("/admin")
    def admin_page():
        return FileResponse(STATIC_DIR / "admin.html", headers=NO_CACHE)

    @app.get("/review")
    def review_page():
        return FileResponse(STATIC_DIR / "review.html", headers=NO_CACHE)

    # --- Traders --------------------------------------------------------------------------

    @app.websocket("/ws")
    async def trader_socket(websocket: WebSocket):
        await websocket.accept()
        try:
            logged_in = await log_in(websocket)
            while logged_in:
                message = parse(await websocket.receive_text())
                logged_in = await run_command(websocket, message)
        except WebSocketDisconnect:
            pass
        finally:
            trader_id = room.trader_of(websocket)
            if trader_id is not None and room.disconnect(trader_id, websocket):
                async with lock:
                    await tell_admins()  # they now show this trader as disconnected

    async def log_in(websocket):
        """Handle the first message (join or rejoin). Returns True if the trader is in."""
        message = parse(await websocket.receive_text())
        async with lock:
            try:
                if message.get("type") == "join":
                    trader_id, token, events = room.join(message.get("code", ""),
                                                         message.get("name", ""))
                elif message.get("type") == "rejoin":
                    token, events = message.get("token"), []
                    trader_id = room.rejoin(token)
                    if trader_id is None:
                        raise ValueError("your session has ended; please join again")
                else:
                    raise ValueError("please join first")
            except ValueError as error:
                await send(websocket, {"type": "error", "reason": str(error)})
                await close(websocket)
                return False

            replaced = room.connect(trader_id, websocket)
            if replaced is not None:
                await send(replaced, {"type": "opened_elsewhere"})
                await close(replaced)

            await send(websocket, welcome_message(trader_id, token))
            await send(websocket, snapshot_message(room.exchange, trader_id))
            if events:  # someone new joined: everyone else's positions table changes
                await broadcast(events, skip=trader_id)
            await tell_admins()
        return True

    def welcome_message(trader_id, token):
        return {"type": "welcome", "trader_id": trader_id,
                "name": room.exchange.traders[trader_id], "token": token}

    async def run_command(websocket, message):
        """Run one trader command. Returns False if this connection no longer belongs to a
        trader (a newer tab replaced it, or the trader was kicked)."""
        async with lock:
            # Looked up on every command, not remembered: a reset gives traders new ids.
            trader_id = room.trader_of(websocket)
            if trader_id is None:
                return False
            await run_trader_command(websocket, trader_id, message)
            if "ref" in message:  # the load test times each command until this reply
                await send(websocket, {"type": "done", "ref": message["ref"]})
            return True

    async def run_trader_command(websocket, trader_id, message):
        # A rejection names the market the command was for (None for e.g. cancel all
        # everywhere), so the page can say which of the markets on screen it was in. It also
        # says what kind of command it was ("limit", "quote", "width", ...) and its side (None
        # for most kinds), so the page can outline the form that sent it.
        sent = {"market_id": message.get("market_id"), "kind": message.get("type"),
                "side": message.get("side")}
        try:
            events = room.handle(trader_id, message)
        except (KeyError, ValueError, TypeError):
            await send(websocket, {"type": "rejected", "command": message.get("type"), **sent,
                                   "reason": "bad message"})
            return

        for event in events:
            if isinstance(event, Rejected):
                await send(websocket, {"type": "rejected", "command": event.command, **sent,
                                       "reason": event.reason})
        if any(not isinstance(event, Rejected) for event in events):
            await broadcast(events)
            await tell_admins()

    async def broadcast(events, skip=None):
        """Send every connected trader (except `skip`) an update about these events."""
        for trader_id, connection in list(room.connections.items()):
            if trader_id == skip:
                continue
            if not await send(connection, update_message(room.exchange, trader_id, events)):
                room.disconnect(trader_id, connection)
                await close(connection)

    # --- Admin ----------------------------------------------------------------------------

    @app.websocket("/admin/ws")
    async def admin_socket(websocket: WebSocket):
        await websocket.accept()
        try:
            message = parse(await websocket.receive_text())
            if not is_admin_login(message, admin_secret):
                await send(websocket, {"type": "error", "reason": "wrong admin secret"})
                await close(websocket)
                return

            async with lock:
                room.admins.add(websocket)
                await send(websocket, {"type": "admin_welcome"})
                await send(websocket, admin_message(room))

            while True:
                message = parse(await websocket.receive_text())
                await run_admin_command(websocket, message)
        except WebSocketDisconnect:
            pass
        finally:
            room.admins.discard(websocket)

    async def run_admin_command(websocket, message):
        async with lock:
            if message.get("type") == "reset":
                await reset_game()
                return
            try:
                events = room.handle_admin(message)
            except (KeyError, TypeError):
                await send(websocket, {"type": "admin_error", "reason": "bad message"})
                return
            except ValueError as error:  # an admin mistake, e.g. settling a CREATED market
                await send(websocket, {"type": "admin_error", "reason": str(error)})
                return

            for event in events:
                if isinstance(event, TraderKicked):
                    await remove(event.trader_id)
                if isinstance(event, ForcedTradeStarted):
                    start_timer(event.market_id)
            if any(not isinstance(event, ADMIN_ONLY_EVENTS) for event in events):
                await broadcast(events)
            await tell_admins()

    async def reset_game():
        """End this game and start a new one (see Room.reset). Connected traders get a new
        welcome and an empty snapshot, then a notice that the game was reset."""
        # Market ids start again at m1, so an old timer must not end the new game's window.
        for timer in list(timers):
            timer.cancel()
        for trader_id, token in room.reset():
            connection = room.connections[trader_id]
            delivered = (await send(connection, welcome_message(trader_id, token))
                         and await send(connection, snapshot_message(room.exchange, trader_id))
                         and await send(connection, {"type": "reset"}))
            if not delivered:
                room.disconnect(trader_id, connection)
                await close(connection)
        await tell_admins()

    def start_timer(market_id):
        timer = asyncio.create_task(end_forced_trade_when_time_is_up(market_id))
        timers.add(timer)
        timer.add_done_callback(timers.discard)

    async def end_forced_trade_when_time_is_up(market_id):
        await asyncio.sleep(room.exchange.markets[market_id].config.forced_trade_seconds)
        async with lock:
            # A market has only one forced-trade window, so if it isn't still running, the
            # admin already pressed "End now" and there is nothing to do.
            if room.exchange.markets[market_id].status is not MarketStatus.FORCED_TRADE:
                return
            events = room.exchange.end_forced_trade(market_id, ended_by="timer")
            await broadcast(events)
            await tell_admins()

    async def remove(trader_id):
        """Tell a kicked trader's browser, then close it."""
        connection = room.connections.get(trader_id)
        if connection is not None:
            room.disconnect(trader_id, connection)
            await send(connection, {"type": "kicked"})
            await close(connection)

    async def tell_admins():
        """Send every admin page the latest admin state (and soon, every review page too)."""
        message = admin_message(room)
        for connection in list(room.admins):
            if not await send(connection, message):
                room.admins.discard(connection)
                await close(connection)
        tell_reviewers()

    @app.get("/admin/export")
    async def export(x_admin_secret: str = Header(default="")):
        """Download the whole game as a zip. The admin page sends the password as a header."""
        if not is_admin_secret(x_admin_secret, admin_secret):
            return Response(status_code=403)
        async with lock:
            data = export_zip(room.exchange)
            filename = export_filename(room.exchange.clock())
            room.mark_exported()
            await tell_admins()  # Reset stops warning that the game isn't exported
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # --- Review page (the projector) ------------------------------------------------------

    @app.websocket("/review/ws")
    async def review_socket(websocket: WebSocket):
        await websocket.accept()
        try:
            message = parse(await websocket.receive_text())
            if not is_admin_login(message, admin_secret):
                await send(websocket, {"type": "error", "reason": "wrong admin secret"})
                await close(websocket)
                return

            async with lock:
                room.reviewers[websocket] = None  # nothing picked yet
                await send(websocket, {"type": "review_welcome"})
                await send(websocket, review_state_message(room, None))

            while True:
                message = parse(await websocket.receive_text())
                if message.get("type") != "review_pick":
                    continue
                market_id, trader_id = message.get("market_id"), message.get("trader_id")
                pick = (market_id, trader_id) if is_text(market_id) and is_text(trader_id) else None
                async with lock:
                    room.reviewers[websocket] = pick
                    await send(websocket, review_state_message(room, pick))
        except WebSocketDisconnect:
            pass
        finally:
            room.reviewers.pop(websocket, None)

    def tell_reviewers():
        """Something changed: push the review pages the latest state, but no more often than
        once per review_interval. Changes in between all go out in the next push."""
        nonlocal review_push_pending
        if review_push_pending or not room.reviewers:
            return
        review_push_pending = True
        push = asyncio.create_task(push_to_reviewers_soon())
        review_pushes.add(push)
        push.add_done_callback(review_pushes.discard)

    async def push_to_reviewers_soon():
        nonlocal review_push_pending, last_review_push
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0.0, last_review_push + review_interval - loop.time()))
        async with lock:
            review_push_pending = False  # any change from now on schedules the next push
            last_review_push = loop.time()
            for connection, pick in list(room.reviewers.items()):
                if not await send(connection, review_state_message(room, pick)):
                    room.reviewers.pop(connection, None)
                    await close(connection)

    return app


class NoCacheStaticFiles(StaticFiles):
    """The files under /static (scripts, styles), sent with the NO_CACHE header."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


def is_admin_login(message, admin_secret):
    """True if the message is a login with the right secret."""
    return (message.get("type") == "admin_login"
            and is_admin_secret(message.get("secret"), admin_secret))


def is_admin_secret(secret, admin_secret):
    """True if `secret` is the admin secret (compared in constant time)."""
    if not is_text(secret):
        return False
    return secrets.compare_digest(secret.encode(), admin_secret.encode())


def is_text(value):
    return isinstance(value, str)


def parse(text):
    """A browser message as a dict; anything that isn't a JSON object becomes {}."""
    try:
        message = json.loads(text)
    except ValueError:
        return {}
    return message if isinstance(message, dict) else {}


async def send(connection, message):
    """Send one message. Returns False if the browser is gone or too slow."""
    try:
        await asyncio.wait_for(connection.send_json(message), SEND_TIMEOUT_SECONDS)
        return True
    except Exception:  # closed socket, network error or timeout: all mean "gone"
        return False


async def close(connection):
    try:
        await asyncio.wait_for(connection.close(), SEND_TIMEOUT_SECONDS)
    except Exception:  # already closed or gone
        pass
