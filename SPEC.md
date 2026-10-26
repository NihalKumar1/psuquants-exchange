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
  - Contract multiplier, **$ per point** (Q144)
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
  - **Amend** a resting order's price or size (Q133–Q138).
- **Names are visible**: book levels show who owns each order, and the trade tape shows buyer and seller names, like open outcry.
- Every order is validated against tick size and max position.
- Trades print at the **resting order's price**.

## Positions and PnL
- Each trader has, per market and in total: position, realized PnL, mark-to-market (unrealized) PnL, and total PnL. Each trader always sees their own numbers live.
- **Mark price = mid of best bid and best offer.** If either side of the book is empty, **use the last traded price.**
- After settlement, the mark is the settlement value.
- Each market's PnL is in its own price units (points). **Total PnL is in dollars**: each market's PnL × its $ per point, added up. **Equity** = the game's starting equity + total PnL (Q144–Q152).

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
   - **ANSWERED (M6):** **Fills only** (forced trades included). Orders and rejections are in the export.
12. Crash recovery on free hosting: which approach? (a) The admin browser tab mirrors the event log and there is a "restore from file" button, (b) a free external database, or (c) accept the risk.
   - **Not in milestone 3** (decided 2026-09-28). Still open for a later milestone.
   - **Not in milestone 7 either** (decided 2026-09-28): for now we **accept the risk**. The README checklist says what to do if Render restarts mid-meeting.
13. Can the admin also trade?
   - **ANSWERED:** Yes, by joining the trader page in another tab like any other trader, with the same rules. The admin page has no order entry.
14. Fat-finger protection: max order size or rate limits?
   - **ANSWERED:** No per-order size cap and no rate limits.
15. Keyboard shortcuts for fast order entry?
   - **PARTLY ANSWERED:** Not in milestone 2. Still open for later.
   - **ANSWERED (2026-10-18):** Yes; see Q142–Q143.

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
37. Trade tape: **all trades**, newest first, scrollable (inside its own box, Q120).
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
Milestones 5 and 6 are skipped for now *(both done since)*. **Reset** moves from M6 into M7 (without export, which stays in M6), so the load-test bots can be cleared off the live server.
64. Reset is an admin **"Reset game"** button with an **OK/Cancel dialog**. It clears every market, trade, position and info drop, and the event log, and it **unlocks joining**.
65. The **room code stays the same** after a reset.
66. Traders **connected** at the moment of reset are **re-added automatically** under the same names (starting flat, in their old join order); their browsers stay logged in. **Offline** traders are not carried over and their old tokens stop working; they re-join by typing name + code.
67. Kicked traders get a **fresh start** at reset: they aren't carried over, but can join the new game like anyone.
68. The old game's event log is **discarded**; the new game starts a new log. Reset itself is not an event (it ends one standalone game and starts the next).
69. Load test: **60 bots plus an admin connection**, in **one market**. The script **runs the admin side itself** (it reads `ADMIN_SECRET`): create a market, the full **Trade or Tighten** opening, **2 minutes** of continuous trading at **one action per bot every ~2 s**, then settle. It does **not** reset at the end.
70. Load test pass: **p95 round trip < 750 ms**, zero errors, zero disconnects, positions sum to 0. *(Raised from 500 ms on 2026-09-28 after a local run with two markets in the room measured 606 ms, which the instructor considers fine.)*
71. Render is set up from a **`render.yaml` Blueprint**. The instructor creates the GitHub repo and pushes.

### Answered after the first Render load test (2026-09-28)
72. The trader page's order book has a **fixed layout** so the clickable best bid and offer never move: always **10 offer slots, the divider, then 10 bid slots** (Q36's depth; 5 and 5 when several markets are running, Q76), every row the same height (since 2026-10-12 that height is sized to fit the screen, Q122), empty slots left blank. The best offer is always directly above the divider and the best bid directly below it.
73. When the names at one price don't fit on one line, they are **cut off with "…"**; hovering the row shows the full list (in time priority).

