"use strict";

// Leap+ frontend: tab routing, one fetch helper, and renderers that always
// show a loading, empty or error state. No framework, no build step.

const VIEWS = ["home", "live", "card", "ask"];
const INTEGRATION_NAMES = { stripe: "Stripe", nta: "NTA", openrouter: "OpenRouter" };

// --- Helpers -----------------------------------------------------------------

// Build DOM nodes. Strings become text nodes, so server data can never inject HTML.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value);
  }
  for (const child of children) if (child != null && child !== false) el.append(child);
  return el;
}

// The only way the frontend calls the API. Returns parsed JSON, or throws an
// Error whose message is safe to show the user.
async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  let res;
  try {
    res = await fetch(path, {
      ...options,
      signal: controller.signal,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
  } catch (err) {
    throw new Error(err.name === "AbortError"
      ? "The server took too long to answer."
      : "Can't reach the Leap+ server. Is it running?");
  } finally {
    clearTimeout(timer);
  }
  let body = null;
  try { body = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) throw new Error(body?.error?.message || `Request failed (${res.status}).`);
  return body;
}

// Display only. The server stores and computes money as integer cents.
const euro = new Intl.NumberFormat("en-IE", { style: "currency", currency: "EUR" });
const formatCents = (cents) => euro.format(cents / 100);

function errorPanel(title, message, onRetry) {
  return h("div", { class: "panel error", role: "alert" },
    h("h2", {}, title),
    h("p", {}, message),
    onRetry && h("button", { class: "button secondary", type: "button", onclick: onRetry }, "Try again"));
}

// --- Tabs ----------------------------------------------------------------------

function currentView() {
  const name = location.hash.slice(1);
  return VIEWS.includes(name) ? name : "home";
}

function showView() {
  const name = currentView();
  if (name === "live" && busMap) setTimeout(() => busMap.invalidateSize(), 0); // map was sized while hidden
  for (const view of VIEWS) document.getElementById(`view-${view}`).hidden = view !== name;
  for (const tab of document.querySelectorAll("[data-tab]")) {
    if (tab.dataset.tab === name) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
}

// --- Balance card (Home and Card tabs) --------------------------------------

function balanceCard({ status, me, error }) {
  const label = h("p", { class: "balance-label" }, "Simulated Leap balance");
  if (status === "loading") {
    return h("div", { class: "balance-card", "aria-busy": "true" },
      label, h("div", { class: "skeleton big" }), h("div", { class: "skeleton", style: "width:45%" }));
  }
  if (status === "error") return errorPanel("Couldn't load your balance", error, loadMe);
  return h("div", { class: "balance-card" },
    label,
    h("p", { class: "balance-amount" }, formatCents(me.balance_cents)),
    h("p", { class: "balance-note" }, "Stored in Leap+ only. Not on a real Leap card."));
}

function renderBalance(state) {
  for (const slot of document.querySelectorAll("[data-balance]")) slot.replaceChildren(balanceCard(state));
}

async function loadMe() {
  renderBalance({ status: "loading" });
  try {
    const me = await api("/api/me");
    document.getElementById("user-chip").textContent = me.name;
    renderBalance({ status: "ok", me });
  } catch (err) {
    renderBalance({ status: "error", error: err.message });
  }
}

// --- Feature availability ------------------------------------------------------
// The server decides what is built and configured; the UI never shows controls
// for a feature that isn't working.

function featurePanel(slot, health, error) {
  const label = slot.dataset.label;
  if (error) return errorPanel(`Can't check ${label}`, error, loadHealth);
  if (!health) return h("div", { class: "panel", "aria-busy": "true" }, h("div", { class: "skeleton", style: "width:60%" }));

  const feature = health.features[slot.dataset.feature];
  if (feature.available) return null; // the feature's own renderer takes over (later stages)

  const why = feature.reason === "missing_keys"
    ? `Turned off: the server has no ${INTEGRATION_NAMES[feature.needs]} keys yet. See the README.`
    : "Not available in this build yet.";
  return h("div", { class: "panel" }, h("h2", {}, `${label} isn't available`), h("p", {}, why));
}

// Each built feature's renderer, called only when the server says it is available.
const FEATURE_RENDERERS = { topup: loadCardOptions, ask: renderAsk, live: renderMap };

// --- Live bus map (Leaflet + OpenStreetMap, positions from NTA GTFS-Realtime Vehicles) ---

let busMap = null;
let stationPin = null;
const busMarkers = new Map();

function renderMap(slot) {
  if (slot.dataset.ready) return;
  slot.dataset.ready = "1";
  if (!window.L) {
    slot.replaceChildren(errorPanel("The map couldn't load", "The map library didn't download. Check the internet connection.", () => location.reload()));
    return;
  }
  const status = h("div", { class: "map-status" });
  const mapEl = h("div", { class: "map", role: "region", "aria-label": "Live bus map" });
  slot.replaceChildren(h("div", { class: "panel map-card" }, mapEl, status));
  busMap = L.map(mapEl, { zoomControl: false }).setView([53.322, -6.25], 13);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(busMap);
  L.control.zoom({ position: "bottomright" }).addTo(busMap);
  loadVehicles(status);
  setInterval(() => { if (currentView() === "live") loadVehicles(status); }, 30_000);
}

async function loadVehicles(status) {
  try {
    const d = await api("/api/vehicles");
    const seen = new Set();
    const fetchedAt = Date.now();
    for (const v of d.vehicles) {
      seen.add(v.id);
      const icon = L.divIcon({ className: "bus-icon", html: busTag(v), iconSize: null });
      let marker = busMarkers.get(v.id);
      if (marker) {
        marker.setLatLng([v.lat, v.lon]).setIcon(icon);
      } else {
        marker = L.marker([v.lat, v.lon], { icon, keyboard: false, title: `Route ${v.route}` }).addTo(busMap);
        marker.bindPopup(() => busPopup(marker.vehicle, marker.fetchedAt), { closeButton: false, offset: [0, -6] });
        busMarkers.set(v.id, marker);
      }
      marker.vehicle = v;
      marker.fetchedAt = fetchedAt;
    }
    for (const [id, marker] of busMarkers) if (!seen.has(id)) { marker.remove(); busMarkers.delete(id); }
    const count = `${d.vehicles.length} buses` + (d.hidden_stale ? ` · ${d.hidden_stale} hidden, no update in 5 min` : "");
    status.replaceChildren(freshness(d.realtime), h("span", { class: "map-count" }, count));
  } catch (err) {
    status.replaceChildren(h("p", { class: "fresh warn" }, err.message));
  }
}

const COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];

function busTag(v) {
  return h("span", { class: "bus-tag" }, v.route,
    v.bearing != null ? h("span", { class: "bus-arrow", style: `transform: rotate(${v.bearing}deg)`, "aria-hidden": "true" }, "↑") : null);
}

function busPopup(v, fetchedAt) {
  const age = v.age_seconds + Math.round((Date.now() - fetchedAt) / 1000);
  return h("div", { class: "bus-popup" },
    h("p", { class: "bus-popup-route" }, `Route ${v.route}`),
    v.bearing != null ? h("p", {}, `Heading ${COMPASS[Math.round(v.bearing / 45) % 8]}`) : null,
    h("p", { class: "bus-popup-age" }, `Position from ${age < 60 ? `${age}s` : `${Math.floor(age / 60)} min`} ago`));
}

function showOnMap(stop) {
  if (!busMap || stop.lat == null) return;
  busMap.setView([stop.lat, stop.lon], 15);
  stationPin?.remove();
  stationPin = L.marker([stop.lat, stop.lon], {
    icon: L.divIcon({ className: "bus-icon", html: h("span", { class: "stop-pin" }, stop.name), iconSize: null }),
  }).addTo(busMap);
}

// --- Ask Leap -------------------------------------------------------------------

const ASK_SUGGESTIONS = ["When's my next bus?", "Train from Connolly to Clonsilla?", "Next Luas at Sandyford?", "What's my balance?"];
let askSend = null; // set once Ask Leap is available; Home uses it too

function renderAsk(slot) {
  if (slot.dataset.ready) return; // keep the conversation when health refreshes
  slot.dataset.ready = "1";
  const log = h("div", { class: "ask-log", "aria-live": "polite" });
  const input = h("input", { class: "input", type: "text", placeholder: "Ask about buses, trains or your balance", "aria-label": "Ask Leap a question", maxlength: "500" });
  const send = h("button", { class: "button", type: "submit" }, "Ask");
  const form = h("form", { class: "ask-form" }, input, send);

  askSend = async (message) => {
    message = message.trim();
    if (!message || send.disabled) return;
    input.value = "";
    send.disabled = true;
    log.append(h("p", { class: "bubble me" }, message));
    const pending = h("p", { class: "bubble bot thinking" }, "Thinking…");
    log.append(pending);
    try {
      const { reply } = await api("/api/ask", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message }),
      });
      pending.className = "bubble bot";
      pending.textContent = reply;
    } catch (err) {
      pending.className = "bubble bot error";
      pending.textContent = err.message;
    } finally {
      send.disabled = false;
      input.focus();
    }
  };
  form.addEventListener("submit", (e) => { e.preventDefault(); askSend(input.value); });

  const suggestions = h("div", { class: "suggestions", role: "group", "aria-label": "Suggested questions" },
    ...ASK_SUGGESTIONS.map((q) => h("button", { class: "suggestion", type: "button", onclick: () => askSend(q) }, q)));

  slot.replaceChildren(h("div", { class: "panel stack" },
    h("p", {}, "Live times come from your favourite stops and your balance from Leap+'s records. Leap+ computes every number; the AI only words the answer."),
    suggestions, log, form));
  renderHomeAsk();
}

