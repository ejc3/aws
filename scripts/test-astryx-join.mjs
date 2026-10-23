// Offline tests for scripts/astryx-join.mjs: node --test scripts/test-astryx-join.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, verify as cryptoVerify, createPublicKey } from 'node:crypto';
import * as mod from './astryx-join.mjs';
import worker, {
  metaEmail, verifyTurnstile, kvLimits, ascTokens, ascClient, inviteTester, makeHandler, sameOrigin, resetForTests,
} from './astryx-join.mjs';

// The Worker's main module may export only handlers and functions (workerd refuses any other named export at startup),
// so its constants are restated here.
const ASC_ORIGIN = 'https://api.appstoreconnect.apple.com';
const SITEVERIFY = 'https://challenges.cloudflare.com/turnstile/v0/siteverify';

const GROUP = '00000000-1111-2222-3333-444444444444';
const SITE_KEY = '0x4AAAAAAAexample-site-key';
const ORIGIN = 'https://join.example.com';
const HOST = 'join.example.com';
const NOW = 1_800_000_000;
// Addresses are assembled from parts, so this file holds no address literal (scripts/test-people.py).
const at = (local, domain) => [local, domain].join('@');
const EMAIL = at('tester', 'meta.com');

const ec = generateKeyPairSync('ec', { namedCurve: 'P-256' });
const EC_PEM = ec.privateKey.export({ format: 'pem', type: 'pkcs8' });

// ---------------------------------------------------------------------------------------------------------------------
// The address
// ---------------------------------------------------------------------------------------------------------------------
test('only an address at exactly meta.com passes, trimmed and lower-cased', () => {
  assert.equal(metaEmail(EMAIL), EMAIL);
  assert.equal(metaEmail(at('First.Last', 'Meta.COM')), at('first.last', 'meta.com'));
  assert.equal(metaEmail(`  ${EMAIL}\n`), EMAIL);
  assert.equal(metaEmail(at("o'brien+tf", 'meta.com')), at("o'brien+tf", 'meta.com'));
  for (const bad of [at('tester', 'example.com'), at('tester', 'sub.meta.com'), at('tester', 'meta.com.example.com'), at('tester', 'fakemeta.com'),
    '@meta.com', at('a@b', 'meta.com'), at('tes ter', 'meta.com'), at('tester', 'meta.co'), at('tester', 'meta.comm'),
    at('.tester', 'meta.com'), at('tester.', 'meta.com'), at('te..ster', 'meta.com'), at('te<b>', 'meta.com'),
    `${'a'.repeat(65)}@meta.com`, '', '   ', null, undefined, 42, [EMAIL]]) {
    assert.equal(metaEmail(bad), null, String(bad));
  }
});

// ---------------------------------------------------------------------------------------------------------------------
// Turnstile
// ---------------------------------------------------------------------------------------------------------------------
function siteverify(responses) {
  const calls = [];
  const fetchFn = async (u, init) => {
    calls.push({ url: u, form: Object.fromEntries(new URLSearchParams(init.body)) });
    const r = responses[Math.min(calls.length - 1, responses.length - 1)];
    if (r instanceof Error) throw r;
    return new Response(JSON.stringify(r.body ?? {}), { status: r.status ?? 200 });
  };
  return { fetchFn, calls };
}
const turnstileArgs = (fetchFn, over = {}) => ({ token: 'tok', ip: '192.0.2.7', secret: 'sec', hostname: HOST, fetchFn, idempotencyKey: 'idem-1', ...over });

test('Turnstile: success for this hostname passes, sending the secret, the token, the IP and the idempotency key', async () => {
  const sv = siteverify([{ body: { success: true, hostname: HOST } }]);
  assert.deepEqual(await verifyTurnstile(turnstileArgs(sv.fetchFn)), { ok: true, codes: [] });
  assert.equal(sv.calls[0].url, SITEVERIFY);
  assert.deepEqual(sv.calls[0].form, { secret: 'sec', response: 'tok', idempotency_key: 'idem-1', remoteip: '192.0.2.7' });
});

