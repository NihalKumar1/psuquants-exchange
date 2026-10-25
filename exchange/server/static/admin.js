// The admin page. It keeps one WebSocket to the server, sends admin commands, and redraws the
// whole screen from the latest admin state every time the server sends one.
// Shared helpers (load, save, tableRow, number, signed, ...) are in common.js.

const SECRET_KEY = "psuq-admin-secret";  // remembered so a refresh doesn't ask again

let socket = null;
let secret = null;       // the admin secret, once typed (or remembered)
let loggedIn = false;
let state = null;        // the latest "admin_state" from the server
let editingMarketId = null;   // the market being edited in the form, or null when creating
let resetFormOnNextState = false;

// --- Connection ---------------------------------------------------------------------------

function connect() {
  const scheme = location.protocol === "https:" ? "wss://" : "ws://";
  socket = new WebSocket(scheme + location.host + "/admin/ws");
  socket.onopen = () => socket.send(JSON.stringify({ type: "admin_login", secret }));
  socket.onmessage = (event) => handle(JSON.parse(event.data));
  socket.onclose = () => {
    if (!loggedIn) return;
    showMessage("Connection lost. Reconnecting…");
    setTimeout(connect, 2000);
  };
}

function send(command) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(command));
  } else {
    showMessage("Not connected. Please wait…");
  }
}

function handle(message) {
  if (message.type === "admin_welcome") {
    loggedIn = true;
    save(SECRET_KEY, secret);
    showMessage("");
    document.getElementById("login-screen").hidden = true;
    document.getElementById("admin-screen").hidden = false;
  } else if (message.type === "admin_state") {
    state = message;
    setCountdowns(state.markets);
    if (resetFormOnNextState) resetMarketForm();
    render();
  } else if (message.type === "admin_error") {
    resetFormOnNextState = false;  // keep what was typed so it can be fixed
    showMessage(message.reason);
  } else if (message.type === "error") {
    logOut(message.reason);
  }
}

function logOut(reason) {
  loggedIn = false;
  secret = null;
  save(SECRET_KEY, null);
  if (socket) socket.close();
  document.getElementById("admin-screen").hidden = true;
  document.getElementById("login-screen").hidden = false;
  document.getElementById("login-error").textContent = reason || "";
}

// --- Drawing the screen -------------------------------------------------------------------

function render() {
  document.getElementById("room-code").textContent = state.room_code;
  document.getElementById("joining-status").textContent = state.joining_locked ? "Locked" : "Open";
  document.getElementById("toggle-joining").textContent =
    state.joining_locked ? "Unlock joining" : "Lock joining";
  document.getElementById("starting-equity").textContent = `· ${money(state.starting_equity)}`;

  // Stop editing a market that has opened since (e.g. in another admin tab), or that is gone
  // because the game was reset.
  const edited = editingMarketId && state.markets[editingMarketId];
  if (editingMarketId && (!edited || edited.status !== "created")) resetMarketForm();

  renderRounds();
  renderMarkets();
  renderTraders();
  renderInfoDrops();
}

// --- Delta-hedging rounds (SPEC Q160-Q195) ------------------------------------------------
// One card per round: its clock and fair price, the settings only the admin sees (realized
// vol, the house's spread and size), its buttons, then each market's fair value, greeks and
// the house's position and PnL there. The admin always sees the greeks; the toggle decides
// whether traders do.

function renderRounds() {
  const rounds = Object.values(state.rounds);
  document.getElementById("rounds-panel").hidden = rounds.length === 0;
  document.getElementById("greeks-status").textContent =
    state.greeks_shown ? "Traders see the greeks" : "Greeks hidden from traders";
  document.getElementById("toggle-greeks").textContent =
    state.greeks_shown ? "Hide greeks" : "Show greeks";
  document.getElementById("rounds").replaceChildren(...rounds.map(roundCard));
}

