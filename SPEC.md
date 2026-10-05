# PSU Quants Trading Exchange: Spec

## Purpose
This is a simulated exchange for PSU Quants general body meetings. It replaces the whiteboard order book used in the liquidity and market-making games. 25–60 students trade against each other live from their laptops. The instructor (admin) runs the game and projects the screen. After each game the instructor pulls up an individual student's trades and walks the room through which trades were good and which were bad.

The games are "Trade or Tighten" estimation markets. Examples are "Number of cars registered in Centre County, PA at end of 2025" and "Median PA home price in the 1950s". Each market settles to a true value that the instructor reveals at the end.

## Stack and hosting (decided)
- The backend is Python (FastAPI + WebSockets unless there is a strong reason otherwise).
- The app is hosted on the **Render free tier**, and the budget is $0. The design must handle these constraints:
  - It runs as a single instance. Use one uvicorn worker, because all live state is in memory.
  - The service spins down after 15 minutes with no inbound traffic, and a cold start takes about 1 minute. The instructor will open it before the meeting.
  - The filesystem is ephemeral. It is wiped on redeploy, restart, or spin-down.
  - Render may restart a free service at any time without warning.
- Scale target: 60 simultaneous traders with WebSocket connections, plus the admin and a projector view.

## Roles
- **Trader**: joins by typing a display name and a room code. There are no accounts. Names must be unique within a room.
- **Admin (instructor)**: uses a separate admin page protected by a secret from an environment variable.

## Markets
- **Several markets can be open at the same time.** Every data structure is keyed by market id from day one.
- The admin sets these per market:
  - Title/question
  - Tick size (values range from about 7,000 to over 100,000, so ticks vary per game)
  - Max absolute position per trader
  - Forced-trade size
  - Forced-trade timer length in seconds
- Market lifecycle: `CREATED → AUCTION → MM_QUOTING → FORCED_TRADE → OPEN ⇄ HALTED → SETTLED`
  - The admin may also open a market directly (`CREATED → OPEN`), skipping Trade or Tighten. This is a permanent option.

## Opening: Trade or Tighten (fully in the app)
1. **Width auction.** The admin opens the auction. Any trader can submit a width, and it must be strictly narrower than the current best. Everyone sees the current best width and who holds it, live. **The admin closes the auction manually.** The holder of the narrowest width becomes the market maker (MM).
2. **MM quotes.** The MM enters a bid and an offer whose difference equals the winning width, or is narrower.
3. **Forced trade.** Every trader except the MM must choose **buy** (at the MM's offer) or **sell** (at the MM's bid). The size is the fixed forced-trade size the admin set. A countdown timer runs. **When the timer expires, anyone who has not chosen is assigned a random side.**
4. After the forced trade, the book opens for continuous trading. The MM carries the resulting position like everyone else.

## Continuous trading
- Orders use price-time priority. Price comes first. At the same price, the order posted first fills first. This test case must pass: a "36 bid for 20" posted at 9:30:01 and a "36 bid for 15" posted at 9:30:04, then "hit the 36 bid, 25", fills 20 of the first order and 5 of the second.
- Trader actions:
  - **Limit bid / limit offer** ("I'm 36 bid for 20"). If an incoming limit order crosses the book, it trades immediately and any remainder rests.
  - **Two-sided quote** ("35 at 38, 100 up") posts a bid and an offer in one action.
  - **Hit / lift** is an immediate order that takes resting liquidity ("lift the 37 offer, 10"). In the UI this is **click-to-take**: the trader clicks the best bid or best offer (see Q2).
  - **Cancel** one order, and **cancel all** (one action for this market and one for all markets).
- **Names are visible**: book levels show who owns each order, and the trade tape shows buyer and seller names, like open outcry.
- Every order is validated against tick size and max position.
- Trades print at the **resting order's price**.

## Positions and PnL
- Each trader has, per market and in total: position, realized PnL, mark-to-market (unrealized) PnL, and total PnL. Each trader always sees their own numbers live.
- **Mark price = mid of best bid and best offer.** If either side of the book is empty, **use the last traded price.**
- After settlement, the mark is the settlement value.