test('Turnstile: failure, a missing or oversized token, another hostname, and an unreachable siteverify all refuse', async () => {
  const fail = siteverify([{ body: { success: false, 'error-codes': ['invalid-input-response', 'free text with spaces'] } }]);
  assert.deepEqual(await verifyTurnstile(turnstileArgs(fail.fetchFn)), { ok: false, codes: ['invalid-input-response'] });
  const never = siteverify([{ body: { success: true, hostname: HOST } }]);
  assert.deepEqual(await verifyTurnstile(turnstileArgs(never.fetchFn, { token: '' })), { ok: false, codes: ['missing-input-response'] });
  assert.deepEqual(await verifyTurnstile(turnstileArgs(never.fetchFn, { token: null })), { ok: false, codes: ['missing-input-response'] });
  assert.equal((await verifyTurnstile(turnstileArgs(never.fetchFn, { token: 'x'.repeat(2049) }))).ok, false);
  assert.equal(never.calls.length, 0); // nothing sent for a missing token
  const other = siteverify([{ body: { success: true, hostname: 'elsewhere.example.com' } }]);
  assert.deepEqual(await verifyTurnstile(turnstileArgs(other.fetchFn)), { ok: false, codes: ['wrong-hostname'] });
  const down = siteverify([new Error('connect'), { status: 503 }]);
  assert.deepEqual(await verifyTurnstile(turnstileArgs(down.fetchFn)), { ok: false, codes: ['siteverify-unavailable'] });
  assert.equal(down.calls.length, 2);
});

test('Turnstile: one retry with the same idempotency key', async () => {
  const sv = siteverify([{ status: 502 }, { body: { success: true, hostname: HOST } }]);
  assert.equal((await verifyTurnstile(turnstileArgs(sv.fetchFn))).ok, true);
  assert.equal(sv.calls.length, 2);
  assert.equal(sv.calls[0].form.idempotency_key, sv.calls[1].form.idempotency_key);
});

// ---------------------------------------------------------------------------------------------------------------------
// The limits
// ---------------------------------------------------------------------------------------------------------------------
function memoryKv() {
  const m = new Map();
  return {
    m,
    get: async (k) => (m.has(k) ? m.get(k).value : null),
    put: async (k, value, opts) => { m.set(k, { value, ttl: opts?.expirationTtl }); },
  };
}

test('per IP: five an hour, then refused until the next hour; another IP is not affected', async () => {
  const kv = memoryKv();
  let t = NOW * 1000;
  const take = kvLimits({ kv, nowMs: () => t });
  for (let i = 0; i < 5; i++) assert.equal(await take('192.0.2.7'), 'ok');
  assert.equal(await take('192.0.2.7'), 'ip');
  assert.equal(await take('198.51.100.9'), 'ok');
  t += 3_600_000;
  assert.equal(await take('192.0.2.7'), 'ok');
  // The IP is kept only as a hash, and every key expires.
  for (const [k, v] of kv.m) {
    assert.doesNotMatch(k, /192\.0\.2\.7|198\.51/);
    assert.ok(v.ttl >= 60, k);
  }
});

test('daily: 500 a day across every IP, then refused until the next UTC day', async () => {
  const kv = memoryKv();
  let t = Date.UTC(2026, 9, 23, 12) ;
  const take = kvLimits({ kv, nowMs: () => t });
  for (let i = 0; i < 500; i++) assert.equal(await take(`10.0.${i >> 8}.${i & 255}`), 'ok');
  assert.equal(await take('203.0.113.1'), 'daily');
  t = Date.UTC(2026, 9, 24, 0, 0, 1);
  assert.equal(await take('203.0.113.1'), 'ok');
});

test('a KV failure refuses rather than skipping the limits', async () => {
  const take = kvLimits({ kv: { get: async () => { throw new Error('kv down'); }, put: async () => {} }, nowMs: () => NOW * 1000 });
  assert.equal(await take('192.0.2.7'), 'unavailable');
  const noWrite = kvLimits({ kv: { get: async () => null, put: async () => { throw new Error('429'); } }, nowMs: () => NOW * 1000 });
  assert.equal(await noWrite('192.0.2.7'), 'unavailable');
});