function roundCard(round) {
  const card = document.createElement("div");
  card.className = "round-card";

  const head = document.createElement("div");
  head.className = "round-head";
  const status = document.createElement("span");
  status.textContent = statusText(round.status);
  status.className = `pill ${round.status}`;
  const title = document.createElement("h3");
  title.textContent = `${round.title} (${round.round_id})`;
  head.append(status, title, roundButtons(round));

  const facts = document.createElement("p");
  facts.className = "round-facts";
  facts.textContent = [
    `Fair ${decimal(round.fair_price, 2)}`,
    `Last move ${moveText(round.last_draw)}`,
    `${timeLeft(round.seconds_left)} left (move ${round.step} of ${round.total_steps})`,
    `Realized vol ${percent(round.realized_vol)}`,
    `Implied vol ${percent(round.implied_vol)}`,
    `House ±${number(round.house_half_spread)} × ${number(round.house_size)}`,
    `House PnL ${money(round.house.total)}`,
  ].join("  ·  ");

  const table = document.createElement("table");
  const header = document.createElement("tr");
  header.append(headerCell("Market"), headerCell("Fair", "num"), headerCell("Delta", "num"),
    headerCell("Gamma", "num"), headerCell("House pos", "num"), headerCell("House PnL", "num"));
  const body = document.createElement("tbody");
  for (const marketId of round.markets) {
    const greeks = round.greeks[marketId];
    const house = round.house.markets[marketId];
    const row = tableRow([state.markets[marketId].title, decimal(greeks.value, 2),
      decimal(greeks.delta, 2), decimal(greeks.gamma, 4), signed(house.position),
      pnlCell(house)]);
    for (let i = 1; i < row.cells.length; i++) row.cells[i].className = "num";
    body.append(row);
  }
  const thead = document.createElement("thead");
  thead.append(header);
  table.append(thead, body);

  card.append(head, facts, table);
  return card;
}

function roundButtons(round) {
  const id = round.round_id;
  const box = document.createElement("div");
  box.className = "action-row";
  if (round.status === "created") {
    box.append(button("Start round", "primary", () => {
      const question = `Start "${round.title}"? The price starts moving once a second, the ` +
        `house starts quoting, and the round settles itself in ${timeLeft(round.seconds_left)}.`;
      if (confirm(question)) send({ type: "start_round", round_id: id });
    }));
  } else if (round.status === "running") {
    box.append(button("Halt", "sell", () => send({ type: "halt_round", round_id: id })));
  } else if (round.status === "halted") {
    box.append(button("Resume", "buy", () => send({ type: "resume_round", round_id: id })));
  } else {
    box.append(totStatus(`Settled at ${number(state.markets[id].settlement_value)}`));
  }
  return box;
}

const ROUND_FIELDS = {
  round_id: "r-id",
  title: "r-title",
  start_price: "r-start",
  tick_size: "r-tick",
  minutes: "r-minutes",
  max_position: "r-max-position",
  dollars_per_point: "r-dollars",
  house_half_spread: "r-half-spread",
  house_size: "r-house-size",
};

function strikes(id) {
  // "950, 1000 1050" -> [950, 1000, 1050]
  return document.getElementById(id).value.split(/[\s,]+/).filter((text) => text !== "").map(Number);
}

document.getElementById("round-form").addEventListener("submit", (event) => {
  // The form keeps what was typed, so a second round with other settings is quick to make.
  event.preventDefault();
  const command = { type: "create_round" };
  for (const [setting, id] of Object.entries(ROUND_FIELDS)) {
    const value = document.getElementById(id).value.trim();
    command[setting] = setting === "round_id" || setting === "title" ? value : Number(value);
  }
  // Typed in percent; the server takes fractions (20% -> 0.2).
  command.realized_vol = Number(document.getElementById("r-realized-vol").value) / 100;
  command.implied_vol = Number(document.getElementById("r-implied-vol").value) / 100;
  command.options = [
    ...strikes("r-calls").map((strike) => ({ kind: "call", strike })),
    ...strikes("r-puts").map((strike) => ({ kind: "put", strike })),
  ];
  send(command);
});

document.getElementById("toggle-greeks").addEventListener("click", () =>
  send({ type: state.greeks_shown ? "hide_greeks" : "show_greeks" }));