### Answered during milestone 5 planning (2026-10-05)
74. The trader page shows several markets **side by side**: every running market gets its own column at the same time. There are no tabs.
75. Only **running** markets get a column (AUCTION, MM_QUOTING, FORCED_TRADE, OPEN, HALTED). CREATED and SETTLED markets have none. *(This replaces the Q50/Q61 rule.)*
76. Book depth: **10 / 10 when one market is running, 5 / 5 when several are.** The fixed layout (Q72) stays; the book changes shape when a second market starts or the second-to-last one settles. *(Since 2026-10-12: 4 / 4 when three or more are running; see Q122.)*
77. When there are more markets than fit across the screen, the columns **squeeze to fit** (one row, narrower columns).
78. Each column holds that market's **own position, realized, MTM and total PnL**, its resting orders and **"Cancel all (this market)"**. **Total PnL, click size and "Cancel all (all markets)"** stay in the top bar. *(Since 2026-10-05 the resting orders are in the sidebar instead; see Q93–Q96.)*
79. The positions table and the trade tape are **combined**: the tape lists trades from every market with a Market column, and the positions table has one column per market. Both **keep settled markets** (but not CREATED ones). A trader's settled PnL shows only inside Total PnL.
80. Positions and tape sit in a **right sidebar**; the market columns share the rest of the width. *(Resting orders joined them on 2026-10-05; see Q93. Each list scrolls inside its own box, Q120.)*
81. A rejection uses the one message line under the top bar, **prefixed with the market title** ("Cars: rejected: …"). *(Since 2026-10-05 that line is a floating red banner that goes away after 3 s; see Q87–Q92.)*
82. Market columns (and the positions table's market columns) go **oldest on the left** (creation order).
83. When no market is running: "No market is running. Wait for the instructor." where the columns go. The sidebar and Total PnL still show.
84. Below 1100 px wide, the market columns **stack vertically** (full width), with the sidebar underneath.
85. The admin page doesn't change in milestone 5.
86. Testing: the **server decides** which markets get columns, their order and the book depth, and pytest tests that. The page only draws what it is told.

### Answered while fixing the trader page's error messages (2026-10-05)
Errors used to stay on screen until the next reconnect, and were easy to miss.
87. An error (a rejection, "Enter a price.", "Not connected") **disappears after 3 seconds**. A new error replaces the one showing and the 3 s start again.
88. It stays in the **top line under the top bar** (Q81, title prefix kept), as a **solid red banner** that **shakes** every time a message arrives, even the same one again.
89. The banner **floats over the page** (it takes no space), so the book never moves. While it shows, it covers the top strip of the page (the market titles).
90. **Red outline**: the empty boxes when the page's own check fails ("Enter a price."), and the **whole form** whose command the server rejected (Bid, Offer, Quote, width, MM quote). The outline goes away **with the banner**. To know the form, the server's rejection says the command's kind and side.
91. **Notices stay until resolved**, in the same red banner: "Connection lost. Reconnecting…" until reconnected; "opened in another tab" and "removed from this game" for good. An error shows on top of a notice for 3 s, then the notice comes back. "The instructor started a new game." goes away after 3 s like an error.
92. **Trader page only**: the admin page keeps its message line.

### Answered while moving resting orders to the sidebar (2026-10-05)
93. A trader's resting orders are in **one "Resting orders" panel at the top of the right sidebar**, above Positions. The market columns no longer list them.
94. **"Cancel all (this market)" stays in each market's column**, under its PnL tiles.
95. The panel lists orders in **running markets only** (the ones with a column). Orders frozen in a settled market aren't listed.
96. It is **one table with a Market column** (Market | Side | Price | Size | ✕): markets oldest first, and within a market bids then offers, best price first.

### Answered during milestone 6 planning (2026-10-06)
97. The review screen is a **separate `/review` page**, protected by the admin password (remembered like `/admin`). The trader and market are picked **on that page**.
98. The review screen is **live**: it updates while the market trades (at most once a second, because the chart can hold thousands of points).
99. "MTM PnL after each trade" uses the mark (mid, or last price if a side is empty) **right after that fill**, before any unfilled part of the same order rests.
100. The price chart is **hand-drawn SVG**, with no chart library.
101. The chart's time axis runs from the market's **open** (right after the forced trades print) to **settlement, or now** while it's still trading.
102. A market's chart and trade list show the info drops **inside that time span**.
103. Once settled, the chart also shows a **settlement line** (horizontal, at the true value). Nothing else is added beyond the spec.
104. The trader picker lists **everyone who joined**, including kicked traders and traders with no fills in that market. The market picker lists markets that have started.
105. The review page uses the **same dark theme** as the rest of the site.
106. Export is **one zip** with trades.csv, orders.csv, events.csv and events.json, from an **Export** button on the admin page.
107. Exported times are **US Eastern**.
108. Exports use **names everywhere** (current names, Q29). In events.json, names **replace** the ids in place (`"buyer_id": "Alice"`).
109. The Reset dialog **warns** when the game hasn't been exported since the last change (people only joining doesn't count as a change).