// ---------------------------------------------------------------------------------------------------------------------
// The App Store Connect token
// ---------------------------------------------------------------------------------------------------------------------
test('the App Store Connect token is ES256 by the key, with the issuer, kid, audience and a 20-minute life', async () => {
  let t = NOW;
  const tokens = ascTokens({ issuerId: 'issuer-uuid', keyId: 'KEYID12345', privateKeyPem: EC_PEM, now: () => t });
  const token = await tokens();
  const [h, p, s] = token.split('.');
  assert.deepEqual(JSON.parse(Buffer.from(h, 'base64url')), { alg: 'ES256', kid: 'KEYID12345', typ: 'JWT' });
  assert.deepEqual(JSON.parse(Buffer.from(p, 'base64url')), { iss: 'issuer-uuid', iat: NOW, exp: NOW + 1200, aud: 'appstoreconnect-v1' });
  const sig = Buffer.from(s, 'base64url');
  assert.equal(sig.length, 64); // r||s, as JWS wants, not DER
  assert.ok(cryptoVerify('sha256', Buffer.from(`${h}.${p}`), { key: createPublicKey(ec.privateKey), dsaEncoding: 'ieee-p1363' }, sig));
  t += 14 * 60;
  assert.equal(await tokens(), token); // reused while more than five minutes remain
  t += 2 * 60;
  const fresh = await tokens();
  assert.notEqual(fresh, token);
  assert.equal(JSON.parse(Buffer.from(fresh.split('.')[1], 'base64url')).iat, t);
});

test('the client sends the bearer token and JSON, and turns a network failure into status 0', async () => {
  const seen = [];
  const call = ascClient({
    fetchFn: async (u, init) => { seen.push([u, init]); return new Response('{"data":[]}', { status: 200 }); },
    token: async () => 'tok',
  });
  assert.deepEqual(await call('POST', '/v1/x', { a: 1 }), { status: 200, body: { data: [] } });
  assert.equal(seen[0][0], `${ASC_ORIGIN}/v1/x`);
  assert.equal(seen[0][1].headers.authorization, 'Bearer tok');
  assert.equal(seen[0][1].body, '{"a":1}');
  const down = ascClient({ fetchFn: async () => { throw new Error('connect refused'); }, token: async () => 'tok' });
  assert.deepEqual(await down('GET', '/v1/x'), { status: 0, body: null });
  const html = ascClient({ fetchFn: async () => new Response('<html>', { status: 503 }), token: async () => 'tok' });
  assert.deepEqual(await html('GET', '/v1/x'), { status: 503, body: null });
});

// ---------------------------------------------------------------------------------------------------------------------
// The invitation, against a mock App Store Connect
// ---------------------------------------------------------------------------------------------------------------------
const conflict = (detail, code = 'ENTITY_ERROR.ATTRIBUTE.INVALID') => ({ status: 409, body: { errors: [{ status: '409', code, title: 'The request could not be completed', detail }] } });

function mockAsc(routes) {
  const calls = [];
  const call = async (method, path, body) => {
    calls.push({ method, path, body });
    for (const [m, re, res] of routes) if (m === method && re.test(path)) return typeof res === 'function' ? res(path, body) : res;
    throw new Error(`unexpected ${method} ${path}`);
  };
  return { call, calls };
}

test('a new address is created as a tester in the group: invited', async () => {
  const asc = mockAsc([['POST', /^\/v1\/betaTesters$/, { status: 201, body: { data: { id: 't1' } } }]]);
  assert.deepEqual(await inviteTester({ email: EMAIL, groupId: GROUP, call: asc.call }), { outcome: 'invited', status: 201, code: null });
  assert.equal(asc.calls.length, 1);
  assert.deepEqual(asc.calls[0].body, {
    data: { type: 'betaTesters', attributes: { email: EMAIL }, relationships: { betaGroups: { data: [{ type: 'betaGroups', id: GROUP }] } } },
  });
});

