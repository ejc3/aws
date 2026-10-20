// pc-enroll: hands a Windows PC its Cloudflare connector token after a person logs in to Cloudflare Access on it
// (pc-enroll.tf; the PC's side is scripts/windows/enroll.ps1). Behind the Access application on the enroll hostname,
// but it checks the Access token itself on every request: the API Gateway custom domain behind that hostname is public,
// so this check, not Access alone, is the boundary.
//
//   GET  /pcs                         the PCs this person may enroll, with their pinned host-key fingerprints
//   POST /pcs/<pc>/enroll             {host_key, user} -> {connector_token, client_keys, hostname, host_key_fingerprint}
//   POST /pcs/<pc>/host-key/replace   {host_key}: an owner replaces a PC's pinned host key (reinstalled PC)
//
// A first enrollment pins the PC's SSH host key (SSM, written only when absent); a later one must present the same key,
// so a re-run is idempotent and a different key is refused until an owner replaces the pin. Every prerequisite (the
// token, every configured client key) is read before anything is written. No token is ever logged.
//
// Node.js 22 with the runtime's own AWS SDK and node:crypto only: no dependencies to vendor.

import { createPublicKey, verify as cryptoVerify, createHash } from 'node:crypto';

const NAME = /^[a-z][a-z0-9-]{0,30}$/;
const USER = /^[A-Za-z0-9._-]{1,32}$/;
const SKEW_SECONDS = 60;
const JWKS_TTL_MS = 10 * 60 * 1000;
const JWKS_REFRESH_MIN_MS = 60 * 1000;

// The PC's or a client's key as exactly `ssh-ed25519 <blob>`, its blob a well-formed ed25519 public key; else null.
export function parseEd25519(text) {
  if (typeof text !== 'string' || /[\r\n\0]/.test(text)) return null;
  const parts = text.trim().split(/ +/);
  if (parts.length < 2 || parts[0] !== 'ssh-ed25519' || !/^AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{48}$/.test(parts[1])) return null;
  const blob = Buffer.from(parts[1], 'base64');
  // string "ssh-ed25519" then a 32-byte key: 4+11+4+32 bytes.
  if (blob.length !== 51 || blob.readUInt32BE(0) !== 11 || blob.subarray(4, 15).toString() !== 'ssh-ed25519' || blob.readUInt32BE(15) !== 32) return null;
  return `ssh-ed25519 ${parts[1]}`;
}

export function fingerprint(key) {
  return 'SHA256:' + createHash('sha256').update(Buffer.from(key.split(' ')[1], 'base64')).digest('base64').replace(/=+$/, '');
}

function b64urlJson(part) {
  return JSON.parse(Buffer.from(part, 'base64url').toString('utf8'));
}

// The Cloudflare Access token's claims, or throws. RS256 only, the key by kid from the team's JWKS, iss exactly the team
// domain, aud containing this application's AUD, exp and nbf with a minute's skew.
export async function verifyAccessToken(token, { teamDomain, aud, getKey, now }) {
  if (typeof token !== 'string' || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) throw new Error('malformed token');
  const [h, p, s] = token.split('.');
  const header = b64urlJson(h);
  if (header.alg !== 'RS256' || typeof header.kid !== 'string') throw new Error('unexpected algorithm or key id');
  const jwk = await getKey(header.kid);
  if (!jwk) throw new Error('unknown key id');
  const ok = cryptoVerify('RSA-SHA256', Buffer.from(`${h}.${p}`), createPublicKey({ key: jwk, format: 'jwk' }), Buffer.from(s, 'base64url'));
  if (!ok) throw new Error('bad signature');
  const claims = b64urlJson(p);
  const t = now();
  if (claims.iss !== teamDomain) throw new Error('wrong issuer');
  const auds = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!auds.includes(aud)) throw new Error('wrong audience');
  if (typeof claims.exp !== 'number' || t >= claims.exp + SKEW_SECONDS) throw new Error('expired');
  if (typeof claims.nbf === 'number' && t + SKEW_SECONDS < claims.nbf) throw new Error('not yet valid');
  if (typeof claims.email !== 'string' || !claims.email) throw new Error('no email');
  return claims;
}

