// Owner app: login gate, module switching, and the four data modules.
//
// The Talk module reuses widget.js unchanged -- the conversation logic is
// identical to the visitor widget, and forking it to change the chrome would
// mean maintaining barge-in twice.

import { startWidget } from "./widget.js";
import { registerServiceWorker, enablePush, sendTestPush, pushStatus } from "./pwa.js";

const API = "https://assistant-ai.duckdns.org";
const SESSION_KEY = "assistant.session";
const EMAIL_KEY = "assistant.email";
const POLL_MS = 20000;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let token = "";
let items = [];
let filters = { status: "new", mode: "" };
let poll = null;
let widgetStarted = false;

const VIEWS = {
  today: "Today",
  talk: "Talk",
  inbox: "Inbox",
  reminders: "Reminders",
  settings: "Settings",
};

// ---------------------------------------------------------------- helpers

function store(key, value) {
  try { sessionStorage.setItem(key, value); } catch { /* private mode */ }
}
function read(key) {
  try { return sessionStorage.getItem(key) || ""; } catch { return ""; }
}

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    ...options,
    headers: { ...(options.headers || {}), Authorization: `Bearer ${token}` },
  });
  if (res.status === 401) { signOut(); throw new Error("session expired"); }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function ago(iso) {
  const mins = Math.round((Date.now() - new Date(iso)) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  return new Date(iso).toLocaleDateString();
}

function dueTag(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const label = d.toLocaleString(undefined,
    { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const mins = Math.round((d - Date.now()) / 60000);
  if (mins < 0) return `<span class="tag high">overdue · ${esc(label)}</span>`;
  if (mins < 1440) return `<span class="tag medium">${esc(label)}</span>`;
  return `<span class="tag">${esc(label)}</span>`;
}

// ---------------------------------------------------------------- routing

function show(name) {
  for (const [key] of Object.entries(VIEWS)) {
    $(`view-${key}`).classList.toggle("active", key === name);
  }
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.view === name));
  }
  $("viewName").textContent = VIEWS[name];
  // The composer belongs to Talk alone, so the tab bar sits flush elsewhere.
  $("form").hidden = name !== "talk";
  $("dot").hidden = name !== "talk";
  $("state").textContent = name === "talk" ? $("state").textContent : "";
  $("views").scrollTop = 0;

  if (name === "talk" && !widgetStarted) startTalk();
  if (name === "today") renderToday();
  if (name === "inbox") renderInbox();
  if (name === "reminders") renderReminders();
  if (name === "settings") renderSettings();
}

// ------------------------------------------------------------------ Talk

function startTalk() {
  widgetStarted = true;
  startWidget({
    wsUrl: "wss://assistant-ai.duckdns.org/ws/chat",
    sessionToken: token,
    tokenKey: "assistant.resume.owner",
    el: {
      log: $("log"), input: $("input"), send: $("send"), form: $("form"),
      dot: $("dot"), state: $("state"), mic: $("mic"), mute: $("mute"),
    },
  });
}

// ----------------------------------------------------------------- Today

function renderToday() {
  const hour = new Date().getHours();
  $("greeting").textContent =
    hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  $("todayDate").textContent =
    new Date().toLocaleDateString(undefined,
      { weekday: "long", day: "numeric", month: "long" });

  const due = items.filter((i) => i.due_at && new Date(i.due_at) <= Date.now() && i.status !== "done");
  const fresh = items.filter((i) => i.status === "new");
  const parts = [];

  if (due.length) {
    parts.push(`<div class="section-label">Due now <span class="count">${due.length}</span></div>`);
    parts.push(due.slice(0, 4).map((i) => card(i, "clock")).join(""));
  }
  if (fresh.length) {
    parts.push(`<div class="section-label">Needs attention <span class="count">${fresh.length}</span></div>`);
    parts.push(fresh.slice(0, 5).map((i) => card(i, i.mode === "owner" ? "note" : "inbox")).join(""));
  }
  if (!parts.length) {
    parts.push('<div class="empty">Nothing needs you right now.</div>');
  }
  $("todayBody").innerHTML = parts.join("");
  for (const el of $("todayBody").querySelectorAll("[data-open]")) {
    el.addEventListener("click", () => show("inbox"));
  }
}

const ICONS = {
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  note: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
};