function renderHomeAsk() {
  const slot = document.querySelector("[data-home-ask]");
  if (!slot) return;
  const input = h("input", { class: "input", type: "text", placeholder: "Ask Leap: when's my next bus?", "aria-label": "Ask Leap a question", maxlength: "500" });
  const form = h("form", { class: "ask-form home-ask" }, input, h("button", { class: "button", type: "submit" }, "Ask"));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const message = input.value;
    input.value = "";
    location.hash = "#ask";
    askSend?.(message);
  });
  slot.replaceChildren(form);
}

function renderFeatures(health, error) {
  for (const slot of document.querySelectorAll("[data-feature]")) {
    if (slot.dataset.ready) continue; // e.g. Ask Leap keeps its conversation
    const panel = featurePanel(slot, health, error);
    slot.replaceChildren(...(panel ? [panel] : []));
    if (!panel) FEATURE_RENDERERS[slot.dataset.feature]?.(slot);
  }
}

async function loadHealth() {
  renderFeatures(null, null);
  try {
    renderFeatures(await api("/api/health"), null);
  } catch (err) {
    renderFeatures(null, err.message);
  }
}

// --- Card: top-up and weekly auto top-up -----------------------------------------

const TEST_CARD_NOTE = "Stripe test mode: pay with card 4242 4242 4242 4242, any future date, any CVC. No real money moves.";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const dateTime = new Intl.DateTimeFormat("en-IE", { dateStyle: "medium", timeStyle: "short", timeZone: "Europe/Dublin" });
const dateOnly = new Intl.DateTimeFormat("en-IE", { dateStyle: "medium", timeZone: "Europe/Dublin" });