function renderMarkets() {
  const markets = Object.values(state.markets);
  const tbody = document.querySelector("#markets tbody");
  if (markets.length === 0) {
    tbody.replaceChildren(emptyRow(10, "No markets yet. Create one on the left."));
    return;
  }
  const rows = markets.map((market) => {
    const status = document.createElement("span");
    status.textContent = statusText(market.status);
    status.className = `pill ${market.status}`;
    const row = tableRow([
      market.title, status, number(market.tick_size), number(market.max_position),
      money(market.dollars_per_point),
      number(market.best_bid), number(market.best_ask), number(market.last_price),
      number(market.mark), marketActions(market),
    ]);
    row.cells[0].title = `${market.title} (${market.market_id})`;
    row.cells[0].className = "title";
    for (const i of [2, 3, 4, 5, 6, 7, 8]) row.cells[i].className = "num";
    row.cells[9].className = "actions";
    return row;
  });
  tbody.replaceChildren(...rows);
}

function marketActions(market) {
  // The buttons that make sense for the market's status.
  const id = market.market_id;
  const box = document.createElement("div");
  box.className = "action-row";
  const tot = market.tot;
  if (market.round_id !== null && market.status !== "settled") {
    box.append(totStatus(`Run by round ${market.round_id} (above)`));
  } else if (market.status === "created") {
    // Two ways to start: the Trade or Tighten opening, or straight into continuous trading.
    box.append(
      button("Edit", "ghost", () => startEditing(market)),
      button("Trade or Tighten", "primary", () => send({ type: "start_auction", market_id: id })),
      button("Open directly", "ghost", () => confirmOpenDirectly(market)),
    );
  } else if (market.status === "auction") {
    const best = tot.best_holder === null
      ? "No widths yet"
      : `Best width ${number(tot.best_width)} (${tot.best_holder})`;
    const close = button("Close auction", "primary", () => send({ type: "close_auction", market_id: id }));
    close.disabled = tot.best_holder === null;
    box.append(totStatus(best), close);
  } else if (market.status === "mm_quoting") {
    const quote = tot.bid === null ? "waiting for quote" : `${number(tot.bid)} @ ${number(tot.ask)}`;
    const start = button("Start forced trade", "primary", () =>
      send({ type: "start_forced_trade", market_id: id }));
    start.disabled = tot.bid === null;
    box.append(totStatus(`MM ${tot.mm} (width ${number(tot.width)}): ${quote}`), start);
  } else if (market.status === "forced_trade") {
    const countdown = document.createElement("b");
    countdown.dataset.countdown = id;
    countdown.textContent = countdownText(id);
    const counts = market.choices;
    const status = totStatus(` · ${tot.mm} ${number(tot.bid)} @ ${number(tot.ask)}` +
      ` · Buy ${counts.buy} · Sell ${counts.sell} · Not yet ${counts.undecided}`);
    status.prepend(countdown);
    box.append(status, button("End now", "sell", () => endForcedTrade(market)));
  } else if (market.status === "open" || market.status === "halted") {
    if (market.status === "open") {
      box.append(button("Halt", "sell", () => send({ type: "halt", market_id: id })));
    } else {
      box.append(button("Resume", "buy", () => send({ type: "resume", market_id: id })));
    }
    const value = document.createElement("input");
    value.type = "number";
    value.step = "1";
    value.placeholder = "True value";
    value.className = "settle-value";
    box.append(value, button("Settle", "primary", () => settle(market, value.value)));
  } else if (market.status === "settled") {
    box.textContent = `Settled at ${number(market.settlement_value)}`;
  }
  return box;
}

function totStatus(text) {
  // A short note next to a market's buttons, e.g. "Best width 1,000 (Bob)".
  const span = document.createElement("span");
  span.className = "tot-status";
  span.textContent = text;
  return span;
}

