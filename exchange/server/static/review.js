// The review page, for the projector. It keeps one WebSocket to the server, sends the picked
// market and trader, and redraws everything from the latest "review_state" the server sends
// (right after a pick, then at most once a second while the market changes).
// Shared helpers (load, save, tableRow, number, signed, clockTime, ...) are in common.js.

const SECRET_KEY = "psuq-admin-secret";  // the same remembered password as the admin page
const PICK_KEY = "psuq-review-pick";     // the last market and trader picked, as JSON
const RANGE_KEY = "psuq-review-range";   // each market's typed price axis Min/Max, as JSON
const VIEW_KEY = "psuq-review-view";     // "review" or "leaderboard": which one is on screen

let socket = null;
let secret = null;
let loggedIn = false;
let state = null;    // the latest "review_state": {options, review}
let pick = loadPick();  // {market_id, trader_id}; either may be ""
let ranges = loadRanges();  // {rangeKey: {min, max}}: the text typed in the Min and Max boxes
let rangeShown = null;      // the rangeKey whose Min and Max are in the boxes now
let view = load(VIEW_KEY) === "leaderboard" ? "leaderboard" : "review";

// --- Connection ---------------------------------------------------------------------------

function connect() {
  const scheme = location.protocol === "https:" ? "wss://" : "ws://";
  socket = new WebSocket(scheme + location.host + "/review/ws");
  socket.onopen = () => socket.send(JSON.stringify({ type: "admin_login", secret }));
  socket.onmessage = (event) => handle(JSON.parse(event.data));
  socket.onclose = () => {
    if (!loggedIn) return;
    showMessage("Connection lost. Reconnecting…");
    setTimeout(connect, 2000);
  };
}

function handle(message) {
  if (message.type === "review_welcome") {
    loggedIn = true;
    save(SECRET_KEY, secret);
    showMessage("");
    document.getElementById("login-screen").hidden = true;
    document.getElementById("review-screen").hidden = false;
  } else if (message.type === "review_state") {
    state = message;
    render();
    // After logging in (or reconnecting), ask again for what was picked before.
    if (state.review === null && pickIsOffered()) sendPick();
  } else if (message.type === "error") {
    logOut(message.reason);
  }
}

function sendPick() {
  save(PICK_KEY, JSON.stringify(pick));
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "review_pick", ...pick }));
  }
}

function loadPick() {
  try {
    const saved = JSON.parse(load(PICK_KEY));
    if (saved && typeof saved.market_id === "string") return saved;
  } catch { /* nothing saved, or something unreadable: start with nothing picked */ }
  return { market_id: "", trader_id: "" };
}

function loadRanges() {
  try {
    const saved = JSON.parse(load(RANGE_KEY));
    if (saved && typeof saved === "object") return saved;
  } catch { /* nothing saved, or something unreadable: every market fits its prices */ }
  return {};
}

function pickIsOffered() {
  // True if the picked market and trader are both in the pickers (e.g. not after a reset).
  return state.options.markets.some((market) => market.market_id === pick.market_id)
    && state.options.traders.some((trader) => trader.trader_id === pick.trader_id);
}

function logOut(reason) {
  loggedIn = false;
  secret = null;
  save(SECRET_KEY, null);
  if (socket) socket.close();
  document.getElementById("review-screen").hidden = true;
  document.getElementById("login-screen").hidden = false;
  document.getElementById("login-error").textContent = reason || "";
}

function showMessage(text) {
  const message = document.getElementById("message");
  message.textContent = text;
  message.title = text;
}

// --- Drawing the screen -------------------------------------------------------------------

function render() {
  renderLeaderboard(state.leaderboard);
  const review = state.review;
  // The pickers always show whose review is on screen.
  if (review) pick = { market_id: review.market_id, trader_id: review.trader_id };
  renderPickers();

  const status = document.getElementById("market-status");
  status.hidden = review === null;
  if (review) {
    status.textContent = statusText(review.status);
    status.className = `pill ${review.status}`;
  }
  const settled = review !== null && review.settlement_value !== null;
  document.getElementById("settled-stat").hidden = !settled;
  if (settled) document.getElementById("settled-value").textContent = number(review.settlement_value);

  document.getElementById("fills-heading").textContent =
    review ? `${review.trader}'s trades in ${review.title}` : "Trades";
  const round = review === null ? null : review.round;
  document.getElementById("chart-heading").textContent = round === null ? "Price" :
    `Price · ${review.trader}'s net delta in ${round.title} (hedging error ${decimal(round.hedging_error, 2)})`;
  document.querySelectorAll(".round-key").forEach((key) => { key.hidden = round === null; });
  renderFills(review);
  renderRangeBoxes(review);
  renderChart(review);
}

