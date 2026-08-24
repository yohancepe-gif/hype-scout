/* Hype Scout service worker.
 *
 * Network-first, always. This app's whole value is freshness - a cache-first
 * shell would happily show you yesterday's "EARLY" call as if it were live,
 * which is exactly the failure mode that loses money. The cache exists only so
 * the app opens to something rather than an error when there's no signal, and
 * anything served from it is flagged to the page as stale.
 */
const CACHE = 'hype-scout-v2';
const SHELL = ['./', './index.html', './manifest.webmanifest',
               './icons/icon-180.png', './icons/icon-192.png',
               './icons/icon-512.png'];
// Precached separately and best-effort. On a first run the page's own data
// fetches can beat the worker to controlling the page, so relying on runtime
// caching alone leaves an installed app that opens offline to an empty screen.
const DATA = ['./data/scan.json', './data/portfolio.json'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL).then(() =>
        // A missing optional file must not fail the whole install.
        Promise.all(DATA.map(u => c.add(u).catch(() => {})))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) return;

  e.respondWith(
    fetch(req)
      .then(res => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req, { ignoreSearch: true }).then(hit => {
        if (hit) {
          // Mark it, so the page can tell the user what they're looking at.
          const h = new Headers(hit.headers);
          h.set('X-From-Cache', '1');
          return hit.blob().then(b => new Response(b, {
            status: hit.status, statusText: hit.statusText, headers: h
          }));
        }
        return new Response('offline', { status: 503 });
      }))
  );
});