test('an address already in the group: already, and nothing is written', async () => {
  const asc = mockAsc([
    ['POST', /^\/v1\/betaTesters$/, conflict('A tester with this email already exists')],
    ['GET', /filter\[betaGroups\]=/, { status: 200, body: { data: [{ type: 'betaTesters', id: 't1' }] } }],
  ]);
  assert.deepEqual(await inviteTester({ email: at('o+x', 'meta.com'), groupId: GROUP, call: asc.call }), { outcome: 'already', status: 200, code: null });
  assert.equal(asc.calls[1].path, `/v1/betaTesters?filter[email]=o%2Bx%40meta.com&filter[betaGroups]=${GROUP}&fields[betaTesters]=email&limit=1`);
  assert.equal(asc.calls.filter((c) => c.method === 'POST').length, 1);
});

test('a tester of the app outside the group (409) is added to the group: invited', async () => {
  const asc = mockAsc([
    ['POST', /^\/v1\/betaTesters$/, conflict('A tester with this email already exists')],
    ['GET', /filter\[betaGroups\]=/, { status: 200, body: { data: [] } }],
    ['GET', /^\/v1\/betaTesters\?filter\[email\]=/, { status: 200, body: { data: [{ type: 'betaTesters', id: 'existing-1' }] } }],
    ['POST', /^\/v1\/betaGroups\/.+\/relationships\/betaTesters$/, { status: 204, body: null }],
  ]);
  assert.deepEqual(await inviteTester({ email: EMAIL, groupId: GROUP, call: asc.call }), { outcome: 'invited', status: 204, code: null });
  const add = asc.calls.at(-1);
  assert.equal(add.path, `/v1/betaGroups/${GROUP}/relationships/betaTesters`);
  assert.deepEqual(add.body, { data: [{ type: 'betaTesters', id: 'existing-1' }] });
});

test("Apple's 10,000-tester limit: limit, on create or on adding to the group", async () => {
  const full = conflict('The maximum number of testers has been reached for this app.', 'ENTITY_ERROR.RELATIONSHIP.INVALID');
  const onCreate = mockAsc([['POST', /^\/v1\/betaTesters$/, full]]);
  assert.equal((await inviteTester({ email: EMAIL, groupId: GROUP, call: onCreate.call })).outcome, 'limit');
  assert.equal(onCreate.calls.length, 1);
  const limitText = mockAsc([['POST', /^\/v1\/betaTesters$/, { status: 422, body: { errors: [{ code: 'STATE_ERROR', detail: 'You have reached the limit of 10,000 testers.' }] } }]]);
  assert.equal((await inviteTester({ email: EMAIL, groupId: GROUP, call: limitText.call })).outcome, 'limit');
  const onAdd = mockAsc([
    ['POST', /^\/v1\/betaTesters$/, conflict('A tester with this email already exists')],
    ['GET', /filter\[betaGroups\]=/, { status: 200, body: { data: [] } }],
    ['GET', /^\/v1\/betaTesters\?filter\[email\]=/, { status: 200, body: { data: [{ id: 'existing-1' }] } }],
    ['POST', /relationships\/betaTesters$/, full],
  ]);
  assert.equal((await inviteTester({ email: EMAIL, groupId: GROUP, call: onAdd.call })).outcome, 'limit');
});

