// Service-worker registration and Web Push subscription.
//
// Imported by every page so the app is installable from wherever the user
// happens to land. The push subscription itself needs a session, so only the
// signed-in pages call enablePush().

const API = "https://assistant-ai.duckdns.org";

export async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return null;
  try {
    // Scope is the directory the worker is served from, which on Pages is the
    // repo subpath. Registering from anywhere else silently gets a narrower
    // scope and then never controls the other pages.
    return await navigator.serviceWorker.register("./sw.js", { scope: "./" });
  } catch (e) {
    console.warn("service worker registration failed", e);
    return null;
  }
}

// VAPID keys travel as base64url; PushManager wants raw bytes.
function urlBase64ToUint8Array(base64) {
  const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4))
    .replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

export function pushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

/**
 * Returns {ok, reason} rather than throwing, because every failure here is a
 * thing the user needs explaining, not a stack trace.
 */
export async function enablePush(token) {
  if (!pushSupported()) {
    // The overwhelmingly common cause on iOS: Safari only exposes PushManager
    // to sites opened from the Home Screen.
    const iOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
    return {
      ok: false,
      reason: iOS
        ? "On iPhone, add this page to your Home Screen first (Share → Add to Home Screen), then open it from there and try again."
        : "This browser does not support notifications.",
    };
  }

  const keyRes = await fetch(`${API}/api/push/key`);
  const { key, enabled } = await keyRes.json();
  if (!enabled || !key) return { ok: false, reason: "Push is not configured on the server." };

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    return {
      ok: false,
      reason: permission === "denied"
        ? "Notifications are blocked for this site. You'll need to allow them in your browser settings."
        : "Notifications were not allowed.",
    };
  }

  const registration = await navigator.serviceWorker.ready;
  // Reuse an existing subscription rather than creating a second one for the
  // same install -- duplicates all fire, so one reminder would arrive twice.
  let sub = await registration.pushManager.getSubscription();
  if (!sub) {
    sub = await registration.pushManager.subscribe({
      userVisibleOnly: true,          // required; silent push is not permitted
      applicationServerKey: urlBase64ToUint8Array(key),
    });
  }

  const json = sub.toJSON();
  const res = await fetch(`${API}/api/push/subscribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      endpoint: sub.endpoint,
      p256dh: json.keys.p256dh,
      auth: json.keys.auth,
    }),
  });
  if (!res.ok) return { ok: false, reason: `Server refused the subscription (${res.status}).` };
  return { ok: true };
}

export async function sendTestPush(token) {
  const res = await fetch(`${API}/api/push/test`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) return { ok: false, reason: `HTTP ${res.status}` };
  const body = await res.json();
  if (body.delivered === 0) {
    return {
      ok: false,
      reason: body.subscriptions === 0
        ? "No devices are subscribed yet."
        : "Subscribed, but the push service did not accept it.",
    };
  }
  return { ok: true, reason: `Sent to ${body.delivered} device(s).` };
}

export async function pushStatus() {
  if (!pushSupported()) return "unsupported";
  if (Notification.permission === "denied") return "blocked";
  const registration = await navigator.serviceWorker.getRegistration();
  if (!registration) return "off";
  const sub = await registration.pushManager.getSubscription();
  return sub ? "on" : "off";
}
