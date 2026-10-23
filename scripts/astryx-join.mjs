// Astryx Swift TestFlight invites for @meta.com addresses: the Worker behind astryx-join.dolphin-labs.dev (astryx-join.tf).
//
//   GET  /      who you are signed in as, and one button
//   POST /join  adds you as an external tester of the app, in the self-serve beta group
//
// Cloudflare Access in front admits only @meta.com addresses, proven by a one-time PIN mailed to the address. This file
// does not trust that alone: every request's Access token is verified here (RS256 by the team's keys, the application's
// audience, the team issuer, the time) and the address must end in @meta.com. The address is never typed: it is the
// token's email claim.
//
// App Store Connect is called with an ES256 token signed here (WebCrypto) by a dedicated App Manager key. Nothing this
// Worker logs carries an address: a log line names the outcome, an HTTP status and Apple's error code, no more.
//
// Bindings (astryx-join.tf): ACCESS_TEAM_DOMAIN, ACCESS_AUD, ASC_BETA_GROUP_ID (plain text); ASC_ISSUER_ID, ASC_KEY_ID,
// ASC_PRIVATE_KEY (secrets, from Secrets Manager astryx/testflight-asc-key). Tests: node --test scripts/test-astryx-join.mjs

const ASC_ORIGIN = 'https://api.appstoreconnect.apple.com';
const EMAIL_DOMAIN = '@meta.com';
const SKEW_SECONDS = 60;
const JWKS_TTL_MS = 10 * 60 * 1000;
const JWKS_REFRESH_MIN_MS = 60 * 1000;
const ASC_TOKEN_SECONDS = 20 * 60; // Apple's maximum lifetime
const ASC_TOKEN_REUSE_MARGIN = 5 * 60; // a fresh token once fewer than five minutes remain
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TEAM = /^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/;

const enc = new TextEncoder();
const b64urlBytes = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const b64url = (text) => b64urlBytes(enc.encode(text));
const unb64url = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (s.length % 4)) % 4)), (c) => c.charCodeAt(0));
const b64urlJson = (s) => JSON.parse(new TextDecoder().decode(unb64url(s)));

// Lower-cased, one @, a plain local part, and exactly the meta.com domain (not a subdomain, not a look-alike).
export function metaEmail(raw) {
  if (typeof raw !== 'string') return null;
  const email = raw.toLowerCase();
  if (!email.endsWith(EMAIL_DOMAIN)) return null;
  const local = email.slice(0, -EMAIL_DOMAIN.length);
  if (!/^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$/.test(local)) return null;
  return email;
}

export async function verifyAccessToken(token, { teamDomain, aud, getKey, now }) {
  if (typeof token !== 'string' || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) throw new Error('malformed token');
  const [h, p, s] = token.split('.');
  const header = b64urlJson(h);
  if (header.alg !== 'RS256' || typeof header.kid !== 'string') throw new Error('unexpected algorithm or key id');
  const key = await getKey(header.kid);
  if (!key) throw new Error('unknown key id');
  const ok = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, unb64url(s), enc.encode(`${h}.${p}`));
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