## Information drops
- The instructor announces info drops out loud or on a slide. They are **not pushed through the app.**
- The admin has a **"Mark info drop"** button with an optional note. It records a timestamped event for the review timeline.

## Admin controls
- Create and configure markets, open and close the width auction, and start the forced-trade window
- Halt and resume trading
- Settle a market at the true value, which computes final PnL
- Mark an info drop
- Kick or rename a trader
- Export and reset (see Persistence)

## Review screen (projector)
The admin picks a trader and a market. The screen shows:
1. **The trader's trades in order**: time, side, price, size, counterparty, and running position, realized PnL, and MTM PnL after each trade.
2. **A price chart**: the market's mark and last price over time, with this trader's fills marked (buys and sells visually distinct).
3. **Info drop markers**: vertical lines on the chart with notes, which also appear in the log.
4. **Edge vs settlement**: after settlement, each trade's PnL measured against the true value, (settle − price) × signed size, plus the total.

## Persistence
- **Each game is standalone.** Logs are kept for review during the meeting. At the end the admin exports CSVs (trades, orders, events) and a full JSON event log, then resets.
- There is no leaderboard across meetings and no accounts.

## Reliability
- Model everything as an **append-only event log** so that current state equals a replay of the events. This makes export, review, and crash recovery straightforward.

## Testing
- Write pytest unit tests for the matching engine (price-time priority, partial fills, crossing limits, cancels), position and PnL math, the mark-price fallback, the auction, and the forced trade (including random assignment at timeout).
- Write a bot script that connects 60 simulated traders over WebSockets and trades randomly, used as a load test before any live meeting.

## Open questions: ask the instructor BEFORE building the part each one affects. Do not guess.
1. Frontend approach: plain HTML/JS served by FastAPI with no build step, or a framework?
   - **ANSWERED:** Plain HTML/JS/CSS served by FastAPI, with no build step.
2. Hit/lift: take only the top price level, or sweep multiple levels up to the size?
   - **ANSWERED:** Click-to-take. The book is displayed and the trader clicks liquidity to take it. Only the **best** bid and best offer are clickable; deeper levels are display-only. One click is one lot, unless the trader sets a different click size. The click fills at the clicked price or better. Any unfilled part is cancelled and never rests.
3. An order that would breach max position: reject it entirely, or clip it to the allowed size?
   - **ANSWERED:** Clip to the allowed size. If the clipped size is 0, the order is rejected.
4. Does max position apply to the MM's forced-trade fills? The MM absorbs (number of traders × forced size).
   - Note: this now interacts with Q3's clipping rule. If the limit applies to the MM, are forced trades clipped?
   - **ANSWERED (M4):** No. Max position is **ignored for everyone's forced-trade fills**, the MM's included; they always fill in full. Afterwards the normal worst-case room rule applies, so anyone over the limit can only trade to reduce.
5. Forced trade: book fills the moment someone clicks, or all at window close? Can a trader change their choice before close?
   - **ANSWERED (M4):** All forced trades print **when the window closes**. A trader may **change their choice** any number of times until then.
6. After the forced trade, does the MM's opening quote stay in the book? If so, at what size?
   - **ANSWERED (M4):** No. The quote is **removed**; the book opens empty, so the mark is the last forced-trade price until someone quotes.
7. Width auction: is width entered in price units or ticks? Is there a max starting width or a minimum width? Can the MM back out?
   - **ANSWERED (M4):** **Price units**, and it must be a **multiple of the tick**. **Minimum 0** (a width of 0 means the MM quotes bid = offer), **no maximum**. The MM **can't back out**. The MM's bid and offer must be on the tick, bid ≤ offer, and offer − bid ≤ the winning width.
8. Self-trade: if a trader's order would match their own resting order, what should happen?
   - **ANSWERED (changed 2026-09-28):** No trading with yourself. If an order (limit, take, or either side of a quote) would reach one of the trader's own resting orders, **the whole new order is rejected** ("would trade with your own order"). Nothing trades and the resting order stays. An order that fills completely against other traders before reaching its owner's resting order is allowed. For a two-sided quote, each side is checked on its own, like any other limit order. *(Earlier answer, now replaced: self-trades printed on the tape but changed no position or PnL.)*
