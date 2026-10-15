import { randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { WebSocketServer } from 'ws';

// The browser's remote-debugging pipe (fds 3/4, NUL-delimited JSON) has one reader. createCdpMux shares it between the
// desktop's own use (the user-agent mask) and any number of bridge clients, and keeps them apart: each client gets its
// own browser-level session (Target.attachToBrowserTarget), which behaves like a separate connection. Its commands go
// to that session, the targets it attaches are its own sessions, and its events reach only it. Detaching that session
// when the client goes undoes everything the client set (emulation, auto-attach, domains enabled), so nothing it did
// outlives it, and it can never send a command on the desktop's own sessions. Command ids are remapped to ids unique
// on the pipe and mapped back on the reply.

// Commands that would end the person's browser rather than drive it. A job attaches to someone's live session; it may
// open and close its own tabs, never the browser.
export const REFUSED_METHODS = new Set(['Browser.close', 'Browser.crash', 'Browser.crashGpuProcess']);

// Commands acknowledged but never forwarded: browser-wide settings no session scopes, which would outlive the client.
// Playwright's connect_over_cdp points downloads at its own temporary directory and deletes it on exit, so a later
// download by the person would land in a directory that no longer exists. The browser keeps its own settings.
export const IGNORED_METHODS = new Set(['Browser.setDownloadBehavior', 'Page.setDownloadBehavior']);

export function createCdpMux(writer, reader) {
  let nextId = 0;
  let buffer = Buffer.alloc(0);
  const pending = new Map(); // pipe id -> { deliver, id }
  const listeners = new Set(); // the desktop's own event listeners
  const owners = new Map(); // session id -> client, for every session a client owns (its browser session included)
  const write = (message) => {
    try { writer.write(`${JSON.stringify(message)}\0`); } catch { /* pipe closed: the desktop is going away */ }
  };
  const request = (method, params, sessionId, deliver = () => {}) => {
    const id = ++nextId;
    pending.set(id, { deliver, id });
    write({ id, method, params, ...(sessionId ? { sessionId } : {}) });
    return id;
  };
  writer.on?.('error', () => {});
  reader.on?.('error', () => {});
  reader.on('data', (part) => {
    buffer = Buffer.concat([buffer, part]);
    let end;
    while ((end = buffer.indexOf(0)) !== -1) {
      const packet = buffer.subarray(0, end);
      buffer = buffer.subarray(end + 1);
      let message;
      try { message = JSON.parse(packet.toString('utf8')); } catch { continue; }
      if (message.id !== undefined) {
        const entry = pending.get(message.id);
        if (!entry) continue;
        pending.delete(message.id);
        entry.deliver({ ...message, id: entry.id });
        continue;
      }
      const client = message.sessionId ? owners.get(message.sessionId) : undefined;
      if (client) {
        client.event(message);
        continue;
      }
      for (const listener of listeners) {
        try { listener(message); } catch { /* one listener never stops the others */ }
      }
    }
  });
  return {
    /** The desktop's own commands: fire and forget, replies are dropped. */
    send(method, params = {}, sessionId) { request(method, params, sessionId); },
    onEvent(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    /** A bridge client: `deliver` gets its replies (with its own ids) and the events of its own sessions. */
    attach(deliver) {
      let root = null; // this client's browser session
      let closed = false;
      const queue = [];
      const mine = new Set(); // pipe ids of this client's commands awaiting replies
      const sessions = new Set(); // child sessions this client owns
      const own = (sessionId) => { if (sessionId && !owners.has(sessionId)) { owners.set(sessionId, client); sessions.add(sessionId); } };
      const disown = (sessionId) => { if (sessions.delete(sessionId)) owners.delete(sessionId); };
      const reply = (message, sessionId, body) => deliver({ id: message.id, ...(sessionId ? { sessionId } : {}), ...body });
      const forward = (message) => {
        const sessionId = message.sessionId || root;
        const id = ++nextId;
        mine.add(id);
        pending.set(id, { id: message.id, deliver: (answer) => {
          mine.delete(id);
          if (message.method === 'Target.attachToTarget') own(answer.result?.sessionId);
          const { sessionId: _ignored, ...rest } = answer;
          deliver(message.sessionId ? { ...rest, sessionId: message.sessionId } : rest);
        } });
        write({ ...message, id, sessionId });
      };
      const client = {
        event(message) {
          if (message.method === 'Target.attachedToTarget') own(message.params?.sessionId);
          if (message.method === 'Target.detachedFromTarget') disown(message.params?.sessionId);
          if (message.sessionId === root) {
            const { sessionId: _ignored, ...rest } = message;
            deliver(rest);
          } else {
            deliver(message);
          }
        },
      };
      request('Target.attachToBrowserTarget', {}, undefined, (answer) => {
        if (closed) {
          if (answer.result?.sessionId) request('Target.detachFromTarget', { sessionId: answer.result.sessionId });
          return;
        }
        if (!answer.result?.sessionId) {
          for (const message of queue.splice(0)) reply(message, message.sessionId, { error: { code: -32000, message: 'browser-manager could not open a session for this client' } });
          return;
        }
        root = answer.result.sessionId;
        owners.set(root, client);
        for (const message of queue.splice(0)) forward(message);
      });
      return {
        command(message) {
          if (!message || typeof message !== 'object' || !Number.isInteger(message.id) || typeof message.method !== 'string') {
            deliver({ id: message?.id ?? null, error: { code: -32600, message: 'Invalid CDP command' } });
            return;
          }
          if (REFUSED_METHODS.has(message.method)) {
            reply(message, message.sessionId, { error: { code: -32000, message: `${message.method} is refused by browser-manager: this is someone's live browser` } });
            return;
          }
          if (IGNORED_METHODS.has(message.method)) {
            reply(message, message.sessionId, { result: {} });
            return;
          }
          if (message.sessionId !== undefined && !sessions.has(message.sessionId)) {
            reply(message, message.sessionId, { error: { code: -32001, message: `Session with given id not found: ${message.sessionId}` } });
            return;
          }
          if (root) forward(message); else queue.push(message);
        },
        detach() {
          if (closed) return;
          closed = true;
          for (const id of mine) pending.delete(id);
          mine.clear();
          queue.length = 0;
          for (const sessionId of sessions) {
            owners.delete(sessionId);
            if (root) request('Target.detachFromTarget', { sessionId }, root);
          }
          sessions.clear();
          if (root) {
            owners.delete(root);
            request('Target.detachFromTarget', { sessionId: root });
          }
        },
      };
    },
  };
}

/**
 * A WebSocket endpoint for one browser, on loopback only, behind a random token: what Playwright's connectOverCDP (or any
 * CDP client) takes. Opened on request through the owner-only control socket; closed with the browser.
 */
export async function startCdpBridge(mux, { host = '127.0.0.1' } = {}) {
  const token = randomBytes(24).toString('hex');
  const path = `/devtools/browser/${token}`;
  const server = createServer((req, res) => { res.writeHead(404).end(); });
  const wss = new WebSocketServer({ noServer: true, maxPayload: 64 * 1024 * 1024, perMessageDeflate: false });
  server.on('upgrade', (req, socket, head) => {
    socket.on('error', () => {});
    if (req.url !== path) { socket.destroy(); return; }
    wss.handleUpgrade(req, socket, head, (ws) => {
      const client = mux.attach((message) => { if (ws.readyState === 1) ws.send(JSON.stringify(message)); });
      ws.on('message', (data) => {
        let message;
        try { message = JSON.parse(data.toString('utf8')); } catch { return; }
        client.command(message);
      });
      ws.on('close', () => client.detach());
      ws.on('error', () => client.detach());
    });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, host, () => { server.off('error', reject); resolve(); });
  });
  const { port } = server.address();
  return {
    wsEndpoint: `ws://${host}:${port}${path}`,
    clients: () => wss.clients.size,
    async close() {
      for (const ws of wss.clients) ws.terminate();
      await new Promise((resolve) => server.close(() => resolve()));
    },
  };
}
