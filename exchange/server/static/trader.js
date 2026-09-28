// The trader page. It keeps one WebSocket to the server, sends commands, and redraws the
// whole screen from the latest state every time the server sends an update.
// Shared helpers (load, save, tableRow, number, signed, ...) are in common.js.

const TOKEN_KEY = "psuq-token";            // lets this browser rejoin as the same trader
const CLICK_SIZE_KEY = "psuq-click-size";  // one click size for every market

let socket = null;
let me = null;          // {trader_id, name, token} once the server welcomes us
let stayDisconnected = false;  // true once we opened another tab, or were removed
let state = null;       // {markets, me, total} from the latest snapshot/update
let tape = {};          // market_id -> list of trades, oldest first
let freshTradeIds = new Set();  // trades from the latest update, briefly highlighted

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
    tape = message.tape;
    setCountdowns(state.markets);
    render();
  } else if (message.type === "update") {
    state = message;
    setCountdowns(state.markets);
    freshTradeIds = new Set(message.new_trades.map((trade) => trade.trade_id));
    for (const trade of message.new_trades) {
      tape[trade.market_id] = tape[trade.market_id] || [];
      tape[trade.market_id].push(trade);
    }
    render();
  } else if (message.type === "reset") {
    // Comes right after a new welcome and an empty snapshot, which already redrew the screen.
    showMessage("The instructor started a new game.");
  } else if (message.type === "rejected") {
    showMessage("Rejected: " + message.reason);
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

function currentMarketId() {
  // Until milestone 5 the page shows one market: the newest one that isn't settled, so traders
  // move on as soon as the admin creates the next market. If every market is settled, the
  // newest one. (Markets arrive in the order they were created.)
  const markets = Object.values(state.markets).reverse();  // newest first
  const unsettled = markets.find((market) => market.status !== "settled");
  return (unsettled || markets[0] || {}).market_id;
}

function render() {
  document.getElementById("my-name").textContent = state.name;  // the admin may rename us
  const marketId = currentMarketId();
  if (marketId === undefined) {
    document.getElementById("market-title").textContent = "No market yet. Wait for the instructor.";
    return;
  }
  const market = state.markets[marketId];
  const mine = state.me[marketId];

  document.getElementById("total-pnl").replaceChildren(signed(state.total.total));
  document.getElementById("market-title").textContent = market.title;
  const status = document.getElementById("market-status");
  status.textContent = statusText(market.status);
  status.className = `pill ${market.status}`;
  document.getElementById("market-info").textContent =
    `Tick ${number(market.tick_size)}  ·  Max position ${number(market.max_position)}`;
  for (const id of ["buy-price", "sell-price", "quote-bid", "quote-ask", "width-input", "mm-bid", "mm-ask"]) {
    document.getElementById(id).step = market.tick_size;
  }

  // While the market is in Trade or Tighten, its panel replaces order entry.
  document.getElementById("tot-panel").hidden = market.tot === null;
  document.getElementById("entry-panel").hidden = market.tot !== null;
  if (market.tot !== null) renderTradeOrTighten(market, mine);

  renderBook(marketId, market);
  renderMyNumbers(mine);
  renderMyOrders(marketId, market, mine);
  renderPositions(market);
  renderTape(tape[marketId] || []);
}

function renderTradeOrTighten(market, mine) {
  // The opening: width auction, then the market maker's quote, then the forced trade.
  // The boxes are part of the page (not rebuilt), so what someone is typing survives updates.
  const tot = market.tot;
  const show = (id, visible) => { document.getElementById(id).hidden = !visible; };

  show("tot-auction", market.status === "auction");
  document.getElementById("tot-best-width").textContent = number(tot.best_width);
  document.getElementById("tot-best-holder").textContent =
    tot.best_holder === null ? "No widths yet" : tot.best_holder;

  // Once there is a market maker: who it is and their quote.
  const mmLine = document.getElementById("tot-mm-line");
  show("tot-mm-line", tot.mm !== null);
  if (tot.mm !== null) {
    const who = mine.is_mm ? "You are" : `${tot.mm} is`;
    const quote = tot.bid === null ? "waiting for the quote…" : `${number(tot.bid)} @ ${number(tot.ask)}`;
    mmLine.textContent = `${who} the market maker at width ${number(tot.width)}: ${quote}`;
  }
  show("mm-quote-form", market.status === "mm_quoting" && mine.is_mm && tot.bid === null);

  // The forced trade: everyone but the MM picks a side; they can change it until time's up.
  show("tot-forced", market.status === "forced_trade");
  document.querySelector("#tot-forced [data-countdown]").dataset.countdown = market.market_id;
  document.getElementById("tot-size").textContent = number(tot.forced_trade_size);
  document.getElementById("forced-ask").textContent = number(tot.ask);
  document.getElementById("forced-bid").textContent = number(tot.bid);
  show("forced-buttons", !mine.is_mm);
  document.getElementById("choose-buy").classList.toggle("chosen", mine.my_choice === "buy");
  document.getElementById("choose-sell").classList.toggle("chosen", mine.my_choice === "sell");
  document.getElementById("forced-buttons").classList.toggle("decided", mine.my_choice !== null);
  let note;
  if (mine.is_mm) {
    note = "Everyone else is choosing. You take the other side of every forced trade.";
  } else if (mine.my_choice === null) {
    note = "Choose buy or sell. If you don't choose in time, you get a random side.";
  } else {
    note = `You chose to ${mine.my_choice.toUpperCase()}. You can change it until time's up.`;
  }
  document.getElementById("forced-note").textContent = note;
}

function renderBook(marketId, market) {
  const tbody = document.querySelector("#book tbody");
  if (market.bids.length === 0 && market.asks.length === 0) {
    const text = market.tot !== null
      ? "The book opens after the forced trade."
      : "No orders yet. Be the first to make a market.";
    tbody.replaceChildren(emptyRow(5, text), spreadRow(market));
    return;
  }

  // The biggest level sets the full width of the depth bars.
  const biggest = Math.max(1, ...[...market.bids, ...market.asks].map(levelSize));
  const rows = [];
  // Offers above bids: highest offer at the top, best (lowest) offer just above the spread.
  for (const level of [...market.asks].reverse()) {
    const isBest = level.price === market.best_ask;
    rows.push(bookRow("ask", level, biggest, isBest && (() => clickTake(marketId, "buy", level.price))));
  }
  rows.push(spreadRow(market));
  for (const level of market.bids) {
    const isBest = level.price === market.best_bid;
    rows.push(bookRow("bid", level, biggest, isBest && (() => clickTake(marketId, "sell", level.price))));
  }
  tbody.replaceChildren(...rows);
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
  // Once settled, it shows the true value instead of the mark.
  const td = document.createElement("td");
  td.colSpan = 5;
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

function renderMyNumbers(mine) {
  document.getElementById("my-position").replaceChildren(signed(mine.position));
  document.getElementById("my-realized").replaceChildren(signed(mine.realized));
  document.getElementById("my-unrealized").replaceChildren(signed(mine.unrealized));
  document.getElementById("my-total").replaceChildren(signed(mine.total));
}

function renderMyOrders(marketId, market, mine) {
  const tbody = document.querySelector("#my-orders tbody");
  if (mine.orders.length === 0) {
    tbody.replaceChildren(emptyRow(4, "No resting orders"));
    return;
  }
  const rows = mine.orders.map((order) => {
    const side = document.createElement("span");
    side.textContent = order.side === "buy" ? "Bid" : "Offer";
    side.className = order.side === "buy" ? "up" : "down";
    const cancel = button("✕", "icon", () =>
      send({ type: "cancel", market_id: marketId, order_id: order.order_id }));
    cancel.title = "Cancel this order";
    // A settled market's orders are frozen for good, so they get no cancel button.
    const frozen = market.status === "settled";
    const row = tableRow([side, number(order.price), number(order.size), frozen ? "" : cancel]);
    row.cells[1].className = "num";
    row.cells[2].className = "num";
    return row;
  });
  tbody.replaceChildren(...rows);
}

function renderPositions(market) {
  const rows = market.positions.map((entry) => {
    const row = tableRow([entry.name, signed(entry.position)]);
    row.cells[1].className = "num";
    if (entry.trader_id === me.trader_id) row.classList.add("mine");
    return row;
  });
  document.querySelector("#positions tbody").replaceChildren(...rows);
}

function renderTape(trades) {
  const tbody = document.querySelector("#tape tbody");
  if (trades.length === 0) {
    tbody.replaceChildren(emptyRow(5, "No trades yet"));
    return;
  }
  const rows = [...trades].reverse().map((trade) => {
    const row = tableRow([clockTime(trade.time), trade.buyer, trade.seller, number(trade.price), number(trade.size)]);
    row.cells[3].className = "num";
    row.cells[4].className = "num";
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
  document.getElementById("message").textContent = text;
}

function readNumber(id, what) {
  // Returns the number typed in a box, or null (with a message) if the box is empty.
  const text = document.getElementById(id).value.trim();
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

// Two separate limit-order entries: one for bids (buy), one for offers (sell).
for (const side of ["buy", "sell"]) {
  document.getElementById(`${side}-form`).addEventListener("submit", (event) => {
    event.preventDefault();
    const price = readNumber(`${side}-price`, "price");
    const size = readNumber(`${side}-size`, "size");
    if (price === null || size === null) return;
    send({ type: "limit", market_id: currentMarketId(), side, price, size });
  });
}

document.getElementById("quote-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const bid = readNumber("quote-bid", "bid");
  const ask = readNumber("quote-ask", "offer");
  const size = readNumber("quote-size", "size");
  if (bid === null || ask === null || size === null) return;
  send({ type: "quote", market_id: currentMarketId(), bid_price: bid, ask_price: ask, size });
});

// Trade or Tighten.
document.getElementById("width-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const width = readNumber("width-input", "width");
  if (width === null) return;
  send({ type: "width", market_id: currentMarketId(), width });
  document.getElementById("width-input").value = "";
});

document.getElementById("mm-quote-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const bid = readNumber("mm-bid", "bid");
  const ask = readNumber("mm-ask", "offer");
  if (bid === null || ask === null) return;
  send({ type: "mm_quote", market_id: currentMarketId(), bid, ask });
});

for (const side of ["buy", "sell"]) {
  document.getElementById(`choose-${side}`).addEventListener("click", () =>
    send({ type: "choose_side", market_id: currentMarketId(), side }));
}

document.getElementById("cancel-all-market").addEventListener("click", () =>
  send({ type: "cancel_all", market_id: currentMarketId() }));

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
