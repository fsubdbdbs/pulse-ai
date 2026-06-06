/* Pulse AI — Service Worker */

self.addEventListener("push", event => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(
    self.registration.showNotification(data.title || "Pulse AI", {
      body:      data.body || "",
      icon:      "/icons/icon-192.png",
      badge:     "/icons/icon-192.png",
      vibrate:   [100, 50, 100],
      tag:       "pulse-ai",
      renotify:  true,
    })
  );
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if ("focus" in c) return c.focus();
      }
      return clients.openWindow("/");
    })
  );
});
