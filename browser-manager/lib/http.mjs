import { createServer } from 'node:http';
import { connect } from 'node:net';
import { WebSocketServer, createWebSocketStream } from 'ws';
import { browserViewport, HttpError, instanceLabel, instanceName, requireOrigin, startUrl } from './auth.mjs';

const json = (res, status, body) => {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff' });
  res.end(JSON.stringify(body));
};

async function readBody(req) {
  if (req.headers['content-type']?.split(';')[0] !== 'application/json') throw new HttpError(415, 'Send application/json');
  const chunks = [];
  let size = 0;
  for await (const part of req) {
    size += part.length;
    if (size > 4096) throw new HttpError(413, 'Request is too large');
    chunks.push(part);
  }
  try {
    const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error();
    return data;
  } catch { throw new HttpError(400, 'Invalid JSON object'); }
}

function pathOf(req, baseUrl) {
  if (!req.url?.startsWith('/') || req.url.startsWith('//')) throw new HttpError(400, 'Invalid path');
  const url = new URL(req.url, baseUrl);
  if (url.origin !== baseUrl) throw new HttpError(400, 'Invalid path');
  return url.pathname;
}

/** Same small API for the authenticated web listener and owner-only local CLI socket. */
export function createApi({ manager, baseUrl, local = false, transport = 'vnc' }) {
  return async (req, res, path) => {
    if (!path.startsWith('/api/')) return false;
    if (req.method === 'GET' && path === '/api/browsers') {
      json(res, 200, { browsers: await manager.list() }); return true;
    }
    if (req.method === 'GET' && path === '/api/config') {
      json(res, 200, { baseUrl, transport }); return true;
    }
    const navigation = /^\/api\/browsers\/([a-z0-9-]+)\/navigation$/.exec(path);
    if (req.method === 'GET' && navigation) {
      json(res, 200, await manager.getNavigation(instanceName(navigation[1]))); return true;
    }
    if (req.method !== 'POST') throw new HttpError(404, 'Unknown API route');
    if (!local) requireOrigin(req, baseUrl);
    const data = await readBody(req);
    // The CDP bridge hands a debugging endpoint for a live, signed-in browser to the caller: the owner-only local
    // control socket only, never the web listener.
    const cdp = /^\/api\/browsers\/([a-z0-9-]+)\/cdp$/.exec(path);
    if (cdp) {
      if (!local) throw new HttpError(404, 'Unknown API route');
      if (Object.keys(data).length) throw new HttpError(400, 'Unsupported cdp field');
      json(res, 200, await manager.openCdpBridge(instanceName(cdp[1]))); return true;
    }
    const action = /^\/api\/browsers\/([a-z0-9-]+)\/(start|stop|rename|viewport)$/.exec(path);
    if (path !== '/api/browsers' && !action) throw new HttpError(404, 'Unknown API route');
    const name = instanceName(action ? action[1] : data.name);
    if (action?.[2] === 'viewport') {
      browserViewport(data);
      json(res, 200, { browser: await manager.setViewport(name, data) }); return true;
    }
    if (action?.[2] === 'rename') {
      if (Object.keys(data).some(key => key !== 'label')) throw new HttpError(400, 'Unsupported rename field');
      const label = instanceLabel(data.label);
      if (!manager.list().some(browser => browser.name === name)) throw new HttpError(404, 'Unknown browser');
      json(res, 200, { browser: await manager.rename(name, label) }); return true;
    }
    if (action?.[2] === 'stop') {
      json(res, 200, await manager.stop(name)); return true;
    }
    const allowed = new Set(local ? ['name', 'url', 'profile'] : ['name', 'url']);
    if (Object.keys(data).some(key => !allowed.has(key))) throw new HttpError(400, 'Unsupported start field');
    if (data.profile !== undefined && (typeof data.profile !== 'string' || !data.profile.startsWith('/'))) {
      throw new HttpError(400, 'Profile must be an absolute local path');
    }
    const url = startUrl(data.url);
    json(res, 200, await manager.start(name, {
      ...(url === undefined ? {} : { url }),
      ...(local && data.profile ? { profile: data.profile } : {}),
    }));
    return true;
  };
}

