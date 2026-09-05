// Owner app: login gate, module switching, and the four data modules.
//
// The Talk module reuses widget.js unchanged -- the conversation logic is
// identical to the visitor widget, and forking it to change the chrome would
// mean maintaining barge-in twice.

import { startWidget } from "./widget.js";
import { registerServiceWorker, enablePush, sendTestPush, pushStatus } from "./pwa.js";
import {
  loadSnapshot, saveSnapshot, forgetSnapshot, enqueue, flush, pending, newId,
} from "./sync.js";

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
  planner: "Planner",
  settings: "Settings",
};

// ---------------------------------------------------------------- helpers

// localStorage, not sessionStorage: an installed app is closed and reopened
// constantly, and sessionStorage dies with the tab. The trade is that the
// token now persists on the device -- which is the right one here, because it
// grants reading and status changes on one person's own assistant, not
// password changes and not deletion. Sign out clears it.
function store(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
}
function read(key) {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}
function forget(key) {
  try { localStorage.removeItem(key); } catch { /* ignore */ }
}

// Two kinds of failure, and the difference decides everything downstream: a
// request that never left the device is retried when the connection returns,
// one the server answered and refused is not.
async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(API + path, {
      ...options,
      headers: { ...(options.headers || {}), Authorization: `Bearer ${token}` },
    });
  } catch {
    const err = new Error("offline");
    err.network = true;
    throw err;
  }
  if (res.status === 401) { sessionExpired(); throw new Error("session expired"); }
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`);
    // 4xx is the server saying no. Replaying it forever would block every
    // write queued behind it, so the queue drops it instead.
    err.rejected = res.status < 500;
    throw err;
  }
  // 204 and friends have no body to read.
  return res.status === 204 ? {} : res.json();
}

// ------------------------------------------------------------------- writes
//
// Every change goes through here: applied on the device, queued, then sent.
// That order is the whole trick -- the app behaves identically with or without
// a connection, and the network is something that happens afterwards.

let offline = false;
let flushPromise = null;
let syncNote = "";

function mutate(op, applyLocally) {
  applyLocally();
  if (!enqueue(op)) {
    // Storage is full or blocked. Send it directly and say so if that fails,
    // rather than pretending it was saved.
    api(op.path, requestOf(op)).catch(() => {
      alert("That change could not be saved. Check your connection and try again.");
    });
  }
  renderActive();
  updateSyncState();
  flushOutbox();
}

function requestOf(op) {
  return {
    method: op.method,
    headers: op.body ? { "Content-Type": "application/json" } : undefined,
    body: op.body ? JSON.stringify(op.body) : undefined,
  };
}

// One run at a time, and anyone who asks for a flush while one is going gets
// that run's promise. Two concurrent drains of the same ordered queue would
// race to send the same operation twice.
function flushOutbox() {
  if (!token || !pending()) return Promise.resolve();
  if (flushPromise) return flushPromise;
  flushPromise = (async () => {
    try {
      const result = await flush((op) => api(op.path, requestOf(op)));
      offline = result.stalled;
      if (result.dropped.length) {
        syncNote = `${result.dropped.length} change${
          result.dropped.length === 1 ? " was" : "s were"} refused`;
        console.warn("sync dropped", result.dropped);
      }
    } catch (e) {
      if (e.message !== "session expired") console.warn(e);
    } finally {
      flushPromise = null;
      updateSyncState();
    }
  })();
  return flushPromise;
}

function renderActive() {
  const active = document.querySelector(".tab[aria-selected='true']");
  if (!active) return;
  if (active.dataset.view === "today") renderToday();
  if (active.dataset.view === "inbox") renderInbox();
  if (active.dataset.view === "planner") renderPlanner();
}

function updateSyncState() {
  const queued = pending();
  const fresh = items.filter((i) => i.status === "new").length;
  $("appSub").textContent =
    syncNote ? syncNote
    : queued ? `${queued} change${queued === 1 ? "" : "s"} to send`
    : offline ? "offline"
    : fresh ? `${fresh} waiting`
    : "at your service";
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
  if (name === "planner") renderPlanner();
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

  const today = dayKey(new Date());
  const overdueItems = items.filter(
    (i) => i.due_at && new Date(i.due_at) <= Date.now() && i.status !== "done");
  const overdueTasks = tasks.filter(
    (t) => !t.done_at && t.due_at && new Date(t.due_at) <= Date.now());
  const todayEvents = events.filter((e) => eventOn(e, today));
  const todayTasks = tasks.filter(
    (t) => !t.done_at && t.due_at && dayKey(new Date(t.due_at)) === today
           && new Date(t.due_at) > Date.now());
  const fresh = items.filter((i) => i.status === "new");

  const parts = [];
  const label = (text, n) =>
    `<div class="section-label">${text}<span class="count">${n}</span></div>`;

  if (overdueItems.length || overdueTasks.length) {
    parts.push(label("Due now", overdueItems.length + overdueTasks.length));
    parts.push(`<div class="row-list">${
      overdueTasks.slice(0, 5).map(taskRow).join("")}</div>`);
    parts.push(overdueItems.slice(0, 4).map((i) => card(i, "clock")).join(""));
  }
  if (todayEvents.length || todayTasks.length) {
    parts.push(label("Today", todayEvents.length + todayTasks.length));
    parts.push(`<div class="row-list">${
      todayEvents.map((e) => `
        <button type="button" class="trow" data-goto="planner">
          <span class="ico">\u{1F551}</span>
          <span class="bd"><span class="t">${esc(e.title)}</span>
            <span class="s">${esc(hhmm(e.starts_at))}${
              e.location ? ` · ${esc(e.location)}` : ""}</span></span>
        </button>`).join("")
      + todayTasks.map(taskRow).join("")}</div>`);
  }
  if (fresh.length) {
    parts.push(label("Needs attention", fresh.length));
    parts.push(fresh.slice(0, 5).map((i) =>
      card(i, i.mode === "owner" ? "note" : "inbox")).join(""));
  }
  if (!parts.length) {
    parts.push('<div class="empty">Nothing needs you right now.</div>');
  }
  $("todayBody").innerHTML = parts.join("");

  // Ticking a task off from Today, without making it a second task module.
  for (const el of $("todayBody").querySelectorAll("[data-toggle]")) {
    el.addEventListener("click", async () => {
      el.disabled = true;
      const task = tasks.find((t) => t.id === el.dataset.toggle);
      if (task) await toggleTask(task);
    });
  }
  for (const el of $("todayBody").querySelectorAll("[data-open]")) {
    el.addEventListener("click", () => show("inbox"));
  }
  for (const el of $("todayBody").querySelectorAll("[data-task]")) {
    el.addEventListener("click", () => {
      const task = tasks.find((t) => t.id === el.dataset.task);
      if (task) { show("planner"); openTask(task); }
    });
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
//
// A list of rows, and the whole message behind whichever one is tapped. It
// used to render every field of every item inline, which meant scrolling past
// four transcripts to reach the fifth message.

const STATUSES_ITEM = [["new", "New"], ["triaged", "Triaged"], ["done", "Done"]];

function contactName(it) {
  const c = it.contact || {};
  return c.name || c.company || c.phone || c.email || "";
}

function itemRow(it) {
  const who = contactName(it);
  const bits = [who, ago(it.created_at), it.type].filter(Boolean);
  const flags = [];
  if (it.urgency === "high") flags.push('<span class="tag high">urgent</span>');
  if (it.due_at && new Date(it.due_at) <= Date.now() && it.status !== "done") {
    flags.push('<span class="tag high">overdue</span>');
  }
  if ((it.lang || "").startsWith("ar")) flags.push('<span class="tag medium">عربي</span>');
  if (it.status === "done") flags.push('<span class="tag ok">done</span>');

  return `
    <button type="button" class="trow" data-item-open="${esc(it.id)}">
      <span class="ico">${it.mode === "owner" ? "\u{1F4DD}" : "\u{1F4E5}"}</span>
      <span class="bd">
        <span class="t">${esc(it.title)}</span>
        <span class="s">${esc(bits.join(" · "))}</span>
      </span>
      ${flags.length ? `<span class="flags">${flags.join("")}</span>` : ""}
    </button>`;
}

function renderInbox() {
  const shown = items.filter((i) =>
    (!filters.status || i.status === filters.status) &&
    (!filters.mode || i.mode === filters.mode));
  $("inboxBody").innerHTML = shown.length
    ? `<div class="row-list">${shown.map(itemRow).join("")}</div>`
    : '<div class="empty">Nothing here.</div>';
}

$("inboxBody").addEventListener("click", (e) => {
  const row = e.target.closest("[data-item-open]");
  if (!row) return;
  const item = items.find((i) => i.id === row.dataset.itemOpen);
  if (item) openItem(item);
});

function openItem(it) {
  const contact = Object.entries(it.contact || {})
    .map(([k, v]) => `<span><i>${esc(k)}</i> <b>${esc(v)}</b></span>`).join("");
  const todo = (it.action_items || []).map((a) => `<li>${esc(a)}</li>`).join("");
  const meta = [
    `<span class="tag ${it.mode === "owner" ? "owner" : ""}">${esc(it.type)}</span>`,
    it.urgency && it.urgency !== "low"
      ? `<span class="tag ${esc(it.urgency)}">${esc(it.urgency)}</span>` : "",
    dueTag(it.due_at),
    it.degraded ? '<span class="tag medium">no model</span>' : "",
    (it.lang || "").startsWith("ar")
      ? '<span class="tag medium" title="Arabic call - speech recognition mangles mixed-in English, so check the transcript">عربي · check transcript</span>'
      : "",
    `<span class="tag">${esc(it.channel)}</span>`,
    `<span class="tag">${esc(ago(it.created_at))}</span>`,
  ].filter(Boolean).join("");

  openSheet(it.title, `
    <div class="meta" style="margin-bottom:var(--sp-3)">${meta}</div>
    <p class="note" style="color:var(--text-dim);font-size:var(--fs-footnote)">${esc(it.summary)}</p>
    ${contact ? `<div class="contact">${contact}</div>` : ""}
    ${it.requested_slot
      ? `<div class="contact"><span><i>asked for</i> <b>${esc(it.requested_slot)}</b></span></div>` : ""}
    ${todo ? `<ul class="todo">${todo}</ul>` : ""}
    <label>Status</label>
    ${chips("status", STATUSES_ITEM, it.status)}
    <label for="f-project">Project</label>
    ${projectOptions(it.project_id || "")}
    <label>Transcript</label>
    <div class="transcript" id="itemTranscript">loading…</div>`,
    async () => {
      const status = chipValue("status");
      if (status && status !== it.status) {
        mutate({ method: "PATCH", path: `/api/items/${it.id}`, body: { status } },
               () => { it.status = status; });
      }
      const project = $("f-project").value || null;
      if (project !== (it.project_id || null)) {
        mutate({ method: "PUT", path: `/api/items/${it.id}/project`,
                 body: { project_id: project } },
               () => { it.project_id = project; });
      }
    },
    { danger: "Delete" });

  // Deleting a message takes its transcript with it and cannot be undone, so
  // it asks -- and says what it is about to lose rather than "are you sure?".
  $("sheetForm").querySelector("[data-danger]").addEventListener("click", async () => {
    const who = contactName(it);
    if (!confirm(`Delete this message${who ? ` from ${who}` : ""}?\n\n` +
                 "The transcript goes with it. This cannot be undone.")) return;
    mutate({ method: "DELETE", path: `/api/items/${it.id}` },
           () => { items = items.filter((x) => x.id !== it.id); });
    closeSheet();
  });

  // Fetched on open rather than with the list: it is by far the largest field
  // and most messages are dealt with from the summary alone.
  api(`/api/items/${it.id}/transcript`).then(({ turns }) => {
    const box = $("itemTranscript");
    if (!box) return;
    box.innerHTML = turns.map((t) =>
      `<div class="line"><b>${t.role === "customer" ? "Them" : "Assistant"}:</b> ${
        esc(t.text)}${t.cancelled ? " <i>(cut off)</i>" : ""}</div>`).join("")
      || '<div class="line">Transcript deleted or never recorded.</div>';
  }).catch(() => {
    const box = $("itemTranscript");
    if (box) box.textContent = "Could not load the transcript.";
  });
}

// --------------------------------------------------------------- Planner
//
// Calendar, tasks and projects, behind one tab. They are three views of the
// same week, and giving each its own tab would have made seven of them.
//
// Everything is rendered from the arrays `refresh()` already holds, so
// switching panes or months costs nothing and the three views can never
// disagree about what is due.

let events = [];
let tasks = [];
let projects = [];

let pane = "calendar";
let calMonth = monthOf(new Date());
let selDay = dayKey(new Date());
let showDone = false;
let taskProject = "";

// ------------------------------------------------------------ date helpers
//
// All local, deliberately. toISOString() answers in UTC, so using it to work
// out "which day is this on" puts anything after 21:00 Beirut time on
// tomorrow's square.

function dayKey(d) {
  const x = new Date(d);
  return `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${
    String(x.getDate()).padStart(2, "0")}`;
}

function monthOf(d) {
  return dayKey(d).slice(0, 7);
}

function fromKey(key) {
  const [y, m, d] = key.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function addDays(key, n) {
  const d = fromKey(key);
  d.setDate(d.getDate() + n);
  return dayKey(d);
}

// Monday, because that is what the column headers say.
function weekStart(key) {
  const d = fromKey(key);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return dayKey(d);
}

function dayName(key) {
  const today = dayKey(new Date());
  if (key === today) return "Today";
  if (key === addDays(today, 1)) return "Tomorrow";
  if (key === addDays(today, -1)) return "Yesterday";
  return fromKey(key).toLocaleDateString(undefined,
    { weekday: "long", day: "numeric", month: "long" });
}

function hhmm(iso) {
  return new Date(iso).toLocaleTimeString(undefined,
    { hour: "2-digit", minute: "2-digit" });
}

// A date input gives back a bare day. All-day things are stored at 09:00
// local -- the same hour the summariser assumes when someone names a day and
// no time -- so a reminder does not fire at midnight.
function toIso(dateStr, timeStr) {
  if (!dateStr) return null;
  const [y, m, d] = dateStr.split("-").map(Number);
  const [hh, mm] = (timeStr || "09:00").split(":").map(Number);
  return new Date(y, m - 1, d, hh || 0, mm || 0).toISOString();
}

// ---------------------------------------------------------------- the sheet

let sheetSubmit = null;

function openSheet(title, bodyHtml, onSubmit, { danger = "" } = {}) {
  sheetSubmit = onSubmit;
  $("sheetForm").innerHTML = `
    <h2>${esc(title)}</h2>
    ${bodyHtml}
    <div class="foot">
      <button type="button" class="pill" data-close>Cancel</button>
      ${danger ? `<button type="button" class="pill danger" data-danger>${esc(danger)}</button>` : ""}
      <button type="submit" class="btn-primary" style="flex:1">Save</button>
    </div>`;
  $("sheet").showModal();
  const first = $("sheetForm").querySelector("input, textarea");
  // Not on a touch keyboard: focusing an input pops it up over the sheet
  // before the user has seen what they are filling in.
  if (first && window.matchMedia("(pointer: fine)").matches) first.focus();
}

function closeSheet() {
  sheetSubmit = null;
  $("sheet").close();
}

// A chip group whose selection lives in the DOM, so the form has no state of
// its own to keep in sync. `swatch` prefixes each chip with a dot in the
// colour it names -- a colour picker that only shows words is not one.
function chips(name, options, current, { swatch = false } = {}) {
  return `<div class="chips" data-chips="${esc(name)}">${
    options.map(([value, label]) =>
      `<button type="button" data-value="${esc(value)}" aria-pressed="${
        String(value === current)}" class="${swatch ? `swatch-${esc(value)}` : ""}">${
        swatch ? '<span class="swatch"></span>' : ""}${esc(label)}</button>`).join("")}</div>`;
}

function chipValue(name) {
  const el = $("sheetForm").querySelector(
    `[data-chips="${name}"] [aria-pressed="true"]`);
  return el ? el.dataset.value : "";
}

$("sheetForm").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-chips] button");
  if (chip) {
    for (const b of chip.parentElement.children) {
      b.setAttribute("aria-pressed", String(b === chip));
    }
    return;
  }
  if (e.target.closest("[data-close]")) closeSheet();
});

$("sheetForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fn = sheetSubmit;
  if (!fn) return;
  const btn = $("sheetForm").querySelector("[type=submit]");
  btn.disabled = true;
  try {
    await fn();
    closeSheet();
    await refresh();
  } catch (err) {
    btn.disabled = false;
    alert(`Could not save: ${err.message}`);
  }
});

$("sheet").addEventListener("close", () => { sheetSubmit = null; });

// ----------------------------------------------------------- task editing

const PRIORITIES = [["low", "Low"], ["med", "Medium"], ["high", "High"]];
const REPEATS = [["0", "Never"], ["1", "Daily"], ["7", "Weekly"],
                 ["14", "Fortnightly"], ["30", "Monthly"]];

function projectOptions(selected) {
  return `<select id="f-project">
    <option value="">No project</option>
    ${projects.filter((p) => p.status !== "done" || p.id === selected)
      .map((p) => `<option value="${esc(p.id)}"${p.id === selected ? " selected" : ""}>${
        esc(p.emoji)} ${esc(p.name)}</option>`).join("")}
  </select>`;
}

function openTask(task, dayHint) {
  const due = task && task.due_at ? new Date(task.due_at) : null;
  const dateVal = due ? dayKey(due) : (dayHint || "");
  const timeVal = due && !task.all_day
    ? `${String(due.getHours()).padStart(2, "0")}:${String(due.getMinutes()).padStart(2, "0")}`
    : "";
  openSheet(task ? "Edit task" : "New task", `
    <label for="f-title">What needs doing?</label>
    <input id="f-title" maxlength="200" placeholder="e.g. Ring Rami back"
           value="${esc(task ? task.title : "")}" required>
    <label>Priority</label>
    ${chips("priority", PRIORITIES, task ? task.priority : "med")}
    <div class="two">
      <div><label for="f-date">Due</label>
        <input id="f-date" type="date" value="${esc(dateVal)}"></div>
      <div><label for="f-time">Time</label>
        <input id="f-time" type="time" value="${esc(timeVal)}"></div>
    </div>
    <label>Repeat</label>
    ${chips("repeat", REPEATS, String(task ? task.repeat_days : 0))}
    <label for="f-project">Project</label>
    ${projectOptions(task ? task.project_id : (taskProject || ""))}
    <label for="f-notes">Notes</label>
    <textarea id="f-notes" maxlength="2000" placeholder="Optional">${
      esc(task && task.notes ? task.notes : "")}</textarea>`,
    async () => {
      const date = $("f-date").value;
      const time = $("f-time").value;
      const repeat = Number(chipValue("repeat") || 0);
      if (repeat && !date) throw new Error("a repeating task needs a due date");
      const payload = {
        title: $("f-title").value.trim(),
        notes: $("f-notes").value.trim() || null,
        priority: chipValue("priority") || "med",
        due_at: toIso(date, time),
        all_day: !time,
        repeat_days: repeat,
        project_id: $("f-project").value || null,
      };
      if (!payload.title) throw new Error("give it a name");
      if (task) {
        mutate({ method: "PATCH", path: `/api/tasks/${task.id}`, body: payload },
               () => Object.assign(task, payload));
      } else {
        const id = newId();
        mutate({ method: "POST", path: "/api/tasks", body: { id, ...payload } },
               () => tasks.push({
                 id, ...payload, done_at: null, completed_count: 0,
                 ticket_id: null, source: "owner",
                 created_at: new Date().toISOString(),
               }));
      }
    },
    { danger: task ? "Archive" : "" });

  if (task) {
    $("sheetForm").querySelector("[data-danger]").addEventListener("click", () => {
      mutate({ method: "POST", path: `/api/tasks/${task.id}/archive` },
             () => { tasks = tasks.filter((t) => t.id !== task.id); });
      closeSheet();
    });
  }
}

function toggleTask(task) {
  const done = !task.done_at;
  mutate({ method: "POST", path: `/api/tasks/${task.id}/done`, body: { done } },
         () => applyToggle(task, done));
}

// Mirrors what the server does to a repeating task, so the row on screen does
// not disagree with the row in Postgres for as long as the queue takes to
// drain -- and so a repeat ticked off on a plane still shows its next date.
function applyToggle(task, done) {
  if (done && task.repeat_days && task.due_at) {
    const next = new Date(task.due_at);
    do {
      next.setDate(next.getDate() + task.repeat_days);
    } while (next <= Date.now());
    task.due_at = next.toISOString();
    task.done_at = null;
    task.completed_count = (task.completed_count || 0) + 1;
  } else {
    task.done_at = done ? new Date().toISOString() : null;
    if (done) task.completed_count = (task.completed_count || 0) + 1;
  }
}

// ---------------------------------------------------------- event editing

function openEvent(event, dayHint) {
  const starts = event ? new Date(event.starts_at) : null;
  const ends = event && event.ends_at ? new Date(event.ends_at) : null;
  const t = (d) => d
    ? `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`
    : "";
  openSheet(event ? "Edit event" : "New event", `
    <label for="f-title">What is it?</label>
    <input id="f-title" maxlength="200" placeholder="e.g. Coffee with Rami"
           value="${esc(event ? event.title : "")}" required>
    <div class="two">
      <div><label for="f-date">Starts</label>
        <input id="f-date" type="date" value="${
          esc(starts ? dayKey(starts) : (dayHint || dayKey(new Date())))}" required></div>
      <div><label for="f-time">At</label>
        <input id="f-time" type="time" value="${esc(t(starts) || "09:00")}"></div>
    </div>
    <div class="two">
      <div><label for="f-end-date">Ends</label>
        <input id="f-end-date" type="date" value="${esc(ends ? dayKey(ends) : "")}"></div>
      <div><label for="f-end-time">At</label>
        <input id="f-end-time" type="time" value="${esc(t(ends))}"></div>
    </div>
    <label for="f-loc">Where</label>
    <input id="f-loc" maxlength="200" placeholder="Optional"
           value="${esc(event && event.location ? event.location : "")}">
    <label for="f-notes">Notes</label>
    <textarea id="f-notes" maxlength="2000" placeholder="Optional">${
      esc(event && event.notes ? event.notes : "")}</textarea>`,
    async () => {
      const startsAt = toIso($("f-date").value, $("f-time").value || "09:00");
      if (!startsAt) throw new Error("give it a date");
      const endDate = $("f-end-date").value || $("f-date").value;
      const endTime = $("f-end-time").value;
      const endsAt = endTime || $("f-end-date").value
        ? toIso(endDate, endTime || $("f-time").value || "10:00")
        : null;
      if (endsAt && endsAt <= startsAt) throw new Error("it cannot end before it starts");
      const title = $("f-title").value.trim();
      if (!title) throw new Error("give it a name");
      // Events have no PATCH: an edit is a cancel and a re-create, which keeps
      // one code path on the server for something changed a handful of times.
      // Both halves queue in order, so an edit made offline replays as an edit.
      const body = {
        id: newId(), title, starts_at: startsAt, ends_at: endsAt,
        location: $("f-loc").value.trim() || null,
        notes: $("f-notes").value.trim() || null,
      };
      if (event) {
        mutate({ method: "DELETE", path: `/api/events/${event.id}` },
               () => { events = events.filter((e) => e.id !== event.id); });
      }
      mutate({ method: "POST", path: "/api/events", body },
             () => events.push({ ...body, source: "owner" }));
    },
    { danger: event ? "Cancel event" : "" });

  if (event) {
    $("sheetForm").querySelector("[data-danger]").addEventListener("click", () => {
      mutate({ method: "DELETE", path: `/api/events/${event.id}` },
             () => { events = events.filter((e) => e.id !== event.id); });
      closeSheet();
    });
  }
}

// -------------------------------------------------------- project editing

const COLOURS = [["violet", "Violet"], ["blue", "Blue"], ["green", "Green"],
                 ["amber", "Amber"], ["red", "Red"], ["grey", "Grey"]];
const STATUSES = [["active", "Active"], ["paused", "Paused"], ["done", "Done"]];

function openProject(project) {
  const due = project && project.due_at ? dayKey(new Date(project.due_at)) : "";
  openSheet(project ? "Edit project" : "New project", `
    ${project ? `<p class="note">${project.tasks_done} of ${project.tasks} tasks done${
      project.items ? ` · ${project.items} message${project.items === 1 ? "" : "s"}` : ""}</p>` : ""}
    <div class="two">
      <div><label for="f-emoji">Icon</label>
        <input id="f-emoji" maxlength="4" value="${esc(project ? project.emoji : "\u{1F4C1}")}"></div>
      <div><label for="f-due">Deadline</label>
        <input id="f-due" type="date" value="${esc(due)}"></div>
    </div>
    <label for="f-title">Name</label>
    <input id="f-title" maxlength="120" placeholder="e.g. Rami's booking app"
           value="${esc(project ? project.name : "")}" required>
    <label>Colour</label>
    ${chips("colour", COLOURS, project ? project.colour : "violet", { swatch: true })}
    ${project ? `<label>Status</label>${chips("status", STATUSES, project.status)}` : ""}
    <label for="f-notes">Notes</label>
    <textarea id="f-notes" maxlength="4000" placeholder="What is this, and what does done look like?">${
      esc(project && project.notes ? project.notes : "")}</textarea>`,
    async () => {
      const name = $("f-title").value.trim();
      if (!name) throw new Error("give it a name");
      const payload = {
        name,
        emoji: $("f-emoji").value.trim() || "\u{1F4C1}",
        colour: chipValue("colour") || "violet",
        notes: $("f-notes").value.trim() || null,
        due_at: $("f-due").value ? toIso($("f-due").value, "09:00") : null,
      };
      if (project) payload.status = chipValue("status") || "active";
      if (project) {
        mutate({ method: "PATCH", path: `/api/projects/${project.id}`, body: payload },
               () => Object.assign(project, payload));
      } else {
        const id = newId();
        mutate({ method: "POST", path: "/api/projects", body: { id, ...payload } },
               () => projects.push({
                 id, ...payload, status: "active", source: "owner",
                 tasks: 0, tasks_done: 0, items: 0,
                 created_at: new Date().toISOString(),
               }));
      }
    },
    { danger: project ? "Archive" : "" });

  if (project) {
    $("sheetForm").querySelector("[data-danger]").addEventListener("click", () => {
      mutate({ method: "POST", path: `/api/projects/${project.id}/archive` },
             () => {
               projects = projects.filter((p) => p.id !== project.id);
               // The server unfiles them rather than deleting them; do the same
               // here so the app does not show a project that is gone.
               for (const t of tasks) if (t.project_id === project.id) t.project_id = null;
               for (const i of items) if (i.project_id === project.id) i.project_id = null;
             });
      closeSheet();
    });
  }
}

// ------------------------------------------------------------- the calendar

function eventDays(e) {
  return [dayKey(new Date(e.starts_at)),
          dayKey(new Date(e.ends_at || e.starts_at))];
}

function eventOn(e, key) {
  const [from, to] = eventDays(e);
  return from <= key && key <= to;
}

function openTasks() { return tasks.filter((t) => !t.done_at); }

function renderCalendar() {
  const today = dayKey(new Date());
  const first = `${calMonth}-01`;
  $("calLabel").textContent = fromKey(first).toLocaleDateString(undefined,
    { month: "long", year: "numeric" });

  const start = weekStart(first);
  const cells = [];
  for (let i = 0; i < 42; i++) {
    const key = addDays(start, i);
    const dayEvents = events.filter((e) => eventOn(e, key));
    const multi = dayEvents.some((e) => { const [f, t] = eventDays(e); return f !== t; });
    const hasTask = openTasks().some((t) => t.due_at && dayKey(new Date(t.due_at)) === key);
    const hasItem = items.some((i) =>
      i.due_at && i.status !== "done" && dayKey(new Date(i.due_at)) === key);
    cells.push(`
      <button type="button" class="cd${key.slice(0, 7) === calMonth ? "" : " out"}${
        key === today ? " today" : ""}" aria-pressed="${String(key === selDay)}"
        data-day="${key}" aria-label="${esc(dayName(key))}">
        <span class="n">${fromKey(key).getDate()}</span>
        <span class="dots">${dayEvents.length ? '<i class="ev"></i>' : ""}${
          hasTask ? '<i class="tk"></i>' : ""}${hasItem ? '<i class="msg"></i>' : ""}</span>
        ${multi ? '<span class="rng"></span>' : ""}
      </button>`);
  }
  $("calGrid").innerHTML = cells.join("");

  const open = openTasks();
  const overdue = open.filter((t) => t.due_at && new Date(t.due_at) <= Date.now()).length
    + items.filter((i) => i.due_at && i.status !== "done" && new Date(i.due_at) <= Date.now()).length;
  const weekFrom = weekStart(today);
  const weekTo = addDays(weekFrom, 6);
  const thisWeek = events.filter((e) => {
    const [f, t] = eventDays(e);
    return f <= weekTo && t >= weekFrom;
  }).length;
  $("calStats").innerHTML =
    stat("This week", thisWeek, thisWeek ? "warn" : "") +
    stat("Open", open.length, "") +
    stat("Overdue", overdue, overdue ? "bad" : "good") +
    stat("Done", tasks.length - open.length, "good");

  $("dayLabel").textContent = dayName(selDay);
  renderAgenda();
}

function stat(label, value, tone) {
  return `<div class="stat"><div class="v ${tone}">${value}</div>
          <div class="l">${esc(label)}</div></div>`;
}

function renderAgenda() {
  const rows = [];
  for (const e of events.filter((e) => eventOn(e, selDay))
      .sort((a, b) => new Date(a.starts_at) - new Date(b.starts_at))) {
    const [from, to] = eventDays(e);
    const span = from === to
      ? hhmm(e.starts_at)
      : `${hhmm(e.starts_at)} · day ${
          Math.round((fromKey(selDay) - fromKey(from)) / 86400000) + 1} of ${
          Math.round((fromKey(to) - fromKey(from)) / 86400000) + 1}`;
    rows.push(`
      <button type="button" class="trow" data-event="${esc(e.id)}">
        <span class="ico">${from === to ? "\u{1F551}" : "\u{1F5D3}"}</span>
        <span class="bd"><span class="t">${esc(e.title)}</span>
          <span class="s">${esc(span)}${e.location ? ` · ${esc(e.location)}` : ""}${
            e.source === "assistant" ? ' · <span class="by-jarvis">by Jarvis</span>' : ""}</span></span>
      </button>`);
  }
  for (const t of tasks.filter((t) => t.due_at && dayKey(new Date(t.due_at)) === selDay)) {
    rows.push(taskRow(t));
  }
  for (const i of items.filter((i) =>
      i.due_at && i.status !== "done" && dayKey(new Date(i.due_at)) === selDay)) {
    rows.push(`
      <button type="button" class="trow" data-item="${esc(i.id)}">
        <span class="ico">\u{1F4E5}</span>
        <span class="bd"><span class="t">${esc(i.title)}</span>
          <span class="s">${esc(hhmm(i.due_at))} · message</span></span>
      </button>`);
  }
  $("agenda").innerHTML = rows.length
    ? `<div class="row-list">${rows.join("")}</div>`
    : '<div class="empty">Nothing on. A free day.</div>';
}

// -------------------------------------------------------------- task lists

function taskRow(t) {
  const done = Boolean(t.done_at);
  const bits = [];
  if (t.due_at) {
    const late = !done && new Date(t.due_at) <= Date.now();
    const when = t.all_day
      ? fromKey(dayKey(new Date(t.due_at))).toLocaleDateString(undefined,
          { weekday: "short", day: "numeric", month: "short" })
      : new Date(t.due_at).toLocaleString(undefined,
          { weekday: "short", day: "numeric", month: "short",
            hour: "2-digit", minute: "2-digit" });
    bits.push(late ? `<span class="bad">Overdue · ${esc(when)}</span>` : esc(when));
  }
  if (t.repeat_days) bits.push(esc(repeatLabel(t.repeat_days)));
  const project = projects.find((p) => p.id === t.project_id);
  if (project) bits.push(`${esc(project.emoji)} ${esc(project.name)}`);
  if (t.source === "assistant") bits.push('<span class="by-jarvis">by Jarvis</span>');

  return `
    <div class="trow${done ? " done" : ""}">
      <button type="button" class="ck" data-toggle="${esc(t.id)}"
              aria-pressed="${String(done)}" aria-label="Mark done">&#10003;</button>
      <span class="bd" data-task="${esc(t.id)}" role="button" tabindex="0">
        <span class="t">${esc(t.title)}</span>
        ${bits.length ? `<span class="s">${bits.join(" · ")}</span>` : ""}
      </span>
      <span class="pdot ${esc(t.priority)}"></span>
    </div>`;
}

function repeatLabel(days) {
  const found = REPEATS.find(([value]) => Number(value) === days);
  return found ? found[1] : `Every ${days} days`;
}

function renderTasks() {
  const today = dayKey(new Date());
  const filtered = tasks.filter((t) => !taskProject || t.project_id === taskProject);
  const open = filtered.filter((t) => !t.done_at);
  const groups = { over: [], today: [], soon: [], someday: [] };
  for (const t of open) {
    const key = t.due_at ? dayKey(new Date(t.due_at)) : "";
    if (!key) groups.someday.push(t);
    else if (key < today) groups.over.push(t);
    else if (key === today) groups.today.push(t);
    else groups.soon.push(t);
  }

  const out = [];
  const project = projects.find((p) => p.id === taskProject);
  if (project) {
    out.push(`<div class="pillrow" style="margin:0 0 var(--sp-3)">
      <button class="pill" data-clear-project aria-pressed="true">${
        esc(project.emoji)} ${esc(project.name)} &times;</button></div>`);
  }
  const section = (label, list) => {
    if (!list.length) return;
    out.push(`<div class="section-label">${esc(label)}<span class="count">${list.length}</span></div>`);
    out.push(`<div class="row-list">${list.map(taskRow).join("")}</div>`);
  };
  section("Overdue", groups.over);
  section("Today", groups.today);
  section("Coming up", groups.soon);
  section("Someday", groups.someday);

  if (showDone) {
    const done = filtered.filter((t) => t.done_at)
      .sort((a, b) => new Date(b.done_at) - new Date(a.done_at)).slice(0, 20);
    section("Done", done);
  }
  if (!out.length || (out.length === 1 && project)) {
    out.push('<div class="empty">Nothing to do.<br>Add one, or ask the assistant to.</div>');
  }
  $("taskBody").innerHTML = out.join("");
}

// --------------------------------------------------------------- projects

function renderProjects() {
  if (!projects.length) {
    $("projectBody").innerHTML =
      '<div class="empty">No projects yet.<br>Start one for anything bigger than a task.</div>';
    return;
  }
  $("projectBody").innerHTML = projects.map((p) => {
    const total = Number(p.tasks) || 0;
    const done = Number(p.tasks_done) || 0;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const bits = [];
    if (total) bits.push(`${done} of ${total} done`);
    if (Number(p.items)) bits.push(`${p.items} message${Number(p.items) === 1 ? "" : "s"}`);
    if (p.due_at) bits.push(`due ${fromKey(dayKey(new Date(p.due_at)))
      .toLocaleDateString(undefined, { day: "numeric", month: "short" })}`);
    if (p.status !== "active") bits.push(p.status);
    if (!bits.length) bits.push("nothing filed yet");
    return `
      <div class="proj ${esc(p.colour)}${p.status === "done" ? " is-done" : ""}">
        <div class="head">
          <span class="emoji">${esc(p.emoji)}</span>
          <span class="name">${esc(p.name)}</span>
          <button type="button" class="pill" data-project-tasks="${esc(p.id)}">Tasks</button>
          <button type="button" class="pill" data-project="${esc(p.id)}">Edit</button>
        </div>
        <div class="sub">${esc(bits.join(" · "))}</div>
        ${total ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}
      </div>`;
  }).join("");
}

// ---------------------------------------------------------------- wiring

function renderPlanner() {
  if (pane === "calendar") renderCalendar();
  if (pane === "tasks") renderTasks();
  if (pane === "projects") renderProjects();
}

$("plannerSeg").addEventListener("click", (e) => {
  const button = e.target.closest("[data-pane]");
  if (!button) return;
  pane = button.dataset.pane;
  for (const b of $("plannerSeg").children) {
    b.setAttribute("aria-selected", String(b === button));
  }
  for (const p of document.querySelectorAll("#view-planner .pane")) {
    p.classList.toggle("active", p.id === `pane-${pane}`);
  }
  renderPlanner();
});

$("calPrev").addEventListener("click", () => shiftMonth(-1));
$("calNext").addEventListener("click", () => shiftMonth(1));
$("calToday").addEventListener("click", () => {
  calMonth = monthOf(new Date());
  selDay = dayKey(new Date());
  renderCalendar();
});

function shiftMonth(delta) {
  const [y, m] = calMonth.split("-").map(Number);
  calMonth = monthOf(new Date(y, m - 1 + delta, 1));
  // Keep the selection inside the month being looked at, or the agenda below
  // the grid describes a day that is not on screen.
  if (selDay.slice(0, 7) !== calMonth) selDay = `${calMonth}-01`;
  renderCalendar();
}

$("toggleDone").addEventListener("click", () => {
  showDone = !showDone;
  $("toggleDone").setAttribute("aria-pressed", String(showDone));
  $("toggleDone").textContent = showDone ? "Hide done" : "Show done";
  renderTasks();
});

// One listener for the whole module: the lists are re-rendered constantly and
// rebinding every row each time is how listeners get left behind.
$("view-planner").addEventListener("click", async (e) => {
  const newThing = e.target.closest("[data-new]");
  if (newThing) {
    const day = pane === "calendar" ? selDay : "";
    if (newThing.dataset.new === "task") openTask(null, day);
    if (newThing.dataset.new === "event") openEvent(null, day);
    if (newThing.dataset.new === "project") openProject(null);
    return;
  }

  const day = e.target.closest("[data-day]");
  if (day) { selDay = day.dataset.day; renderCalendar(); return; }

  const toggle = e.target.closest("[data-toggle]");
  if (toggle) {
    toggle.disabled = true;
    const task = tasks.find((t) => t.id === toggle.dataset.toggle);
    if (task) await toggleTask(task);
    return;
  }

  const taskEl = e.target.closest("[data-task]");
  if (taskEl) {
    const task = tasks.find((t) => t.id === taskEl.dataset.task);
    if (task) openTask(task);
    return;
  }

  const eventEl = e.target.closest("[data-event]");
  if (eventEl) {
    const found = events.find((x) => x.id === eventEl.dataset.event);
    if (found) openEvent(found);
    return;
  }

  const itemEl = e.target.closest("[data-item]");
  if (itemEl) { show("inbox"); return; }

  const projectTasks = e.target.closest("[data-project-tasks]");
  if (projectTasks) {
    taskProject = projectTasks.dataset.projectTasks;
    $("plannerSeg").querySelector('[data-pane="tasks"]').click();
    return;
  }

  const projectEl = e.target.closest("[data-project]");
  if (projectEl) {
    const found = projects.find((p) => p.id === projectEl.dataset.project);
    if (found) openProject(found);
    return;
  }

  if (e.target.closest("[data-clear-project]")) {
    taskProject = "";
    renderTasks();
  }
});

// -------------------------------------------------------------- Settings

// The standing instructions the visitor-facing assistant is given. The box
// has been in the page since it was built and was never connected to anything,
// so whatever was typed in it went nowhere.
let briefing = null;

async function renderSettings() {
  $("acctEmail").textContent = read(EMAIL_KEY) || "signed in";

  if (briefing === null) {
    try {
      briefing = (await api("/api/briefing")).briefing || "";
      $("briefing").value = briefing;
      $("briefingState").textContent = "";
    } catch (e) {
      briefing = null;
      $("briefingState").textContent = e.network
        ? "Cannot load while offline."
        : "Could not load.";
    }
  }

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
    $("svcState").textContent = navigator.onLine
      ? "Service unreachable" : "Offline";
    $("svcDetail").textContent = navigator.onLine
      ? "The assistant cannot be contacted."
      : "Your changes are saved here and will sync when you reconnect.";
  }
}

$("saveBriefing").addEventListener("click", () => {
  const text = $("briefing").value.trim().slice(0, 1200);
  mutate({ method: "PUT", path: "/api/briefing", body: { briefing: text } },
         () => { briefing = text; });
  $("briefingState").textContent = pending() ? "Saved here; will sync." : "Saved.";
  setTimeout(() => { $("briefingState").textContent = ""; }, 4000);
});

// ------------------------------------------------------------------ data

async function refresh() {
  if (!token) return;
  // Send what is waiting before asking for the truth. The server's answer is
  // only the truth once it has heard everything this device has to say.
  await flushOutbox();
  if (pending()) { badges(); renderActive(); return; }

  try {
    // Everything at once, filtered client-side: the whole set is small, it
    // keeps Today, Inbox and Planner consistent, and it is exactly the shape
    // that gets written to the offline snapshot.
    const [itemData, eventData, taskData, projectData] = await Promise.all([
      api("/api/items"),
      // A year forward and six months back, so paging the calendar into the
      // past shows what was actually there.
      api("/api/events?days=365&back=180"),
      api("/api/tasks"),
      api("/api/projects"),
    ]);
    items = itemData.items;
    events = eventData.events;
    tasks = taskData.tasks;
    projects = projectData.projects;
    saveSnapshot({ items, events, tasks, projects });
    offline = false;
    syncNote = "";
  } catch (e) {
    if (e.message === "session expired") return;
    // A request that never left the device is a connection problem, and the
    // snapshot already on screen stays. Anything else is a real fault worth
    // seeing in the console.
    offline = Boolean(e.network);
    if (!e.network) console.warn(e);
  }
  badges();
  renderActive();
  updateSyncState();
}

function badges() {
  const dueNow =
    items.filter((i) => i.due_at && new Date(i.due_at) <= Date.now() && i.status !== "done").length
    + tasks.filter((t) => !t.done_at && t.due_at && new Date(t.due_at) <= Date.now()).length;
  badge("inboxBadge", items.filter((i) => i.status === "new").length);
  badge("dueBadge", dueNow);
}

function badge(id, n) {
  const el = $(id);
  el.hidden = n === 0;
  el.textContent = n > 9 ? "9+" : String(n);
}

// ------------------------------------------------------------------ auth

// An expired token is not the same as choosing to leave. Reloading the page
// for it -- which is what used to happen -- threw away the loaded app and, if
// anything about the reload went wrong, left a blank screen behind the login
// dialog. Now the app stays exactly where it is, the dialog comes back, and
// signing in picks up where it left off. The snapshot and the outbox are kept:
// unsent work must survive a token expiring.
function sessionExpired() {
  forget(SESSION_KEY);
  token = "";
  clearInterval(poll);
  requireLogin();
}

function signOut() {
  // Signing out clears the device's copy, and anything still queued goes with
  // it. That is only ever a surprise, so it is asked about first.
  if (pending() && !confirm(
      `${pending()} change${pending() === 1 ? " has" : "s have"} not reached the ` +
      "server yet. Signing out now will lose them. Continue?")) {
    return;
  }
  forgetSnapshot();
  forget(SESSION_KEY);
  forget(EMAIL_KEY);
  clearInterval(poll);
  token = "";
  // Reload rather than tearing down: the widget holds a socket, a mic stream
  // and timers, and unwinding all of that by hand is more error-prone than
  // starting clean.
  location.reload();
}

function launch(t) {
  token = t;
  if ($("loginDialog").open) $("loginDialog").close();
  // The app is not in the page until there is a session to view it with.
  $("shell").hidden = false;

  // Draw from the last snapshot before asking the network for anything. The
  // app opens with content on a train, and opens instantly everywhere else.
  const snap = loadSnapshot();
  if (snap) {
    items = snap.items;
    events = snap.events;
    tasks = snap.tasks;
    projects = snap.projects;
  }
  show("today");
  badges();
  updateSyncState();
  refresh();
  poll = setInterval(refresh, POLL_MS);
  // The browser's own signal, which beats waiting up to twenty seconds for the
  // next poll to notice the connection is back.
  window.addEventListener("online", () => { offline = false; refresh(); });
  window.addEventListener("offline", () => { offline = true; updateSyncState(); });
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
// Delegated, not bound per element: Today re-renders its rows on every poll,
// and anything bound at load would stop working the moment it did.
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-goto]");
  if (el) show(el.dataset.goto);
});
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

// Two layers, because one was not enough.
//
// preventDefault on `cancel` stops Escape closing the dialog, but it does not
// stop everything: Android's back gesture dismisses a modal dialog anyway, and
// what was behind it was the whole app, rendered and interactive. It showed
// nothing real -- every request needs the token the server checks -- but a
// signed-out person could still page through empty modules, which is not what
// a login screen is for.
//
// So the shell is not in the page at all until there is a session, and the
// dialog reopens if it is ever dismissed without one.
$("loginDialog").addEventListener("cancel", (e) => {
  e.preventDefault();
});
$("loginDialog").addEventListener("close", () => {
  if (!token) requireLogin();
});

function requireLogin() {
  $("shell").hidden = true;
  if (!$("loginDialog").open) $("loginDialog").showModal();
}

registerServiceWorker();

const existing = read(SESSION_KEY);
if (existing) launch(existing);
else requireLogin();
