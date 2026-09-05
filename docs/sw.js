// Service worker: push notifications, and just enough caching to make the
// installed app open instantly rather than staring at white.
//
// Deliberately NOT an offline-first cache. This app is a thin client over a
// WebSocket -- with no network there is nothing to say and nothing to show, so
// pretending otherwise would only produce a convincing but useless shell.

const CACHE = "assistant-v2";

// The shell only. Never cache API responses: a stale inbox that looks current
// is worse than one that admits it cannot load.
const SHELL = [
  "./",
  "./index.html",
  "./message.html",
  "./app.html",
  "./css/app.css",
  "./css/shell.css",
  "./js/widget.js",
  "./js/app.js",
  "./js/pwa.js",
  "./manifest.json",
  "./icons/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // Individually, so one 404 does not fail the whole install.
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
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
  // beats the few milliseconds a cache-first would save.
  event.respondWith(
    fetch(event.request)
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
  let payload = { title: "Assistant", body: "", url: "./dashboard.html" };
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
  const target = (event.notification.data && event.notification.data.url) || "./dashboard.html";

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