// "7.5" -> 750. String maths only, so no floating-point rounding ever touches money.
function parseEuroInput(text) {
  const match = /^\s*€?\s*(\d{1,6})(?:[.,](\d{1,2}))?\s*$/.exec(text);
  if (!match) return null;
  return Number(match[1]) * 100 + Number((match[2] || "0").padEnd(2, "0"));
}

async function loadCardOptions(slot) {
  slot.replaceChildren(h("div", { class: "panel", "aria-busy": "true" }, h("div", { class: "skeleton", style: "width:50%" })));
  try {
    const options = await api("/api/card");
    slot.replaceChildren(topUpPanel(options), weeklyPlanPanel(options));
  } catch (err) {
    slot.replaceChildren(errorPanel("Couldn't load top-up options", err.message, () => loadCardOptions(slot)));
  }
}

function topUpPanel({ topup }) {
  let amount = topup.presets_cents[1];
  const message = h("p", { class: "field-message", role: "status" });
  const button = h("button", { class: "button wide", type: "button" });
  const custom = h("input", {
    class: "input", type: "text", inputmode: "decimal", autocomplete: "off",
    placeholder: "Other amount, e.g. 7.50", "aria-label": "Other amount in euro",
  });

  const chips = topup.presets_cents.map((cents) =>
    h("button", { class: "chip", type: "button", onclick: () => { custom.value = ""; select(cents); } }, formatCents(cents)));

  function select(cents) {
    amount = cents;
    for (const [i, chip] of chips.entries()) chip.setAttribute("aria-pressed", String(topup.presets_cents[i] === cents));
    const valid = cents !== null && cents >= topup.min_cents && cents <= topup.max_cents;
    button.disabled = !valid;
    button.textContent = valid ? `Top up ${formatCents(cents)}` : "Top up";
    message.className = "field-message";
    message.textContent = valid || custom.value === "" ? "" :
      cents === null ? "Enter an amount like 7.50."
      : `Choose between ${formatCents(topup.min_cents)} and ${formatCents(topup.max_cents)}.`;
    if (!valid && custom.value !== "") message.className = "field-message error";
  }

  custom.addEventListener("input", () => select(custom.value === "" ? topup.presets_cents[1] : parseEuroInput(custom.value)));

  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Opening Stripe…";
    try {
      const { checkout_url } = await api("/api/topups", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount_cents: amount }),
      });
      location.assign(checkout_url);
    } catch (err) {
      select(amount);
      message.className = "field-message error";
      message.textContent = err.message;
    }
  });

  select(amount);
  return h("div", { class: "panel stack" },
    h("h2", {}, "Top up"),
    h("div", { class: "chips", role: "group", "aria-label": "Top-up amount" }, ...chips),
    custom, message, button,
    h("p", { class: "fineprint" }, TEST_CARD_NOTE));
}

