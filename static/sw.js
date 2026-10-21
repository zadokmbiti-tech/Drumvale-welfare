// Drumvale PWA service worker.
// Rule: never touch API / auth / payment traffic. Only cache static assets and show an offline page.
const VERSION = 'drumvale-v1';
const PRECACHE = ['/static/offline.html', '/static/icons/icon-192.png', '/static/icons/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(PRECACHE)));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;                       // never intercept POST/PUT/DELETE
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;        // fonts/CDNs: let the browser handle

  // Page navigations: always network, fall back to offline page. Never serve stale portal pages.
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/static/offline.html')));
    return;
  }

  // Icons only: cache-first. Everything else (API, /auth, /members, /contributions ...) goes straight to network.
  if (url.pathname.startsWith('/static/icons/')) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});