9. Realized PnL method: FIFO or average cost?
   - **ANSWERED:** FIFO.
10. During the game, do traders see a leaderboard or other people's PnL, or only their own?
   - **ANSWERED:** Traders see their own PnL plus **everyone's positions**. Other traders' PnL is never sent to a trader's browser.
11. Review screen: fills only, or also orders placed and cancelled?
12. Crash recovery on free hosting: which approach? (a) The admin browser tab mirrors the event log and there is a "restore from file" button, (b) a free external database, or (c) accept the risk.
   - **Not in milestone 3** (decided 2026-09-28). Still open for a later milestone.
   - **Not in milestone 7 either** (decided 2026-09-28): for now we **accept the risk**. The README checklist says what to do if Render restarts mid-meeting.
13. Can the admin also trade?
   - **ANSWERED:** Yes, by joining the trader page in another tab like any other trader, with the same rules. The admin page has no order entry.
14. Fat-finger protection: max order size or rate limits?
   - **ANSWERED:** No per-order size cap and no rate limits.
15. Keyboard shortcuts for fast order entry?
   - **PARTLY ANSWERED:** Not in milestone 2. Still open for later.

### Answered during milestone 1 planning (2026-09-27)
16. Max-position basis: **worst case**. Buy room = max − (position + own resting bids). Sell room = max − (−position + own resting offers). Orders are clipped to the room.
17. Trade price when orders cross: the **resting order's price**.
18. Numbers: all integers. The tick is a positive integer. A price is any integer multiple of the tick, **including 0 and negatives**. Size is an integer ≥ 1.
19. Two-sided quote: it **adds** one bid and one offer of the same size, and existing orders are untouched. Each side behaves like an independent limit order (a crossing side trades immediately, and clipping applies per side). The quote is **rejected if bid ≥ offer**.
20. Mark when there's no mid and no last trade: **blank** ("—"). This only happens while position is 0, so MTM is 0.
21. Cancel all: **both** actions exist, one for this market and one for all markets.
22. Direct `CREATED → OPEN`: a **permanent** admin option.
23. Packaging: **pip + requirements.txt** in a venv.

### New open questions (found during planning)
24. (M2) One room per server, or several rooms?
   - **ANSWERED:** One room per server. Several markets can still run inside that room.
25. (M2) Click size: set per market or once per trader?
   - **ANSWERED:** Once per trader, for all markets. The browser remembers it.
26. (M3) On halt: do resting orders stay? Can traders cancel while halted?
   - **ANSWERED:** Resting orders stay. New orders and takes are rejected. Traders can still cancel (one order or cancel all).
27. (M3) On settle: are resting orders cancelled?
   - **ANSWERED:** No. They stay in the book, frozen: they can never trade and can't be cancelled.
28. (M3) On kick: what happens to the kicked trader's resting orders and position?
   - **ANSWERED:** Their resting orders are cancelled in every market that isn't settled. Their position and PnL stay and settle like anyone's. They are disconnected.
29. (M3) On rename: does history (tape, review) show the old name or the new one?
   - **ANSWERED:** The new name, everywhere (old trades included). The event log records the rename with the old and new name.
30. (M7) Which Python version to pin on Render (local is 3.14.4)?
   - **ANSWERED (M7):** Pin **3.14.4**, the same as local.
31. (M3) The lifecycle reads `OPEN ⇄ HALTED → SETTLED`. Can the admin settle directly from OPEN, or must they halt first? (The engine currently allows only HALTED → SETTLED.)
   - **ANSWERED:** Settle is allowed from OPEN or HALTED.