### Answered while adding fill alerts to the trader page (2026-10-12)
When one of a trader's orders fills, their page flashes and says what happened.
110. **What flashes**: that market's **column** and its **Position tile**, **green for a buy, red for a sell**. It happens for every fill: a resting order being filled, a take, a limit order that crosses, and **forced trades** too.
111. **The message** goes in the **top banner** (the one errors use, Q88–Q89), coloured **green for a buy, red for a sell** (the book's bid and offer colours): "You bought 5 of Cars at 36, now LONG 5" (or "now SHORT 5", or "now FLAT"). The position is the trader's position in that market after the fill. The market's **full title** is used, even if the banner wraps.
112. The flash and the message last **2 seconds**. The message **shakes** like an error.
113. Fills and errors share the banner: **the newest message wins** and keeps its own time (2 s for a fill, 3 s for an error). A notice ("Connection lost…") comes back once both are gone.
114. One action that fills several times (several prices or several counterparties) gives **one message** with the **total size and the average price**, shown with **up to 1 decimal** ("at avg 36.2").
115. When the market maker both buys and sells in the same moment (the forced trade), it is **one line with both sides**: "You bought 30 of Cars at avg 36 and sold 45 at avg 38, now SHORT 15". The flash is green or red by the **net change** (net bought = green, net sold = red).
116. The average is **cut down to 1 decimal** (truncated, not rounded): 36.25 → 36.2, 36.29 → 36.2.
   - A negative average is cut **towards zero**: −36.25 → −36.2.
117. When the MM's buys and sells cancel out (bought 30 and sold 30), the flash and message use the page's **neutral accent colour**.
118. "avg" is written **only when the fills were at different prices**: "You bought 25 of Cars at 36", but "You bought 25 of Cars at avg 36.2".

### Answered while redesigning the trader layout (2026-10-12)
Traders had to scroll to reach order entry and lost sight of the book while they did.
119. The trader page fits a **1366×768 laptop** (about 1366×650 inside the browser) with **no page scrolling**: the book, order entry, the PnL tiles, Cancel all and the sidebar are all visible at once. Bigger screens get more room.
120. Lists that can be longer than their space (Resting orders, Positions, Trades) sit in **fixed boxes that scroll inside**; their headings stay put. The page itself never scrolls (except in the fallbacks of Q123).
121. **One market**: the book on the left at full height, the controls (order entry or Trade or Tighten, the tiles, Cancel all) in a column to its right. **Several markets**: each column has the book on top and compact controls under it.
122. Book depth may go **as low as 3 levels per side**. It is **10 for one running market, 5 for two, 4 for three or more** (replaces Q76's "5 when several"). Book rows are sized to fit the space (at least 20 px; since Q127 with no maximum), still all the same height, so the best bid and offer never move (Q72).
123. Fallbacks: below 1100 px wide the Q84 stacked layout stays (the page scrolls). If the window is shorter than 600 px, the page scrolls rather than squashing the book further.

### Answered while adding the average price to the Position tile (2026-10-12)
Each market's Position tile also shows the average price the trader is in at: "+10 @ 117,500".
124. The average is of the **open lots, first in, first out** (Q9): buy 10 @ 100, buy 10 @ 110, sell 10 @ 120 leaves "+10 @ 110". So MTM is always (mark − average) × position.
125. It is **cut to 1 decimal** like the fill alerts (Q116): 36.25 → 36.2, −36.25 → −36.2; whole numbers have no decimal.
126. When the trader is **flat**, the tile shows **just 0** (no average).

### Answered while enlarging the order book (2026-10-12)
On a big screen the 34 px row limit left much of the book empty.
127. The book's rows **fill the whole book panel** (no maximum height), and its **text grows with the space**: prices up to 26 px, sizes a little smaller, names smaller still so more of them fit (the names keep about 40% of the width). The **depth does not change** (Q122).

### Answered while fixing the review chart's price axis (2026-10-17)
One wild trade stretched the chart's price axis and squashed every other price into a flat line.
128. The chart's price axis has **Min and Max boxes** next to the legend. A blank box fits that side to the prices on the chart. There is no automatic outlier detection and no "show all" toggle.
129. Lines and fill triangles beyond the range are **cut off** at the chart edge (no label).
130. The **settlement line is always in view**: the range stretches to include it.
131. The boxes are **remembered per market** in this browser, so they survive a reload. A Reset's new markets start blank (even though market ids like `m1` are reused).
132. If Min ≥ Max, the boxes are outlined in red and the chart fits everything.

### Answered while adding standard trading-platform features (2026-10-18)
The exec asked for the features most trading platforms have. They picked amend, click the book to fill the order form, a "my trades" list and keyboard shortcuts, and the real-exchange priority rule for amends. The details marked were chosen while building and are waiting for the instructor's review.
133. A trader can **amend** a resting order's price and size from the Resting orders list (✎, then Enter to send or Esc to stop). The size is **what should be left resting** (the size the list shows).
134. **Priority (real-exchange rule):** a **smaller size at the same price keeps the order's place in line**. A **new price or a bigger size** cancels the order and places a new limit order, which joins the **back of the line** (and trades if it crosses the book).
135. An amend that sends the order to the back is checked **like a new limit order** (tick size, max position with Q3's clipping, no trading with yourself). The old order's size doesn't count against max position. If the new order would be rejected, **the amend is rejected and the old order stays as it was**.
136. Shrinking works **while halted** (it only lowers risk, like cancelling). A new price or a bigger size is rejected while halted ("market is not open"). Settled markets' orders can't be amended (Q27). **Approved by the instructor (2026-10-19).**
137. Amending to size 0 is rejected ("use cancel"); amending to the same price and size is rejected ("nothing to change").
138. In the log: a shrink is an `OrderReduced` event; a move is `OrderCancelled` (reason "amended") followed by the new order, whose `OrderAccepted` says which order it `replaces`. orders.csv gains `reduced_to` and `replaces` columns.
139. **Click the book to fill the order form:** clicking a **deeper level** copies its price into the **Bid box** (for a bid level) or the **Offer box** (for an offer level) and puts the cursor in that size box. The best bid and offer still **trade** when clicked (Q2); **shift-click** copies their price instead.
140. While a market is in Trade or Tighten (no order entry), clicking the book does nothing.
141. **My trades:** the Trades panel has an **All / Mine** switch. Mine lists only the trader's own trades (forced trades included): time, market, **Buy/Sell**, price, size and **who with**. The browser remembers the choice. Updates carry each trade's `buyer_id` and `seller_id` (already public in the book) so the page can pick out its own trades even after a rename.
142. **Keyboard shortcuts**, only when the cursor is not in a box: **B** → the Bid price box, **S** → the Offer price box, **Q** → the Quote's bid box, **Esc** → cancel all of my orders in that market. In a box, **Esc** just leaves the box (so Esc twice cancels all). **Enter** sends the form the cursor is in. A hint is in the top bar. **Approved by the instructor (2026-10-19), Esc cancel-all included.**
143. With several markets, shortcuts act on the **market last clicked or typed in** (the first column until then); its title turns blue.

### Answered while adding contract multipliers and starting equity (2026-10-18)
Markets priced near 7,000 and near 120,000 were added up point for point, so the expensive market decided who was winning. The exec chose a contract multiplier per market (A) plus a starting equity shown as a score (B). Details marked were chosen while building and await the instructor's review.
144. Each market has a **contract multiplier, "$ per point"**, set by the admin when creating (or editing, before it opens) the market, like a future's multiplier (the E-mini is $50 per index point). PnL in dollars = PnL in points × $ per point. The admin picks it so a typical miss costs about the same in every market (e.g. $0.01 for a ~120,000 market, $0.20 for a ~7,000 one).
145. $ per point is **any number above 0** (decimals allowed). It **defaults to 1**: the form starts at 1, and a create command without it (older scripts, the load test) gets 1. Logs saved before this existed replay with 1.
146. **Equity is a score only.** It never blocks an order; **max position stays the only risk limit**. (Using equity as a margin limit was considered and rejected: it would need a worst-case price for every market.)
147. Each market's own numbers (Position tile, Realized, MTM, Total, the admin's per-market PnL, the review screen, the export) **stay in points**. Only **Total PnL and Equity are in dollars**.
148. **Starting equity** is one amount for the whole game, set by the admin (**default $100,000**): a whole number of dollars, 0 or more. Changing it mid-game is allowed (it only shifts everyone's score). It is logged as a `StartingEquitySet` event. **Approved by the instructor (2026-10-19).**
149. **Reset keeps the starting equity** (like the room code). **Approved by the instructor (2026-10-19).**
150. Traders see **only their own** equity and total PnL (top bar), as Q10 requires. The admin sees everyone's Total PnL ($) and Equity. **Instructor (2026-10-19): yes, the instructor can show a leaderboard; build it now.** Its details are open questions (Q153 on).
151. Dollar amounts are **rounded to the cent** before being sent, and shown with cents only when there are some ("$100,000", "-$12.50"). Each market's title line shows its multiplier ("$0.01/pt").
152. The export is unchanged except that events.csv/events.json carry `dollars_per_point` on market events and the new `StartingEquitySet` event.

### Answered while planning the leaderboard (2026-10-19)
153. The leaderboard is shown on the **review screen (projector)**, never on traders' pages, so Q10 still holds (other traders' PnL never reaches a trader's browser).
154. It can be shown **at any time**, live: equity with open positions valued at each market's current mark, updating like the review chart.
155. Each row shows **rank, name, equity, total PnL ($)** and **each market's PnL (in that market's points, Q147)**. Ranked by equity, highest first.
156. The review page has a **Review | Leaderboard switch** in its top bar; Leaderboard replaces the chart and trades with the ranked table.
157. Traders the admin removed **are shown, marked "(removed)"**, and ranked like everyone else.
158. **Ties share a rank** (1, 2, 2, 4), listed in name order.
159. The leaderboard is built in the same PR as the trader features and equity (PR #7).

### Answered while planning bots and price limits (2026-10-25)
The exec asked for maker and taker bots that the admin can turn on and off, whose PnL shows like everyone's and who give away no extra information, and for price limits so nobody can put absurd numbers in the book. (Numbered from 204: the delta-hedging branch uses 160-203.) Details marked *(proposed)* were chosen while building and await the instructor's review.

Price limits:
204. Each market may have a **min price** and a **max price**, set by the admin when creating it (or editing it before it opens). Either or both may be left blank (no limit). *(proposed: whole numbers, min below max.)*
205. *(proposed)* Every bid, offer, quote, click on the book and amend must be priced inside the limits, or it is rejected ("price must be between 0 and 100"; "at least 30" / "at most 180" with only one limit). The market maker's Trade or Tighten quote must be inside them too.
206. *(proposed)* The **settlement value is not limited**: it is the truth, whatever the limits.
207. *(proposed)* Traders see a market's limits on its title line ("Prices 0-100"); the admin sees them in a Range column.
208. *(proposed)* Logs saved before limits existed replay with no limits, and an export of a game without limits looks as it did.

Bots:
209. The admin page has **two switches: Makers on/off and Takers on/off**, for the whole game (all open markets).
210. Bots are **labelled**: "Maker Bot 1", "Maker Bot 2", "Taker Bot 1" to "Taker Bot 3" *(proposed: 2 makers, 3 takers)*. They are ordinary traders, so their positions and PnL show in the positions table, the admin's table (status "Bot"), the leaderboard, the review screen and the export like everyone's.
211. Bots **see only what students see**: the book, the last trade and their own position. They never know the true value or anyone's PnL, so they give nothing away.
212. Bots **sit out Trade or Tighten**: they never bid widths and are not in the forced trade. They act only in open markets.
213. *(proposed)* **Makers:** about every 3 seconds (at random), each maker replaces its quote in every open market: a bid and an offer a random 1-3 ticks either side of the middle of other people's best bid and offer (the last trade if the book isn't two-sided; no quote while a market has no price at all), size 1-5, rounded outwards to the tick, inside the price limits. It shades the quote 1 tick away from its position for every fifth of max position it holds, and leaves out a side it has no room for.
214. *(proposed)* **Takers:** about every 4 seconds (at random), each taker picks a random open market and side and buys at the best offer or sells at the best bid, size 1-3. Noise flow: it gives makers and students something to trade against.
215. *(proposed)* Bots follow every rule (tick size, max position, price limits, no trading with themselves).
216. *(proposed)* The first "on" brings the bots in (even if joining is locked). "Off" cancels their resting orders; they stay in the game with their positions, and "on" again reuses them. Nobody can take a bot's name or rejoin as a bot. The admin can rename or remove a bot like any trader.
217. *(proposed)* Each bot decision is logged as ordinary orders (plus `BotsStarted`/`BotsStopped` for the switches), so replay never runs a bot.
218. *(proposed)* **Reset turns both switches off**; the new game has no bots until they are turned on again.

### Answered while planning the trading timer (2026-10-25)
The exec asked whether markets should end on a timer instead of the instructor stopping them by hand, and approved: a trading timer that halts the market, plus an optional true value entered in advance so the market settles itself. (Numbered from 219: the delta-hedging and bots branches use 160-218.) Details marked *(proposed)* were chosen while building and await the instructor's review.
219. A market may have a **trading time**, set by the admin when creating it (or editing it before it opens), in minutes. Blank = no timer, as before.
220. When the trading time runs out, the market **halts by itself**, and every screen says **"Time's up"**.
221. The admin may enter the **true value in advance**. If it is there when time runs out, the market **settles itself at it** right after halting. If it is blank, the market waits, halted, for the instructor to settle it by hand (as for Dice Night, where the answer isn't known until the dice are rolled).
222. *(proposed)* The true value entered in advance is **admin-only**: it is never sent to a trader's browser. The admin page shows it as "Settles at ●●●●" until clicked, and the form box shows dots, in case the admin page is on the projector. It is in the event log and the export (after the game).
223. *(proposed)* The clock **starts when continuous trading starts** (Open directly, or the end of the forced trade), so Trade or Tighten doesn't eat into it.
224. *(proposed)* A **halt pauses the clock**; Resume continues it. The admin can add time (**+1 min**) or **End now** (the same as time running out: halt, then settle if there is a true value; it asks to confirm).
225. *(proposed)* After time is up the admin can **Settle** (any value, as always) or **Resume**, and trading then carries on with no clock. The admin can also settle by hand at any time before, at any value.
226. *(proposed)* Traders see the time left next to the market's title, counting down on their own laptop's clock (like the forced-trade timer), "(paused)" while halted, and "Time's up" in red at the end.
227. *(proposed)* The server checks four times a second for markets whose time is up. A game without timers exports as it always did.

## Build order (one milestone at a time; tests pass and instructor reviews before moving on)
1. Matching engine, positions, and PnL as pure Python with no web code, plus unit tests.
2. FastAPI server, WebSockets, join with name + room code, and a minimal trader UI for one market in continuous trading.
3. Admin page: create market, halt/resume, mark info drop, settle, kick/rename.
4. Trade or Tighten flow: width auction, MM quote, forced-trade window with timer.
5. Multiple concurrent markets in the UI.
6. Review/projector screen and CSV/JSON export.
7. 60-bot load test, deploy to Render, and meeting-day checklist in the README.