function renderPickers() {
  fillSelect("pick-market", "Pick a market", pick.market_id,
    state.options.markets.map((market) =>
      [market.market_id, `${market.title} (${statusText(market.status).toLowerCase()})`]));
  fillSelect("pick-trader", "Pick a trader", pick.trader_id,
    state.options.traders.map((trader) =>
      [trader.trader_id, trader.kicked ? `${trader.name} (removed)` : trader.name]));
}

function fillSelect(id, placeholder, selected, choices) {
  // choices: [[value, text], ...]. The first option, "", is a placeholder for "nothing picked".
  const select = document.getElementById(id);
  const options = [["", placeholder], ...choices].map(([value, text]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    return option;
  });
  select.replaceChildren(...options);
  select.value = choices.some(([value]) => value === selected) ? selected : "";
}

for (const [id, key] of [["pick-market", "market_id"], ["pick-trader", "trader_id"]]) {
  document.getElementById(id).addEventListener("change", (event) => {
    pick = { ...pick, [key]: event.target.value };
    sendPick();
  });
}

// The table: the trader's fills, with the info drops in between at their times.

function renderFills(review) {
  const settled = review !== null && review.settlement_value !== null;
  const headers = ["Time", "Side", "Price", "Size", "Counterparty", "Position", "Realized", "MTM"];
  if (settled) headers.push("Edge");
  const head = document.createElement("tr");
  for (const [i, text] of headers.entries()) {
    const th = document.createElement("th");
    th.textContent = text;
    if (i === 2 || i === 3 || i >= 5) th.className = "num";
    head.append(th);
  }
  document.querySelector("#fills thead").replaceChildren(head);

  const tbody = document.querySelector("#fills tbody");
  if (review === null) {
    tbody.replaceChildren(emptyRow(headers.length, "Pick a market and a trader."));
    return;
  }
  // Fills and info drops in time order (a drop at the same moment as a fill goes after it).
  const lines = [
    ...review.rows.map((row) => ({ time: row.time, fill: row })),
    ...review.info_drops.map((drop) => ({ time: drop.time, drop })),
  ].sort((a, b) => Date.parse(a.time) - Date.parse(b.time));

  const rows = lines.map((line) => line.fill ? fillRow(line.fill, settled) : dropRow(line.drop, headers.length));
  if (review.rows.length === 0) rows.unshift(emptyRow(headers.length, `${review.trader} has no trades in this market.`));
  if (settled) rows.push(totalRow(review.total_edge, headers.length));
  tbody.replaceChildren(...rows);
}

function fillRow(fill, settled) {
  const side = document.createElement("span");
  side.textContent = (fill.side === "buy" ? "Buy" : "Sell") + (fill.forced ? " (forced)" : "");
  side.className = fill.side === "buy" ? "up" : "down";
  const cells = [clockTime(fill.time), side, number(fill.price), number(fill.size), fill.counterparty,
    signed(fill.position), signed(fill.realized), signed(fill.mtm)];
  if (settled) cells.push(signed(fill.edge));
  const row = tableRow(cells);
  for (const i of [2, 3, 5, 6, 7, 8]) if (row.cells[i]) row.cells[i].className = "num";
  if (fill.forced) row.classList.add("forced");
  return row;
}

function dropRow(drop, columns) {
  const td = document.createElement("td");
  td.colSpan = columns;
  td.textContent = `${clockTime(drop.time)}  Info drop${drop.note ? ": " + drop.note : ""}`;
  const row = document.createElement("tr");
  row.className = "drop";
  row.append(td);
  return row;
}

function totalRow(totalEdge, columns) {
  const label = document.createElement("td");
  label.colSpan = columns - 1;
  label.textContent = "Total edge vs settlement";
  const value = document.createElement("td");
  value.className = "num";
  value.append(signed(totalEdge));
  const row = document.createElement("tr");
  row.className = "total";
  row.append(label, value);
  return row;
}

// --- The leaderboard (SPEC Q153–Q158) -----------------------------------------------------
// Everyone ranked by equity, live. Equity and total PnL are in dollars; each market's column
// is that market's PnL in its own points. Removed traders are ranked too, greyed out.

