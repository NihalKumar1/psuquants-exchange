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

  // Stop editing a market that has opened since (e.g. in another admin tab), or that is gone
  // because the game was reset.
  const edited = editingMarketId && state.markets[editingMarketId];
  if (editingMarketId && (!edited || edited.status !== "created")) resetMarketForm();

  renderMarkets();
  renderTraders();
  renderInfoDrops();
}

function renderMarkets() {
  const markets = Object.values(state.markets);
  const tbody = document.querySelector("#markets tbody");
  if (markets.length === 0) {
    tbody.replaceChildren(emptyRow(9, "No markets yet. Create one on the left."));
    return;
  }
  const rows = markets.map((market) => {
    const status = document.createElement("span");
    status.textContent = statusText(market.status);
    status.className = `pill ${market.status}`;
    const row = tableRow([
      market.title, status, number(market.tick_size), number(market.max_position),
      number(market.best_bid), number(market.best_ask), number(market.last_price),
      number(market.mark), marketActions(market),
    ]);
    row.cells[0].title = `${market.title} (${market.market_id})`;
    row.cells[0].className = "title";
    for (const i of [2, 3, 4, 5, 6, 7]) row.cells[i].className = "num";
    row.cells[8].className = "actions";
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
  if (market.status === "created") {
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
  // One row per trader: status, then position and PnL in each market, then total PnL.
  const markets = Object.values(state.markets);

  // Two header rows: market titles on top (each over its Pos and PnL columns), then labels.
  const top = document.createElement("tr");
  top.append(headerCell(""), headerCell(""),
    ...markets.map((market) => headerCell(market.title, "market-head-cell", 2)),
    headerCell(""), headerCell(""));
  const labels = document.createElement("tr");
  labels.append(headerCell("Trader"), headerCell("Status"),
    ...markets.flatMap(() => [headerCell("Pos", "num"), headerCell("PnL", "num")]),
    headerCell("Total PnL", "num"), headerCell(""));
  document.querySelector("#traders thead").replaceChildren(top, labels);

  const tbody = document.querySelector("#traders tbody");
  if (state.traders.length === 0) {
    tbody.replaceChildren(emptyRow(4 + 2 * markets.length, "Nobody has joined yet."));
    return;
  }
  const rows = state.traders.map((trader) => {
    const status = trader.kicked ? "Removed" : trader.connected ? "Online" : "Offline";
    const cells = [trader.name, status];
    for (const market of markets) {
      const mine = trader.markets[market.market_id];
      cells.push(signed(mine.position), pnlCell(mine));
    }
    cells.push(pnlCell(trader.total), traderActions(trader));
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

function pnlCell(pnl) {
  // Total PnL, with the realized/MTM split on hover.
  const span = signed(pnl.total);
  span.title = `Realized ${number(pnl.realized)} · MTM ${number(pnl.unrealized)}`;
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
