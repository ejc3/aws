// Astryx Swift TestFlight invitations for @meta.com addresses: the Worker behind astryx-join.dolphin-labs.dev (astryx-join.tf).
//
//   GET  /      a form: the address, a Turnstile check, one button
//   POST /join  adds the address as an external tester of the app, in the self-serve beta group
//
// Public on purpose (owner, 2026-10-23: "Turnstile please. Qr code will stay quiet, and I trust meta engineers"): no
// sign-in, so no Zero Trust seat per person. An address is not proven when it is typed, but the invitation goes to that
// inbox and can only be accepted from it. What bounds abuse, in this order: the request must come from this page; a
// Turnstile token verified with Cloudflare (secret, the visitor's IP, an idempotency key) before anything else; an
// address at exactly meta.com; at most PER_IP_PER_HOUR requests per IP an hour and DAILY_CAP a day overall (counters
// in Workers KV); then App Store Connect, with an ES256 token signed here by a dedicated App Manager key.
//
// Nothing this Worker logs carries an address or an IP: a log line names the outcome, an HTTP status and Apple's or
// Turnstile's error code, no more.
//
// Bindings (astryx-join.tf): TURNSTILE_SITE_KEY, ASC_BETA_GROUP_ID (plain text); TURNSTILE_SECRET, ASC_ISSUER_ID,
// ASC_KEY_ID, ASC_PRIVATE_KEY (secrets); LIMITS (KV). Tests: node --test scripts/test-astryx-join.mjs
//
// The main module may export only functions besides default: workerd refuses to start on any other named export.

const ASC_ORIGIN = 'https://api.appstoreconnect.apple.com';
const SITEVERIFY = 'https://challenges.cloudflare.com/turnstile/v0/siteverify';
const TURNSTILE_SCRIPT = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
const EMAIL_DOMAIN = '@meta.com';
const ASC_TOKEN_SECONDS = 20 * 60; // Apple's maximum lifetime
const ASC_TOKEN_REUSE_MARGIN = 5 * 60; // a fresh token once fewer than five minutes remain
const PER_IP_PER_HOUR = 5;
const DAILY_CAP = 500;
const MAX_BODY = 8 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const enc = new TextEncoder();
const b64urlBytes = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const b64url = (text) => b64urlBytes(enc.encode(text));
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
// Only well-formed error codes reach a log line: never free text, which can quote what was sent.
const safeCode = (c) => (typeof c === 'string' && /^[A-Za-z0-9_.-]{1,80}$/.test(c) ? c : null);

// Trimmed and lower-cased; one @, a plain local part, and exactly the meta.com domain (not a subdomain, not a look-alike).
export function metaEmail(raw) {
  if (typeof raw !== 'string') return null;
  const email = raw.trim().toLowerCase();
  if (!email.endsWith(EMAIL_DOMAIN)) return null;
  const local = email.slice(0, -EMAIL_DOMAIN.length);
  if (!/^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$/.test(local)) return null;
  if (local.startsWith('.') || local.endsWith('.') || local.includes('..')) return null;
  return email;
}

// Cloudflare's siteverify: { ok, codes }. ok only when Cloudflare says success for this hostname. One retry, with the
// same idempotency key, if the first call fails to arrive or Cloudflare answers 5xx.
export async function verifyTurnstile({ token, ip, secret, hostname, fetchFn, idempotencyKey }) {
  if (typeof token !== 'string' || token.length === 0) return { ok: false, codes: ['missing-input-response'] };
  if (token.length > 2048) return { ok: false, codes: ['invalid-input-response'] };
  const form = new URLSearchParams({ secret, response: token, idempotency_key: idempotencyKey });
  if (ip) form.set('remoteip', ip);
  let body = null;
  for (let attempt = 0; attempt < 2 && body === null; attempt++) {
    try {
      const res = await fetchFn(SITEVERIFY, { method: 'POST', body: form });
      if (res.status >= 500) continue;
      body = await res.json();
    } catch {
      body = null;
    }
  }
  if (!body) return { ok: false, codes: ['siteverify-unavailable'] };
  const codes = (Array.isArray(body['error-codes']) ? body['error-codes'] : []).map(safeCode).filter(Boolean);
  if (body.success !== true) return { ok: false, codes: codes.length ? codes : ['not-success'] };
  if (hostname && body.hostname !== hostname) return { ok: false, codes: ['wrong-hostname'] };
  return { ok: true, codes: [] };
}