function showView(name) {
  // Review (one trader in one market) or Leaderboard; the switch's buttons show which is on.
  view = name;
  save(VIEW_KEY, view);
  document.body.classList.toggle("leaderboard-view", view === "leaderboard");
  document.querySelectorAll("#view-switch button").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === view));
  if (state) render();  // the chart takes its width from the screen, so draw it once visible
}

function renderLeaderboard(board) {
  // Then each delta-hedging round's hedging error (average |net delta|, lower is better; "—" for
  // anyone who never held one of its options). The ranking stays by equity (SPEC Q176).
  const headings = [["#", "num"], ["Trader", ""], ["Equity", "num"], ["Total PnL ($)", "num"],
    ...board.markets.map((market) => [`${market.title} (pts)`, "num"]),
    ...board.rounds.map((round) => [`${round.title} hedging error`, "num"])];
  const head = document.createElement("tr");
  for (const [text, className] of headings) {
    const th = document.createElement("th");
    th.textContent = text;
    th.className = className;
    head.append(th);
  }
  // Hovering a market's heading shows its $ per point.
  board.markets.forEach((market, i) => { head.cells[4 + i].title = perPoint(market.dollars_per_point); });
  document.querySelector("#leaderboard thead").replaceChildren(head);

  const tbody = document.querySelector("#leaderboard tbody");
  if (board.rows.length === 0) {
    tbody.replaceChildren(emptyRow(headings.length, "Nobody has joined yet."));
    return;
  }
  tbody.replaceChildren(...board.rows.map((trader) => {
    const row = tableRow([
      number(trader.rank), trader.kicked ? `${trader.name} (removed)` : trader.name,
      money(trader.equity), signedMoney(trader.total_pnl),
      ...board.markets.map((market) => signed(trader.markets[market.market_id])),
      ...board.rounds.map((round) => decimal(trader.hedging_errors[round.round_id], 2)),
    ]);
    for (let i = 0; i < row.cells.length; i++) if (i !== 1) row.cells[i].className = "num";
    if (trader.kicked) row.classList.add("kicked");
    return row;
  }));
}

document.querySelectorAll("#view-switch button").forEach((b) =>
  b.addEventListener("click", () => showView(b.dataset.view)));
showView(view);

// --- The price chart (plain SVG) ----------------------------------------------------------
// Time runs left to right from the open to settlement (or now). The mark and the last price
// are step lines: each value holds until the next change. The trader's fills are triangles
// (green ▲ buy, red ▼ sell), info drops are dashed vertical lines, and once settled a
// horizontal line shows the true value.
// The price axis fits every price, unless the instructor typed a Min or Max: one wild trade
// would otherwise squash the rest (SPEC Q128–Q132). Lines and fills beyond it are cut off.

const CHART_HEIGHT = 380;
const MARGIN = { top: 28, right: 20, bottom: 30, left: 76 };
const DELTA_AXIS = 56;  // extra room on the right for the net-delta axis (round markets only)
const RANGE_BOXES = { min: "chart-min", max: "chart-max" };

function rangeKey(review) {
  // Which market the Min and Max belong to: its id and when it opened. Ids are reused after a
  // Reset ("m1" again), so the open time keeps an old game's range off the new market.
  if (review === null || review.chart === null) return null;
  return `${review.market_id} ${review.chart.start}`;
}

function renderRangeBoxes(review) {
  // Fill the boxes only when the market changes: this runs every second, and refilling them
  // each time would undo what the instructor is typing.
  const key = rangeKey(review);
  if (key === rangeShown) return;
  rangeShown = key;
  const typed = (key && ranges[key]) || { min: "", max: "" };
  for (const [end, id] of Object.entries(RANGE_BOXES)) {
    const box = document.getElementById(id);
    box.value = typed[end];
    box.disabled = key === null;
    box.classList.remove("wrong");
  }
}

for (const [end, id] of Object.entries(RANGE_BOXES)) {
  document.getElementById(id).addEventListener("input", (event) => {
    if (rangeShown === null) return;
    ranges[rangeShown] = { ...(ranges[rangeShown] || { min: "", max: "" }), [end]: event.target.value };
    save(RANGE_KEY, JSON.stringify(ranges));
    renderChart(state.review);
  });
}

function typedRange(review) {
  // The Min and Max typed for this market, as numbers, or null where a box is blank.
  const typed = ranges[rangeKey(review)] || { min: "", max: "" };
  const toNumber = (text) => (text === "" || !Number.isFinite(Number(text)) ? null : Number(text));
  return { min: toNumber(typed.min), max: toNumber(typed.max) };
}

