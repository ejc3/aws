// Offline tests for scripts/astryx-join.mjs: node --test scripts/test-astryx-join.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign as cryptoSign, verify as cryptoVerify, createPublicKey } from 'node:crypto';
import * as mod from './astryx-join.mjs';
import worker, {
  metaEmail, verifyAccessToken, jwksCache, ascTokens, ascClient, inviteTester, makeHandler, sameOrigin, resetForTests,
} from './astryx-join.mjs';

// The Worker's main module may export only handlers and functions (workerd refuses any other named export at startup),
// so its constants are restated here.
const ASC_ORIGIN = 'https://api.appstoreconnect.apple.com';

const TEAM = 'https://example.cloudflareaccess.com';
const AUD = 'aud-join-0123456789abcdef';
const GROUP = '00000000-1111-2222-3333-444444444444';
const ORIGIN = 'https://join.example.com';
const NOW = 1_800_000_000;
// Addresses are assembled from parts, so this file holds no address literal (scripts/test-people.py).
const at = (local, domain) => [local, domain].join('@');
const EMAIL = at('tester', 'meta.com');

const rsa = generateKeyPairSync('rsa', { modulusLength: 2048 });
const JWK = { ...rsa.publicKey.export({ format: 'jwk' }), kid: 'k1', alg: 'RS256', use: 'sig' };
const otherRsa = generateKeyPairSync('rsa', { modulusLength: 2048 });
const ec = generateKeyPairSync('ec', { namedCurve: 'P-256' });
const EC_PEM = ec.privateKey.export({ format: 'pem', type: 'pkcs8' });

const b64j = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
function accessToken(claims = {}, header = {}, key = rsa.privateKey) {
  const h = b64j({ alg: 'RS256', kid: 'k1', typ: 'JWT', ...header });
  const p = b64j({ iss: TEAM, aud: [AUD], exp: NOW + 600, nbf: NOW - 10, iat: NOW - 10, email: EMAIL, type: 'app', ...claims });
  const s = cryptoSign('RSA-SHA256', Buffer.from(`${h}.${p}`), key).toString('base64url');
  return `${h}.${p}.${s}`;
}

const getKey = jwksCache({ teamDomain: TEAM, fetchJson: async () => ({ keys: [JWK] }), nowMs: () => NOW * 1000 });
const verify = (token, over = {}) => verifyAccessToken(token, { teamDomain: TEAM, aud: AUD, getKey, now: () => NOW, ...over });

// ---------------------------------------------------------------------------------------------------------------------
// The address
// ---------------------------------------------------------------------------------------------------------------------
test('only an address at exactly meta.com passes, lower-cased', () => {
  assert.equal(metaEmail(at('tester', 'meta.com')), at('tester', 'meta.com'));
  assert.equal(metaEmail(at('First.Last', 'Meta.COM')), at('first.last', 'meta.com'));
  assert.equal(metaEmail(at("o'brien+tf", "meta.com")), at("o'brien+tf", "meta.com"));
  for (const bad of [at('tester', 'example.com'), at('tester', 'sub.meta.com'), at('tester', 'meta.com.example.com'), at('tester', 'fakemeta.com'),
    '@meta.com', at('a@b', 'meta.com'), at(' tester', 'meta.com'), at('tester', 'meta.com '), at('tes ter', 'meta.com'), at('tester', 'meta.co'),
    at('tester', 'meta.comm'), `${'a'.repeat(65)}@meta.com`, '', null, undefined, 42, [at('tester', 'meta.com')]]) {
    assert.equal(metaEmail(bad), null, String(bad));
  }
});

// ---------------------------------------------------------------------------------------------------------------------
// The Access token
// ---------------------------------------------------------------------------------------------------------------------
test('a token from the team for this application verifies', async () => {
  const claims = await verify(accessToken());
  assert.equal(claims.email, EMAIL);
  assert.equal((await verify(accessToken({ aud: AUD }))).email, EMAIL); // a string aud as well as a list
});

test('every way a token can be wrong is refused', async () => {
  const cases = [
    [accessToken({ aud: ['another-application'] }), /wrong audience/],
    [accessToken({ iss: 'https://other.cloudflareaccess.com' }), /wrong issuer/],
    [accessToken({ exp: NOW - 61 }), /expired/],
    [accessToken({ exp: undefined }), /expired/],
    [accessToken({ nbf: NOW + 120 }), /not yet valid/],
    [accessToken({ email: undefined }), /no email/],
    [accessToken({ email: '' }), /no email/],
    [accessToken({}, {}, otherRsa.privateKey), /bad signature/],
    [accessToken({}, { kid: 'k2' }), /unknown key id/],
    [accessToken({}, { alg: 'HS256' }), /unexpected algorithm/],
    [accessToken({}, { alg: 'none' }), /unexpected algorithm/],
    ['not-a-token', /malformed/],
    [`${accessToken()},${accessToken()}`, /malformed/],
    [null, /malformed/],
  ];
  for (const [token, err] of cases) await assert.rejects(verify(token), err);
  // A token whose payload was changed after signing.
  const [h, , s] = accessToken().split('.');
  await assert.rejects(verify(`${h}.${b64j({ iss: TEAM, aud: [AUD], exp: NOW + 600, email: at('someone-else', 'meta.com') })}.${s}`), /bad signature/);
});