function weeklyPlanPanel({ weekly_plan, subscription }) {
  const price = h("p", { class: "plan-price" }, formatCents(weekly_plan.amount_cents), h("span", {}, " / week"));
  const message = h("p", { class: "field-message", role: "status" });

  if (subscription) {
    const cancel = h("button", { class: "button secondary wide", type: "button" }, "Cancel auto top-up");
    cancel.addEventListener("click", async () => {
      if (!confirm("Cancel weekly auto top-up? Money already added stays in your balance.")) return;
      cancel.disabled = true;
      try {
        await api("/api/subscription", { method: "DELETE" });
        loadHealth();
      } catch (err) {
        cancel.disabled = false;
        message.className = "field-message error";
        message.textContent = err.message;
      }
    });
    return h("div", { class: "panel stack" },
      h("h2", {}, "Weekly auto top-up"),
      price,
      h("p", { class: "status-line" }, h("span", { class: "dot" }), `Active since ${dateOnly.format(new Date(subscription.created_at))}`),
      h("p", {}, weekly_plan.what_you_get),
      cancel, message);
  }

  const subscribe = h("button", { class: "button wide", type: "button" }, `Subscribe · ${formatCents(weekly_plan.amount_cents)}/week`);
  subscribe.addEventListener("click", async () => {
    subscribe.disabled = true;
    subscribe.textContent = "Opening Stripe…";
    try {
      const { checkout_url } = await api("/api/subscription", { method: "POST" });
      location.assign(checkout_url);
    } catch (err) {
      subscribe.disabled = false;
      subscribe.textContent = `Subscribe · ${formatCents(weekly_plan.amount_cents)}/week`;
      message.className = "field-message error";
      message.textContent = err.message;
    }
  });
  return h("div", { class: "panel stack" },
    h("h2", {}, "Weekly auto top-up"),
    price,
    h("p", {}, weekly_plan.what_you_get),
    h("p", { class: "fineprint" }, weekly_plan.why_this_price, " Source: transportforireland.ie, checked 10 Oct 2026."),
    subscribe, message,
    h("p", { class: "fineprint" }, TEST_CARD_NOTE));
}

// --- Card: transaction history ----------------------------------------------------

const TX_LABELS = { topup: "Top-up", auto_topup: "Weekly auto top-up" };

async function loadTransactions() {
  const slot = document.querySelector("[data-transactions]");
  slot.replaceChildren(h("div", { class: "panel", "aria-busy": "true" }, h("div", { class: "skeleton", style: "width:70%" })));
  try {
    const { transactions } = await api("/api/transactions");
    if (transactions.length === 0) {
      slot.replaceChildren(h("div", { class: "panel" },
        h("h2", {}, "No top-ups yet"),
        h("p", {}, "Top-ups appear here once Stripe confirms the payment.")));
      return;
    }
    slot.replaceChildren(h("ul", { class: "list" }, ...transactions.map((tx) =>
      h("li", { class: "list-row" },
        h("div", {}, h("p", { class: "row-title" }, TX_LABELS[tx.type] || tx.type),
                     h("p", { class: "row-sub" }, dateTime.format(new Date(tx.created_at)))),
        h("p", { class: "amount-in" }, `+${formatCents(tx.amount_cents)}`)))));
  } catch (err) {
    slot.replaceChildren(errorPanel("Couldn't load your history", err.message, loadTransactions));
  }
}