function priceRange(review, prices) {
  // [low, high] for the price axis: the typed Min and Max, else the lowest and highest price.
  // The settlement value always stays in view. Min >= Max is ignored, and outlines the boxes.
  const typed = typedRange(review);
  const valid = typed.min === null || typed.max === null || typed.min < typed.max;
  for (const id of Object.values(RANGE_BOXES)) document.getElementById(id).classList.toggle("wrong", !valid);

  let low = valid && typed.min !== null ? typed.min : Math.min(...prices);
  let high = valid && typed.max !== null ? typed.max : Math.max(...prices);
  if (low > high) {  // only one box typed, beyond every price: fit the prices instead
    low = Math.min(...prices);
    high = Math.max(...prices);
  }
  if (review.settlement_value !== null) {
    low = Math.min(low, review.settlement_value);
    high = Math.max(high, review.settlement_value);
  }
  return [low, high];
}

function renderChart(review) {
  const box = document.getElementById("chart");
  if (review === null || review.chart === null) {
    const note = document.createElement("p");
    note.className = "waiting";
    note.textContent = review === null ? "Pick a market and a trader." : "Not open yet.";
    box.replaceChildren(note);
    return;
  }
  const chart = review.chart;
  const round = review.round;
  const width = Math.max(box.clientWidth, 320);
  const right = MARGIN.right + (round === null ? 0 : DELTA_AXIS);  // the plot's right margin
  const svg = svgElement("svg", { width, height: CHART_HEIGHT, class: "chart" });

  // Scales: time -> x pixels, price -> y pixels.
  const t0 = Date.parse(chart.start);
  const t1 = Math.max(Date.parse(chart.end), t0 + 1000);  // at least one second wide
  const x = (time) => MARGIN.left + (Date.parse(time) - t0) / (t1 - t0) * (width - MARGIN.left - right);
  const prices = [
    ...chart.points.flatMap((point) => [point.mark, point.last]),
    ...chart.fills.map((fill) => fill.price),
    ...(round === null ? [] : round.fair.map((point) => point.value)),
    review.settlement_value,
  ].filter((price) => price !== null);
  if (prices.length === 0) prices.push(0);
  const ticks = priceTicks(...priceRange(review, prices));
  const low = ticks[0], high = ticks[ticks.length - 1];
  const y = (price) => MARGIN.top + (high - price) / (high - low) * (CHART_HEIGHT - MARGIN.top - MARGIN.bottom);

  // Grid lines and price labels.
  for (const price of ticks) {
    svg.append(svgElement("line", { x1: MARGIN.left, x2: width - right, y1: y(price), y2: y(price), class: "grid" }));
    svg.append(svgText(number(price), { x: MARGIN.left - 8, y: y(price) + 4, class: "axis", "text-anchor": "end" }));
  }
  // Time labels: the start, the end, and a few in between.
  for (let i = 0; i <= 4; i++) {
    const time = new Date(t0 + (t1 - t0) * i / 4).toISOString();
    const anchor = i === 0 ? "start" : i === 4 ? "end" : "middle";
    svg.append(svgText(clockTime(time), { x: x(time), y: CHART_HEIGHT - 8, class: "axis", "text-anchor": anchor }));
  }

  // Info drops: a dashed line at the time, with the note at the top (all of it on hover).
  for (const drop of review.info_drops) {
    svg.append(svgElement("line", { x1: x(drop.time), x2: x(drop.time), y1: MARGIN.top - 8, y2: CHART_HEIGHT - MARGIN.bottom, class: "drop" }));
    const label = svgText(shorten(drop.note || "Info drop", 28), { x: x(drop.time) + 4, y: MARGIN.top - 12, class: "drop-note" });
    label.append(svgElement("title", {}, drop.note || "Info drop"));
    svg.append(label);
  }

  // The settlement line.
  if (review.settlement_value !== null) {
    const level = y(review.settlement_value);
    svg.append(svgElement("line", { x1: MARGIN.left, x2: width - right, y1: level, y2: level, class: "settle" }));
    svg.append(svgText(`Settled ${number(review.settlement_value)}`, { x: width - right, y: level - 6, class: "settle-note", "text-anchor": "end" }));
  }

  // Prices beyond the axis are cut off at the plot area (8 px spare at the top and bottom, so a
  // fill triangle right on the top or bottom gridline still shows whole).
  const clip = svgElement("clipPath", { id: "plot-area" });
  clip.append(svgElement("rect", {
    x: MARGIN.left - 8, y: MARGIN.top - 8,
    width: width - MARGIN.left - right + 16, height: CHART_HEIGHT - MARGIN.top - MARGIN.bottom + 16,
  }));
  const defs = svgElement("defs", {});
  defs.append(clip);
  const plot = svgElement("g", { "clip-path": "url(#plot-area)" });
  svg.append(defs, plot);

  // The last price and the mark, as step lines that run to the end of the chart.
  plot.append(svgElement("path", { d: stepPath(chart.points, "last", x, y, chart.end), class: "line last" }));
  plot.append(svgElement("path", { d: stepPath(chart.points, "mark", x, y, chart.end), class: "line mark" }));
  if (round !== null) {
    plot.append(svgElement("path", { d: stepPath(round.fair, "value", x, y, chart.end), class: "line fair" }));
    drawNetDelta(svg, plot, round.net_delta, x, chart.end, width - right);
  }

  // The trader's fills.
  for (const fill of chart.fills) {
    const cx = x(fill.time), cy = y(fill.price);
    const corners = fill.side === "buy"
      ? [[cx, cy - 7], [cx - 7, cy + 6], [cx + 7, cy + 6]]   // ▲
      : [[cx, cy + 7], [cx - 7, cy - 6], [cx + 7, cy - 6]];  // ▼
    const marker = svgElement("polygon", { points: corners.join(" "), class: `fill ${fill.side}` });
    marker.append(svgElement("title", {}, `${fill.side === "buy" ? "Bought" : "Sold"} at ${number(fill.price)}`));
    plot.append(marker);
  }

  box.replaceChildren(svg);
}

