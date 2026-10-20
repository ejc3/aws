// Offline tests for scripts/pc-enroll.mjs: node --test scripts/test-pc-enroll.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign as cryptoSign, createHash } from 'node:crypto';
import { makeHandler, verifyAccessToken, jwksCache, parseEd25519, fingerprint } from './pc-enroll.mjs';

const TEAM = 'https://example.cloudflareaccess.com';
const AUD = 'aud-enroll-0123456789abcdef';
const NOW = 1_800_000_000;
const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const JWK = { ...publicKey.export({ format: 'jwk' }), kid: 'k1', alg: 'RS256', use: 'sig' };
const other = generateKeyPairSync('rsa', { modulusLength: 2048 });

function token(claims = {}, header = {}, key = privateKey) {
  const h = Buffer.from(JSON.stringify({ alg: 'RS256', kid: 'k1', typ: 'JWT', ...header })).toString('base64url');
  const p = Buffer.from(JSON.stringify({ iss: TEAM, aud: [AUD], exp: NOW + 600, nbf: NOW - 10, email: 'owner@example.com', ...claims })).toString('base64url');
  const s = cryptoSign('RSA-SHA256', Buffer.from(`${h}.${p}`), key).toString('base64url');
  return `${h}.${p}.${s}`;
}

function edKey(seed) {
  // A well-formed ssh-ed25519 public key with a deterministic 32-byte body.
  const body = createHash('sha256').update(seed).digest();
  const blob = Buffer.concat([Buffer.from([0, 0, 0, 11]), Buffer.from('ssh-ed25519'), Buffer.from([0, 0, 0, 32]), body]);
  return `ssh-ed25519 ${blob.toString('base64')}`;
}
const PC_KEY = edKey('pc');
const PC_KEY2 = edKey('pc-reinstalled');
const FCVM_KEY = edKey('fcvm');
const CONNECTOR = 'eyJhIjoiY29ubmVjdG9yLXRva2VuIn0=';