### Answered during milestone 2 planning (2026-09-27)
32. Room code: **4 random digits**, generated by the server at startup.
33. Rejoin: **both** methods. The browser stores a secret token and rejoins automatically after a refresh or Wi-Fi drop. Typing the same name (with the room code) also reclaims that trader if nobody is currently connected as them. Tokens are kept in memory only and never go in the event log or export.
34. Names: trimmed, **1–20 characters**, unique **ignoring case** ("alice" is refused if "Alice" exists).
35. Same trader in two tabs or devices: **the newest connection wins**. The old tab is told "opened elsewhere" and disconnected.
36. Book display: the **top 10 price levels** on each side, with owner names and sizes. *(From M5: 5 levels when several markets are running; see Q76.)*
37. Trade tape: **all trades**, newest first, scrollable.
38. Positions table: **every joined trader**, including those at 0.
39. Late joiners: the **admin can lock or unlock joining** (the control arrives in M3). Joining is always open in M2. *(Done in M3.)*
40. M2 setup: until the admin page exists, the server creates one open test market at startup (env vars `TEST_MARKET_TITLE`, `TEST_MARKET_TICK`, `TEST_MARKET_MAX_POSITION`; defaults "Test market", 1, 100) and prints the room code in the console. **This is temporary and removed in M3.** *(Removed in M3.)*
43. Limit-order entry (2026-09-28): **separate bid (buy) and offer (sell) entries**, each with its own price, size and button. There is no buy/sell dropdown.

### New open questions (found during milestone 2)
41. (M3, with Q12) After a Render restart, should the room code stay the same (restored) or change?
42. (M4) How do late joiners interact with the forced trade? For example, does someone who joins during the forced-trade window have to choose a side?
   - **ANSWERED (M4):** The forced trade includes **every non-kicked trader in the room when the window closes**, except the MM. That includes late joiners and traders who are offline. Anyone who hasn't chosen gets a random side.

### Answered during milestone 3 planning (2026-09-28)
44. Kicked trader: **blocked for the rest of the game.** Their token stops working and their name can't be reclaimed or reused. (They could still join under a brand-new name; there are no accounts.)
45. Info drops apply to the **whole room**, not to one market.
46. Admin login: a **password box** on `/admin`. The secret comes from the `ADMIN_SECRET` env var, and the admin's browser remembers it.
47. If `ADMIN_SECRET` isn't set, the server **refuses to start**.
48. Settlement value: **any integer** (0 and negatives allowed). It doesn't have to be on the tick.
49. The admin page shows **every trader's positions and PnL**. The admin page is never projected.
50. Until milestone 5, the trader page shows the **newest market that isn't settled** (if every market is settled, the newest one). *(Changed 2026-09-28. The earlier answer, "the first market created", left traders stuck on a settled market after the admin opened a new one.)* *(Replaced in M5 by Q75.)*
51. While joining is locked, **only new names are refused**. Existing traders who aren't kicked can still rejoin by token or by typing their name.
52. A market can be **edited** (all five settings) **only while it is CREATED**. Markets can't be deleted.

