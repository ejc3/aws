import { randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { WebSocketServer } from 'ws';

// The browser's remote-debugging pipe (fds 3/4, NUL-delimited JSON) has one reader. createCdpMux shares it between the
// desktop's own use (the user-agent mask) and any number of bridge clients: each client's command ids are remapped to
// ids that are unique on the pipe and mapped back on the reply, and every event goes to everyone. The browser sees one
// connection, so sessions are shared too, as with two DevTools clients attached to one browser.

// Commands that would end the person's browser rather than drive it. A job attaches to someone's live session; it may
// open and close its own tabs, never the browser.
export const REFUSED_METHODS = new Set(['Browser.close', 'Browser.crash', 'Browser.crashGpuProcess']);

// Commands acknowledged but never forwarded: they change how the person's browser behaves after the client has gone.
// Playwright's connect_over_cdp points downloads at its own temporary directory and deletes it on exit, so a later
// download by the person would land in a directory that no longer exists. The browser keeps its own settings.
export const IGNORED_METHODS = new Set(['Browser.setDownloadBehavior', 'Page.setDownloadBehavior']);

export function createCdpMux(writer, reader) {
  let nextId = 0;
  let buffer = Buffer.alloc(0);
  const pending = new Map(); // pipe id -> { deliver, id }
  const listeners = new Set();
  const write = (message) => {
    try { writer.write(`${JSON.stringify(message)}\0`); } catch { /* pipe closed: the desktop is going away */ }
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
      } else {
        for (const listener of listeners) {
          try { listener(message); } catch { /* one listener never stops the others */ }
        }
      }
    }
  });
  return {
    /** The desktop's own commands: fire and forget, replies are dropped. */
    send(method, params = {}, sessionId) {
      const id = ++nextId;
      pending.set(id, { deliver: () => {}, id });
      write({ id, method, params, ...(sessionId ? { sessionId } : {}) });
    },
    onEvent(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    /** A bridge client: `deliver` gets its replies (with its own ids) and every event. */
    attach(deliver) {
      const mine = new Set();
      const onEvent = (message) => deliver(message);
      listeners.add(onEvent);
      return {
        command(message) {
          if (!message || typeof message !== 'object' || !Number.isInteger(message.id) || typeof message.method !== 'string') {
            deliver({ id: message?.id ?? null, error: { code: -32600, message: 'Invalid CDP command' } });
            return;
          }
          if (REFUSED_METHODS.has(message.method)) {
            deliver({ id: message.id, ...(message.sessionId ? { sessionId: message.sessionId } : {}),
              error: { code: -32000, message: `${message.method} is refused by browser-manager: this is someone's live browser` } });
            return;
          }
          if (IGNORED_METHODS.has(message.method)) {
            deliver({ id: message.id, ...(message.sessionId ? { sessionId: message.sessionId } : {}), result: {} });
            return;
          }
          const id = ++nextId;
          mine.add(id);
          pending.set(id, { deliver: (reply) => { mine.delete(id); deliver(reply); }, id: message.id });
          write({ ...message, id });
        },
        detach() {
          listeners.delete(onEvent);
          for (const id of mine) pending.delete(id);
          mine.clear();
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