function card(item, icon) {
  return `
    <button class="card" data-open="${esc(item.id)}">
      <span class="chip-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[icon]}</svg></span>
      <span class="body"><b>${esc(item.title)}</b><span>${
        item.due_at ? esc(new Date(item.due_at).toLocaleString(undefined,
          { weekday: "short", hour: "2-digit", minute: "2-digit" })) : esc(ago(item.created_at))
      } · ${esc(item.type)}</span></span>
      <span class="trail"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round"><path d="m9 18 6-6-6-6"/></svg></span>
    </button>`;
}

// ----------------------------------------------------------------- Inbox

function itemHtml(it) {
  const contact = Object.entries(it.contact || {})
    .map(([k, v]) => `<span><i>${esc(k)}</i> <b>${esc(v)}</b></span>`).join("");
  const todo = (it.action_items || []).map((a) => `<li>${esc(a)}</li>`).join("");
  return `
    <div class="item" data-id="${esc(it.id)}">
      <h2>${esc(it.title)}</h2>
      <div class="meta">
        <span class="tag ${it.mode === "owner" ? "owner" : ""}">${esc(it.type)}</span>
        ${it.urgency && it.urgency !== "low" ? `<span class="tag ${esc(it.urgency)}">${esc(it.urgency)}</span>` : ""}
        ${dueTag(it.due_at)}
        ${it.degraded ? '<span class="tag medium">no model</span>' : ""}
        <span>${esc(it.channel)}</span><span>${esc(ago(it.created_at))}</span>
        <span>${esc(it.status)}</span>
      </div>
      <p>${esc(it.summary)}</p>
      ${contact ? `<div class="contact">${contact}</div>` : ""}
      ${it.requested_slot ? `<div class="contact"><span><i>asked for</i> <b>${esc(it.requested_slot)}</b></span></div>` : ""}
      ${todo ? `<ul class="todo">${todo}</ul>` : ""}
      <details class="tr"><summary>Transcript</summary><div class="body">loading…</div></details>
      <div class="actions">
        ${it.status !== "triaged" ? `<button class="pill" data-set="triaged">Triaged</button>` : ""}
        ${it.status !== "done" ? `<button class="pill" data-set="done">Done</button>` : ""}
      </div>
    </div>`;
}

