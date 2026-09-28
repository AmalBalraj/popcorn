/* Popcorn service worker.
 *
 * Caches the application shell only. Authenticated pages, API responses and
 * media are never cached: watch state and progress must always be live.
 */

const CACHE = 'popcorn-shell-v4';

const ASSETS = [
  '/static/css/base.css',
  '/static/css/components.css',
  '/static/css/pages.css',
  '/static/css/player.css',
  '/static/vendor/hls.min.js',
  '/static/js/core/api.js',
  '/static/js/core/art.js',
  '/static/js/core/dom.js',
  '/static/js/core/downloads.js',
  '/static/js/core/icons.js',
  '/static/js/core/items.js',
  '/static/js/core/shell.js',
  '/static/js/core/toast.js',
  '/static/js/core/watchlist.js',
  '/static/js/components/card.js',
  '/static/js/components/clips.js',
  '/static/js/components/hero.js',
  '/static/js/components/modal.js',
  '/static/js/components/row.js',
  '/static/js/components/state.js',
  '/static/js/pages/browse.js',
  '/static/js/pages/detail.js',
  '/static/js/pages/downloads.js',
  '/static/js/pages/home.js',
  '/static/js/pages/player.js',
  '/static/js/pages/search.js',
  '/static/js/pages/settings.js',
  '/static/manifest.webmanifest',
  '/static/icon.svg',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      // One missing file must not fail the whole install.
      Promise.all(ASSETS.map((asset) => cache.add(asset).catch(() => null)))),
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))),
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin) return;
  if (!url.pathname.startsWith('/static/')) return;

  // Network first, so an updated stylesheet or module always wins; the cache
  // is the safety net for a flaky connection.
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request)),
  );
});