function renderTraders() {
  // One row per trader: status, then position and PnL in each market (in that market's points),
  // then total PnL and equity in dollars (each market's PnL × its $ per point).
  // Then, for each delta-hedging round, their net delta and hedging error ("—" for anyone who
  // never held one of its options).
  const markets = Object.values(state.markets);
  const rounds = Object.values(state.rounds);

  // Two header rows: market and round titles on top (each over its two columns), then labels.
  const top = document.createElement("tr");
  top.append(headerCell(""), headerCell(""),
    ...markets.map((market) => headerCell(market.title, "market-head-cell", 2)),
    ...rounds.map((round) => headerCell(round.title, "market-head-cell", 2)),
    headerCell(""), headerCell(""), headerCell(""));
  const labels = document.createElement("tr");
  labels.append(headerCell("Trader"), headerCell("Status"),
    ...markets.flatMap(() => [headerCell("Pos", "num"), headerCell("PnL", "num")]),
    ...rounds.flatMap(() => [headerCell("Net delta", "num"), headerCell("Hedge err", "num")]),
    headerCell("Total PnL ($)", "num"), headerCell("Equity", "num"), headerCell(""));
  document.querySelector("#traders thead").replaceChildren(top, labels);

  const tbody = document.querySelector("#traders tbody");
  if (state.traders.length === 0) {
    tbody.replaceChildren(emptyRow(5 + 2 * (markets.length + rounds.length), "Nobody has joined yet."));
    return;
  }
  const rows = state.traders.map((trader) => {
    const status = trader.kicked ? "Removed" : trader.connected ? "Online" : "Offline";
    const cells = [trader.name, status];
    for (const market of markets) {
      const mine = trader.markets[market.market_id];
      cells.push(signed(mine.position), pnlCell(mine));
    }
    for (const round of rounds) {
      const mine = trader.rounds[round.round_id];
      cells.push(decimal(mine.net_delta, 2), decimal(mine.hedging_error, 2));
    }
    cells.push(pnlCell(trader.total, signedMoney, money), money(trader.total.equity),
      traderActions(trader));
    const row = tableRow(cells);
    row.cells[1].className = trader.kicked ? "down" : trader.connected ? "up" : "muted";
    for (let i = 2; i < cells.length - 1; i++) row.cells[i].className = "num";
    row.cells[cells.length - 1].className = "actions";
    if (trader.kicked) row.classList.add("kicked");
    return row;
  });
  tbody.replaceChildren(...rows);
}

function headerCell(text, className = "", colSpan = 1) {
  const th = document.createElement("th");
  th.textContent = text;
  th.title = text;
  th.className = className;
  th.colSpan = colSpan;
  return th;
}

function pnlCell(pnl, show = signed, format = number) {
  // Total PnL, with the realized/MTM split on hover. In points, or in dollars when given
  // signedMoney and money.
  const span = show(pnl.total);
  span.title = `Realized ${format(pnl.realized)} · MTM ${format(pnl.unrealized)}`;
  return span;
}

function traderActions(trader) {
  const box = document.createElement("div");
  box.className = "action-row";
  box.append(button("Rename", "ghost", () => rename(trader)));
  if (!trader.kicked) box.append(button("Kick", "ghost", () => kick(trader)));
  return box;
}

function renderInfoDrops() {
  const tbody = document.querySelector("#info-drops tbody");
  if (state.info_drops.length === 0) {
    tbody.replaceChildren(emptyRow(2, "No info drops yet"));
    return;
  }
  // Newest first, like the trade tape.
  const rows = [...state.info_drops].reverse().map((drop) =>
    tableRow([clockTime(drop.time), drop.note || "—"]));
  tbody.replaceChildren(...rows);
}

function showMessage(text) {
  // The message line is one line high; a message too long for it ends in "…" and shows in
  // full on hover.
  const message = document.getElementById("message");
  message.textContent = text;
  message.title = text;
}

// --- Actions ------------------------------------------------------------------------------

function settle(market, text) {
  if (text.trim() === "") {
    showMessage("Enter the true value to settle at.");
    return;
  }
  const value = Number(text);
  if (!confirm(`Settle "${market.title}" at ${number(value)}? This can't be undone.`)) return;
  send({ type: "settle", market_id: market.market_id, value });
}

function confirmOpenDirectly(market) {
  if (!confirm(`Open "${market.title}" for trading now, skipping Trade or Tighten?`)) return;
  send({ type: "open_market", market_id: market.market_id });
}