// Per-IP and daily counters in Workers KV: 'ok', 'ip', 'daily', or 'unavailable' (KV failed: refuse, never skip).
// Fixed windows (the clock hour, the UTC day). KV is eventually consistent, so a burst from many places at once can pass a
// limit by a few; Turnstile is in front of it. The IP is stored only as a hash.
export function kvLimits({ kv, nowMs, perIp = PER_IP_PER_HOUR, daily = DAILY_CAP }) {
  return async (ip) => {
    try {
      const t = nowMs();
      const ipHash = hex(await crypto.subtle.digest('SHA-256', enc.encode(`astryx-join:${ip || 'unknown'}`))).slice(0, 32);
      const ipKey = `ip:${ipHash}:${Math.floor(t / 3_600_000)}`;
      const dayKey = `day:${new Date(t).toISOString().slice(0, 10)}`;
      const [ipCount, dayCount] = (await Promise.all([kv.get(ipKey), kv.get(dayKey)])).map((v) => Number.parseInt(v || '0', 10) || 0);
      if (ipCount >= perIp) return 'ip';
      if (dayCount >= daily) return 'daily';
      await Promise.all([
        kv.put(ipKey, String(ipCount + 1), { expirationTtl: 3600 + 120 }),
        kv.put(dayKey, String(dayCount + 1), { expirationTtl: 2 * 86400 }),
      ]);
      return 'ok';
    } catch {
      return 'unavailable';
    }
  };
}

// App Store Connect's bearer token: ES256 over {iss, iat, exp, aud}, kid in the header, reused until five minutes remain.
export function ascTokens({ issuerId, keyId, privateKeyPem, now }) {
  let key = null;
  let token = null;
  let exp = 0;
  return async () => {
    const t = now();
    if (token && t < exp - ASC_TOKEN_REUSE_MARGIN) return token;
    if (!key) {
      const b64 = String(privateKeyPem).replace(/-----(BEGIN|END) PRIVATE KEY-----/g, '').replace(/\s+/g, '');
      const der = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      key = await crypto.subtle.importKey('pkcs8', der, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
    }
    const h = b64url(JSON.stringify({ alg: 'ES256', kid: keyId, typ: 'JWT' }));
    const p = b64url(JSON.stringify({ iss: issuerId, iat: t, exp: t + ASC_TOKEN_SECONDS, aud: 'appstoreconnect-v1' }));
    // WebCrypto's ECDSA signature is r||s, which is what JWS ES256 wants.
    const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, key, enc.encode(`${h}.${p}`));
    token = `${h}.${p}.${b64urlBytes(sig)}`;
    exp = t + ASC_TOKEN_SECONDS;
    return token;
  };
}

