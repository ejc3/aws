import { test } from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { PassThrough } from 'node:stream';
import { WebSocket } from 'ws';
import { generateKeyPair, SignJWT } from 'jose';
import { createAuthorizer } from '../lib/auth.mjs';
import { createCdpMux, startCdpBridge, REFUSED_METHODS, IGNORED_METHODS } from '../lib/cdp-bridge.mjs';
import { createHttpServer } from '../lib/http.mjs';

// A fake browser on the other end of the pipe: records what reaches it and answers every command with { echo: id }.
function fakeBrowser() {
  const toBrowser = new PassThrough();
  const fromBrowser = new PassThrough();
  const seen = [];
  let buffer = '';
  toBrowser.on('data', (part) => {
    buffer += part.toString('utf8');
    let end;
    while ((end = buffer.indexOf('\0')) !== -1) {
      const message = JSON.parse(buffer.slice(0, end));
      buffer = buffer.slice(end + 1);
      seen.push(message);
      fromBrowser.write(`${JSON.stringify({ id: message.id, result: { echo: message.method } })}\0`);
    }
  });
  const event = (message) => fromBrowser.write(`${JSON.stringify(message)}\0`);
  return { writer: toBrowser, reader: fromBrowser, seen, event };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

test('two clients may use the same ids: each reply returns with its own id to its own sender', async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  const a = [], b = [];
  const ca = mux.attach((m) => a.push(m));
  const cb = mux.attach((m) => b.push(m));
  ca.command({ id: 1, method: 'Target.getTargets' });
  cb.command({ id: 1, method: 'Browser.getVersion' });
  mux.send('Page.enable', {}, 'S1'); // the desktop's own command: its reply goes nowhere
  await tick(); await tick();
  const pipeIds = fake.seen.map((m) => m.id);
  assert.equal(new Set(pipeIds).size, 3, 'ids on the pipe are unique');
  assert.deepEqual(a, [{ id: 1, result: { echo: 'Target.getTargets' } }]);
  assert.deepEqual(b, [{ id: 1, result: { echo: 'Browser.getVersion' } }]);
  assert.equal(fake.seen[2].sessionId, 'S1');
});

test('events reach every client and the desktop listener', async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  const a = [], b = [], own = [];
  mux.attach((m) => a.push(m));
  mux.attach((m) => b.push(m));
  mux.onEvent((m) => own.push(m));
  fake.event({ method: 'Target.attachedToTarget', params: { sessionId: 'S9' } });
  await tick();
  for (const list of [a, b, own]) assert.equal(list[0].method, 'Target.attachedToTarget');
});

test('commands that would end the live browser are refused and never reach it', async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  const got = [];
  const client = mux.attach((m) => got.push(m));
  for (const method of REFUSED_METHODS) client.command({ id: 7, method });
  client.command({ id: 8, method: 'Target.createTarget', params: { url: 'about:blank' } });
  await tick(); await tick();
  assert.deepEqual(fake.seen.map((m) => m.method), ['Target.createTarget']);
  assert.equal(got.filter((m) => m.error?.message?.includes('refused')).length, REFUSED_METHODS.size);
  client.command({ method: 'No.id' });
  await tick();
  assert.equal(got.at(-1).error.code, -32600);
});

test('settings that would outlive the client are acknowledged but never reach the browser', async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  const got = [];
  const client = mux.attach((m) => got.push(m));
  client.command({ id: 3, method: 'Browser.setDownloadBehavior', params: { behavior: 'allowAndName', downloadPath: '/tmp/playwright-artifacts' } });
  client.command({ id: 4, method: 'Page.setDownloadBehavior', sessionId: 'S2', params: { behavior: 'deny' } });
  await tick(); await tick();
  assert.equal(IGNORED_METHODS.size, 2);
  assert.deepEqual(fake.seen, []);
  assert.deepEqual(got, [{ id: 3, result: {} }, { id: 4, sessionId: 'S2', result: {} }]);
});

test("a client cannot replace the desktop's own auto-attach setting", async () => {
  const fake = fakeBrowser();
  const mux = createCdpMux(fake.writer, fake.reader);
  const own = { autoAttach: true, waitForDebuggerOnStart: true, flatten: true };
  mux.send('Target.setAutoAttach', own);
  const got = [];
  const client = mux.attach((m) => got.push(m));
  client.command({ id: 1, method: 'Target.setAutoAttach', params: { flatten: true, waitForDebuggerOnStart: true, autoAttach: true } });
  client.command({ id: 2, method: 'Target.setAutoAttach', params: { autoAttach: false, waitForDebuggerOnStart: false } });
  client.command({ id: 3, method: 'Target.setAutoAttach', params: { ...own, filter: [{ type: 'iframe' }] } });
  client.command({ id: 4, method: 'Target.setAutoAttach', sessionId: 'S5', params: { autoAttach: false, waitForDebuggerOnStart: false } });
  await tick(); await tick();
  const forwarded = fake.seen.filter((m) => m.method === 'Target.setAutoAttach');
  assert.equal(forwarded.length, 3, "the desktop's own call, the identical client call, and the session-scoped one");
  assert.deepEqual(forwarded.slice(1).map((m) => [m.params.autoAttach, m.sessionId]), [[true, undefined], [false, 'S5']]);
  assert.deepEqual(got.find((m) => m.id === 2), { id: 2, result: {} });
  assert.deepEqual(got.find((m) => m.id === 3), { id: 3, result: {} });
});

test('a detached client gets nothing more, and its pending replies are dropped', async () => {
  const fake = fakeBrowser();
  fake.writer.removeAllListeners('data'); // this browser never answers
  const mux = createCdpMux(fake.writer, fake.reader);
  const got = [];
  const client = mux.attach((m) => got.push(m));
  client.command({ id: 1, method: 'Page.navigate' });
  client.detach();
  fake.event({ method: 'Page.loadEventFired' });
  fake.reader.write(`${JSON.stringify({ id: 1, result: {} })}\0`);
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