// --- Card: confirmation after returning from Stripe -------------------------------
// The redirect back from Stripe proves nothing about payment. We only show money as
// added once our webhook has recorded it, which this panel polls for.

function checkItem(done, text) {
  return h("li", { class: done ? "done" : "" }, h("span", { class: "tick", "aria-hidden": "true" }, done ? "✓" : "…"), text);
}

function checkoutPanel(s, timedOut) {
  const amount = formatCents(s.amount_cents);
  const done = s.status === "credited";
  const title = done ? "Payment confirmed" : timedOut ? "Still waiting for Stripe" : "Confirming with Stripe…";
  const items = s.kind === "topup"
    ? [checkItem(done, done ? `${amount} added to your balance` : `Waiting for Stripe to confirm ${amount}`)]
    : [checkItem(s.subscription_created, "Weekly auto top-up created"),
       checkItem(s.first_payment_credited, `First weekly payment received: ${amount} added`)];
  return h("div", { class: `panel stack ${done ? "success" : ""}` },
    h("h2", {}, title),
    h("ul", { class: "checklist" }, ...items),
    timedOut && h("p", {}, "Stripe hasn't confirmed this payment to Leap+ yet. Your balance updates as soon as it does. "
      + "Running locally? Check that `stripe listen` is forwarding webhooks."),
    timedOut && h("button", { class: "button secondary", type: "button", onclick: () => watchCheckout(s.session_id) }, "Check again"));
}

async function watchCheckout(sessionId) {
  const slot = document.querySelector("[data-checkout-status]");
  const started = Date.now();
  for (;;) {
    let s;
    try {
      s = { ...(await api(`/api/checkout/${encodeURIComponent(sessionId)}`)), session_id: sessionId };
    } catch (err) {
      slot.replaceChildren(errorPanel("Couldn't check your payment", err.message, () => watchCheckout(sessionId)));
      return;
    }
    const timedOut = s.status !== "credited" && Date.now() - started > 30_000;
    slot.replaceChildren(checkoutPanel(s, timedOut));
    if (s.status === "credited") {
      loadMe(); loadTransactions(); loadHealth();
      return;
    }
    if (timedOut) return;
    await sleep(1500);
  }
}

function handleCheckoutReturn() {
  const params = new URLSearchParams(location.search);
  const result = params.get("checkout");
  if (!result) return;
  history.replaceState(null, "", "/#card"); // a refresh shouldn't replay this
  showView();
  if (result === "success" && params.get("session_id")) {
    watchCheckout(params.get("session_id"));
  } else if (result === "cancelled") {
    document.querySelector("[data-checkout-status]").replaceChildren(
      h("div", { class: "panel" }, h("h2", {}, "Checkout cancelled"), h("p", {}, "You weren't charged.")));
  }
}

// --- Stops: stations list (Live) and favourites (Home) ----------------------------
// Static NTA GTFS data only. No departure times are shown until live data is built.

const KIND_LABEL = { rail: "DART", tram: "Luas", bus: "Bus" };
const stationState = { all: [], filter: "all", query: "" };

function starButton(stop) {
  const btn = h("button", {
    class: "star", type: "button", "aria-pressed": String(stop.favourite),
    "aria-label": `${stop.favourite ? "Remove" : "Add"} ${stop.name} ${stop.favourite ? "from" : "to"} favourites`,
  }, stop.favourite ? "★" : "☆");
  btn.addEventListener("click", () => toggleFavourite(stop, btn));
  return btn;
}

function stopRow(stop) {
  return h("li", { class: "list-row" },
    h("button", { class: "stop-main", type: "button", onclick: () => openStation(stop) },
      h("span", { class: `badge ${stop.kind}` }, KIND_LABEL[stop.kind]),
      h("span", { class: "row-title" }, stop.name),
      stop.kind === "bus" && stop.code ? h("span", { class: "stop-code" }, stop.code) : null,
      h("span", { class: "chevron", "aria-hidden": "true" }, "›")),
    starButton(stop));
}

