import { test } from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { PassThrough } from 'node:stream';
import { WebSocket } from 'ws';
import { generateKeyPair, SignJWT } from 'jose';
import { createAuthorizer } from '../lib/auth.mjs';
import { createCdpMux, startCdpBridge, REFUSED_METHODS, IGNORED_METHODS } from '../lib/cdp-bridge.mjs';
import { createHttpServer } from '../lib/http.mjs';

// A fake browser on the other end of the pipe: records what reaches it, gives each Target.attachToBrowserTarget a new
// session id, and answers every other command with { echo: method }, carrying the command's session id as Chrome does.
function fakeBrowser() {
  const toBrowser = new PassThrough();
  const fromBrowser = new PassThrough();
  const seen = [];
  let buffer = '';
  let browserSessions = 0;
  toBrowser.on('data', (part) => {
    buffer += part.toString('utf8');
    let end;
    while ((end = buffer.indexOf('\0')) !== -1) {
      const message = JSON.parse(buffer.slice(0, end));
      buffer = buffer.slice(end + 1);
      seen.push(message);
      const result = message.method === 'Target.attachToBrowserTarget' ? { sessionId: `B${++browserSessions}` }
        : message.method === 'Target.attachToTarget' ? { sessionId: `T-${message.params.targetId}` } : { echo: message.method };
      fromBrowser.write(`${JSON.stringify({ id: message.id, result, ...(message.sessionId ? { sessionId: message.sessionId } : {}) })}\0`);
    }
  });
  const event = (message) => fromBrowser.write(`${JSON.stringify(message)}\0`);
  return { writer: toBrowser, reader: fromBrowser, seen, event };
}
const tick = async (n = 3) => { for (let i = 0; i < n; i++) await new Promise((resolve) => setImmediate(resolve)); };
const sent = (fake, method) => fake.seen.filter((m) => m.method === method);

test('each client is a separate connection: its commands run in its own browser session, replies keep its ids', async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  const a = [], b = [];
  const ca = mux.attach((m) => a.push(m));
  const cb = mux.attach((m) => b.push(m));
  ca.command({ id: 1, method: 'Target.getTargets' }); // queued until its session exists
  cb.command({ id: 1, method: 'Browser.getVersion' });
  mux.send('Page.enable', {}, 'S1'); // the desktop's own command: its reply goes nowhere
  await tick();
  assert.equal(new Set(fake.seen.map((m) => m.id)).size, fake.seen.length, 'ids on the pipe are unique');
  assert.equal(sent(fake, 'Target.attachToBrowserTarget').length, 2);
  assert.equal(sent(fake, 'Target.getTargets')[0].sessionId, 'B1');
  assert.equal(sent(fake, 'Browser.getVersion')[0].sessionId, 'B2');
  assert.deepEqual(a, [{ id: 1, result: { echo: 'Target.getTargets' } }]);
  assert.deepEqual(b, [{ id: 1, result: { echo: 'Browser.getVersion' } }]);
});

test("events reach only the session's owner; the desktop never sees a client's", async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  const a = [], b = [], desktop = [];
  mux.onEvent((m) => desktop.push(m));
  mux.attach((m) => a.push(m));
  mux.attach((m) => b.push(m));
  await tick();
  fake.event({ method: 'Target.attachedToTarget', sessionId: 'B1', params: { sessionId: 'P1', targetInfo: { type: 'page' } } });
  fake.event({ method: 'Page.loadEventFired', sessionId: 'P1', params: {} });
  fake.event({ method: 'Target.attachedToTarget', params: { sessionId: 'S9', targetInfo: { type: 'page' } } });
  await tick();
  assert.deepEqual(a.map((m) => [m.method, m.sessionId]), [['Target.attachedToTarget', undefined], ['Page.loadEventFired', 'P1']]);
  assert.deepEqual(b, []);
  assert.deepEqual(desktop.map((m) => m.params.sessionId), ['S9']);
});

test("a client cannot use the desktop's sessions or another client's", async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  const a = [];
  const ca = mux.attach((m) => a.push(m));
  mux.attach(() => {});
  await tick();
  fake.event({ method: 'Target.attachedToTarget', sessionId: 'B2', params: { sessionId: 'P2', targetInfo: { type: 'page' } } });
  await tick();
  ca.command({ id: 5, method: 'Emulation.setFocusEmulationEnabled', sessionId: 'S1', params: { enabled: true } });
  ca.command({ id: 6, method: 'Runtime.evaluate', sessionId: 'P2', params: { expression: '1' } });
  await tick();
  assert.equal(sent(fake, 'Emulation.setFocusEmulationEnabled').length + sent(fake, 'Runtime.evaluate').length, 0);
  assert.deepEqual(a.map((m) => m.error?.code), [-32001, -32001]);
});

test('commands that would end or reconfigure the live browser never reach it', async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  const got = [];
  const client = mux.attach((m) => got.push(m));
  await tick();
  for (const method of REFUSED_METHODS) client.command({ id: 7, method });
  client.command({ id: 3, method: 'Browser.setDownloadBehavior', params: { behavior: 'allowAndName', downloadPath: '/tmp/playwright-artifacts' } });
  client.command({ id: 8, method: 'Target.createTarget', params: { url: 'about:blank' } });
  await tick();
  assert.deepEqual(fake.seen.map((m) => m.method), ['Target.attachToBrowserTarget', 'Target.createTarget']);
  assert.equal(got.filter((m) => m.error?.message?.includes('refused')).length, REFUSED_METHODS.size);
  assert.deepEqual(got.find((m) => m.id === 3), { id: 3, result: {} });
  assert.equal(IGNORED_METHODS.size, 2);
  client.command({ method: 'No.id' });
  await tick();
  assert.equal(got.at(-1).error.code, -32600);
});