// One App Store Connect call: { status, body }. A network failure is status 0.
export function ascClient({ fetchFn, token }) {
  return async (method, path, body) => {
    try {
      const res = await fetchFn(`${ASC_ORIGIN}${path}`, {
        method,
        headers: { authorization: `Bearer ${await token()}`, 'content-type': 'application/json', accept: 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await res.text();
      let parsed = null;
      try {
        parsed = text ? JSON.parse(text) : null;
      } catch {
        parsed = null;
      }
      return { status: res.status, body: parsed };
    } catch {
      return { status: 0, body: null };
    }
  };
}

const errorsOf = (res) => (Array.isArray(res?.body?.errors) ? res.body.errors : []);
// Apple refuses a tester past the app's 10,000 external testers with a conflict whose text names the limit.
const isTesterLimit = (res) =>
  (res.status === 409 || res.status === 422) &&
  errorsOf(res).some((e) => /limit|maximum number|too many testers|10,?000/i.test(`${e?.code ?? ''} ${e?.title ?? ''} ${e?.detail ?? ''}`));
// Apple's error code only (ENTITY_ERROR.ATTRIBUTE.INVALID and the like): never its detail, which can quote the address.
const codeOf = (res) => {
  const code = errorsOf(res)[0]?.code;
  return typeof code === 'string' && /^[A-Z0-9_.]{1,80}$/.test(code) ? code : null;
};
const failed = (res) => ({ outcome: 'error', status: res.status, code: codeOf(res) });

// Invite one address into the group. Outcomes: invited, already (in this group), limit, error. Idempotent: asking again
// for an address already in the group changes nothing and answers already.
export async function inviteTester({ email, groupId, call }) {
  const created = await call('POST', '/v1/betaTesters', {
    data: {
      type: 'betaTesters',
      attributes: { email },
      relationships: { betaGroups: { data: [{ type: 'betaGroups', id: groupId }] } },
    },
  });
  if (created.status === 201) return { outcome: 'invited', status: 201, code: null };
  if (isTesterLimit(created)) return { outcome: 'limit', status: created.status, code: codeOf(created) };
  if (created.status !== 409) return failed(created);

  // A tester with this address exists: already in this group, or a tester of the app through another group.
  const q = encodeURIComponent(email);
  const inGroup = await call('GET', `/v1/betaTesters?filter[email]=${q}&filter[betaGroups]=${groupId}&fields[betaTesters]=email&limit=1`);
  if (inGroup.status !== 200) return failed(inGroup);
  if (Array.isArray(inGroup.body?.data) && inGroup.body.data.length > 0) return { outcome: 'already', status: 200, code: null };

  const anywhere = await call('GET', `/v1/betaTesters?filter[email]=${q}&fields[betaTesters]=email&limit=1`);
  if (anywhere.status !== 200) return failed(anywhere);
  const id = anywhere.body?.data?.[0]?.id;
  if (typeof id !== 'string' || !id) return failed(created);
  const added = await call('POST', `/v1/betaGroups/${groupId}/relationships/betaTesters`, { data: [{ type: 'betaTesters', id }] });
  if (added.status === 204 || added.status === 200) return { outcome: 'invited', status: added.status, code: null };
  if (isTesterLimit(added)) return { outcome: 'limit', status: added.status, code: codeOf(added) };
  return failed(added);
}

// ---------------------------------------------------------------------------------------------------------------------
// Pages: plain, phone first (people arrive from a QR code), system font, light and dark.
// ---------------------------------------------------------------------------------------------------------------------
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

const STYLE = `
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--text:#14171a;--muted:#5b6470;--line:#c9ced5;--accent:#0064e0;--accent-text:#fff;--ok:#0a7a3e;--warn:#9a5b00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#0f1114;--card:#1a1d21;--text:#eceff2;--muted:#a3abb5;--line:#3a4048;--accent:#4a9dff;--accent-text:#06121f;--ok:#5fd394;--warn:#f0b35a;--bad:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:17px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-text-size-adjust:100%}
main{max-width:30rem;margin:0 auto;padding:2.5rem 1rem 3rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:1.5rem}
h1{font-size:1.5rem;line-height:1.25;margin:0 0 .75rem}
p{margin:.75rem 0}
.for{color:var(--muted);overflow-wrap:anywhere}
.for strong{color:var(--text)}
.note{color:var(--muted);font-size:.95rem}
.msg{border-left:4px solid var(--bad);padding:.25rem 0 .25rem .75rem;margin:1rem 0}
label{display:block;font-weight:600;margin:1rem 0 .4rem}
input{display:block;width:100%;min-height:48px;padding:.6rem .75rem;border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--text);font:inherit}
.cf-turnstile{margin:1rem 0 0;min-height:65px}
button{display:block;width:100%;min-height:48px;margin:1rem 0 .5rem;border:0;border-radius:12px;background:var(--accent);color:var(--accent-text);font:inherit;font-weight:600;cursor:pointer}
button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
a{color:var(--accent)}
.ok h1{color:var(--ok)}.warn h1{color:var(--warn)}.bad h1{color:var(--bad)}
`;

function page(status, title, body, { tone = '', turnstile = false } = {}) {
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>${escapeHtml(title)} · Astryx Swift TestFlight</title>
<style>${STYLE}</style>
${turnstile ? `<script src="${TURNSTILE_SCRIPT}" async defer></script>\n` : ''}</head>
<body>
<main>
<div class="card ${tone}">
${body}
</div>
</main>
</body>
</html>
`;
  const csp = turnstile
    ? "default-src 'none'; script-src https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    : "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'";
  return new Response(html, {
    status,
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'cache-control': 'no-store',
      'content-security-policy': csp,
      'x-content-type-options': 'nosniff',
      'referrer-policy': 'no-referrer',
    },
  });
}

const forAddress = (email) => `<p class="for">For <strong>${escapeHtml(email)}</strong></p>`;

// The form, with an optional message above it and the address the visitor typed kept in the field.
export function formPage(siteKey, { status = 200, message = '', email = '' } = {}) {
  return page(status, 'Get the beta', `<h1>Astryx Swift on TestFlight</h1>
<p class="note">For Meta engineers: only @meta.com addresses can join, and TestFlight sends the invitation to that address.</p>
${message ? `<p class="msg" role="alert">${escapeHtml(message)}</p>\n` : ''}<form method="post" action="/join">
<label for="email">Your @meta.com email</label>
<input id="email" name="email" type="email" autocomplete="email" inputmode="email" autocapitalize="none" spellcheck="false" required maxlength="254" value="${escapeHtml(email)}">
<div class="cf-turnstile" data-sitekey="${escapeHtml(siteKey)}" data-theme="auto"></div>
<button type="submit">Send me the TestFlight invite</button>
</form>
<p class="note">TestFlight emails an invitation to this address. Install the TestFlight app on your iPhone or Mac, then open the invitation there to install Astryx Swift.</p>`, { status, turnstile: true });
}

export function outcomePage(outcome, email) {
  switch (outcome) {
    case 'invited':
      return page(200, 'Invitation sent', `<h1>Invitation on its way</h1>
${forAddress(email)}
<p>Look for an email from TestFlight at this address. Open it on your iPhone or Mac with the TestFlight app installed, and accept it to install Astryx Swift.</p>`, { tone: 'ok' });
    case 'already':
      return page(200, 'Already invited', `<h1>You're already invited</h1>
${forAddress(email)}
<p>This address is already a tester. Look for the email from TestFlight, or open the TestFlight app on your iPhone or Mac, signed in with the Apple Account that accepted the invitation.</p>`, { tone: 'ok' });
    case 'limit':
      return page(200, 'No places left', `<h1>The beta is full</h1>
${forAddress(email)}
<p>Apple allows 10,000 external testers per app, and Astryx Swift has reached that limit, so no new invitations can be sent. Nothing was changed.</p>`, { tone: 'warn' });
    default:
      throw new Error(`no page for ${outcome}`);
  }
}

const tryLater = () => page(429, 'Try again later', `<h1>Try again later</h1>
<p>Too many invitations have been asked for just now. Nothing was sent. Try again later.</p>
<p><a href="/">Back to the form</a></p>`, { tone: 'warn' });

// A POST must come from this page: the browser's Origin, or failing that its Fetch Metadata, names this origin.
export function sameOrigin(request) {
  const self = new URL(request.url).origin;
  const origin = request.headers.get('origin');
  if (origin !== null) return origin === self;
  return request.headers.get('sec-fetch-site') === 'same-origin';
}

// deps: { config: {siteKey, groupId}, turnstile({token, ip, hostname}) -> {ok, codes}, limits(ip) -> 'ok'|'ip'|'daily'|
//         'unavailable', call(method, path, body) -> {status, body}, log(obj) }
export function makeHandler(deps) {
  const { config, log } = deps;
  const configured = typeof config.siteKey === 'string' && config.siteKey.length > 0 && UUID.test(config.groupId || '');
  return async (request) => {
    const url = new URL(request.url);
    const method = request.method;
    if (!configured) {
      log({ event: 'misconfigured' });
      return page(500, 'Not set up', '<h1>Not set up yet</h1><p>This page is missing its configuration. Try again later.</p>', { tone: 'bad' });
    }
    if (url.pathname === '/' && (method === 'GET' || method === 'HEAD')) return formPage(config.siteKey);
    if (url.pathname === '/join' && method === 'GET') return new Response(null, { status: 303, headers: { location: '/', 'cache-control': 'no-store' } });
    if (url.pathname !== '/join' || method !== 'POST') return page(404, 'Not found', '<h1>Not found</h1><p><a href="/">Back to the form</a></p>');

    if (!sameOrigin(request)) {
      log({ event: 'refused', reason: 'cross-origin post' });
      return page(403, 'Start from the form', '<h1>Start from the form</h1><p><a href="/">Open the form</a> and use its button to ask for the invitation.</p>', { tone: 'bad' });
    }
    const type = (request.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    const length = Number(request.headers.get('content-length') || '0');
    if (type !== 'application/x-www-form-urlencoded' || length > MAX_BODY) {
      log({ event: 'refused', reason: 'not a form post' });
      return formPage(config.siteKey, { status: 400, message: 'Use the form below to ask for the invitation.' });
    }
    const text = await request.text();
    if (text.length > MAX_BODY) return formPage(config.siteKey, { status: 400, message: 'Use the form below to ask for the invitation.' });
    const form = new URLSearchParams(text);
    const typed = (form.get('email') || '').slice(0, 254);
    const ip = request.headers.get('cf-connecting-ip') || '';

    // Turnstile first: nothing else runs for a request that is not a person on this page.
    const human = await deps.turnstile({ token: form.get('cf-turnstile-response'), ip, hostname: url.hostname });
    if (!human.ok) {
      log({ event: 'refused', reason: 'turnstile', codes: human.codes });
      return formPage(config.siteKey, { status: 403, email: typed, message: "We couldn't confirm the check above. Wait for it to finish, then press the button again." });
    }

    const email = metaEmail(typed);
    if (!email) {
      log({ event: 'refused', reason: 'outside the allowed domain' });
      return formPage(config.siteKey, {
        status: 400,
        email: typed,
        message: 'Only @meta.com addresses can join this beta. Enter your @meta.com work address.',
      });
    }

    const limit = await deps.limits(ip);
    if (limit !== 'ok') {
      log({ event: 'limited', which: limit });
      return tryLater();
    }

    const result = await inviteTester({ email, groupId: config.groupId, call: deps.call });
    log({ event: 'join', outcome: result.outcome, status: result.status, code: result.code });
    if (result.outcome === 'error') {
      return formPage(config.siteKey, {
        status: 502,
        email,
        message: 'App Store Connect did not accept the request just now, so no invitation was sent. Try again in a minute.',
      });
    }
    return outcomePage(result.outcome, email);
  };
}

// The Worker. One handler per isolate, built from its bindings on first use.
let handler = null;
export default {
  async fetch(request, env) {
    if (!handler) {
      const now = () => Math.floor(Date.now() / 1000);
      handler = makeHandler({
        config: { siteKey: env.TURNSTILE_SITE_KEY, groupId: env.ASC_BETA_GROUP_ID },
        turnstile: ({ token, ip, hostname }) =>
          verifyTurnstile({ token, ip, hostname, secret: env.TURNSTILE_SECRET, fetchFn: (u, init) => fetch(u, init), idempotencyKey: crypto.randomUUID() }),
        limits: kvLimits({ kv: env.LIMITS, nowMs: () => Date.now() }),
        call: ascClient({
          fetchFn: (u, init) => fetch(u, init),
          token: ascTokens({ issuerId: env.ASC_ISSUER_ID, keyId: env.ASC_KEY_ID, privateKeyPem: env.ASC_PRIVATE_KEY, now }),
        }),
        log: (o) => console.log(JSON.stringify(o)),
      });
    }
    return handler(request);
  },
};

// Tests only: forget the handler built from an earlier env.
export function resetForTests() {
  handler = null;
}
