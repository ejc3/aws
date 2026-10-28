#!/usr/bin/env node
// Offline regression tests. All requests and identities are synthetic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'nextjs-ct/ct/opencode-web-service-worker.js'), 'utf8');
const origin = 'https://opencode.example.test';

function fixture(network, clientPath = '/') {
  const handlers = {};
  const calls = [];
  const navigations = [];
  const deletedCaches = [];
  let claims = 0;
  const self = {
    registration: { scope: origin + '/' }, location: { origin },
    skipWaiting: async () => {},
    clients: {
      claim: async () => { claims++; },
      get: async (id) => id === 'current' ? {
        type: 'window', url: origin + clientPath,
        navigate: async (url) => { navigations.push(url); },
      } : null,
    },
    addEventListener: (type, handler) => { handlers[type] = handler; },
  };
  vm.runInNewContext(source, {
    self, URL, Request, Headers, AbortController, setTimeout, clearTimeout,
    caches: { delete: async (name) => { deletedCaches.push(name); } },
    fetch: async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      calls.push(request);
      return network(request);
    },
  });
  return {
    calls, navigations, deletedCaches, claims: () => claims,
    lifecycle: async (type) => {
      let pending;
      handlers[type]({ waitUntil: (work) => { pending = work; } });
      await pending;
    },
    request: async (request) => {
      let response;
      const pending = [];
      handlers.fetch({ request, clientId: 'current',
        respondWith: (work) => { response = work; },
        waitUntil: (work) => { pending.push(work); },
      });
      let result, failure;
      try { result = await response; } catch (error) { failure = error; }
      await Promise.all(pending);
      if (failure) throw failure;
      return result;
    },
  };
}

(async () => {
  const life = fixture(() => new Response('ok'));
  await life.lifecycle('install');
  await life.lifecycle('activate');
  assert.deepEqual(life.deletedCaches, ['workbox-precache-v2-' + origin + '/']);
  assert.equal(life.claims(), 1);
  assert.deepEqual(life.navigations, [], 'activation must not reload another tab');

  const stream = new Response(new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode('data: connected\n\n')); } }));
  const streaming = fixture(() => stream);
  const streamed = await streaming.request(new Request(origin + '/api/event'));
  assert.equal(streamed, stream, 'preserve the original streaming response without reading its body');
  assert.equal(stream.bodyUsed, false);
  await stream.body.cancel();

  const posting = fixture(async (request) => {
    assert.equal(request.method, 'POST');
    assert.equal(await request.text(), 'keep this request body');
    assert.equal(request.headers.get('X-Preserve'), 'yes');
    assert.equal(request.headers.get('X-Requested-With'), 'XMLHttpRequest');
    assert.equal(request.credentials, 'include');
    return new Response('ok');
  });
  await posting.request(new Request(origin + '/api/session', {
    method: 'POST', headers: { 'X-Preserve': 'yes' }, body: 'keep this request body', credentials: 'include',
  }));

  const nav = fixture(() => new Response('network HTML'));
  const navigation = new Request(origin + '/');
  Object.defineProperty(navigation, 'mode', { value: 'navigate' });
  await nav.request(navigation);
  assert.equal(nav.calls[0].cache, 'no-store');

  const foreign = fixture(() => { throw Error('foreign request must not be intercepted'); });
  assert.equal(await foreign.request(new Request('https://other.example.test/api/info')), undefined);
  assert.equal(foreign.calls.length, 0);

  const provider = fixture((request) => new Response('{}', { status: new URL(request.url).pathname === '/api/info' ? 200 : 401 }));
  const denied = await provider.request(new Request(origin + '/api/provider'));
  assert.equal(denied.status, 401);
  assert.deepEqual(provider.navigations, [], 'a provider error is not an expired Access login');

  const expired = fixture(() => new Response('{}', { status: 401 }));
  await expired.request(new Request(origin + '/api/session'));
  assert.deepEqual(expired.navigations, [origin + '/auth/access-refresh']);

  const recovering = fixture(() => new Response('{}', { status: 401 }), '/auth/access-refresh');
  await recovering.request(new Request(origin + '/api/info'));
  assert.deepEqual(recovering.navigations, [], 'recovery must show its error instead of redirecting itself');
  assert.equal(recovering.calls.length, 1);

  const offline = fixture(() => { throw new TypeError('synthetic offline'); });
  await assert.rejects(offline.request(new Request(origin + '/api/info')), /synthetic offline/);
  assert.deepEqual(offline.navigations, []);
  console.log('OpenCode Access worker regression checks passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
