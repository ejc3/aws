import { createRemoteJWKSet, jwtVerify } from 'jose';

export class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export function readConfig(env = process.env) {
  const baseUrl = new URL(env.BM_BASE_URL || 'https://browsers.cc-games.dev');
  const issuer = new URL(env.BM_ACCESS_ISSUER || 'https://ejc3.cloudflareaccess.com');
  const audience = env.BM_ACCESS_AUD || '';
  const serviceTokenId = env.BM_ACCESS_SERVICE_TOKEN_ID || '';
  const owner = (env.BM_OWNER_EMAIL || '').toLowerCase();
  const port = Number(env.BM_PORT || 3210);
  if (baseUrl.protocol !== 'https:' || baseUrl.pathname !== '/' || baseUrl.search ||
      baseUrl.hash || baseUrl.username || baseUrl.password ||
      issuer.protocol !== 'https:' || !issuer.hostname.endsWith('.cloudflareaccess.com') ||
      issuer.pathname !== '/' || issuer.search || issuer.hash || issuer.username || issuer.password ||
      !audience || (serviceTokenId && !/^[a-f0-9]{32}\.access$/.test(serviceTokenId)) ||
      !owner.includes('@') || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('Set BM_BASE_URL, BM_ACCESS_AUD, BM_ACCESS_ISSUER, BM_ACCESS_SERVICE_TOKEN_ID, and BM_OWNER_EMAIL to the Terraform outputs');
  }
  return { baseUrl: baseUrl.origin, issuer: issuer.origin, audience, serviceTokenId, owner, port };
}

/** Trust the signed identity, never the unverified email header. No token/session cache. */
export function createAuthorizer(config, key = createRemoteJWKSet(
  new URL(`${config.issuer}/cdn-cgi/access/certs`), { timeoutDuration: 5000 },
)) {
  return async (request) => {
    const token = request.headers['cf-access-jwt-assertion'];
    if (typeof token !== 'string' || token.length > 16384) throw new HttpError(401, 'Sign in through Cloudflare Access');
    try {
      const { payload } = await jwtVerify(token, key, {
        issuer: config.issuer, audience: config.audience, algorithms: ['RS256'],
        requiredClaims: ['exp', 'iat'],
      });
      const owner = typeof payload.email === 'string' && payload.email.toLowerCase() === config.owner;
      const service = Boolean(config.serviceTokenId) && payload.type === 'app' && payload.sub === '' &&
        payload.common_name === config.serviceTokenId;
      if (!owner && !service) throw new Error('principal mismatch');
      return { expiresAt: payload.exp * 1000 };
    } catch {
      throw new HttpError(403, 'Cloudflare Access session is invalid or expired');
    }
  };
}

export function requireOrigin(request, baseUrl) {
  if (request.headers.origin !== baseUrl) throw new HttpError(403, 'Request origin is not allowed');
}

export function instanceName(value) {
  if (typeof value !== 'string' || !/^[a-z0-9][a-z0-9-]{0,47}$/.test(value)) {
    throw new HttpError(400, 'Use a name of 1–48 lowercase letters, numbers, or hyphens');
  }
  return value;
}

export function instanceLabel(value) {
  if (typeof value !== 'string' || /[\u0000-\u001f\u007f-\u009f]/u.test(value) ||
      !value.trim() || value.trim().length > 80) {
    throw new HttpError(400, 'Use a label of 1–80 characters without control characters');
  }
  return value.trim();
}

export const DESKTOP_VIEWPORT = Object.freeze({ mode: 'desktop', width: 1440, height: 900 });

/** Two explicit display modes, never arbitrary native command/geometry arguments. */
export function browserViewport(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new HttpError(400, 'Invalid browser viewport');
  if (value.mode === 'desktop' && Object.keys(value).length === 1) return { ...DESKTOP_VIEWPORT };
  if (value.mode !== 'phone' || Object.keys(value).some(key => !['mode', 'width', 'height'].includes(key)) ||
      !Number.isInteger(value.width) || value.width < 320 || value.width > 500 ||
      !Number.isInteger(value.height) || value.height < 480 || value.height > 900) {
    throw new HttpError(400, 'Choose Desktop mode or a phone viewport 320–500 pixels wide and 480–900 pixels high');
  }
  return { mode: 'phone', width: value.width, height: value.height };
}

export function startUrl(value) {
  if (value === undefined) return undefined;
  try {
    if (typeof value !== 'string' || value.length > 2048) throw new Error();
    const url = new URL(value);
    if (!['http:', 'https:', 'about:'].includes(url.protocol) || url.username || url.password ||
        (url.protocol === 'about:' && value !== 'about:blank')) throw new Error();
    return url.href;
  } catch { throw new HttpError(400, 'Use an http(s) URL or about:blank'); }
}
