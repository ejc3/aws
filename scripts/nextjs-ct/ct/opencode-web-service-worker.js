'use strict';

// Network-only replacement for this OpenCode origin's old Workbox precache.
const ownPrecache = 'workbox-precache-v2-' + self.registration.scope;
const recoveryPath = '/auth/access-refresh';
let healthProbe;
const recovering = new Set();

self.addEventListener('install', (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', (event) => event.waitUntil((async () => {
  await caches.delete(ownPrecache);
  await self.clients.claim();
})()));

function accessExpired() {
  if (!healthProbe) {
    healthProbe = (async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(new URL('/api/info', self.location.origin), {
          credentials: 'same-origin', redirect: 'manual', cache: 'no-store',
          headers: { 'X-Requested-With': 'XMLHttpRequest' }, signal: controller.signal,
        });
        if (response.status === 401 || response.type === 'opaqueredirect') return true;
        return false;
      } catch (_) {
        return false; // An offline or unreachable server is not proof of an expired sign-in.
      } finally {
        clearTimeout(timeout);
      }
    })().finally(() => { healthProbe = undefined; });
  }
  return healthProbe;
}

async function recoverClient(clientId) {
  if (!clientId || recovering.has(clientId)) return;
  recovering.add(clientId);
  try {
    let client = await self.clients.get(clientId);
    if (!client || client.type !== 'window') return;
    let url = new URL(client.url);
    if (url.origin !== self.location.origin || url.pathname === recoveryPath) return;
    if (!await accessExpired()) return; // An unrelated API 401 must not restart a healthy session.
    client = await self.clients.get(clientId);
    if (!client) return;
    url = new URL(client.url);
    if (url.origin === self.location.origin && url.pathname !== recoveryPath) {
      await client.navigate(new URL(recoveryPath, self.location.origin).href);
    }
  } finally {
    recovering.delete(clientId);
  }
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(new Request(event.request, { cache: 'no-store' })));
    return;
  }
  if (url.pathname !== '/api' && !url.pathname.startsWith('/api/')) return;
  const headers = new Headers(event.request.headers);
  headers.set('X-Requested-With', 'XMLHttpRequest');
  const response = fetch(new Request(event.request, { headers }));
  event.respondWith(response);
  event.waitUntil(response.then((result) => {
    if (result.status === 401 || result.type === 'opaqueredirect') return recoverClient(event.clientId);
  }).catch(() => {}));
});