// The team's signing keys as CryptoKeys, cached; an unknown kid refetches at most once a minute.
export function jwksCache({ teamDomain, fetchJson, nowMs }) {
  let keys = null;
  let fetchedAt = 0;
  const load = async () => {
    const body = await fetchJson(`${teamDomain}/cdn-cgi/access/certs`);
    const next = new Map();
    for (const k of Array.isArray(body?.keys) ? body.keys : []) {
      if (k?.kty !== 'RSA' || typeof k.kid !== 'string' || typeof k.n !== 'string' || typeof k.e !== 'string') continue;
      next.set(k.kid, await crypto.subtle.importKey('jwk', { kty: 'RSA', n: k.n, e: k.e }, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']));
    }
    keys = next;
    fetchedAt = nowMs();
  };
  return async (kid) => {
    if (!keys || nowMs() - fetchedAt > JWKS_TTL_MS) await load();
    if (!keys.has(kid) && nowMs() - fetchedAt > JWKS_REFRESH_MIN_MS) await load();
    return keys.get(kid) || null;
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
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--text:#14171a;--muted:#5b6470;--line:#dde1e6;--accent:#0064e0;--accent-text:#fff;--ok:#0a7a3e;--warn:#9a5b00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#0f1114;--card:#1a1d21;--text:#eceff2;--muted:#a3abb5;--line:#2c3137;--accent:#4a9dff;--accent-text:#06121f;--ok:#5fd394;--warn:#f0b35a;--bad:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:17px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-text-size-adjust:100%}
main{max-width:30rem;margin:0 auto;padding:2.5rem 1rem 3rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:1.5rem}
h1{font-size:1.5rem;line-height:1.25;margin:0 0 .75rem}
p{margin:.75rem 0}
.who{color:var(--muted);overflow-wrap:anywhere}
.who strong{color:var(--text)}
.note{color:var(--muted);font-size:.95rem}
button{display:block;width:100%;min-height:48px;margin:1.25rem 0 .5rem;border:0;border-radius:12px;background:var(--accent);color:var(--accent-text);font:inherit;font-weight:600;cursor:pointer}
button:focus-visible,a:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
a{color:var(--accent)}
.ok h1{color:var(--ok)}.warn h1{color:var(--warn)}.bad h1{color:var(--bad)}
footer{margin-top:1.25rem;font-size:.9rem;color:var(--muted);text-align:center}
`;

function page(status, title, body, tone = '') {
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>${escapeHtml(title)} · Astryx Swift TestFlight</title>
<style>${STYLE}</style>
</head>
<body>
<main>
<div class="card ${tone}">
${body}
</div>
<footer><a href="/cdn-cgi/access/logout">Sign out</a></footer>
</main>
</body>
</html>
`;
  return new Response(html, {
    status,
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'cache-control': 'no-store',
      'content-security-policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
      'x-content-type-options': 'nosniff',
      'referrer-policy': 'no-referrer',
    },
  });
}

const signedInAs = (email) => `<p class="who">Signed in as <strong>${escapeHtml(email)}</strong></p>`;
const joinForm = (label) => `<form method="post" action="/join"><button type="submit">${escapeHtml(label)}</button></form>`;

export function homePage(email) {
  return page(200, 'Get the beta', `<h1>Astryx Swift on TestFlight</h1>
${signedInAs(email)}
${joinForm('Send me the TestFlight invite')}
<p class="note">TestFlight emails an invitation to this address. Install the TestFlight app on your iPhone or Mac, then open the invitation there to install Astryx Swift.</p>`);
}

export function outcomePage(outcome, email) {
  switch (outcome) {
    case 'invited':
      return page(200, 'Invitation sent', `<h1>Invitation on its way</h1>
${signedInAs(email)}
<p>Look for an email from TestFlight at this address. Open it on your iPhone or Mac with the TestFlight app installed, and accept it to install Astryx Swift.</p>`, 'ok');
    case 'already':
      return page(200, 'Already invited', `<h1>You're already invited</h1>
${signedInAs(email)}
<p>This address is already a tester. Look for the email from TestFlight, or open the TestFlight app on your iPhone or Mac, signed in with the Apple Account that accepted the invitation.</p>`, 'ok');
    case 'limit':
      return page(200, 'No places left', `<h1>The beta is full</h1>
${signedInAs(email)}
<p>Apple allows 10,000 external testers per app, and Astryx Swift has reached that limit, so no new invitations can be sent. Nothing was changed.</p>`, 'warn');
    default:
      return page(502, 'Try again', `<h1>That didn't work</h1>
${signedInAs(email)}
<p>App Store Connect did not accept the request just now, so no invitation was sent. Try again in a minute.</p>
${joinForm('Try again')}`, 'bad');
  }
}

const refused = (title, text) => page(403, title, `<h1>${escapeHtml(title)}</h1><p>${escapeHtml(text)}</p>`, 'bad');

// A POST must come from this page: the browser's Origin, or failing that its Fetch Metadata, names this origin.
export function sameOrigin(request) {
  const self = new URL(request.url).origin;
  const origin = request.headers.get('origin');
  if (origin !== null) return origin === self;
  return request.headers.get('sec-fetch-site') === 'same-origin';
}

// deps: { config: {teamDomain, aud, groupId}, getKey(kid) -> CryptoKey|null, call(method, path, body) -> {status, body},
//         now() seconds, log(obj) }
export function makeHandler(deps) {
  const { config, log } = deps;
  const configured = TEAM.test(config.teamDomain || '') && typeof config.aud === 'string' && config.aud.length > 0 && UUID.test(config.groupId || '');
  return async (request) => {
    const url = new URL(request.url);
    const method = request.method;
    if (!configured) {
      log({ event: 'misconfigured' });
      return page(500, 'Not set up', '<h1>Not set up yet</h1><p>This page is missing its configuration. Try again later.</p>', 'bad');
    }

    let claims;
    try {
      claims = await verifyAccessToken(request.headers.get('cf-access-jwt-assertion'), { teamDomain: config.teamDomain, aud: config.aud, getKey: deps.getKey, now: deps.now });
    } catch (e) {
      log({ event: 'refused', reason: String(e.message), method, path: url.pathname });
      return refused('Sign-in required', 'Open this page again to sign in with your @meta.com address.');
    }
    const email = metaEmail(claims.email);
    if (!email) {
      log({ event: 'refused', reason: 'outside the allowed domain', method, path: url.pathname });
      return refused('Only @meta.com addresses', 'This beta is open to @meta.com addresses. Sign out and sign in with yours.');
    }

    if (url.pathname === '/' && (method === 'GET' || method === 'HEAD')) return homePage(email);
    if (url.pathname === '/join' && method === 'GET') return new Response(null, { status: 303, headers: { location: '/', 'cache-control': 'no-store' } });
    if (url.pathname === '/join' && method === 'POST') {
      if (!sameOrigin(request)) {
        log({ event: 'refused', reason: 'cross-origin post', method, path: url.pathname });
        return refused('Start from the page', 'Open this page and use its button to ask for the invitation.');
      }
      const result = await inviteTester({ email, groupId: config.groupId, call: deps.call });
      log({ event: 'join', outcome: result.outcome, status: result.status, code: result.code });
      return outcomePage(result.outcome, email);
    }
    return page(404, 'Not found', '<h1>Not found</h1><p><a href="/">Back to the invitation page</a></p>');
  };
}

// The Worker. One handler per isolate, built from its bindings on first use.
let handler = null;
export default {
  async fetch(request, env) {
    if (!handler) {
      const now = () => Math.floor(Date.now() / 1000);
      const fetchJson = async (u) => {
        const res = await fetch(u, { headers: { accept: 'application/json' } });
        if (!res.ok) throw new Error(`certs ${res.status}`);
        return res.json();
      };
      handler = makeHandler({
        config: { teamDomain: env.ACCESS_TEAM_DOMAIN, aud: env.ACCESS_AUD, groupId: env.ASC_BETA_GROUP_ID },
        getKey: jwksCache({ teamDomain: env.ACCESS_TEAM_DOMAIN, fetchJson, nowMs: () => Date.now() }),
        call: ascClient({
          fetchFn: (u, init) => fetch(u, init),
          token: ascTokens({ issuerId: env.ASC_ISSUER_ID, keyId: env.ASC_KEY_ID, privateKeyPem: env.ASC_PRIVATE_KEY, now }),
        }),
        now,
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