function endForcedTrade(market) {
  const question = `End the forced trade in "${market.title}" now? Anyone who hasn't chosen ` +
    "gets a random side.";
  if (!confirm(question)) return;
  send({ type: "end_forced_trade", market_id: market.market_id });
}

function rename(trader) {
  const name = prompt(`New name for ${trader.name}:`, trader.name);
  if (name === null) return;  // cancelled
  send({ type: "rename", trader_id: trader.trader_id, name });
}

function kick(trader) {
  const question = `Remove ${trader.name} from this game? Their resting orders are cancelled ` +
    "and they can't rejoin under this name. Their position stays.";
  if (!confirm(question)) return;
  send({ type: "kick", trader_id: trader.trader_id });
}

// The market form: creates a market, or edits one that hasn't opened yet.

const FORM_FIELDS = {
  title: "f-title",
  tick_size: "f-tick",
  max_position: "f-max-position",
  forced_trade_size: "f-forced-size",
  forced_trade_seconds: "f-forced-seconds",
  dollars_per_point: "f-dollars",
};

function startEditing(market) {
  editingMarketId = market.market_id;
  for (const [setting, id] of Object.entries(FORM_FIELDS)) {
    document.getElementById(id).value = market[setting];
  }
  document.getElementById("market-form-heading").textContent = `Edit market: ${market.title}`;
  document.getElementById("market-submit").textContent = "Save changes";
  document.getElementById("market-cancel-edit").hidden = false;
}

function resetMarketForm() {
  editingMarketId = null;
  resetFormOnNextState = false;
  document.getElementById("market-form").reset();
  document.getElementById("market-form-heading").textContent = "Create market";
  document.getElementById("market-submit").textContent = "Create market";
  document.getElementById("market-cancel-edit").hidden = true;
}

document.getElementById("market-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const command = editingMarketId
    ? { type: "edit_market", market_id: editingMarketId }
    : { type: "create_market" };
  for (const [setting, id] of Object.entries(FORM_FIELDS)) {
    const value = document.getElementById(id).value;
    command[setting] = setting === "title" ? value : Number(value);
  }
  resetFormOnNextState = true;  // cleared by an admin_error, so a mistake can be fixed
  send(command);
});

document.getElementById("market-cancel-edit").addEventListener("click", resetMarketForm);

document.getElementById("equity-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const box = document.getElementById("equity-input");
  if (box.value.trim() === "") {
    showMessage("Enter the starting equity in dollars.");
    return;
  }
  send({ type: "set_starting_equity", amount: Number(box.value) });
  box.value = "";
});

document.getElementById("info-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const note = document.getElementById("info-note");
  send({ type: "info_drop", note: note.value });
  note.value = "";
});

document.getElementById("toggle-joining").addEventListener("click", () =>
  send({ type: state.joining_locked ? "unlock_joining" : "lock_joining" }));

document.getElementById("reset-game").addEventListener("click", () => {
  const warning = state && state.unexported
    ? "You haven't exported this game since the last change.\n\n"
    : "";
  const question = warning + "Reset the game? This permanently deletes every market, trade " +
    "and position. Traders who are connected now stay in, under the same names, starting " +
    "flat. The room code stays the same.";
  if (confirm(question)) send({ type: "reset" });
});

document.getElementById("export-game").addEventListener("click", exportGame);

async function exportGame() {
  // Download the whole game as a zip. A plain link can't send the password, so the page
  // fetches the file with the password in a header, then hands it to the browser to save.
  try {
    const response = await fetch("/admin/export", { headers: { "X-Admin-Secret": secret } });
    if (!response.ok) {
      showMessage(response.status === 403 ? "Export refused: wrong admin secret." : "Export failed.");
      return;
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(await response.blob());
    link.download = match ? match[1] : "psuquants-game.zip";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);  // after the browser has saved it
    showMessage("");
  } catch {
    showMessage("Export failed. Check the connection and try again.");
  }
}

document.getElementById("log-out").addEventListener("click", () => logOut(""));

document.getElementById("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  document.getElementById("login-error").textContent = "";
  secret = document.getElementById("login-secret").value;
  connect();
});

// On page load, log in automatically if this browser remembers the secret.
secret = load(SECRET_KEY);
if (secret) connect();