function drawNetDelta(svg, plot, points, x, end, plotRight) {
  // The trader's net delta as a step line on its own axis at the right (0 always in view, with
  // a dotted line at 0: a perfectly hedged trader's line sits on it).
  const values = points.map((point) => point.value);
  const ticks = priceTicks(Math.min(0, ...values), Math.max(0, ...values));
  const low = ticks[0], high = ticks[ticks.length - 1];
  const y = (delta) => MARGIN.top + (high - delta) / (high - low) * (CHART_HEIGHT - MARGIN.top - MARGIN.bottom);
  for (const delta of ticks) {
    svg.append(svgText(number(delta), { x: plotRight + 8, y: y(delta) + 4, class: "axis delta-axis" }));
  }
  svg.append(svgElement("line", { x1: MARGIN.left, x2: plotRight, y1: y(0), y2: y(0), class: "delta-zero" }));
  plot.append(svgElement("path", { d: stepPath(points, "value", x, y, end), class: "line delta" }));
}

function stepPath(points, key, x, y, end) {
  // "M x y H x V y H x ...": hold each value flat until the next point, then step to the new
  // value. A blank value (no mark yet) leaves a gap.
  let path = "";
  let drawing = false;
  for (const [i, point] of points.entries()) {
    const value = point[key];
    const next = i + 1 < points.length ? points[i + 1].time : end;
    if (value === null) {
      drawing = false;
      continue;
    }
    path += drawing ? ` V ${y(value)}` : ` M ${x(point.time)} ${y(value)}`;
    path += ` H ${x(next)}`;
    drawing = true;
  }
  return path.trim() || "M 0 0";
}

function priceTicks(low, high) {
  // About five round price levels covering low..high (e.g. 7,000 7,500 8,000 ...).
  if (low === high) { low -= 1; high += 1; }
  const rough = (high - low) / 5;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 5, 10].map((m) => m * power).find((s) => s >= rough);
  const ticks = [];
  for (let price = Math.floor(low / step) * step; price < high + step; price += step) {
    ticks.push(price);
    if (price >= high) break;
  }
  return ticks;
}

function shorten(text, length) {
  return text.length > length ? text.slice(0, length - 1) + "…" : text;
}

function svgElement(tag, attributes, text) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  if (text !== undefined) element.textContent = text;
  return element;
}

function svgText(text, attributes) {
  return svgElement("text", attributes, text);
}

// Redraw the chart to the new width when the window changes size.
window.addEventListener("resize", () => { if (state) renderChart(state.review); });

// --- Logging in ---------------------------------------------------------------------------

document.getElementById("log-out").addEventListener("click", () => logOut(""));

document.getElementById("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  document.getElementById("login-error").textContent = "";
  secret = document.getElementById("login-secret").value;
  connect();
});

// On page load, log in automatically if this browser remembers the password.
secret = load(SECRET_KEY);
if (secret) connect();
