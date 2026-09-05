// Service worker: push notifications, and the shell that lets the app open
// with no network at all.
//
// It used to say this was deliberately not an offline cache, on the grounds
// that the app was a thin client over a WebSocket. That stopped being true:
// the planner works on the device and syncs afterwards (js/sync.js), so the
// shell has to be there to run it. Talk still needs a connection, and says so
// rather than pretending.
//
// API responses are still never cached. The data the app shows offline is its
// own snapshot, written by code that knows what is stale and what is queued --
// a cached HTTP response knows neither.

const CACHE = "assistant-v3";

const SHELL = [
  "./",
  "./index.html",
  "./message.html",
  "./app.html",
  "./css/app.css",
  "./css/shell.css",
  "./js/widget.js",
  "./js/app.js",
  "./js/sync.js",
  "./js/pwa.js",
  "./manifest.json",
  "./icons/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // Individually, so one 404 does not fail the whole install. `reload`
      // bypasses the browser's own HTTP cache -- GitHub Pages serves these
      // with a ten-minute max-age, so without it a fresh deploy could install
      // a mix of new HTML and last version's JavaScript.
      .then((c) => Promise.allSettled(
        SHELL.map((u) => c.add(new Request(u, { cache: "reload" }))),
      ))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Anything crossing to the backend goes straight to the network. Caching an
  // API response here would silently serve yesterday's inbox.
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;

  // Network first, cache as the fallback: the shell is small and correctness
  // beats the few milliseconds a cache-first would save. `no-cache` makes that
  // network request revalidate rather than be answered from the browser's own
  // ten-minute copy, which is what left the app running yesterday's script
  // after a deploy.
  event.respondWith(
    fetch(new Request(event.request.url, {
      cache: "no-cache", credentials: "same-origin",
    }))
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match("./index.html"))),
  );
});

self.addEventListener("push", (event) => {
  let payload = { title: "Assistant", body: "", url: "./app.html" };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch {
    // A push with an unparseable body still deserves to surface -- silently
    // dropping it would look identical to push being broken.
    if (event.data) payload.body = event.data.text();
  }

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "./icons/icon-192.png",
      badge: "./icons/icon-192.png",
      // Same tag replaces rather than stacks, so a reminder retried by the
      // sweeper cannot pile up three copies on the lock screen.
      tag: payload.tag || "assistant",
      renotify: true,
      data: { url: payload.url },
      requireInteraction: false,
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "./app.html";

  // Focus an existing window if one is open rather than piling up new tabs.
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if (client.url.includes("/assistant-ai/") && "focus" in client) {
          client.navigate(target).catch(() => {});
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});