export function createHttpServer({ manager, config, authorize, nextHandler, local = false, isReady = () => true }) {
  const api = createApi({ manager, baseUrl: config.baseUrl, local, transport: config.transport });
  const server = createServer(async (req, res) => {
    try {
      if (!local) await authorize(req);
      if (!isReady()) throw new HttpError(503, 'Browser manager is starting or stopping');
      const path = pathOf(req, config.baseUrl);
      res.setHeader('Cache-Control', 'no-store');
      res.setHeader('X-Content-Type-Options', 'nosniff');
      res.setHeader('Referrer-Policy', 'no-referrer');
      res.setHeader('X-Frame-Options', 'DENY');
      if (await api(req, res, path)) return;
      if (local) throw new HttpError(404, 'Unknown local route');
      await nextHandler(req, res);
    } catch (error) {
      if (!res.headersSent) json(res, error instanceof HttpError ? error.status : 500,
        { error: error instanceof HttpError ? error.message : 'Operation failed; check the host service' });
      else res.destroy();
    }
  });
  server.requestTimeout = 15000;
  server.headersTimeout = 10000;
  server.on('clientError', (_error, socket) => socket.destroy());
  if (local) return server;

  const wss = new WebSocketServer({ noServer: true, maxPayload: 2 * 1024 * 1024, perMessageDeflate: false });
  const pages = new WebSocketServer({ noServer: true, maxPayload: 16384, perMessageDeflate: false });
  const pageViewers = new Map();
  server.closeVnc = () => {
    for (const pool of [wss, pages]) for (const client of pool.clients) client.terminate();
  };
  server.on('upgrade', async (req, socket, head) => {
    socket.on('error', () => {});
    try {
      const identity = await authorize(req);
      if (!isReady()) throw new HttpError(503, 'Browser manager is starting or stopping');
      requireOrigin(req, config.baseUrl);
      const path = pathOf(req, config.baseUrl);
      const pageRoute = /^\/browsers\/([a-z0-9-]+)\/page$/.exec(path);
      if (pageRoute) {
        const name = instanceName(pageRoute[1]);
        const desktop = manager.getPage?.(name);
        if (!desktop) throw new HttpError(404, 'Browser is not running');
        if ((pageViewers.get(name) ?? 0) >= 8) throw new HttpError(429, 'Too many browser viewers');
        if (!Number.isFinite(identity.expiresAt) || identity.expiresAt <= Date.now()) {
          throw new HttpError(401, 'Sign in again');
        }
        pages.handleUpgrade(req, socket, head, ws => {
          pageViewers.set(name, (pageViewers.get(name) ?? 0) + 1);
          let active = true;
          let unsubscribe = () => {};
          let pending = 0;
          let tail = Promise.resolve();
          let tokens = 120;
          let updated = Date.now();
          let alive = true;
          const authorized = () => active && isReady() && Date.now() < identity.expiresAt &&
            manager.getPage?.(name) === desktop;
          const send = message => {
            if (message.type === 'closed') { disconnect(1000, 'Browser stopped'); return; }
            if (!authorized() || ws.readyState !== 1) return;
            // Frames may be dropped, but losing tab identity would show new pixels beneath an
            // old address/title and allow input into the wrong labeled page. Reconnect to resync.
            if (ws.bufferedAmount > 2 * 1024 * 1024) {
              if (message.type !== 'frame') disconnect(1013, 'Slow viewer; reconnect to resync');
              return;
            }
            ws.send(JSON.stringify(message));
          };
          const cleanup = () => {
            if (!active) return;
            active = false;
            clearTimeout(expire);
            clearInterval(heartbeat);
            unsubscribe();
            const remaining = (pageViewers.get(name) ?? 1) - 1;
            if (remaining) pageViewers.set(name, remaining); else pageViewers.delete(name);
          };
          const disconnect = (code, reason) => { cleanup(); ws.close(code, reason); };
          const expire = setTimeout(() => disconnect(1008, 'Sign in again'),
            Math.max(1, Math.min(identity.expiresAt - Date.now(), 2147483647)));
          expire.unref();
          const heartbeat = setInterval(() => {
            if (!alive || !authorized()) { cleanup(); ws.terminate(); return; }
            alive = false;
            ws.ping();
          }, 30000);
          heartbeat.unref();
          ws.on('pong', () => { alive = true; });
          ws.once('close', cleanup);
          ws.once('error', cleanup);
          ws.on('message', (bytes, binary) => {
            if (!authorized()) { disconnect(1008, 'Browser session expired'); return; }
            const now = Date.now();
            tokens = Math.min(120, tokens + (now - updated) * 0.12);
            updated = now;
            if (binary || bytes.length > 16384 || tokens < 1 || pending >= 32) {
              disconnect(1008, 'Invalid or excessive browser input'); return;
            }
            tokens--;
            let message;
            try {
              message = JSON.parse(bytes.toString('utf8'));
              if (!message || typeof message !== 'object' || Array.isArray(message)) throw new Error();
            } catch { disconnect(1008, 'Invalid browser input'); return; }
            pending++;
            tail = tail.then(async () => {
              // Queued input loses authority immediately on expiry/disconnect/replacement.
              if (authorized()) await desktop.command(message, { isAuthorized: authorized });
            }).catch(() => send({ type: 'error', message: 'Browser action could not be completed.' }))
              .finally(() => { pending--; });
          });
          try { unsubscribe = desktop.subscribe(send); }
          catch { disconnect(1011, 'Browser stream unavailable'); }
        });
        return;
      }
      const route = /^\/browsers\/([a-z0-9-]+)\/vnc$/.exec(path);
      if (!route) throw new HttpError(404, 'Unknown desktop');
      const target = manager.getSocket(instanceName(route[1]));
      if (!target) throw new HttpError(404, 'Desktop is not running');
      wss.handleUpgrade(req, socket, head, ws => {
        const upstream = connect({ path: target });
        const stream = createWebSocketStream(ws, { encoding: undefined });
        const expire = setTimeout(() => {
          // Closing WebSocket peers have up to 30s to answer the close frame. Desktop
          // authority expires now, even if a client ignores that handshake and sends input.
          upstream.unpipe(stream);
          stream.unpipe(upstream);
          upstream.destroy();
          ws.close(1008, 'Sign in again to reconnect');
        },
          Math.max(1, Math.min(identity.expiresAt - Date.now(), 2147483647)));
        expire.unref();
        const close = () => { clearTimeout(expire); upstream.destroy(); stream.destroy(); };
        ws.once('close', close);
        stream.once('error', close);
        upstream.once('error', () => ws.close(1011, 'Desktop disconnected'));
        upstream.once('close', () => ws.close(1000, 'Desktop stopped'));
        upstream.pipe(stream).pipe(upstream);
      });
    } catch (error) {
      const status = error instanceof HttpError ? error.status : 403;
      socket.end(`HTTP/1.1 ${status} Rejected\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);
    }
  });
  server.once('close', () => { server.closeVnc(); wss.close(); pages.close(); });
  return server;
}
