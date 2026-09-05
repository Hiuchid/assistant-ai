// Offline: a snapshot to read from, and an outbox to write into.
//
// The app is usable with no network. Reads come from the last snapshot the
// server gave us; writes are applied on the device straight away and queued,
// and the queue is replayed when the connection comes back.
//
// Two rules keep this honest, and both matter more than any of the code below:
//
// 1. **The server is the truth, but only when we are level with it.** A fetch
//    is only allowed to replace local state once the outbox is empty. Pulling
//    a fresh snapshot while writes are still queued would show the old values
//    back to someone who has already changed them, and then overwrite them.
//
// 2. **The device chooses ids.** A task created offline is created with a real
//    UUID, and the server is told to use it. So a row can be edited before it
//    has ever been sent, an edit and its create replay in order against the
//    same id, and a create that already got through is a conflict rather than
//    a duplicate.
//
// Conflicts are last-write-wins, which for one person on their own devices is
// not a compromise so much as a description of what they meant.
//
// What is NOT offline: Talk. It needs a socket, a transcription service and a
// model. Queueing half a conversation to be had later is not a conversation,
// so the module says so rather than pretending.

const CACHE_KEY = "assistant.cache";
const OUTBOX_KEY = "assistant.outbox";

// The queue is small by nature -- it is one person's afternoon of edits, not a
// sync engine. The cap exists so a bug cannot fill the origin's storage.
const MAX_QUEUED = 500;

function readJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    // Quota, or private mode. Offline stops working; the app does not.
    return false;
  }
}

// ------------------------------------------------------------------ snapshot

export function loadSnapshot() {
  const snap = readJson(CACHE_KEY, null);
  if (!snap || typeof snap !== "object") return null;
  return {
    items: snap.items || [],
    events: snap.events || [],
    tasks: snap.tasks || [],
    projects: snap.projects || [],
    at: snap.at || 0,
  };
}

export function saveSnapshot({ items, events, tasks, projects }) {
  writeJson(CACHE_KEY, { items, events, tasks, projects, at: Date.now() });
}

export function forgetSnapshot() {
  try {
    localStorage.removeItem(CACHE_KEY);
    localStorage.removeItem(OUTBOX_KEY);
  } catch { /* nothing to do */ }
}

// -------------------------------------------------------------------- outbox

export function outbox() {
  const queue = readJson(OUTBOX_KEY, []);
  return Array.isArray(queue) ? queue : [];
}

export function pending() {
  return outbox().length;
}

/** Queue one write. `path`, `method` and `body` are exactly what fetch needs. */
export function enqueue(op) {
  const queue = outbox();
  if (queue.length >= MAX_QUEUED) return false;
  queue.push({ ...op, at: Date.now() });
  return writeJson(OUTBOX_KEY, queue);
}

/**
 * Replay the queue in order.
 *
 * Order is not an optimisation here: an edit that arrives before its own
 * create is addressed to a row that does not exist yet. So the first thing
 * that cannot be sent stops the run, and everything behind it waits.
 *
 * `send(op)` should throw for a network failure and resolve for anything the
 * server actually answered. A request the server rejected outright is dropped
 * rather than retried forever -- it is not going to start working, and a queue
 * that can never drain blocks every write behind it.
 */
export async function flush(send) {
  let queue = outbox();
  if (!queue.length) return { sent: 0, dropped: [], stalled: false };

  let sent = 0;
  const dropped = [];
  let stalled = false;

  while (queue.length) {
    const op = queue[0];
    try {
      await send(op);
      sent += 1;
    } catch (e) {
      if (e && e.rejected) {
        dropped.push({ op, reason: e.message });
      } else {
        stalled = true;
        break;
      }
    }
    queue = queue.slice(1);
    writeJson(OUTBOX_KEY, queue);
  }
  return { sent, dropped, stalled };
}

/** A real UUID, so the row keeps the same identity once it reaches Postgres. */
export function newId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // Older WebViews. Still a v4 shape, still from a CSPRNG.
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const hex = [...b].map((n) => n.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${
    hex.slice(16, 20)}-${hex.slice(20)}`;
}