test('errors are errors, with the status and only a well-formed Apple error code', async () => {
  const cases = [
    [{ status: 401, body: { errors: [{ code: 'NOT_AUTHORIZED', detail: 'token expired' }] } }, { outcome: 'error', status: 401, code: 'NOT_AUTHORIZED' }],
    [{ status: 403, body: { errors: [{ code: 'FORBIDDEN_ERROR', detail: 'limit of the role' }] } }, { outcome: 'error', status: 403, code: 'FORBIDDEN_ERROR' }],
    [{ status: 429, body: { errors: [{ code: 'RATE_LIMIT_EXCEEDED', detail: 'rate limit' }] } }, { outcome: 'error', status: 429, code: 'RATE_LIMIT_EXCEEDED' }],
    [{ status: 500, body: null }, { outcome: 'error', status: 500, code: null }],
    [{ status: 0, body: null }, { outcome: 'error', status: 0, code: null }],
    [{ status: 400, body: { errors: [{ code: at('tester', 'meta.com is bad') }] } }, { outcome: 'error', status: 400, code: null }],
  ];
  for (const [res, want] of cases) {
    const asc = mockAsc([['POST', /^\/v1\/betaTesters$/, res]]);
    assert.deepEqual(await inviteTester({ email: EMAIL, groupId: GROUP, call: asc.call }), want);
  }
  // A 409 whose tester cannot then be found, and a failing lookup, are errors too.
  const ghost = mockAsc([
    ['POST', /^\/v1\/betaTesters$/, conflict('A tester with this email already exists')],
    ['GET', /filter\[betaGroups\]=/, { status: 200, body: { data: [] } }],
    ['GET', /^\/v1\/betaTesters\?filter\[email\]=/, { status: 200, body: { data: [] } }],
  ]);
  assert.equal((await inviteTester({ email: EMAIL, groupId: GROUP, call: ghost.call })).outcome, 'error');
  const lookupDown = mockAsc([
    ['POST', /^\/v1\/betaTesters$/, conflict('exists')],
    ['GET', /filter\[betaGroups\]=/, { status: 503, body: null }],
  ]);
  assert.deepEqual(await inviteTester({ email: EMAIL, groupId: GROUP, call: lookupDown.call }), { outcome: 'error', status: 503, code: null });
  const addFails = mockAsc([
    ['POST', /^\/v1\/betaTesters$/, conflict('exists')],
    ['GET', /filter\[betaGroups\]=/, { status: 200, body: { data: [] } }],
    ['GET', /^\/v1\/betaTesters\?filter\[email\]=/, { status: 200, body: { data: [{ id: 'x' }] } }],
    ['POST', /relationships\/betaTesters$/, { status: 500, body: null }],
  ]);
  assert.equal((await inviteTester({ email: EMAIL, groupId: GROUP, call: addFails.call })).outcome, 'error');
});


// ---------------------------------------------------------------------------------------------------------------------
// The handler: the form, Turnstile first, the domain, the limits, the outcomes, and no address in any log line
// ---------------------------------------------------------------------------------------------------------------------
const allLogs = [];
function harness({ routes = [['POST', /^\/v1\/betaTesters$/, { status: 201, body: {} }]], config = {}, human = true, limit = 'ok' } = {}) {
  const asc = mockAsc(routes);
  const logs = [];
  const order = [];
  const handler = makeHandler({
    config: { siteKey: SITE_KEY, groupId: GROUP, ...config },
    turnstile: async (args) => { order.push(['turnstile', args]); return human ? { ok: true, codes: [] } : { ok: false, codes: ['invalid-input-response'] }; },
    limits: async (ip) => { order.push(['limits', ip]); return typeof limit === 'function' ? limit(ip) : limit; },
    call: async (...a) => { order.push(['asc']); return asc.call(...a); },
    log: (o) => { const line = JSON.stringify(o); logs.push(line); allLogs.push(line); },
  });
  return { handler, asc, logs, order };
}
const FORM = 'application/x-www-form-urlencoded';
function post({ email = EMAIL, token = 'tok', headers = {}, body } = {}) {
  const b = body ?? new URLSearchParams({ email, ...(token === null ? {} : { 'cf-turnstile-response': token }) }).toString();
  return new Request(`${ORIGIN}/join`, { method: 'POST', body: b, headers: { origin: ORIGIN, 'content-type': FORM, 'cf-connecting-ip': '192.0.2.7', ...headers } });
}
const get = (path) => new Request(`${ORIGIN}${path}`);