test('the team keys are cached, and an unknown kid refetches at most once a minute', async () => {
  let fetches = 0;
  let nowMs = NOW * 1000;
  const keys = jwksCache({ teamDomain: TEAM, fetchJson: async (u) => { assert.equal(u, `${TEAM}/cdn-cgi/access/certs`); fetches++; return { keys: [JWK, { kty: 'EC', kid: 'ec' }] }; }, nowMs: () => nowMs });
  assert.ok(await keys('k1'));
  assert.ok(await keys('k1'));
  assert.equal(fetches, 1);
  assert.equal(await keys('ec'), null); // only RSA keys are kept
  assert.equal(fetches, 1);
  nowMs += 61_000;
  assert.equal(await keys('k9'), null);
  assert.equal(fetches, 2);
  assert.equal(await keys('k9'), null);
  assert.equal(fetches, 2);
  nowMs += 10 * 60_000 + 1;
  await keys('k1');
  assert.equal(fetches, 3);
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
// The handler: pages, the same-origin rule, and no address in any log line
// ---------------------------------------------------------------------------------------------------------------------
const allLogs = [];
function harness({ routes = [['POST', /^\/v1\/betaTesters$/, { status: 201, body: {} }]], config = {} } = {}) {
  const asc = mockAsc(routes);
  const logs = [];
  const handler = makeHandler({
    config: { teamDomain: TEAM, aud: AUD, groupId: GROUP, ...config },
    getKey,
    call: asc.call,
    now: () => NOW,
    log: (o) => { const line = JSON.stringify(o); logs.push(line); allLogs.push(line); },
  });
  return { handler, asc, logs };
}
const req = (path, { method = 'GET', token = accessToken(), headers = {} } = {}) =>
  new Request(`${ORIGIN}${path}`, { method, headers: { ...(token ? { 'cf-access-jwt-assertion': token } : {}), ...headers } });
const post = (opts = {}) => req('/join', { method: 'POST', headers: { origin: ORIGIN }, ...opts });

test('GET / says who is signed in, escaped, and offers one button that posts to /join', async () => {
  const { handler, asc } = harness();
  const res = await handler(req('/', { token: accessToken({ email: at("O'Brien&Co", "Meta.com") }) }));
  assert.equal(res.status, 200);
  const html = await res.text();
  assert.match(html, /Signed in as <strong>o&#39;brien&amp;co@meta\.com<\/strong>/);
  assert.equal((html.match(/<button/g) || []).length, 1);
  assert.match(html, /<form method="post" action="\/join"><button type="submit">Send me the TestFlight invite<\/button><\/form>/);
  assert.match(html, /TestFlight emails an invitation to this address\. Install the TestFlight app on your iPhone or Mac/);
  assert.match(html, /prefers-color-scheme:dark/);
  assert.match(html, /<meta name="viewport" content="width=device-width, initial-scale=1">/);
  assert.match(res.headers.get('content-security-policy'), /frame-ancestors 'none'/);
  assert.match(res.headers.get('content-security-policy'), /form-action 'self'/);
  assert.equal(res.headers.get('cache-control'), 'no-store');
  assert.equal(asc.calls.length, 0);
});

test('no token, a bad token or an address outside meta.com gets nothing, and App Store Connect is not called', async () => {
  const { handler, asc } = harness();
  for (const r of [req('/', { token: null }), post({ token: null }), post({ token: accessToken({ aud: ['other'] }) }),
    post({ token: accessToken({}, {}, otherRsa.privateKey) }), req('/', { token: accessToken({ email: at('tester', 'example.com') }) }),
    post({ token: accessToken({ email: at('tester', 'example.com') }) }), post({ token: accessToken({ email: at('tester', 'sub.meta.com') }) })]) {
    const res = await handler(r);
    assert.equal(res.status, 403);
    assert.doesNotMatch(await res.text(), /<form/);
  }
  assert.equal(asc.calls.length, 0);
});

test('POST /join must come from this origin', async () => {
  const { handler, asc } = harness();
  for (const headers of [{ origin: 'https://evil.example.com' }, { origin: 'null' }, {}, { 'sec-fetch-site': 'cross-site' }, { 'sec-fetch-site': 'same-site' }]) {
    const res = await handler(req('/join', { method: 'POST', headers }));
    assert.equal(res.status, 403, JSON.stringify(headers));
  }
  assert.equal(asc.calls.length, 0);
  assert.equal((await handler(req('/join', { method: 'POST', headers: { 'sec-fetch-site': 'same-origin' } }))).status, 200);
  assert.equal(sameOrigin(new Request(`${ORIGIN}/join`, { method: 'POST', headers: { origin: ORIGIN } })), true);
});

test('each outcome is said in words', async () => {
  const want = {
    invited: [[['POST', /^\/v1\/betaTesters$/, { status: 201, body: {} }]], 200, /Invitation on its way/],
    already: [[['POST', /^\/v1\/betaTesters$/, conflict('exists')], ['GET', /filter\[betaGroups\]=/, { status: 200, body: { data: [{ id: 't' }] } }]], 200, /already invited[\s\S]*Look for the email from TestFlight/],
    limit: [[['POST', /^\/v1\/betaTesters$/, conflict('The maximum number of testers has been reached')]], 200, /Apple allows 10,000 external testers per app/],
    error: [[['POST', /^\/v1\/betaTesters$/, { status: 500, body: null }]], 502, /Try again in a minute[\s\S]*<form method="post" action="\/join"><button type="submit">Try again<\/button>/],
  };
  for (const [outcome, [routes, status, text]] of Object.entries(want)) {
    const { handler, logs } = harness({ routes });
    const res = await handler(post());
    assert.equal(res.status, status, outcome);
    assert.match(await res.text(), text, outcome);
    assert.equal(JSON.parse(logs.at(-1)).outcome, outcome);
  }
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
  const { handler, asc } = harness();
  const get = await handler(req('/join'));
  assert.equal(get.status, 303);
  assert.equal(get.headers.get('location'), '/');
  assert.equal((await handler(req('/admin'))).status, 404);
  assert.equal((await handler(req('/', { method: 'POST', headers: { origin: ORIGIN } }))).status, 404);
  assert.equal((await handler(req('/join', { method: 'PUT', headers: { origin: ORIGIN } }))).status, 404);
  assert.equal(asc.calls.length, 0);
});

test('a Worker missing its configuration refuses everything', async () => {
  for (const config of [{ groupId: 'not-a-uuid' }, { groupId: `${GROUP}/../x` }, { aud: '' }, { teamDomain: 'https://evil.example.com' }]) {
    const { handler, asc } = harness({ config });
    assert.equal((await handler(post())).status, 500);
    assert.equal(asc.calls.length, 0);
  }
});

// ---------------------------------------------------------------------------------------------------------------------
// The Worker's entry point, wired from its bindings, with fetch stubbed
// ---------------------------------------------------------------------------------------------------------------------
test('the Worker wires its bindings: Access certs from the team, ES256 to App Store Connect, the group from config', async () => {
  const realFetch = globalThis.fetch;
  const realLog = console.log;
  const seen = [];
  const lines = [];
  globalThis.fetch = async (u, init = {}) => {
    seen.push([String(u), init]);
    if (String(u) === `${TEAM}/cdn-cgi/access/certs`) return new Response(JSON.stringify({ keys: [JWK] }), { status: 200 });
    if (String(u) === `${ASC_ORIGIN}/v1/betaTesters`) return new Response('{"data":{"id":"t"}}', { status: 201 });
    return new Response('', { status: 404 });
  };
  console.log = (...a) => lines.push(a.join(' '));
  try {
    resetForTests();
    const env = { ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD, ASC_BETA_GROUP_ID: GROUP, ASC_ISSUER_ID: 'issuer-uuid', ASC_KEY_ID: 'KEYID12345', ASC_PRIVATE_KEY: EC_PEM };
    const realNow = Date.now;
    Date.now = () => NOW * 1000;
    let res;
    try {
      res = await worker.fetch(post(), env);
    } finally {
      Date.now = realNow;
    }
    assert.equal(res.status, 200);
    assert.match(await res.text(), /Invitation on its way/);
    const asc = seen.find(([u]) => u.startsWith(ASC_ORIGIN));
    assert.equal(asc[1].method, 'POST');
    const token = asc[1].headers.authorization.replace(/^Bearer /, '');
    const [h, p] = token.split('.');
    assert.equal(JSON.parse(Buffer.from(h, 'base64url')).kid, 'KEYID12345');
    assert.equal(JSON.parse(Buffer.from(p, 'base64url')).iss, 'issuer-uuid');
    assert.equal(JSON.parse(asc[1].body).data.relationships.betaGroups.data[0].id, GROUP);
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

// Last: every line any handler logged above, across every outcome and refusal, names no address.
test('no log line carries an address', () => {
  assert.ok(allLogs.length > 20, String(allLogs.length));
  for (const line of allLogs) {
    assert.doesNotMatch(line, /@/, line);
    assert.doesNotMatch(line, /meta\.com|example\.com/i, line);
  }
});