// The team's signing keys, cached; an unknown kid refetches at most once a minute.
export function jwksCache({ teamDomain, fetchJson, nowMs }) {
  let keys = null;
  let fetchedAt = 0;
  const load = async () => {
    const body = await fetchJson(`${teamDomain}/cdn-cgi/access/certs`);
    keys = Array.isArray(body?.keys) ? body.keys : [];
    fetchedAt = nowMs();
  };
  return async (kid) => {
    if (!keys || nowMs() - fetchedAt > JWKS_TTL_MS) await load();
    let jwk = keys.find((k) => k.kid === kid);
    if (!jwk && nowMs() - fetchedAt > JWKS_REFRESH_MIN_MS) {
      await load();
      jwk = keys.find((k) => k.kid === kid);
    }
    return jwk && jwk.kty === 'RSA' ? jwk : null;
  };
}

const json = (statusCode, body) => ({
  statusCode,
  headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
  body: JSON.stringify(body),
});

// deps: { config: {teamDomain, aud, pcs: {name: {secret_arn, hostname, approvers, owners, clients: {slot: param}}}},
//         getKey(kid), getSecret(arn), getParam(name) -> string|null, putParam(name, value, overwrite) -> 'written'|'exists',
//         now() seconds, log(obj) }
export function makeHandler(deps) {
  const { config, log } = deps;
  return async (event) => {
    const method = event?.requestContext?.http?.method;
    const path = event?.rawPath || '';
    const headers = event?.headers || {};
    const assertion = headers['cf-access-jwt-assertion'];
    let claims;
    try {
      // One assertion, exactly: API Gateway joins repeated headers with a comma, which the token pattern refuses.
      claims = await verifyAccessToken(assertion, { teamDomain: config.teamDomain, aud: config.aud, getKey: deps.getKey, now: deps.now });
    } catch (e) {
      log({ event: 'refused', reason: String(e.message), path, method });
      return json(403, { error: 'forbidden' });
    }
    const email = claims.email.toLowerCase();
    const mayEnroll = (pc) => (pc.approvers || []).map((a) => a.toLowerCase()).includes(email);
    const isOwner = (pc) => (pc.owners || []).map((a) => a.toLowerCase()).includes(email);

    if (method === 'GET' && path === '/pcs') {
      const out = [];
      for (const [name, pc] of Object.entries(config.pcs)) {
        if (!mayEnroll(pc)) continue;
        const pinned = parseEd25519((await deps.getParam(pc.host_key_param)) || '');
        out.push({ name, hostname: pc.hostname, host_key_fingerprint: pinned ? fingerprint(pinned) : null });
      }
      return json(200, { pcs: out });
    }

    const m = /^\/pcs\/([^/]+)\/(enroll|host-key\/replace)$/.exec(path);
    if (method !== 'POST' || !m || !NAME.test(m[1])) return json(404, { error: 'not found' });
    const name = m[1];
    const pc = config.pcs[name];
    if (!pc) return json(404, { error: `no PC named ${name} is enrollable` });
    let body;
    try {
      body = JSON.parse(event.body ? (event.isBase64Encoded ? Buffer.from(event.body, 'base64').toString('utf8') : event.body) : '{}');
    } catch {
      return json(400, { error: 'body is not JSON' });
    }
    if (!body || typeof body !== 'object' || Array.isArray(body)) return json(400, { error: 'body must be a JSON object' });
    const hostKey = parseEd25519(body.host_key);
    if (!hostKey) return json(400, { error: 'host_key must be one ssh-ed25519 public key' });

    if (m[2] === 'host-key/replace') {
      if (!isOwner(pc)) {
        log({ event: 'replace-refused', pc: name, email });
        return json(403, { error: 'only an owner replaces a pinned host key' });
      }
      const old = parseEd25519((await deps.getParam(pc.host_key_param)) || '');
      await deps.putParam(pc.host_key_param, hostKey, true);
      log({ event: 'host-key-replaced', pc: name, email, old: old ? fingerprint(old) : null, new: fingerprint(hostKey) });
      return json(200, { host_key_fingerprint: fingerprint(hostKey) });
    }

    if (!mayEnroll(pc)) {
      log({ event: 'enroll-refused', pc: name, email });
      return json(403, { error: `you may not enroll ${name}` });
    }
    if (typeof body.user !== 'string' || !USER.test(body.user)) return json(400, { error: 'user must be a Windows account name' });

    // Every prerequisite before any write.
    const token = await deps.getSecret(pc.secret_arn);
    if (typeof token !== 'string' || !token.trim()) return json(409, { error: `${name} has no connector token yet` });
    const clientKeys = [];
    const missing = [];
    for (const [slot, param] of Object.entries(pc.clients || {})) {
      const key = parseEd25519((await deps.getParam(param)) || '');
      if (key) clientKeys.push(`${key} ${slot}`);
      else missing.push(slot);
    }
    if (missing.length || clientKeys.length === 0) {
      log({ event: 'enroll-waiting', pc: name, email, missing });
      return json(409, { error: `client keys not published yet: ${missing.join(', ') || 'none configured'}` });
    }

    // The pin: written only when absent; otherwise it must be this key.
    let pinned = parseEd25519((await deps.getParam(pc.host_key_param)) || '');
    if (!pinned) {
      const r = await deps.putParam(pc.host_key_param, hostKey, false);
      pinned = r === 'written' ? hostKey : parseEd25519((await deps.getParam(pc.host_key_param)) || '');
    }
    if (pinned !== hostKey) {
      log({ event: 'enroll-host-key-mismatch', pc: name, email, pinned: pinned ? fingerprint(pinned) : null, offered: fingerprint(hostKey) });
      return json(409, { error: `${name}'s pinned host key is ${pinned ? fingerprint(pinned) : 'unreadable'}, not ${fingerprint(hostKey)}; an owner replaces it with POST /pcs/${name}/host-key/replace` });
    }
    log({ event: 'enrolled', pc: name, email, user: body.user, host_key: fingerprint(hostKey), clients: clientKeys.length });
    return json(200, { connector_token: token.trim(), client_keys: clientKeys, hostname: pc.hostname, host_key_fingerprint: fingerprint(hostKey) });
  };
}