test("a client's auto-attach is scoped to its own session, never the desktop's connection", async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  mux.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
  const client = mux.attach(() => {});
  client.command({ id: 1, method: 'Target.setAutoAttach', params: { autoAttach: false, waitForDebuggerOnStart: false } });
  await tick();
  assert.deepEqual(sent(fake, 'Target.setAutoAttach').map((m) => m.sessionId), [undefined, 'B1']);
});

test('detaching a client detaches every session it owned, so nothing it set outlives it', async () => {
  const fake = fakeBrowser();
  fake.writer.removeAllListeners('data');
  const mux = createCdpMux(fake.writer, fake.reader);
  const got = [];
  let buffer = '';
  const answered = [];
  fake.writer.on('data', (part) => {
    buffer += part.toString('utf8');
    let end;
    while ((end = buffer.indexOf('\0')) !== -1) {
      const message = JSON.parse(buffer.slice(0, end));
      buffer = buffer.slice(end + 1);
      answered.push(message);
      // Answers only the session request: the client's command stays pending.
      if (message.method === 'Target.attachToBrowserTarget') fake.reader.write(`${JSON.stringify({ id: message.id, result: { sessionId: 'B1' } })}\0`);
    }
  });
  const client = mux.attach((m) => got.push(m));
  await tick();
  fake.event({ method: 'Target.attachedToTarget', sessionId: 'B1', params: { sessionId: 'P1', targetInfo: { type: 'page' } } });
  await tick();
  client.command({ id: 1, method: 'Emulation.setFocusEmulationEnabled', sessionId: 'P1', params: { enabled: true } });
  await tick();
  got.length = 0;
  client.detach();
  await tick();
  const detaches = answered.filter((m) => m.method === 'Target.detachFromTarget');
  assert.deepEqual(detaches.map((m) => [m.params.sessionId, m.sessionId]), [['P1', 'B1'], ['B1', undefined]]);
  fake.event({ method: 'Page.loadEventFired', sessionId: 'P1', params: {} });
  const pendingId = answered.find((m) => m.method === 'Emulation.setFocusEmulationEnabled').id;
  fake.reader.write(`${JSON.stringify({ id: pendingId, result: {} })}\0`);
  await tick();
  assert.deepEqual(got, []);
});

test('the bridge listens on loopback behind a token, relays commands, and closes its clients', async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  const bridge = await startCdpBridge(mux);
  try {
    const url = new URL(bridge.wsEndpoint);
    assert.equal(url.hostname, '127.0.0.1');
    assert.match(url.pathname, /^\/devtools\/browser\/[0-9a-f]{48}$/);
    // The wrong token is refused before any WebSocket exists.
    const wrong = new WebSocket(`ws://127.0.0.1:${url.port}/devtools/browser/${'0'.repeat(48)}`);
    await assert.rejects(once(wrong, 'open'));
    const ws = new WebSocket(bridge.wsEndpoint);
    await once(ws, 'open');
    ws.send(JSON.stringify({ id: 42, method: 'Browser.getVersion' }));
    const [data] = await once(ws, 'message');
    assert.deepEqual(JSON.parse(data.toString()), { id: 42, result: { echo: 'Browser.getVersion' } });
    ws.send(JSON.stringify({ id: 43, method: 'Browser.close' }));
    const [refused] = await once(ws, 'message');
    assert.match(JSON.parse(refused.toString()).error.message, /refused/);
    assert.equal(fake.seen.filter((m) => m.method === 'Browser.close').length, 0);
    const closed = once(ws, 'close');
    await bridge.close();
    await closed;
  } finally {
    await bridge.close().catch(() => {});
  }
});

test('the CDP route exists only on the local control socket, never on the web listener', async () => {
  const keys = await generateKeyPair('RS256');
  const config = { baseUrl: 'https://browsers.example.test', issuer: 'https://team.cloudflareaccess.com', audience: 'browser-audience', serviceTokenId: '', owner: 'owner@example.com' };
  const token = await new SignJWT({ email: config.owner }).setProtectedHeader({ alg: 'RS256' })
    .setIssuedAt().setSubject('owner').setIssuer(config.issuer).setAudience(config.audience)
    .setExpirationTime('5m').sign(keys.privateKey);
  const calls = [];
  const manager = { openCdpBridge: async (name) => { calls.push(name); return { wsEndpoint: 'ws://127.0.0.1:1/devtools/browser/x' }; } };
  const bind = async (server) => { server.listen(0, '127.0.0.1'); await once(server, 'listening'); return `http://127.0.0.1:${server.address().port}`; };
  const web = createHttpServer({ manager, config, authorize: createAuthorizer(config, keys.publicKey), nextHandler: (_req, res) => res.end() });
  const local = createHttpServer({ manager, config, local: true });
  const webBase = await bind(web);
  const localBase = await bind(local);
  try {
    const headers = { 'cf-access-jwt-assertion': token, Origin: config.baseUrl, 'Content-Type': 'application/json' };
    assert.equal((await fetch(`${webBase}/api/browsers/alpha/cdp`, { method: 'POST', headers, body: '{}' })).status, 404);
    assert.deepEqual(calls, []);
    const ok = await fetch(`${localBase}/api/browsers/alpha/cdp`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    assert.equal(ok.status, 200);
    assert.deepEqual(await ok.json(), { wsEndpoint: 'ws://127.0.0.1:1/devtools/browser/x' });
    assert.equal((await fetch(`${localBase}/api/browsers/alpha/cdp`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"x":1}' })).status, 400);
    assert.deepEqual(calls, ['alpha']);
  } finally {
    for (const server of [web, local]) { server.closeAllConnections?.(); await new Promise((done) => server.close(done)); }
  }
});