test('GET / is the form: the labelled email field, the Turnstile widget, one button, and why only @meta.com', async () => {
  const { handler, asc } = harness();
  const res = await handler(get('/'));
  assert.equal(res.status, 200);
  const html = await res.text();
  assert.match(html, /<label for="email">Your @meta\.com email<\/label>/);
  const input = html.match(/<input [^>]*>/)[0];
  for (const attr of ['id="email"', 'name="email"', 'type="email"', 'autocomplete="email"', 'inputmode="email"', 'required']) assert.ok(input.includes(attr), attr);
  assert.match(html, new RegExp(`<div class="cf-turnstile" data-sitekey="${SITE_KEY}"`));
  assert.match(html, /<script src="https:\/\/challenges\.cloudflare\.com\/turnstile\/v0\/api\.js" async defer><\/script>/);
  assert.equal((html.match(/<button/g) || []).length, 1);
  assert.match(html, /<form method="post" action="\/join">[\s\S]*<button type="submit">Send me the TestFlight invite<\/button>\s*<\/form>/);
  assert.match(html, /only @meta\.com addresses can join/);
  assert.match(html, /TestFlight emails an invitation to this address\. Install the TestFlight app on your iPhone or Mac/);
  assert.match(html, /prefers-color-scheme:dark/);
  const csp = res.headers.get('content-security-policy');
  assert.match(csp, /script-src https:\/\/challenges\.cloudflare\.com;/);
  assert.match(csp, /frame-src https:\/\/challenges\.cloudflare\.com;/);
  assert.match(csp, /frame-ancestors 'none'/);
  assert.match(csp, /form-action 'self'/);
  assert.equal(res.headers.get('cache-control'), 'no-store');
  assert.equal(asc.calls.length, 0);
});