// The Lambda's own wiring: the runtime's AWS SDK, a JWKS fetch, the configuration from the environment.
let handlerPromise = null;
async function build() {
  const { SecretsManagerClient, GetSecretValueCommand } = await import('@aws-sdk/client-secrets-manager');
  const { SSMClient, GetParameterCommand, PutParameterCommand } = await import('@aws-sdk/client-ssm');
  const sm = new SecretsManagerClient({});
  const ssm = new SSMClient({});
  const config = JSON.parse(process.env.PC_ENROLL_CONFIG);
  return makeHandler({
    config,
    getKey: jwksCache({
      teamDomain: config.teamDomain,
      fetchJson: async (url) => (await fetch(url, { signal: AbortSignal.timeout(5000) })).json(),
      nowMs: () => Date.now(),
    }),
    getSecret: async (arn) => (await sm.send(new GetSecretValueCommand({ SecretId: arn }))).SecretString,
    getParam: async (name) => {
      try {
        return (await ssm.send(new GetParameterCommand({ Name: name }))).Parameter.Value;
      } catch (e) {
        if (e.name === 'ParameterNotFound') return null;
        throw e;
      }
    },
    putParam: async (name, value, overwrite) => {
      try {
        await ssm.send(new PutParameterCommand({ Name: name, Value: value, Type: 'String', Overwrite: overwrite }));
        return 'written';
      } catch (e) {
        if (e.name === 'ParameterAlreadyExists') return 'exists';
        throw e;
      }
    },
    now: () => Math.floor(Date.now() / 1000),
    log: (o) => console.log(JSON.stringify(o)),
  });
}

export const handler = async (event) => {
  handlerPromise ||= build();
  return (await handlerPromise)(event);
};
