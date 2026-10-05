// The trader page. It keeps one WebSocket to the server, sends commands, and redraws the
// whole screen from the latest state every time the server sends an update.
// Shared helpers (load, save, tableRow, number, signed, ...) are in common.js.
//
// Every running market gets its own column, side by side. The server decides which markets
// get a column, in what order, and how deep their books are (state.columns, state.book_depth);
// this page just draws that.

const TOKEN_KEY = "psuq-token";            // lets this browser rejoin as the same trader
const CLICK_SIZE_KEY = "psuq-click-size";  // one click size for every market

let socket = null;
let me = null;          // {trader_id, name, token} once the server welcomes us
let stayDisconnected = false;  // true once we opened another tab, or were removed
let state = null;       // {columns, table_markets, book_depth, markets, me, total} from the server
let tape = [];          // every trade in every market, oldest first
let freshTradeIds = new Set();  // trades from the latest update, briefly highlighted
const columns = {};     // market_id -> that market's column on the page

// --- Connection ---------------------------------------------------------------------------

function connect(firstMessage) {
  const scheme = location.protocol === "https:" ? "wss://" : "ws://";
  socket = new WebSocket(scheme + location.host + "/ws");
  socket.onopen = () => socket.send(JSON.stringify(firstMessage));
  socket.onmessage = (event) => handle(JSON.parse(event.data));
  socket.onclose = () => {
    if (stayDisconnected || me === null) return;
    showMessage("Connection lost. Reconnecting…");
    setTimeout(() => connect({ type: "rejoin", token: me.token }), 2000);
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
  if (message.type === "welcome") {
    me = message;
    save(TOKEN_KEY, message.token);
    showMessage("");
    document.getElementById("join-screen").hidden = true;
    document.getElementById("trade-screen").hidden = false;
    document.getElementById("my-name").textContent = message.name;
  } else if (message.type === "snapshot") {
    state = message;
    // The snapshot's tape is per market; the page shows one tape for all markets, in the
    // order the trades happened (trade ids count up across every market).
    tape = Object.values(message.tape).flat().sort((a, b) => a.trade_id - b.trade_id);
    setCountdowns(state.markets);
    render();
  } else if (message.type === "update") {
    state = message;
    setCountdowns(state.markets);
    freshTradeIds = new Set(message.new_trades.map((trade) => trade.trade_id));
    tape.push(...message.new_trades);
    render();
  } else if (message.type === "reset") {
    // Comes right after a new welcome and an empty snapshot, which already redrew the screen.
    showMessage("The instructor started a new game.");
  } else if (message.type === "rejected") {
    // Several markets are on screen, so say which one the rejected order was for.
    const market = state && state.markets[message.market_id];
    showMessage((market ? `${market.title}: rejected: ` : "Rejected: ") + message.reason);
  } else if (message.type === "error") {
    // Joining failed (wrong code, name taken, or the server restarted and forgot us).
    me = null;
    save(TOKEN_KEY, null);
    document.getElementById("trade-screen").hidden = true;
    document.getElementById("join-screen").hidden = false;
    document.getElementById("join-error").textContent = message.reason;
  } else if (message.type === "opened_elsewhere") {
    stayDisconnected = true;
    showMessage("You opened the exchange in another tab or device. This tab is no longer connected.");
  } else if (message.type === "kicked") {
    stayDisconnected = true;
    save(TOKEN_KEY, null);
    showMessage("The instructor removed you from this game.");
  }
}

// --- Drawing the screen -------------------------------------------------------------------

function render() {
  document.getElementById("my-name").textContent = state.name;  // the admin may rename us
  document.getElementById("total-pnl").replaceChildren(signed(state.total.total));
  renderColumns();
  renderPositions();
  renderTape();
}

function renderColumns() {
  // Make the columns on the page match state.columns: add new markets, drop ones that settled,
  // keep the order. A column is built once and then reused, so prices half-typed into its
  // boxes survive updates.
  const container = document.getElementById("markets");
  for (const marketId of Object.keys(columns)) {
    if (!state.columns.includes(marketId)) {
      columns[marketId].remove();
      delete columns[marketId];
    }
  }
  state.columns.forEach((marketId, i) => {
    if (!columns[marketId]) columns[marketId] = buildColumn(marketId);
    // Only move a column that is out of place: moving one takes the cursor out of its boxes.
    if (container.children[i] !== columns[marketId]) {
      container.insertBefore(columns[marketId], container.children[i] || null);
    }
    renderColumn(columns[marketId], state.markets[marketId], state.me[marketId]);
  });
  document.getElementById("no-markets").hidden = state.columns.length > 0;
}

function part(column, name) {
  // One element inside a market's column, e.g. part(column, "buy-price").
  return column.querySelector("." + name);
}

function renderColumn(column, market, mine) {
  part(column, "title").textContent = market.title;
  part(column, "title").title = market.title;  // long titles are cut off; hover shows it all
  const status = part(column, "status");
  status.textContent = statusText(market.status);
  status.className = `status pill ${market.status}`;
  const info = `Tick ${number(market.tick_size)}  ·  Max position ${number(market.max_position)}` +
    "  ·  Click the best bid or offer to trade";
  part(column, "market-info").textContent = info;
  part(column, "market-info").title = info;
  for (const name of ["buy-price", "sell-price", "quote-bid", "quote-ask", "width-input", "mm-bid", "mm-ask"]) {
    part(column, name).step = market.tick_size;
  }

  // While the market is in Trade or Tighten, its panel replaces order entry.
  part(column, "tot-panel").hidden = market.tot === null;
  part(column, "entry-panel").hidden = market.tot !== null;
  if (market.tot !== null) renderTradeOrTighten(column, market, mine);

  renderBook(column, market);
  renderMyNumbers(column, mine);
  renderMyOrders(column, market, mine);
}

function renderTradeOrTighten(column, market, mine) {
  // The opening: width auction, then the market maker's quote, then the forced trade.
  // The boxes are part of the column (not rebuilt), so what someone is typing survives updates.
  const tot = market.tot;
  const show = (name, visible) => { part(column, name).hidden = !visible; };

  show("tot-auction", market.status === "auction");
  part(column, "tot-best-width").textContent = number(tot.best_width);
  part(column, "tot-best-holder").textContent =
    tot.best_holder === null ? "No widths yet" : tot.best_holder;

  // Once there is a market maker: who it is and their quote.
  show("tot-mm-line", tot.mm !== null);
  if (tot.mm !== null) {
    const who = mine.is_mm ? "You are" : `${tot.mm} is`;
    const quote = tot.bid === null ? "waiting for the quote…" : `${number(tot.bid)} @ ${number(tot.ask)}`;
    part(column, "tot-mm-line").textContent =
      `${who} the market maker at width ${number(tot.width)}: ${quote}`;
  }
  show("mm-quote-form", market.status === "mm_quoting" && mine.is_mm && tot.bid === null);

  // The forced trade: everyone but the MM picks a side; they can change it until time's up.
  show("tot-forced", market.status === "forced_trade");
  part(column, "tot-size").textContent = number(tot.forced_trade_size);
  part(column, "forced-ask").textContent = number(tot.ask);
  part(column, "forced-bid").textContent = number(tot.bid);
  show("forced-buttons", !mine.is_mm);
  part(column, "choose-buy").classList.toggle("chosen", mine.my_choice === "buy");
  part(column, "choose-sell").classList.toggle("chosen", mine.my_choice === "sell");
  part(column, "forced-buttons").classList.toggle("decided", mine.my_choice !== null);
  let note;
  if (mine.is_mm) {
    note = "Everyone else is choosing. You take the other side of every forced trade.";
  } else if (mine.my_choice === null) {
    note = "Choose buy or sell. If you don't choose in time, you get a random side.";
  } else {
    note = `You chose to ${mine.my_choice.toUpperCase()}. You can change it until time's up.`;
  }
  part(column, "forced-note").textContent = note;
}

function renderBook(column, market) {
  // The book never changes shape: always book_depth offer slots, the divider, then book_depth
  // bid slots, every row the same height (see style.css). So the best offer is always the row
  // just above the divider and the best bid the row just below it, and the row under the mouse
  // can't slide away between seeing it and clicking it. Empty slots are blank rows.
  // (The depth is 10 with one market on screen and 5 with several; the server decides.)
  // Slot 0 on each side is the best price; offers count upwards from the divider.
  const marketId = market.market_id;
  const biggest = Math.max(1, ...[...market.bids, ...market.asks].map(levelSize));
  const offerRows = [];
  const bidRows = [];
  for (let slot = 0; slot < state.book_depth; slot++) {
    const ask = market.asks[slot];
    const bid = market.bids[slot];
    offerRows.unshift(ask
      ? bookRow("ask", ask, biggest, slot === 0 && (() => clickTake(marketId, "buy", ask.price)))
      : blankBookRow());
    bidRows.push(bid
      ? bookRow("bid", bid, biggest, slot === 0 && (() => clickTake(marketId, "sell", bid.price)))
      : blankBookRow());
  }
  part(column, "book").tBodies[0].replaceChildren(...offerRows, spreadRow(market), ...bidRows);
}

function blankBookRow() {
  // An empty slot: the same height as a price level, so nothing below it moves.
  const row = tableRow(["", "", "", "", ""]);
  row.className = "blank";
  return row;
}

function bookRow(side, level, biggest, onClick) {
  // Columns: Bidders | Size | Price | Size | Offerers
  const names = ordersText(level);
  const size = number(levelSize(level));
  const price = number(level.price);
  const row = tableRow(side === "bid" ? [names, size, price, "", ""] : ["", "", price, size, names]);
  row.classList.add(side);

  const namesCell = side === "bid" ? row.cells[0] : row.cells[4];
  const sizeCell = side === "bid" ? row.cells[1] : row.cells[3];
  namesCell.className = side === "bid" ? "names" : "names right";
  namesCell.title = names;
  sizeCell.className = "num depth";
  sizeCell.style.setProperty("--depth", `${(100 * levelSize(level)) / biggest}%`);
  row.cells[2].className = "price";

  if (level.orders.some((order) => order.trader_id === me.trader_id)) row.classList.add("mine");
  if (onClick) {
    row.classList.add("clickable");
    row.title = side === "bid" ? "Click to sell at this bid" : "Click to buy at this offer";
    row.addEventListener("click", onClick);
  }
  return row;
}

function spreadRow(market) {
  // The divider between offers and bids: last price, mark, and the width of the market.
  // Once settled, it shows the true value instead of the mark. When the book is empty it also
  // says why (on the same line, so the book keeps its shape).
  const td = document.createElement("td");
  td.colSpan = 5;
  if (market.bids.length === 0 && market.asks.length === 0) {
    td.append(market.tot !== null
      ? "The book opens after the forced trade.   ·   "
      : "No orders yet. Be the first to make a market.   ·   ");
  }
  const parts = [["Last", market.last_price]];
  if (market.settlement_value !== null) {
    parts.push(["Settled at", market.settlement_value]);
  } else {
    parts.push(["Mark", market.mark]);
    if (market.best_bid !== null && market.best_ask !== null) {
      parts.push(["Width", market.best_ask - market.best_bid]);
    }
  }
  parts.forEach(([label, value], i) => {
    const b = document.createElement("b");
    b.textContent = number(value);
    td.append(`${i > 0 ? "   ·   " : ""}${label} `, b);
  });
  const row = document.createElement("tr");
  row.className = "spread";
  row.append(td);
  return row;
}

function levelSize(level) {
  return level.orders.reduce((total, order) => total + order.size, 0);
}

function ordersText(level) {
  // "Alice 20 · Bob 15": everyone in line at this price, first in line first.
  // Non-breaking spaces keep each "name size" together if the line wraps.
  return level.orders
    .map((order) => `${order.name} ${number(order.size)}`.replaceAll(" ", " "))
    .join("  ·  ");
}

function renderMyNumbers(column, mine) {
  part(column, "my-position").replaceChildren(signed(mine.position));
  part(column, "my-realized").replaceChildren(signed(mine.realized));
  part(column, "my-unrealized").replaceChildren(signed(mine.unrealized));
  part(column, "my-total").replaceChildren(signed(mine.total));
}

function renderMyOrders(column, market, mine) {
  const tbody = part(column, "my-orders").tBodies[0];
  if (mine.orders.length === 0) {
    tbody.replaceChildren(emptyRow(4, "No resting orders"));
    return;
  }
  const rows = mine.orders.map((order) => {
    const side = document.createElement("span");
    side.textContent = order.side === "buy" ? "Bid" : "Offer";
    side.className = order.side === "buy" ? "up" : "down";
    const cancel = button("✕", "icon", () =>
      send({ type: "cancel", market_id: market.market_id, order_id: order.order_id }));
    cancel.title = "Cancel this order";
    const row = tableRow([side, number(order.price), number(order.size), cancel]);
    row.cells[1].className = "num";
    row.cells[2].className = "num";
    return row;
  });
  tbody.replaceChildren(...rows);
}

function renderPositions() {
  // Everyone's position in every market that has started (settled ones too): one row per
  // trader, one column per market.
  const marketIds = state.table_markets;
  const header = document.createElement("tr");
  header.append(headerCell("Trader"), ...marketIds.map((marketId) =>
    headerCell(state.markets[marketId].title, "right")));
  document.querySelector("#positions thead").replaceChildren(header);

  const tbody = document.querySelector("#positions tbody");
  if (marketIds.length === 0) {
    tbody.replaceChildren(emptyRow(1, "No market has started yet"));
    return;
  }
  // Every market lists every trader, in the same (join) order, so row i is the same trader in
  // each market's list.
  const rows = state.markets[marketIds[0]].positions.map((entry, i) => {
    const row = tableRow([entry.name, ...marketIds.map((marketId) =>
      signed(state.markets[marketId].positions[i].position))]);
    for (let c = 1; c < row.cells.length; c++) row.cells[c].className = "num";
    if (entry.trader_id === me.trader_id) row.classList.add("mine");
    return row;
  });
  tbody.replaceChildren(...rows);
}

function headerCell(text, className = "") {
  // A table heading; long market titles are cut off and shown in full on hover.
  const th = document.createElement("th");
  th.textContent = text;
  th.title = text;
  th.className = className;
  return th;
}

function renderTape() {
  const tbody = document.querySelector("#tape tbody");
  if (tape.length === 0) {
    tbody.replaceChildren(emptyRow(6, "No trades yet"));
    return;
  }
  const rows = [...tape].reverse().map((trade) => {
    const title = state.markets[trade.market_id].title;
    const row = tableRow([clockTime(trade.time), title, trade.buyer, trade.seller,
      number(trade.price), number(trade.size)]);
    row.cells[1].title = title;
    row.cells[4].className = "num";
    row.cells[5].className = "num";
    if (freshTradeIds.has(trade.trade_id)) row.classList.add("fresh");
    if (trade.forced) {
      row.classList.add("forced");
      row.title = "Forced trade (Trade or Tighten)";
    }
    return row;
  });
  tbody.replaceChildren(...rows);
}

// --- Small helpers ------------------------------------------------------------------------

function showMessage(text) {
  // The message line is one line high; a message too long for it ends in "…" and shows in
  // full on hover.
  const message = document.getElementById("message");
  message.textContent = text;
  message.title = text;
}

function readNumber(input, what) {
  // Returns the number typed in a box, or null (with a message) if the box is empty.
  const text = input.value.trim();
  if (text === "") {
    showMessage(`Enter a ${what}.`);
    return null;
  }
  return Number(text);
}

function clickSize() {
  return Number(document.getElementById("click-size").value) || 1;
}

// --- Actions ------------------------------------------------------------------------------

function clickTake(marketId, side, price) {
  send({ type: "take", market_id: marketId, side, price, size: clickSize() });
}

function buildColumn(marketId) {
  // A new column for one market, copied from the <template> in trader.html. Its buttons and
  // forms send commands for this market only.
  const column = document.getElementById("market-column").content.firstElementChild.cloneNode(true);
  const onSubmit = (name, sendCommand) => {
    part(column, name).addEventListener("submit", (event) => {
      event.preventDefault();
      sendCommand();
    });
  };
  const read = (name, what) => readNumber(part(column, name), what);

  // Two separate limit-order entries: one for bids (buy), one for offers (sell).
  for (const side of ["buy", "sell"]) {
    onSubmit(`${side}-form`, () => {
      const price = read(`${side}-price`, "price");
      const size = read(`${side}-size`, "size");
      if (price === null || size === null) return;
      send({ type: "limit", market_id: marketId, side, price, size });
    });
  }

  onSubmit("quote-form", () => {
    const bid = read("quote-bid", "bid");
    const ask = read("quote-ask", "offer");
    const size = read("quote-size", "size");
    if (bid === null || ask === null || size === null) return;
    send({ type: "quote", market_id: marketId, bid_price: bid, ask_price: ask, size });
  });

  // Trade or Tighten.
  onSubmit("width-form", () => {
    const width = read("width-input", "width");
    if (width === null) return;
    send({ type: "width", market_id: marketId, width });
    part(column, "width-input").value = "";
  });

  onSubmit("mm-quote-form", () => {
    const bid = read("mm-bid", "bid");
    const ask = read("mm-ask", "offer");
    if (bid === null || ask === null) return;
    send({ type: "mm_quote", market_id: marketId, bid, ask });
  });

  for (const side of ["buy", "sell"]) {
    part(column, `choose-${side}`).addEventListener("click", () =>
      send({ type: "choose_side", market_id: marketId, side }));
  }
  column.querySelector("[data-countdown]").dataset.countdown = marketId;

  part(column, "cancel-all-market").addEventListener("click", () =>
    send({ type: "cancel_all", market_id: marketId }));
  return column;
}

document.getElementById("join-form").addEventListener("submit", (event) => {
  event.preventDefault();
  document.getElementById("join-error").textContent = "";
  stayDisconnected = false;
  connect({
    type: "join",
    name: document.getElementById("join-name").value,
    code: document.getElementById("join-code").value,
  });
});

document.getElementById("cancel-all-everywhere").addEventListener("click", () =>
  send({ type: "cancel_all" }));

const clickSizeBox = document.getElementById("click-size");
clickSizeBox.value = load(CLICK_SIZE_KEY) || 1;
clickSizeBox.addEventListener("change", () => save(CLICK_SIZE_KEY, clickSizeBox.value));

// On page load, rejoin automatically if this browser has joined before.
const savedToken = load(TOKEN_KEY);
if (savedToken) {
  me = { token: savedToken };  // filled in properly when the welcome arrives
  connect({ type: "rejoin", token: savedToken });
}
