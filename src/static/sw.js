/* Fleet Hub v1 service worker: versioned public shell only. */
'use strict';

const SHELL_CACHE = 'fleet-hub-shell-v1-20260826-r2';
const SHELL_PATHS = [
  './',
  './static/app.js',
  './static/style.css',
  './static/manifest.webmanifest',
  './static/icons/icon-180.png',
  './static/icons/icon-192.png',
  './static/icons/icon-512.png',
];

const NEVER_CACHE = [
  /\/api(?:\/|$)/,
  /\/events(?:\/|$)/,
  /\/login(?:\/|$)/,
  /\/logout(?:\/|$)/,
  /\/commands?(?:\/|$)/,
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_PATHS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        .filter((key) => key.startsWith('fleet-hub-shell-') && key !== SHELL_CACHE)
        .map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

function isPrivateRequest(url) {
  return NEVER_CACHE.some((pattern) => pattern.test(url.pathname));
}

function relativeShellPath(url) {
  const scopePath = new URL(self.registration.scope).pathname;
  if (!url.pathname.startsWith(scopePath)) return null;
  const suffix = url.pathname.slice(scopePath.length);
  return suffix ? `./${suffix}` : './';
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || request.method !== 'GET') return;

  // Auth, events, APIs, and commands are always network-only. There is no
  // Background Sync handler and failed commands are never queued or replayed.
  if (isPrivateRequest(url)) {
    event.respondWith(fetch(request));
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('./')),
    );
    return;
  }

  const relative = relativeShellPath(url);
  if (!SHELL_PATHS.includes(relative)) return;
  // Explicit shell assets are network-first so a long-running installation
  // revalidates promptly. Only allowlisted public assets may update this cache.
  event.respondWith(
    caches.open(SHELL_CACHE).then(async (cache) => {
      try {
        const response = await fetch(request);
        if (response.ok) await cache.put(request, response.clone());
        return response;
      } catch {
        return await cache.match(request) || Response.error();
      }
    }),
  );
});
