# Dice Night: run sheet

A probability activity for the exchange (GBM of the week of 2026-10-26). It uses only what the
exchange already does: no new code.

## At a glance

Students trade four markets on three dice that the instructor rolls live, one at a time. Prices are probabilities, and each roll forces everyone to reprice.

- **Teaches:** expected value, the complement rule, conditional probability as information arrives, order statistics, and consistency between linked prices.
- **Time:** about 35 minutes of trading plus 10 minutes of debrief.
- **Bring:** three large dice (or one die rolled three times), a camera or a student volunteer to show each roll, and the projector for the review screen.
- **Room:** open the Render site about 2 minutes early so it wakes up; the room code is on the admin page.

## Setup on the admin page

Create these four markets before the meeting (Create market form), in this order, and leave them unopened. Paste each title exactly: it is what students see.

| # | Title / question | Settles at | Tick | Max position | Forced-trade size | Timer (s) | $ per point |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Sum of 3 dice × 10 | 30 to 180 | 1 | 50 | 5 | 30 | 2 |
| 2 | Pays 100 if any die is a six | 0 or 100 | 1 | 50 | 5 | 30 | 1 |
| 3 | Pays 100 if the sum is 12 or more | 0 or 100 | 1 | 50 | 5 | 30 | 1 |
| 4 | Highest die × 10 | 10 to 60 | 1 | 50 | 5 | 30 | 4 |

- **Why × 10:** tick sizes are whole numbers, so scaling by 10 lets students quote between whole sums (fair is 105, not 10.5).
- **Why different $ per point:** a typical miss is then worth about the same in each market, roughly $50 per contract, so no single market decides the leaderboard.
- Leave starting equity at the default $100,000.

## Running it

1. **Join (3 min).** Students open the site and join with the room code. Lock joining once everyone is in.
2. **Explain the markets (3 min).** Three dice, rolled one at a time. Market 1 settles at 10 × the sum, market 4 at 10 × the highest die, markets 2 and 3 at 100 or 0. A price of 42 in a "pays 100" market means "I think it's 42% likely".
3. **Open market 1 with Trade or Tighten (5 min).** Run the width auction, the market maker's quote and the forced trade as usual.
4. **Open markets 2 to 4 directly.** Click Open directly on each. Give students about 8 minutes to trade all four before any roll.
5. **Roll die 1.** Show it to the room, then click Mark info drop with the note "Die 1: 4" (the number rolled). Give 5 minutes to reprice.
6. **Roll die 2.** Same: info drop "Die 2: …", 5 minutes.
7. **Roll die 3.** Info drop "Die 3: …", then give 1 minute: every price is now known, so watch for anyone still quoting a stale price.
8. **Settle all four.** Enter the true value in each market's box and click Settle:
   - Market 1: 10 × the sum (e.g. 4 + 2 + 6 → 120)
   - Market 2: 100 if any die showed a six, else 0
   - Market 3: 100 if the sum is 12 or more, else 0
   - Market 4: 10 × the highest die
9. **Export** (top bar) before doing anything else, so the game is kept.

## Fair values (instructor only)

Fair value before any roll: **market 1: 105 · market 2: 42.1 · market 3: 37.5 · market 4: 49.6**. Keep this off the projector until the debrief.

**After die 1**, by the number rolled:

| Die 1 | 1. Sum × 10 | 2. Any six | 3. Sum ≥ 12 | 4. Highest × 10 |
| --- | --- | --- | --- | --- |
| 1 | 80 | 30.6 | 8.3 | 44.7 |
| 2 | 90 | 30.6 | 16.7 | 45.0 |
| 3 | 100 | 30.6 | 27.8 | 46.1 |
| 4 | 110 | 30.6 | 41.7 | 48.6 |
| 5 | 120 | 30.6 | 58.3 | 53.1 |
| 6 | 130 | 100 | 72.2 | 60 |

**After die 2:**

- **Sum × 10:** 10 × (sum of the two dice) + 35.
- **Any six:** 100 if a six has shown, else 16.7.
- **Sum ≥ 12**, by the sum of the two dice: 5 or less → 0 · 6 → 16.7 · 7 → 33.3 · 8 → 50 · 9 → 66.7 · 10 → 83.3 · 11 or 12 → 100.
- **Highest × 10**, by the highest die so far: 1 → 35 · 2 → 36.7 · 3 → 40 · 4 → 45 · 5 → 51.7 · 6 → 60.

After die 3 every market's value is known (the settlement).

## Debrief (10 min)

On the projector, open the review screen, pick market 3 (sum ≥ 12) and a student who traded it. The chart shows the price stepping at each info drop line, which is conditional probability made visible. Then switch to Leaderboard.

The math for each market, to reveal one at a time:

- **Sum × 10:** each die averages 3.5, so the sum averages 10.5 and fair is 105. After each die, swap 3.5 for what was rolled.
- **Any six:** easier through the complement: P(no six) = (5/6)³ = 125/216, so P(any six) = 91/216 ≈ 42%. A non-six on die 1 only drops it to 1 − (5/6)² ≈ 31%.
- **Sum ≥ 12:** count the outcomes: 81 of the 216 combinations, so 37.5%. It moves the most with each roll.
- **Highest × 10:** P(highest ≤ k) = (k/6)³, which gives an average of 4.96, not 3.5. Most students guess too low.

Questions to ask the room:

1. Before any roll, did market 2 trade above market 3? Should it have (42 vs 37.5)?
2. Who made money right after an info drop, and was that skill or speed?
3. After die 1 showed a six, market 2 was worth exactly 100. How long did it take the room to get there?
4. Did anyone trade markets against each other (e.g. high sum and highest die move together)?

## Second round and troubleshooting

**If there's time for a second round,** create the same four markets again with "(round 2)" at the end of each title and run the same steps. Don't press Reset game: it deletes round 1, and the leaderboard is more fun added up over both rounds. Settled markets drop off students' screens by themselves.

| Problem | What to do |
| --- | --- |
| A student's page froze or they closed the tab | Refresh the page: it rejoins as the same trader |
| A student joins late after joining is locked | Unlock joining for a moment, or they type their old name to get back in |
| A die falls off the table or is unclear | Re-roll before marking the info drop; only the info drop counts |
| Wrong settlement value typed | Settling can't be undone, so read the value aloud before clicking Settle |
| Site slow to load at the start | Render was asleep: it wakes in about 1 minute |
