// Small helpers shared by the trader page and the admin page.

// --- Browser storage (can fail in private windows, so every use is wrapped) ---------------

function load(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function save(key, value) {
  try {
    if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
  } catch { /* nothing to do: the page still works, it just won't remember */ }
}

// --- Tables -------------------------------------------------------------------------------

function tableRow(cells) {
  // Each cell is text, or a DOM node (e.g. a button) to put in the cell as is.
  const row = document.createElement("tr");
  for (const cell of cells) {
    const td = document.createElement("td");
    if (cell instanceof Node) td.append(cell); else td.textContent = cell;
    row.append(td);
  }
  return row;
}

function emptyRow(columns, text) {
  const td = document.createElement("td");
  td.colSpan = columns;
  td.textContent = text;
  const row = document.createElement("tr");
  row.className = "empty";
  row.append(td);
  return row;
}

function button(text, className, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = text;
  b.className = className;
  b.addEventListener("click", onClick);
  return b;
}

// --- Numbers and times --------------------------------------------------------------------

function number(value) {
  // Blank ("—") when there is no value, e.g. no mark before the first trade.
  if (value === null || value === undefined) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function signed(value) {
  // A number coloured green when positive and red when negative, with a "+" when positive.
  const span = document.createElement("span");
  span.textContent = (value > 0 ? "+" : "") + number(value);
  span.className = value > 0 ? "up" : value < 0 ? "down" : "";
  return span;
}

function statusText(status) {
  // "mm_quoting" -> "MM QUOTING"
  return status.replaceAll("_", " ").toUpperCase();
}

function clockTime(isoTime) {
  // "09:31:04" in the viewer's own time zone.
  return new Date(isoTime).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

// --- Forced-trade countdown ---------------------------------------------------------------
// Every message from the server says how many seconds each forced-trade window has left. The
// page turns that into a deadline on its own clock, so a laptop whose clock is off still
// counts down correctly. Any element with data-countdown="<market id>" shows the time left.

const countdownDeadlines = {};  // market_id -> performance.now() (ms) when the window closes

function setCountdowns(markets) {
  for (const market of Object.values(markets)) {
    const secondsLeft = market.tot === null ? null : market.tot.seconds_left;
    if (secondsLeft === null) {
      delete countdownDeadlines[market.market_id];
    } else {
      countdownDeadlines[market.market_id] = performance.now() + secondsLeft * 1000;
    }
  }
  showCountdowns();
}

function countdownText(marketId) {
  const deadline = countdownDeadlines[marketId];
  if (deadline === undefined) return "";
  const seconds = Math.ceil((deadline - performance.now()) / 1000);
  return seconds > 0 ? `${seconds}s` : "time's up";
}

function showCountdowns() {
  for (const element of document.querySelectorAll("[data-countdown]")) {
    element.textContent = countdownText(element.dataset.countdown);
  }
}

setInterval(showCountdowns, 250);
