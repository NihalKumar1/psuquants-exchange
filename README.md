# PSU Quants Exchange

A simulated exchange for PSU Quants meetings. Students trade "Trade or Tighten" estimation
markets against each other from their laptops. The instructor runs the game from the admin page.
The full rules are in [SPEC.md](SPEC.md).

## Run it on your laptop

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest                      # run all tests

$env:ADMIN_SECRET="choose-a-password"
uvicorn exchange.server.main:app --workers 1
```

- Trader page: http://localhost:8000
- Admin page: http://localhost:8000/admin (log in with the password above)
- The room code is printed in the console and shown on the admin page.

## Deploy to Render (free plan)

The service is described in [render.yaml](render.yaml): free plan, Python 3.14.4, one worker.

1. Push this project to a GitHub repository.
2. On [render.com](https://render.com), choose **New → Blueprint** and pick the repository.
3. Render asks for **ADMIN_SECRET**. Enter the admin password you want. It is stored only on
   Render, never in the repo.
4. Click **Deploy**. When it finishes, the service page shows its address, e.g.
   `https://psuquants-exchange.onrender.com`. Students go there, and you go to `/admin`.

Every push to the repository redeploys automatically. **A redeploy wipes the running game**, so
don't push during a meeting.

What the free plan means for us:
- **Everything lives in memory.** A restart, redeploy or sleep erases the game: markets,
  traders, positions. The room code changes too.
- **It sleeps after 15 minutes without traffic** and takes about a minute to wake up.
- If Render can't build Python 3.14.4, the build log will say so. Don't switch versions on
  your own; the version is a recorded decision in SPEC.md (Q30).

## Load test

[scripts/load_test.py](scripts/load_test.py) plays a whole game with 60 bot traders plus an
admin connection. It creates a market, runs Trade or Tighten, trades for 2 minutes (each bot
acts about every 2 seconds), then settles and checks the results.

```
$env:ADMIN_SECRET="the-server's-admin-password"
python scripts/load_test.py https://psuquants-exchange.onrender.com
```

(Use `http://127.0.0.1:8000` to test a server on your laptop. `--help` lists the options.)

It prints **RESULT: PASS** when all of these hold:
- 95% of commands get their reply within **750 ms**
- there are no errors
- no bot disconnects
- every forced trade prints
- positions add up to 0
- total PnL adds up to 0 after settlement

The bots show up on the admin page like real traders while it runs.

**Afterwards:** let the script finish (or press Ctrl+C) **before** pressing Reset game. Bots
that are still connected at a reset are carried into the new game.

Worth knowing:
- The laptop running the script receives everything that 60 students' laptops would: about
  2 GB of JSON in 2 minutes, roughly 300 MB after compression on the wire. Use a decent
  connection.
- **Every market in the room makes every update bigger**, settled markets included. On a
  laptop, with the room otherwise empty, p95 was 163 ms with one market and 606 ms with two.
  Reset game between games (if you don't need the old one) keeps the server fast.

## Meeting-day checklist

**The day before**
- [ ] Deploy (or confirm the latest version is deployed).
- [ ] Run the load test against the Render address. Check it says **RESULT: PASS**.
- [ ] Open `/admin` and press **Reset game** so the room is empty.

**5–10 minutes before the meeting**
- [ ] Open `https://<your-app>.onrender.com/admin`. If the service was asleep, this takes about
      a minute.
- [ ] Log in, and note the **room code** at the top of the admin page.
- [ ] Put the address and the room code on the slide or whiteboard.

**During the meeting**
- [ ] Create a market, then start it with **Trade or Tighten** or **Open directly**.
- [ ] Lock joining once everyone is in (people who already joined can still get back in).
- [ ] Press **Mark info drop** each time you announce new information.
- [ ] If more than 15 minutes might pass with no one using the site (e.g. a long talk), reload
      the admin page once in a while so Render doesn't put it to sleep.
- [ ] Settle each market at the true value.
- [ ] Between games, **Reset game** starts a fresh one. Everyone connected stays in under the
      same name, and the room code doesn't change.

**At the end**
- [ ] There is **no export yet** (milestone 6). Take screenshots of anything you want to keep
      before resetting or leaving. The game disappears when the service restarts or sleeps.

**If the server restarts mid-meeting** (every page loses its connection at once)
- Everything from the current game is gone, and there is a **new room code**.
- Reload the admin page, log in, and read out the new code. Students' pages go back to the join
  screen; they re-join with their name and the new code.
- Recreate the market and start again.
