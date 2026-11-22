// JulAIn Music Player — the shell cache for an installed window.
//
// The server is the library: every answer comes from /api/*, carries the
// access token and changes between two polls, so nothing here may cache it.
// The page itself is token-protected too (its URL carries ?t=), so it is not
// cached either.  What is left is the shell: the stylesheet, the script and
// the icon.  They are fetched from the network first — the server already
// sends no-store — and the previous copy is only used when the network
// cannot answer at all.
const CACHE = "julians-music-shell-v2";
const SHELL = [
  "/static/app.css",
  "/static/app.js",
  "/static/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .catch(() => undefined)
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith("/static/")) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => undefined);
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || Response.error()))
  );
});