function setup({ params = {}, secret = CONNECTOR, beforePut = () => {} } = {}) {
  const store = { '/pc-ssh/test-pc/clients/fcvm-ubuntu': `${FCVM_KEY} fcvm`, ...params };
  const logs = [];
  const writes = [];
  const config = {
    teamDomain: TEAM,
    aud: AUD,
    pcs: {
      'test-pc': {
        secret_arn: 'arn:aws:secretsmanager:us-west-1:1:secret:test-pc-ssh-tunnel-token-abc',
        hostname: 'test-pc-ssh.example.com',
        host_key_param: '/pc-ssh/test-pc/host-key',
        approvers: ['Owner@example.com', 'approver@example.com'],
        owners: ['owner@example.com'],
        clients: { 'fcvm-ubuntu': '/pc-ssh/test-pc/clients/fcvm-ubuntu' },
      },
    },
  };
  const handler = makeHandler({
    config,
    getKey: async (kid) => (kid === 'k1' ? JWK : null),
    getSecret: async () => secret,
    getParam: async (name) => (name in store ? store[name] : null),
    putParam: async (name, value, overwrite) => {
      beforePut(store);
      if (!overwrite && name in store) return 'exists';
      store[name] = value;
      writes.push(name);
      return 'written';
    },
    now: () => NOW,
    log: (o) => logs.push(JSON.stringify(o)),
  });
  const call = (method, path, body, assertion = token()) =>
    handler({
      requestContext: { http: { method } },
      rawPath: path,
      headers: assertion === null ? {} : { 'cf-access-jwt-assertion': assertion }, // null: no header at all
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then((r) => ({ status: r.statusCode, body: JSON.parse(r.body), headers: r.headers }));
  return { call, store, logs, writes };
}

test('a valid Access token passes; every malformed or foreign one is refused', async () => {
  const opts = { teamDomain: TEAM, aud: AUD, getKey: async (kid) => (kid === 'k1' ? JWK : null), now: () => NOW };
  assert.equal((await verifyAccessToken(token(), opts)).email, 'owner@example.com');
  assert.equal((await verifyAccessToken(token({ aud: AUD }), opts)).email, 'owner@example.com'); // aud as a string
  const refused = {
    'wrong audience': token({ aud: ['another-app'] }),
    'wrong issuer': token({ iss: 'https://evil.cloudflareaccess.com' }),
    expired: token({ exp: NOW - 61 }),
    'not yet valid': token({ nbf: NOW + 61 }),
    'no email': token({ email: '' }),
    'HS256': token({}, { alg: 'HS256' }),
    'alg none': token({}, { alg: 'none' }),
    'unknown kid': token({}, { kid: 'k2' }),
    'another key': token({}, {}, other.privateKey),
    'two tokens joined': `${token()},${token()}`,
    'a trailing comma': `${token()},`,
    'not a token': 'abc',
  };
  for (const [what, t] of Object.entries(refused)) {
    await assert.rejects(verifyAccessToken(t, opts), undefined, what);
  }
  // A signature over different claims (a tampered payload) fails.
  const [h, , s] = token().split('.');
  const forged = Buffer.from(JSON.stringify({ iss: TEAM, aud: [AUD], exp: NOW + 600, email: 'attacker@example.com' })).toString('base64url');
  await assert.rejects(verifyAccessToken(`${h}.${forged}.${s}`, opts));
});

test('no token, a bad token, or an email not listed gets nothing and writes nothing', async () => {
  const { call, writes } = setup();
  for (const assertion of [null, '', 'x.y.z', token({ aud: ['other'] })]) {
    const r = await call('POST', '/pcs/test-pc/enroll', { host_key: PC_KEY, user: 'colin' }, assertion);
    assert.equal(r.status, 403);
    assert.equal(r.body.connector_token, undefined);
  }
  const stranger = await call('POST', '/pcs/test-pc/enroll', { host_key: PC_KEY, user: 'colin' }, token({ email: 'someone@example.com' }));
  assert.equal(stranger.status, 403);
  assert.deepEqual(writes, []);
});

test('the first enrollment pins the host key and returns the token and client keys; a re-run is idempotent', async () => {
  const { call, store, logs } = setup();
  const r = await call('POST', '/pcs/test-pc/enroll', { host_key: `${PC_KEY} root@pc`, user: 'colin' });
  assert.equal(r.status, 200);
  assert.equal(r.body.connector_token, CONNECTOR);
  assert.deepEqual(r.body.client_keys, [`${FCVM_KEY} fcvm-ubuntu`]);
  assert.equal(r.body.hostname, 'test-pc-ssh.example.com');
  assert.equal(r.headers['cache-control'], 'no-store');
  assert.equal(store['/pc-ssh/test-pc/host-key'], PC_KEY);
  const again = await call('POST', '/pcs/test-pc/enroll', { host_key: PC_KEY, user: 'colin' });
  assert.equal(again.status, 200);
  for (const line of logs) assert.ok(!line.includes(CONNECTOR), 'a log line carries the token');
});

test('a different host key against an existing pin is refused; an owner replaces it; a non-owner cannot', async () => {
  const { call, store } = setup({ params: { '/pc-ssh/test-pc/host-key': PC_KEY } });
  const r = await call('POST', '/pcs/test-pc/enroll', { host_key: PC_KEY2, user: 'colin' });
  assert.equal(r.status, 409);
  assert.equal(r.body.connector_token, undefined);
  assert.equal(store['/pc-ssh/test-pc/host-key'], PC_KEY);
  const notOwner = await call('POST', '/pcs/test-pc/host-key/replace', { host_key: PC_KEY2 }, token({ email: 'approver@example.com' }));
  assert.equal(notOwner.status, 403);
  const owner = await call('POST', '/pcs/test-pc/host-key/replace', { host_key: PC_KEY2 });
  assert.equal(owner.status, 200);
  assert.equal(store['/pc-ssh/test-pc/host-key'], PC_KEY2);
  assert.equal((await call('POST', '/pcs/test-pc/enroll', { host_key: PC_KEY2, user: 'colin' })).status, 200);
});

test('two enrollments racing with different keys: the first pin stands', async () => {
  // The pin is absent when this enrollment reads it, and another enrollment writes PC_KEY before this one's write.
  const { call, store } = setup({ beforePut: (s) => { s['/pc-ssh/test-pc/host-key'] ??= PC_KEY; } });
  const r = await call('POST', '/pcs/test-pc/enroll', { host_key: PC_KEY2, user: 'colin' });
  assert.equal(r.status, 409);
  assert.equal(r.body.connector_token, undefined);
  assert.equal(store['/pc-ssh/test-pc/host-key'], PC_KEY);
});

test('nothing is written until every prerequisite is there', async () => {
  const noClients = setup({ params: { '/pc-ssh/test-pc/clients/fcvm-ubuntu': 'not a key' } });
  const r = await noClients.call('POST', '/pcs/test-pc/enroll', { host_key: PC_KEY, user: 'colin' });
  assert.equal(r.status, 409);
  assert.deepEqual(noClients.writes, []);
  const noToken = setup({ secret: '' });
  assert.equal((await noToken.call('POST', '/pcs/test-pc/enroll', { host_key: PC_KEY, user: 'colin' })).status, 409);
  assert.deepEqual(noToken.writes, []);
});

test('inputs are validated: names, keys, users, routes', async () => {
  const { call, writes } = setup();
  assert.equal((await call('POST', '/pcs/other-pc/enroll', { host_key: PC_KEY, user: 'colin' })).status, 404);
  assert.equal((await call('POST', '/pcs/Test_PC/enroll', { host_key: PC_KEY, user: 'colin' })).status, 404);
  assert.equal((await call('GET', '/pcs/test-pc/enroll')).status, 404);
  for (const bad of ['', 'ssh-rsa AAAA', `${PC_KEY}\nssh-ed25519 AAAA`, PC_KEY.slice(0, -4)]) {
    assert.equal((await call('POST', '/pcs/test-pc/enroll', { host_key: bad, user: 'colin' })).status, 400, bad);
  }
  for (const user of ['', 'a b', 'x'.repeat(33), 'colin\nProxyCommand evil']) {
    assert.equal((await call('POST', '/pcs/test-pc/enroll', { host_key: PC_KEY, user })).status, 400, user);
  }
  assert.deepEqual(writes, []);
});

test('GET /pcs lists only the PCs this person may enroll, with their fingerprints', async () => {
  const { call } = setup({ params: { '/pc-ssh/test-pc/host-key': PC_KEY } });
  const mine = await call('GET', '/pcs');
  assert.deepEqual(mine.body.pcs, [{ name: 'test-pc', hostname: 'test-pc-ssh.example.com', host_key_fingerprint: fingerprint(PC_KEY) }]);
  assert.deepEqual((await call('GET', '/pcs', undefined, token({ email: 'someone@example.com' }))).body.pcs, []);
});

test('parseEd25519 accepts exactly one well-formed key, and fingerprints match ssh-keygen', () => {
  assert.equal(parseEd25519(`${PC_KEY} comment here`), PC_KEY);
  assert.equal(parseEd25519('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA'), null);
  assert.equal(parseEd25519(`${PC_KEY}\r`), null);
  assert.match(fingerprint(PC_KEY), /^SHA256:[A-Za-z0-9+/]{43}$/);
});

test('the JWKS is cached and refetched for an unknown kid at most once a minute', async () => {
  let fetches = 0;
  let t = 0;
  const get = jwksCache({ teamDomain: TEAM, fetchJson: async () => (fetches++, { keys: [JWK] }), nowMs: () => t });
  assert.ok(await get('k1'));
  assert.ok(await get('k1'));
  assert.equal(fetches, 1);
  assert.equal(await get('k9'), null); // within the minute: no refetch
  assert.equal(fetches, 1);
  t = 61_000;
  assert.equal(await get('k9'), null);
  assert.equal(fetches, 2);
  t = 61_000 + 10 * 60_000 + 1;
  await get('k1');
  assert.equal(fetches, 3); // the cache expires
});