### Answered during milestone 4 planning (2026-09-28)
53. Trade or Tighten is **optional per market**: each CREATED market has two buttons, **"Trade or Tighten"** and **"Open directly"**. There is no extra market setting.
54. Closing the width auction when nobody has submitted a width is **refused**; the auction stays open.
55. There is **no way to abandon** Trade or Tighten partway (no cancel or skip). Halt and settle still work only from OPEN/HALTED. (The one exception is in Q58.)
56. The MM's **first quote is final**. The **admin** then starts the forced-trade window.
57. During the window, traders see **only their own choice** and the countdown, never anyone else's. The admin page (never projected) shows buy / sell / not-yet counts.
58. Kicking during Trade or Tighten: in **AUCTION**, the kicked trader's widths stop counting and the best width **reverts** to the narrowest remaining one (or none). In **MM_QUOTING**, if the MM is kicked, the **next-narrowest width holder becomes MM** at their width; if there is none, the market **returns to CREATED**. In **FORCED_TRADE**, the window runs on and the kicked MM's forced trades print as usual (like any kicked trader's position, it stays). Kicked non-MM traders are left out of the forced trade.
59. Forced trades print in the **order of each trader's final choice**, then randomly assigned traders in **join order**.
60. The admin can **end the window early** ("End now"); it works the same as the timer running out.
61. The trader page keeps the Q50 rule (newest unsettled market) until M5. *(Replaced in M5 by Q75.)*
62. Random side: an independent **coin flip** (50/50) for each trader who hasn't chosen.
63. Traders see the MM's bid and offer **as soon as the MM submits it**, before the window starts.

### Answered during milestone 7 planning (2026-09-28)
Milestones 5 and 6 are skipped for now. **Reset** moves from M6 into M7 (without export, which stays in M6), so the load-test bots can be cleared off the live server.
64. Reset is an admin **"Reset game"** button with an **OK/Cancel dialog**. It clears every market, trade, position and info drop, and the event log, and it **unlocks joining**.
65. The **room code stays the same** after a reset.
66. Traders **connected** at the moment of reset are **re-added automatically** under the same names (starting flat, in their old join order); their browsers stay logged in. **Offline** traders are not carried over and their old tokens stop working; they re-join by typing name + code.
67. Kicked traders get a **fresh start** at reset: they aren't carried over, but can join the new game like anyone.
68. The old game's event log is **discarded**; the new game starts a new log. Reset itself is not an event (it ends one standalone game and starts the next).
69. Load test: **60 bots plus an admin connection**, in **one market**. The script **runs the admin side itself** (it reads `ADMIN_SECRET`): create a market, the full **Trade or Tighten** opening, **2 minutes** of continuous trading at **one action per bot every ~2 s**, then settle. It does **not** reset at the end.
70. Load test pass: **p95 round trip < 750 ms**, zero errors, zero disconnects, positions sum to 0. *(Raised from 500 ms on 2026-09-28 after a local run with two markets in the room measured 606 ms, which the instructor considers fine.)*
71. Render is set up from a **`render.yaml` Blueprint**. The instructor creates the GitHub repo and pushes.

### Answered after the first Render load test (2026-09-28)
72. The trader page's order book has a **fixed layout** so the clickable best bid and offer never move: always **10 offer slots, the divider, then 10 bid slots** (Q36's depth; 5 and 5 when several markets are running, Q76), every row the same height, empty slots left blank. The best offer is always directly above the divider and the best bid directly below it.
73. When the names at one price don't fit on one line, they are **cut off with "…"**; hovering the row shows the full list (in time priority).

### Answered during milestone 5 planning (2026-10-05)
74. The trader page shows several markets **side by side**: every running market gets its own column at the same time. There are no tabs.
75. Only **running** markets get a column (AUCTION, MM_QUOTING, FORCED_TRADE, OPEN, HALTED). CREATED and SETTLED markets have none. *(This replaces the Q50/Q61 rule.)*
76. Book depth: **10 / 10 when one market is running, 5 / 5 when several are.** The fixed layout (Q72) stays; the book changes shape when a second market starts or the second-to-last one settles.
77. When there are more markets than fit across the screen, the columns **squeeze to fit** (one row, narrower columns).
78. Each column holds that market's **own position, realized, MTM and total PnL**, its resting orders and **"Cancel all (this market)"**. **Total PnL, click size and "Cancel all (all markets)"** stay in the top bar.
79. The positions table and the trade tape are **combined**: the tape lists trades from every market with a Market column, and the positions table has one column per market. Both **keep settled markets** (but not CREATED ones). A trader's settled PnL shows only inside Total PnL.
80. Positions and tape sit in a **right sidebar**; the market columns share the rest of the width.
81. A rejection uses the one message line under the top bar, **prefixed with the market title** ("Cars: rejected: …").
82. Market columns (and the positions table's market columns) go **oldest on the left** (creation order).
83. When no market is running: "No market is running. Wait for the instructor." where the columns go. The sidebar and Total PnL still show.
84. Below 1100 px wide, the market columns **stack vertically** (full width), with the sidebar underneath.
85. The admin page doesn't change in milestone 5.
86. Testing: the **server decides** which markets get columns, their order and the book depth, and pytest tests that. The page only draws what it is told.

## Build order (one milestone at a time; tests pass and instructor reviews before moving on)
1. Matching engine, positions, and PnL as pure Python with no web code, plus unit tests.
2. FastAPI server, WebSockets, join with name + room code, and a minimal trader UI for one market in continuous trading.
3. Admin page: create market, halt/resume, mark info drop, settle, kick/rename.
4. Trade or Tighten flow: width auction, MM quote, forced-trade window with timer.
5. Multiple concurrent markets in the UI.
6. Review/projector screen and CSV/JSON export.
7. 60-bot load test, deploy to Render, and meeting-day checklist in the README.