test('Turnstile runs first: a failed or missing check stops everything, keeps the typed address, and says so', async () => {
  for (const token of [null, '', 'bad']) {
    const { handler, order, asc } = harness({ human: false });
    const res = await handler(post({ token, email: at('someone', 'example.com') }));
    assert.equal(res.status, 403);
    const html = await res.text();
    assert.match(html, /couldn&#39;t confirm the check above/);
    assert.match(html, /value="someone@example\.com"/);
    assert.deepEqual(order.map((o) => o[0]), ['turnstile']); // not the domain, not the limits, not Apple
    assert.equal(asc.calls.length, 0);
  }
  const { handler, order } = harness();
  await handler(post());
  assert.deepEqual(order[0][1], { token: 'tok', ip: '192.0.2.7', hostname: HOST });
});

test('an address outside meta.com is refused in plain words, after Turnstile and before the limits', async () => {
  for (const email of [at('tester', 'example.com'), at('tester', 'sub.meta.com'), 'not an address', '']) {
    const { handler, order, asc } = harness();
    const res = await handler(post({ email }));
    assert.equal(res.status, 400);
    const html = await res.text();
    assert.match(html, /Only @meta\.com addresses can join this beta\. Enter your @meta\.com work address/);
    assert.deepEqual(order.map((o) => o[0]), ['turnstile']);
    assert.equal(asc.calls.length, 0);
  }
  const { handler, asc } = harness();
  assert.match(await (await handler(post({ email: `  ${at('Tester', 'META.com')} ` }))).text(), /Invitation on its way/);
  assert.equal(asc.calls[0].body.data.attributes.email, EMAIL);
});

test('either limit gives a plain try-again-later and calls nobody', async () => {
  for (const limit of ['ip', 'daily', 'unavailable']) {
    const { handler, order, asc } = harness({ limit });
    const res = await handler(post());
    assert.equal(res.status, 429);
    assert.match(await res.text(), /Try again later/);
    assert.deepEqual(order.map((o) => o[0]), ['turnstile', 'limits']);
    assert.equal(order[1][1], '192.0.2.7');
    assert.equal(asc.calls.length, 0);
  }
});

test('the real limits behind the handler: the sixth request from one IP in an hour is refused', async () => {
  const kv = memoryKv();
  const take = kvLimits({ kv, nowMs: () => NOW * 1000 });
  const { handler } = harness({ limit: (ip) => take(ip), routes: [['POST', /^\/v1\/betaTesters$/, { status: 201, body: {} }]] });
  for (let i = 0; i < 5; i++) assert.equal((await handler(post({ email: at(`t${i}`, 'meta.com') }))).status, 200);
  assert.equal((await handler(post({ email: at('t6', 'meta.com') }))).status, 429);
});

test('POST /join must come from this origin, as a small form', async () => {
  const { handler, asc, order } = harness();
  for (const headers of [{ origin: 'https://evil.example.com' }, { origin: 'null' }, { origin: null }, { origin: null, 'sec-fetch-site': 'cross-site' }, { origin: null, 'sec-fetch-site': 'same-site' }]) {
    const h = { ...headers };
    const r = post({ headers: h });
    if (h.origin === null) r.headers.delete('origin');
    assert.equal((await handler(r)).status, 403, JSON.stringify(headers));
  }
  const sameOriginByFetchMetadata = post({ headers: { 'sec-fetch-site': 'same-origin' } });
  sameOriginByFetchMetadata.headers.delete('origin');
  assert.equal((await handler(sameOriginByFetchMetadata)).status, 200);
  assert.equal(sameOrigin(new Request(`${ORIGIN}/join`, { method: 'POST', headers: { origin: ORIGIN } })), true);
  const asJson = await handler(post({ headers: { 'content-type': 'application/json' }, body: '{}' }));
  assert.equal(asJson.status, 400);
  const huge = await handler(post({ body: `email=${'a'.repeat(9000)}` }));
  assert.equal(huge.status, 400);
  assert.equal(asc.calls.length, 1);
  assert.equal(order.filter((o) => o[0] === 'turnstile').length, 1);
});

test('each outcome is said in words', async () => {
  const want = {
    invited: [[['POST', /^\/v1\/betaTesters$/, { status: 201, body: {} }]], 200, /Invitation on its way/],
    already: [[['POST', /^\/v1\/betaTesters$/, conflict('exists')], ['GET', /filter\[betaGroups\]=/, { status: 200, body: { data: [{ id: 't' }] } }]], 200, /already invited[\s\S]*Look for the email from TestFlight/],
    limit: [[['POST', /^\/v1\/betaTesters$/, conflict('The maximum number of testers has been reached')]], 200, /Apple allows 10,000 external testers per app/],
    error: [[['POST', /^\/v1\/betaTesters$/, { status: 500, body: null }]], 502, /Try again in a minute[\s\S]*<form method="post" action="\/join">[\s\S]*class="cf-turnstile"/],
  };
  for (const [outcome, [routes, status, text]] of Object.entries(want)) {
    const { handler, logs } = harness({ routes });
    const res = await handler(post());
    assert.equal(res.status, status, outcome);
    assert.match(await res.text(), text, outcome);
    assert.equal(JSON.parse(logs.at(-1)).outcome, outcome);
  }
});

test('an address is shown escaped wherever it is echoed', async () => {
  const { handler } = harness();
  const html = await (await handler(post({ email: at("O'Brien&Co", 'Meta.com') }))).text();
  assert.match(html, /For <strong>o&#39;brien&amp;co@meta\.com<\/strong>/);
  const refused = await (await harness().handler(post({ email: '"><script>x</script>' }))).text();
  assert.doesNotMatch(refused, /<script>x/);
  assert.match(refused, /value="&quot;&gt;&lt;script&gt;x&lt;\/script&gt;"/);
});

test('asking twice is safe: the second answer is already', async () => {
  const members = new Set();
  const routes = [
    ['POST', /^\/v1\/betaTesters$/, (_p, body) => {
      const e = body.data.attributes.email;
      if (members.has(e)) return conflict('exists');
      members.add(e);
      return { status: 201, body: {} };
    }],
    ['GET', /filter\[betaGroups\]=/, (p) => ({ status: 200, body: { data: members.has(decodeURIComponent(/filter\[email\]=([^&]+)/.exec(p)[1])) ? [{ id: 't' }] : [] } })],
  ];
  const { handler } = harness({ routes });
  assert.match(await (await handler(post())).text(), /Invitation on its way/);
  assert.match(await (await handler(post())).text(), /already invited/);
});

test('other paths and methods do nothing', async () => {
  const { handler, asc, order } = harness();
  const back = await handler(get('/join'));
  assert.equal(back.status, 303);
  assert.equal(back.headers.get('location'), '/');
  assert.equal((await handler(get('/admin'))).status, 404);
  assert.equal((await handler(new Request(`${ORIGIN}/`, { method: 'POST', headers: { origin: ORIGIN } }))).status, 404);
  assert.equal((await handler(new Request(`${ORIGIN}/join`, { method: 'PUT', headers: { origin: ORIGIN } }))).status, 404);
  assert.equal(asc.calls.length, 0);
  assert.equal(order.length, 0);
});

test('a Worker missing its configuration refuses everything', async () => {
  for (const config of [{ groupId: 'not-a-uuid' }, { groupId: `${GROUP}/../x` }, { siteKey: '' }, { siteKey: undefined }]) {
    const { handler, asc, order } = harness({ config });
    assert.equal((await handler(post())).status, 500);
    assert.equal((await handler(get('/'))).status, 500);
    assert.equal(asc.calls.length, 0);
    assert.equal(order.length, 0);
  }
});

// ---------------------------------------------------------------------------------------------------------------------
// The Worker's entry point, wired from its bindings, with fetch and KV stubbed
// ---------------------------------------------------------------------------------------------------------------------
test('the Worker wires its bindings: siteverify with the secret, KV limits, ES256 to App Store Connect, the group', async () => {
  const realFetch = globalThis.fetch;
  const realLog = console.log;
  const seen = [];
  const lines = [];
  globalThis.fetch = async (u, init = {}) => {
    seen.push([String(u), init]);
    if (String(u) === SITEVERIFY) return new Response(JSON.stringify({ success: true, hostname: HOST }), { status: 200 });
    if (String(u) === `${ASC_ORIGIN}/v1/betaTesters`) return new Response('{"data":{"id":"t"}}', { status: 201 });
    return new Response('', { status: 404 });
  };
  console.log = (...a) => lines.push(a.join(' '));
  try {
    resetForTests();
    const kv = memoryKv();
    const env = {
      TURNSTILE_SITE_KEY: SITE_KEY, TURNSTILE_SECRET: 'turnstile-secret', ASC_BETA_GROUP_ID: GROUP,
      ASC_ISSUER_ID: 'issuer-uuid', ASC_KEY_ID: 'KEYID12345', ASC_PRIVATE_KEY: EC_PEM, LIMITS: kv,
    };
    assert.match(await (await worker.fetch(get('/'), env)).text(), new RegExp(SITE_KEY));
    const res = await worker.fetch(post(), env);
    assert.equal(res.status, 200);
    assert.match(await res.text(), /Invitation on its way/);
    const sv = seen.find(([u]) => u === SITEVERIFY);
    const form = Object.fromEntries(new URLSearchParams(sv[1].body));
    assert.equal(form.secret, 'turnstile-secret');
    assert.equal(form.response, 'tok');
    assert.equal(form.remoteip, '192.0.2.7');
    assert.match(form.idempotency_key, /^[0-9a-f-]{36}$/);
    const asc = seen.find(([u]) => u.startsWith(ASC_ORIGIN));
    const [h, p] = asc[1].headers.authorization.replace(/^Bearer /, '').split('.');
    assert.equal(JSON.parse(Buffer.from(h, 'base64url')).kid, 'KEYID12345');
    assert.equal(JSON.parse(Buffer.from(p, 'base64url')).iss, 'issuer-uuid');
    assert.equal(JSON.parse(asc[1].body).data.relationships.betaGroups.data[0].id, GROUP);
    assert.equal(kv.m.size, 2); // one per-IP counter, one daily counter
    assert.equal(lines.length, 1);
    allLogs.push(...lines);
  } finally {
    globalThis.fetch = realFetch;
    console.log = realLog;
    resetForTests();
  }
});

test('the main module exports only functions and the default handler, as workerd requires', () => {
  for (const [name, value] of Object.entries(mod)) {
    if (name === 'default') assert.equal(typeof value.fetch, 'function');
    else assert.equal(typeof value, 'function', name);
  }
});

// Last: every line any handler logged above, across every outcome, refusal and limit, names no address and no IP.
test('no log line carries an address or an IP', () => {
  assert.ok(allLogs.length > 25, String(allLogs.length));
  for (const line of allLogs) {
    assert.doesNotMatch(line, /@/, line);
    assert.doesNotMatch(line, /meta\.com|example\.com/i, line);
    assert.doesNotMatch(line, /192\.0\.2|198\.51\.100|203\.0\.113/, line);
  }
});
