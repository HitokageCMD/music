/* App-shell cache only — enough to install as a PWA and cold-start offline. */

const CACHE = 'tunebox-shell-v2';
const SHELL = [
  '/', '/static/app.js', '/static/idb.js', '/static/style.css',
  '/static/icon.svg', '/static/icon-192.png', '/manifest.json',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Hands off everything under /api/. Audio is served with Range requests, and a
  // service worker that touches those breaks seeking and can stall playback
  // outright. The server already caches the audio on disk anyway.
  if (url.pathname.startsWith('/api/')) return;
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;

  // Network first so edits show up without a hard reload; cache is the fallback.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return res;
        }
        // A dead backend behind a reverse proxy is not a network failure — the
        // proxy answers 502/503/504 and fetch() *resolves*, so .catch() below
        // never runs and the cached shell never gets a chance. Turn it back into
        // a rejection. Measured: with the server stopped, Tailscale returns 502
        // and the app rendered a blank page despite the whole shell being cached.
        if (res.status >= 500) throw new Error(`upstream ${res.status}`);
        return res; // a real 404 is an answer; pass it through
      })
      .catch(() => caches.match(e.request).then((hit) => hit || Response.error()))
  );
});
