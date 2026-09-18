// Real, minimal service worker - PWA installability plus a genuine
// offline fallback, nothing more. Deliberately does NOT cache pages or
// API responses: an order's status, a transcript, a review queue - all
// of that changes between visits, and serving a stale cached copy while
// offline would be actively misleading (a "processing" order that's
// actually long since delivered). Only truly static assets (CSS, the
// PWA icons, this file's own manifest) are cached, cache-first; every
// page navigation goes to the network first, falling back to a plain
// "you're offline" response only when the network genuinely fails.
const CACHE_NAME = "kauli-shell-v1";
const SHELL_ASSETS = [
  "/static/style.css",
  "/static/manifest.json",
  "/static/pwa-icon-192.png",
  "/static/pwa-icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // never intercept a POST (order actions, form submits)

  const url = new URL(req.url);
  const isShellAsset = SHELL_ASSETS.some((path) => url.pathname === path);

  if (isShellAsset) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req))
    );
    return;
  }

  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(
        () =>
          new Response(
            "<!doctype html><html><head><meta charset='utf-8'><title>Offline - Kauli</title>" +
              "<style>body{font-family:system-ui,sans-serif;background:#141023;color:#e4e4e7;" +
              "display:flex;align-items:center;justify-content:center;height:100vh;margin:0;" +
              "text-align:center;padding:24px}</style></head><body><div>" +
              "<h1>You're offline</h1><p>Kauli needs a connection to show real, up-to-date order data - " +
              "reconnect and reload.</p></div></body></html>",
            { headers: { "Content-Type": "text/html" } }
          )
      )
    );
  }
});
