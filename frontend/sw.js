self.TS_SW_VERSION = "2026-07-31-auth-ui-v4";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.map((key) => caches.delete(key)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.mode === "navigate" || new URL(request.url).pathname === "/") {
    event.respondWith(fetch(request, { cache: "no-store" }));
  }
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (error) {
    payload = {};
  }

  const title = payload.title || "Total Solutions";
  const options = {
    body: payload.body || payload.message || "Nueva alerta disponible.",
    icon: "/assets/assets/favicon.svg",
    badge: "/assets/assets/favicon.svg",
    tag: payload.tag || `ts-${payload.notification_id || Date.now()}`,
    renotify: false,
    data: {
      url: payload.url || "/",
      lead_id: payload.lead_id || null,
      notification_id: payload.notification_id || null,
    },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const targetUrl = new URL(data.url || "/", self.location.origin).href;

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ("navigate" in client && "focus" in client) {
          return client.navigate(targetUrl).then(() => client.focus());
        }
      }
      return self.clients.openWindow(targetUrl);
    })
  );
});