function wireItems(root) {
  for (const el of root.querySelectorAll(".item")) {
    const id = el.dataset.id;
    for (const b of el.querySelectorAll("[data-set]")) {
      b.addEventListener("click", async () => {
        b.disabled = true;
        await api(`/api/items/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: b.dataset.set }),
        });
        await refresh();
      });
    }
    // Fetched on expand: most items are actioned from the summary alone, and
    // the transcript is by far the largest field.
    const det = el.querySelector("details");
    det.addEventListener("toggle", async () => {
      const body = det.querySelector(".body");
      if (!det.open || body.dataset.loaded) return;
      body.dataset.loaded = "1";
      try {
        const { turns } = await api(`/api/items/${id}/transcript`);
        body.innerHTML = turns.map((t) =>
          `<div class="line"><b>${t.role === "customer" ? "Them" : "Assistant"}:</b> ${esc(t.text)}${
            t.cancelled ? " <i>(cut off)</i>" : ""}</div>`).join("")
          || '<div class="line">Transcript deleted or never recorded.</div>';
      } catch {
        body.textContent = "Could not load the transcript.";
      }
    });
  }
}

function renderInbox() {
  const shown = items.filter((i) =>
    (!filters.status || i.status === filters.status) &&
    (!filters.mode || i.mode === filters.mode));
  $("inboxBody").innerHTML = shown.length
    ? shown.map(itemHtml).join("")
    : '<div class="empty">Nothing here.</div>';
  wireItems($("inboxBody"));
}

// ------------------------------------------------------------- Reminders

function renderReminders() {
  const withDue = items
    .filter((i) => i.due_at && i.status !== "done")
    .sort((a, b) => new Date(a.due_at) - new Date(b.due_at));
  $("remindersBody").innerHTML = withDue.length
    ? withDue.map(itemHtml).join("")
    : '<div class="empty">No reminders set.<br>Ask the assistant to remind you about something.</div>';
  wireItems($("remindersBody"));
}

// -------------------------------------------------------------- Settings

async function renderSettings() {
  $("acctEmail").textContent = read(EMAIL_KEY) || "signed in";

  const state = await pushStatus();
  const label = {
    on: ["Notifications are on", "Reminders will reach this device."],
    off: ["Turn on notifications", "Reminders will only appear here otherwise."],
    blocked: ["Notifications are blocked", "Allow them in your browser settings."],
    unsupported: ["Add to Home Screen first", "On iPhone, that is what lets alerts through."],
  }[state];

  $("pushBody").innerHTML = `
    <div class="card static">
      <span class="chip-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
        <path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg></span>
      <div class="body"><b>${esc(label[0])}</b><span>${esc(label[1])}</span></div>
    </div>
    <div class="pillrow">
      ${state === "off" ? '<button class="pill" id="pushOn">Enable</button>' : ""}
      ${state === "on" ? '<button class="pill" id="pushTest">Send a test</button>' : ""}
    </div>`;

  $("pushOn")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    const { ok, reason } = await enablePush(token);
    if (!ok) alert(reason);
    renderSettings();
  });
  $("pushTest")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    const { ok, reason } = await sendTestPush(token);
    alert(ok ? reason : `Test failed: ${reason}`);
    e.target.disabled = false;
  });

  try {
    const h = await (await fetch(`${API}/health`)).json();
    $("svcState").textContent = "Service is up";
    $("svcDetail").textContent = `v${h.version} · ${h.active_sessions} active`;
  } catch {
    $("svcState").textContent = "Service unreachable";
    $("svcDetail").textContent = "The assistant cannot be contacted.";
  }
}

// ------------------------------------------------------------------ data

async function refresh() {
  try {
    // Everything, filtered client-side: the whole set is small and it makes
    // Today, Inbox and Reminders consistent without three round trips.
    const data = await api("/api/items?status=");
    items = data.items;
    const dueNow = items.filter((i) => i.due_at && new Date(i.due_at) <= Date.now() && i.status !== "done").length;
    const fresh = items.filter((i) => i.status === "new").length;
    badge("inboxBadge", fresh);
    badge("dueBadge", dueNow);
    $("appSub").textContent = fresh ? `${fresh} waiting` : "at your service";

    const active = document.querySelector(".tab[aria-selected='true']").dataset.view;
    if (active === "today") renderToday();
    if (active === "inbox") renderInbox();
    if (active === "reminders") renderReminders();
  } catch (e) {
    if (e.message !== "session expired") console.warn(e);
  }
}

function badge(id, n) {
  const el = $(id);
  el.hidden = n === 0;
  el.textContent = n > 9 ? "9+" : String(n);
}

// ------------------------------------------------------------------ auth

function signOut() {
  try {
    sessionStorage.removeItem(SESSION_KEY);
    sessionStorage.removeItem(EMAIL_KEY);
  } catch { /* ignore */ }
  clearInterval(poll);
  token = "";
  $("shell").hidden = true;
  $("loginView").hidden = false;
  // Reload rather than tearing down: the widget holds a socket, a mic stream
  // and timers, and unwinding all of that by hand is more error-prone than
  // starting clean.
  location.reload();
}

function launch(t) {
  token = t;
  $("loginView").hidden = true;
  $("shell").hidden = false;
  show("today");
  refresh();
  poll = setInterval(refresh, POLL_MS);
}

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("loginErr");
  err.textContent = "";
  $("loginBtn").disabled = true;
  try {
    const res = await fetch(`${API}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("email").value, password: $("password").value }),
    });
    if (!res.ok) {
      // The server answers unknown email and wrong password identically, so
      // there is nothing more specific to say here either.
      err.textContent = res.status === 429
        ? "Too many attempts. Wait a minute."
        : "Those details were not accepted.";
      return;
    }
    const { token: t } = await res.json();
    store(SESSION_KEY, t);
    store(EMAIL_KEY, $("email").value);
    launch(t);
  } catch {
    err.textContent = "Could not reach the server.";
  } finally {
    $("loginBtn").disabled = false;
  }
});

// ----------------------------------------------------------------- wiring

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => show(tab.dataset.view));
}
for (const el of document.querySelectorAll("[data-goto]")) {
  el.addEventListener("click", () => show(el.dataset.goto));
}
$("tabs").addEventListener("click", () => {}, { passive: true });

document.querySelector("#view-inbox .pillrow").addEventListener("click", (e) => {
  const pill = e.target.closest(".pill");
  if (!pill) return;
  filters[pill.dataset.filter] = pill.dataset.value;
  for (const p of document.querySelectorAll(`#view-inbox [data-filter="${pill.dataset.filter}"]`)) {
    p.setAttribute("aria-pressed", String(p === pill));
  }
  renderInbox();
});

$("refreshBtn").addEventListener("click", refresh);
$("signOutBtn").addEventListener("click", signOut);
$("settingsSignOut").addEventListener("click", signOut);

registerServiceWorker();

const existing = read(SESSION_KEY);
if (existing) launch(existing);
