# PSU Quants Exchange

A simulated exchange for PSU Quants meetings: 25–60 students trade "Trade or Tighten"
estimation markets against each other from laptops; the instructor (admin) runs the game and
projects a review screen. **SPEC.md is the source of truth.**

## Ground rules
- **Do not make assumptions.** If SPEC.md doesn't specify something, ask the instructor instead
  of choosing. Record every answer in SPEC.md → "Open questions" (and add new questions there).
- **One milestone at a time**, in the order of SPEC.md → "Build order". For each milestone:
  ask the open questions it depends on → propose a plan → wait for approval → **write tests
  first** → implement until tests pass → **stop and show the instructor** before the next one.
- **Simple, readable code.** The instructor maintains it and uses it to teach market mechanics,
  so prefer clear names and plain logic over cleverness.
- **Multiple concurrent markets from day one.** Every data structure is keyed by market id.
- **Append-only event log.** Commands validate and return events; state changes only by
  applying events; current state == replay of the log.

## Stack
- Python (local: 3.14.4), FastAPI + WebSockets (from milestone 2), pytest.
- Dependencies: pip + `requirements.txt` in a venv.
- `exchange/engine/` is pure Python with no web imports.

## Hosting constraints (Render free tier, $0 budget)
- Single instance, **one uvicorn worker**: all live state is in memory.
- Spins down after 15 min without inbound traffic; cold start ≈ 1 min (instructor opens it
  before the meeting).
- Filesystem is ephemeral (wiped on redeploy/restart/spin-down).
- Render may restart the service at any time without warning.
- Scale target: 60 traders on WebSockets + admin + projector view.

## Commands (Windows)
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest            # run all tests
```
Server: set the admin password, then start uvicorn (it refuses to start without `ADMIN_SECRET`):
```
$env:ADMIN_SECRET="choose-a-password"
uvicorn exchange.server.main:app --workers 1
```
Admin page: http://localhost:8000/admin (create markets there, then start each one with
"Trade or Tighten" or "Open directly"; Export downloads the game as a zip). Trader page:
http://localhost:8000. Review screen (projector, admin password): http://localhost:8000/review.
The room code is printed in the console and shown on the admin page.

Load test (60 bots, a whole game; the server must be running with the same `ADMIN_SECRET`):
```
python scripts/load_test.py http://127.0.0.1:8000     # or the Render https:// address
```
Deploy: `render.yaml` Blueprint (steps and the meeting-day checklist are in README.md).

## Layout
- `exchange/engine/`: matching engine, positions, PnL, the event log, and the Trade or Tighten
  opening (pure Python). Random forced-trade sides come from `Exchange(rng=...)` and are logged as
  `SideAssigned` events, so replay never flips a coin. Each market also keeps a `price_history`
  (a `PricePoint` whenever the mark or last price changes) for the review chart, and
  `engine/review.py` turns a market into one trader's fills with running position, realized,
  MTM (at the mark right after each fill) and edge vs settlement.
- `exchange/server/room.py`: room code, join/rejoin tokens, live trader and admin connections,
  trader + admin commands, and `reset()` (new empty game, same code; connected traders are
  carried over with **new ids and tokens**, so the socket loop looks up `trader_of(connection)`
  on every command instead of remembering an id).
- `exchange/server/views.py`: engine state → JSON for browsers (public, private, and admin views).
  It also decides the trader page's layout: one column per running market (`columns`), the
  markets in the positions table (`table_markets`), and the book depth (10, or 5 with several).
- `exchange/server/app.py`: FastAPI + WebSocket wiring (`create_app(room, admin_secret)`, used by
  tests). Traders on `/ws`, admin on `/admin/ws`. Also runs the forced-trade timer (an asyncio
  task that ends the window when time is up; Reset cancels running timers). A trader command
  may carry a `"ref"`; the server then replies `{"type": "done", "ref"}` once it has told
  everyone (used by the load test to time commands; browsers never send it). A `rejected` reply
  carries the command's `market_id`, `kind` and `side`, so the trader page can outline the form
  that sent it.
  The review page is on `/review/ws` (admin password, then `review_pick`); its live pushes are
  throttled to one per `review_interval` (1 s). `GET /admin/export` (password in the
  `X-Admin-Secret` header) downloads the zip; the room remembers the export so Reset can warn.
- `exchange/server/export.py`: the export zip (trades/orders/events CSVs + events.json), with US
  Eastern times and names in place of trader ids. Needs `tzdata` on Windows.
- `exchange/server/main.py`: the app uvicorn runs (`build_app(environ)` reads `ADMIN_SECRET`).
- `exchange/server/static/`: trader page, admin page, review page (`review.js` draws the chart as
  plain SVG), and `common.js` helpers (plain HTML/JS/CSS, no build step).
- `scripts/load_test.py`: the 60-bot load test (also imported by `tests/test_load_test.py`).
  `scripts/demo_fill.py` (gitignored) fills a book for screenshots.
- `render.yaml`: the Render Blueprint (free plan, Python 3.14.4, one worker).
- Tests: enter `TestClient` with `with` so all sockets share one event loop (like uvicorn).
