import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPair, SignJWT } from 'jose';
import { createAuthorizer, readConfig, instanceName, startUrl, requireOrigin } from '../lib/auth.mjs';

const serviceTokenId = '0123456789abcdef0123456789abcdef.access';
const config = { baseUrl: 'https://browsers.example.test', issuer: 'https://team.cloudflareaccess.com', audience: 'browser-audience', serviceTokenId, owner: 'owner@example.test' };
const keys = await generateKeyPair('RS256');
const signed = (claims = {}, options = {}) => new SignJWT({ email: config.owner, ...claims })
  .setProtectedHeader({ alg: 'RS256' }).setIssuedAt().setSubject('owner')
  .setIssuer(options.issuer || config.issuer).setAudience(options.audience || config.audience)
  .setExpirationTime(options.exp || '5m').sign(keys.privateKey);
const request = token => ({ headers: { 'cf-access-jwt-assertion': token } });

test('Access validates signature, owner, audience, issuer and expiry on every request', async () => {
  const authorize = createAuthorizer(config, keys.publicKey);
  assert.ok((await authorize(request(await signed()))).expiresAt > Date.now());
  for (const token of [undefined, 'not-a-jwt', await signed({ email: 'other@example.test' }),
    await signed({}, { audience: 'other-app' }), await signed({}, { issuer: 'https://wrong.cloudflareaccess.com' }),
    await signed({}, { exp: '1 second ago' })]) {
    await assert.rejects(authorize(request(token)), error => [401, 403].includes(error.status));
  }
  const forged = `${(await signed()).slice(0, -20)}aaaaaaaaaaaaaaaaaaaa`;
  await assert.rejects(authorize(request(forged)));
  await assert.rejects(authorize({ headers: { 'cf-access-authenticated-user-email': config.owner } }));
});

test('Access accepts only the configured signed service-token principal', async () => {
  const authorize = createAuthorizer(config, keys.publicKey);
  const service = claims => new SignJWT({ type: 'app', common_name: serviceTokenId, ...claims })
    .setProtectedHeader({ alg: 'RS256' }).setIssuedAt().setSubject('')
    .setIssuer(config.issuer).setAudience(config.audience).setExpirationTime('5m').sign(keys.privateKey);
  assert.ok((await authorize(request(await service({})))).expiresAt > Date.now());
  await assert.rejects(authorize(request(await service({ common_name: 'ffffffffffffffffffffffffffffffff.access' }))));
  await assert.rejects(authorize(request(await service({ type: 'org' }))));
  await assert.rejects(authorize(request(await signed({ email: undefined, common_name: serviceTokenId, type: 'app' }))));
});

test('mutation Origin must match exactly; absent and lookalike origins are refused', () => {
  requireOrigin({ headers: { origin: config.baseUrl } }, config.baseUrl);
  for (const origin of [undefined, 'null', 'https://browsers.example.test.evil', `${config.baseUrl}/`, 'http://browsers.example.test']) {
    assert.throws(() => requireOrigin({ headers: { origin } }, config.baseUrl));
  }
});

test('instance names and start URLs cannot select filesystem, socket, or executable targets', () => {
  assert.equal(instanceName('claude-2'), 'claude-2');
  for (const value of ['..', '../profile', 'a/b', 'a%2fb', '-name', 'A', '', 'a'.repeat(49), null]) assert.throws(() => instanceName(value));
  assert.equal(startUrl('https://example.test'), 'https://example.test/');
  assert.equal(startUrl('about:blank'), 'about:blank');
  for (const value of ['file:///etc/passwd', 'javascript:alert(1)', 'about:config', '--no-sandbox', 'https://user:password@example.test']) assert.throws(() => startUrl(value));
});

test('production config refuses missing audience and unsafe origins', () => {
  assert.throws(() => readConfig({}));
  // #268 took the owner's address out of the code: it now comes only from BM_OWNER_EMAIL.
  const env = { BM_ACCESS_AUD: config.audience, BM_ACCESS_SERVICE_TOKEN_ID: serviceTokenId, BM_OWNER_EMAIL: 'owner@example.com' };
  assert.equal(readConfig(env).port, 3210);
  for (const change of [{ BM_BASE_URL: 'http://public.example.test' }, { BM_BASE_URL: 'https://x.test/path' },
    { BM_ACCESS_ISSUER: 'http://team.cloudflareaccess.com' }, { BM_ACCESS_ISSUER: 'https://attacker.test' },
    { BM_ACCESS_SERVICE_TOKEN_ID: 'not-a-client-id' }, { BM_PORT: '0' }]) {
    assert.throws(() => readConfig({ ...env, ...change }));
  }
});