// --- Live departures (NTA GTFS-Realtime + timetable) -------------------------------

function departureRow(d) {
  const when = d.status === "cancelled" ? "Cancelled" : d.minutes === 0 ? "Due" : `${d.minutes} min`;
  const note = d.status === "cancelled" ? `Was due ${d.scheduled_time}`
    : d.status === "scheduled" ? `${d.scheduled_time} · timetable`
    : d.delay_minutes > 0 ? `${d.expected_time} · ${d.delay_minutes} min late`
    : d.delay_minutes < 0 ? `${d.expected_time} · ${-d.delay_minutes} min early`
    : `${d.expected_time} · on time`;
  return h("li", { class: `dep ${d.status}` },
    h("span", { class: "route-pill" }, d.route),
    h("div", { class: "dep-main" }, h("p", { class: "row-title" }, d.headsign), h("p", { class: "row-sub" }, note)),
    h("p", { class: "dep-when" }, d.status === "live" ? h("span", { class: "live-dot", title: "Live" }) : null, when));
}

// "Updated Xs ago" keeps counting between refreshes, so the age shown is always honest.
function freshness(info) {
  if (info.status !== "live") return h("p", { class: "fresh warn", role: "status" }, info.message);
  const el = h("p", { class: "fresh live", "data-fetched": String(Date.now() - info.feed_age_seconds * 1000) });
  tickFreshness(el);
  return el;
}
function tickFreshness(el) {
  const age = Math.max(0, Math.round((Date.now() - Number(el.dataset.fetched)) / 1000));
  el.replaceChildren(h("span", { class: "pulse", "aria-hidden": "true" }), `Live · updated ${age < 60 ? `${age}s` : `${Math.floor(age / 60)} min`} ago`);
}
setInterval(() => document.querySelectorAll(".fresh.live").forEach(tickFreshness), 1000);

function departureList(d, emptyText) {
  return d.departures.length
    ? h("ul", { class: "deps" }, ...d.departures.map(departureRow))
    : h("p", { class: "empty-line" }, emptyText);
}

let stationTimer = null;
async function openStation(stop) {
  const slot = document.querySelector("[data-station-detail]");
  if (location.hash !== "#live") location.hash = "#live";
  clearInterval(stationTimer);
  showOnMap(stop);
  slot.replaceChildren(h("div", { class: "panel", "aria-busy": "true" }, h("div", { class: "skeleton big" }), h("div", { class: "skeleton" })));
  slot.scrollIntoView({ behavior: "smooth", block: "start" });
  const load = async () => {
    try {
      const d = await api(`/api/stops/${encodeURIComponent(stop.stop_id)}/departures?limit=8`);
      const close = h("button", { class: "icon-btn", type: "button", "aria-label": "Close station" }, "×");
      close.addEventListener("click", () => { clearInterval(stationTimer); slot.replaceChildren(); });
      slot.replaceChildren(h("div", { class: "panel station-card" },
        h("div", { class: "station-head" },
          h("span", { class: `badge ${stop.kind}` }, KIND_LABEL[stop.kind]),
          h("h2", {}, stop.name), starButton(stop), close),
        freshness(d.realtime),
        departureList(d, "No departures in the next 2 hours.")));
    } catch (err) {
      slot.replaceChildren(errorPanel(`Couldn't load times for ${stop.name}`, err.message, () => openStation(stop)));
    }
  };
  await load();
  stationTimer = setInterval(load, 30_000);
}

async function toggleFavourite(stop, btn) {
  btn.disabled = true;
  try {
    await api(`/api/favourites/${encodeURIComponent(stop.stop_id)}`, { method: stop.favourite ? "DELETE" : "PUT" });
    const now = !stop.favourite;
    for (const s of stationState.all) if (s.stop_id === stop.stop_id) s.favourite = now;
    stop.favourite = now;
    renderStations();
    loadFavourites();
  } catch (err) {
    btn.disabled = false;
    btn.title = err.message;
  }
}

function renderStations() {
  const slot = document.querySelector("[data-stations] .station-list");
  if (!slot) return;
  const q = stationState.query.trim().toLowerCase();
  const matches = stationState.all.filter((s) =>
    (stationState.filter === "all" || s.kind === stationState.filter)
    && (!q || s.name.toLowerCase().includes(q) || (s.code && s.code.startsWith(q))));
  const MAX_ROWS = 60; // 2,500 bus stops: render a page, let search do the rest
  const shown = matches.slice(0, MAX_ROWS);
  slot.replaceChildren(...(shown.length
    ? [h("ul", { class: "list" }, ...shown.map(stopRow)),
       matches.length > MAX_ROWS ? h("p", { class: "source-note" }, `Showing ${MAX_ROWS} of ${matches.length}. Search by name or stop number to find yours.`) : null]
    : [h("div", { class: "panel" }, h("h2", {}, "No stops match"), h("p", {}, `Nothing matches "${stationState.query}". Try a name like UCD or a stop number like 768.`))]
  ).filter(Boolean));
}

async function loadStations() {
  const slot = document.querySelector("[data-stations]");
  slot.replaceChildren(h("div", { class: "panel", "aria-busy": "true" }, h("div", { class: "skeleton", style: "width:60%" })));
  try {
    stationState.all = (await api("/api/stations")).stations;
  } catch (err) {
    slot.replaceChildren(errorPanel("Couldn't load stations", err.message, loadStations));
    return;
  }
  const search = h("input", { class: "input search", type: "search", placeholder: "Name or stop number, e.g. 768", "aria-label": "Search stops" });
  search.addEventListener("input", () => { stationState.query = search.value; renderStations(); });
  const counts = { all: stationState.all.length, bus: 0, rail: 0, tram: 0 };
  for (const s of stationState.all) counts[s.kind]++;
  const tabs = [["all", "All"], ["bus", "Bus"], ["rail", "DART"], ["tram", "Luas"]].map(([key, label]) => {
    const b = h("button", { class: "chip", type: "button", "aria-pressed": String(stationState.filter === key) }, `${label} (${counts[key]})`);
    b.addEventListener("click", () => {
      stationState.filter = key;
      for (const t of tabs) t.setAttribute("aria-pressed", String(t === b));
      renderStations();
    });
    return b;
  });
  slot.replaceChildren(
    h("div", { class: "chips", role: "group", "aria-label": "Filter stations" }, ...tabs),
    search,
    h("div", { class: "station-list" }),
    h("p", { class: "source-note" }, "Live times: NTA GTFS-Realtime + timetable (feed of 9 Oct 2026). Buses: south Dublin stops."));
  renderStations();
}

async function loadFavourites() {
  const slot = document.querySelector("[data-favourites]");
  try {
    const { favourites } = await api("/api/favourites");
    if (!favourites.length) {
      slot.replaceChildren(h("div", { class: "panel" },
        h("h2", {}, "No favourite stops yet"),
        h("p", {}, "Open the Live tab and tap ☆ next to a station to pin it here."),
        h("a", { class: "button secondary", href: "#live" }, "Browse stations")));
      return;
    }
    const cards = await Promise.all(favourites.map(async (stop) => {
      let body;
      try {
        const d = await api(`/api/stops/${encodeURIComponent(stop.stop_id)}/departures?limit=3`);
        body = [freshness(d.realtime), departureList(d, "No departures in the next 2 hours.")];
      } catch (err) {
        body = [h("p", { class: "empty-line" }, err.message)];
      }
      return h("div", { class: "panel fav-card" },
        h("div", { class: "station-head" },
          h("span", { class: `badge ${stop.kind}` }, KIND_LABEL[stop.kind]),
          h("button", { class: "link-title", type: "button", onclick: () => openStation(stop) }, stop.name),
          starButton(stop)),
        ...body);
    }));
    slot.replaceChildren(h("div", { class: "fav-grid" }, ...cards));
  } catch (err) {
    slot.replaceChildren(errorPanel("Couldn't load favourites", err.message, loadFavourites));
  }
}
setInterval(() => { if (currentView() === "home") loadFavourites(); }, 30_000);

// --- Start -------------------------------------------------------------------

window.addEventListener("hashchange", showView);
showView();
handleCheckoutReturn();
loadHealth();
loadMe();
loadTransactions();
loadFavourites();
loadStations();
