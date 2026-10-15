import { constants } from 'node:fs';
import { lstat, mkdir, mkdtemp, open, realpath, rename, rm } from 'node:fs/promises';
import { isAbsolute, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { launchDesktop } from './desktop.mjs';
import { launchMacDesktop } from './macos-desktop.mjs';
import { browserViewport, DESKTOP_VIEWPORT, HttpError, instanceLabel } from './auth.mjs';

const NAME = /^[a-z0-9][a-z0-9-]{0,47}$/;
const MAX_INSTANCES = 32;

function checkedName(name) {
  if (typeof name !== 'string' || !NAME.test(name)) throw new Error('Invalid browser name');
  return name;
}

function checkedUrl(value = 'about:blank') {
  if (typeof value !== 'string' || value.length > 2_048) throw new Error('Invalid start URL');
  if (value === 'about:blank') return value;
  let url;
  try { url = new URL(value); } catch { throw new Error('Invalid start URL'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('Start URL must use HTTP or HTTPS without embedded credentials');
  }
  return url.href;
}

async function directory(path, create = false, privateMode = false) {
  try {
    if (create) await mkdir(path, { recursive: true, mode: 0o700 });
    const info = await lstat(path);
    if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid() ||
        (privateMode && (info.mode & 0o077) !== 0) || await realpath(path) !== path) throw new Error('Invalid directory');
    return path;
  } catch (cause) {
    throw new Error('Browser directory must be an existing owned, non-symlink private directory', { cause });
  }
}

async function profileAvailable(profile) {
  for (const file of ['SingletonLock', 'SingletonSocket']) {
    try { await lstat(join(profile, file)); } catch (error) {
      if (error.code === 'ENOENT') continue;
      throw new Error('Could not check browser profile availability', { cause: error });
    }
    // Never remove a browser lock or trust its stored PID, including a stale-looking lock.
    throw new Error('Browser profile is already in use; close its existing browser first');
  }
}

/** Single-owner lifecycle and desired state. The optional launcher is a deterministic test seam. */
export function createInstanceManager({ stateDir, browserBin, baseUrl }, {
  launch = process.platform === 'darwin' ? launchMacDesktop : launchDesktop,
} = {}) {
  if (typeof stateDir !== 'string' || !isAbsolute(stateDir) || resolve(stateDir) !== stateDir) {
    throw new Error('Browser state directory must be an absolute normalized path');
  }
  if (typeof browserBin !== 'string' || !isAbsolute(browserBin)) throw new Error('Browser binary must be an absolute path');
  const origin = new URL(baseUrl);
  if (!['http:', 'https:'].includes(origin.protocol) || origin.username || origin.password ||
      origin.pathname !== '/' || origin.search || origin.hash) throw new Error('Invalid browser manager URL');
  const records = new Map();
  const lifetime = new AbortController();
  let initialized = false;
  let initializing;
  let closing;
  let closed = false;
  let tail = Promise.resolve();
  const stateFile = join(stateDir, 'instances.json');
  const publicRow = (r) => ({
    name: r.name, label: r.label ?? r.name, url: `${origin.origin}/browsers/${r.name}`, state: r.state,
    viewport: { ...(r.viewport ?? DESKTOP_VIEWPORT) },
    createdAt: r.createdAt, ...(r.error ? { error: r.error } : {}),
  });
  const enqueue = (operation) => {
    const task = tail.then(operation);
    tail = task.catch(() => {});
    return task;
  };
  function assertOpen() {
    if (closed) throw new Error('Browser manager is closed');
    if (!initialized) throw new Error('Browser manager is not initialized');
  }
  async function save() {
    const temp = join(stateDir, `.instances-${randomUUID()}.tmp`);
    let file;
    let created = false;
    let stateReplaced = false;
    try {
      file = await open(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
      created = true;
      await file.writeFile(JSON.stringify([...records.values()].map((r) => ({
        name: r.name, profile: r.profile, startUrl: r.startUrl, createdAt: r.createdAt, desired: r.desired,
        ...(r.label === undefined ? {} : { label: r.label }),
      }))));
      await file.sync();
      await file.close();
      file = undefined;
      await rename(temp, stateFile);
      stateReplaced = true;
      const dir = await open(stateDir, constants.O_RDONLY | constants.O_DIRECTORY);
      try { await dir.sync(); } finally { await dir.close(); }
    } catch (cause) {
      const error = new Error(stateReplaced
        ? 'Browser state was replaced, but its durability could not be confirmed'
        : 'Could not save private browser state', { cause });
      // A successful rename changes the visible snapshot even if directory fsync subsequently fails.
      // Callers may undo an uncommitted mutation, never pretend the replaced file was rolled back.
      error.stateReplaced = stateReplaced;
      throw error;
    } finally {
      await file?.close().catch(() => {});
      if (created) await rm(temp, { force: true }).catch(() => {});
    }
  }
  async function startRecord(record) {
    assertOpen();
    record.state = 'starting';
    record.error = undefined;
    let runtimeDir;
    try {
      await directory(record.profile);
      // Chromium owns stale-lock recovery for our exact managed profile. External profiles remain
      // conservative: never forward a launch into an existing browser or remove its lock files.
      if (record.profile !== join(stateDir, 'profiles', record.name)) await profileAvailable(record.profile);
      if ([...records.values()].some((other) => other !== record &&
          other.profile === record.profile && ['starting', 'running'].includes(other.state))) {
        throw new Error('Browser profile is already assigned to a running instance');
      }
      runtimeDir = await mkdtemp(join(stateDir, 'run', 'd-'));
      if (Buffer.byteLength(join(runtimeDir, 'vnc.sock')) >= 104) throw new Error('Browser state path is too long for private sockets');
      const desktop = await launch({ runtimeDir, profile: record.profile, browserBin,
        url: record.startUrl, signal: lifetime.signal });
      if (closed) { await desktop.close(); throw new Error('Browser manager is closed'); }
      record.desktop = desktop;
      record.runtimeDir = runtimeDir;
      record.viewport = { ...DESKTOP_VIEWPORT };
      record.state = 'running';
      void desktop.closed.then(() => enqueue(async () => {
        if (record.desktop !== desktop) return;
        record.desktop = undefined;
        record.state = closed ? 'stopped' : 'error';
        record.error = closed ? undefined : 'Browser desktop exited; start it again to reconnect';
        await rm(runtimeDir, { recursive: true, force: true });
      })).catch(() => {});
      return publicRow(record);
    } catch (error) {
      record.state = closed ? 'stopped' : 'error';
      record.error = closed ? undefined : 'Browser desktop could not start; check host dependencies and profile availability';
      record.desired = false;
      try { await save(); }
      finally { if (runtimeDir) await rm(runtimeDir, { recursive: true, force: true }); }
      throw new Error(record.error ?? 'Browser manager is closed', { cause: error });
    }
  }
  return {
    async initialize() {
      if (initializing) return initializing;
      if (initialized) return;
      if (closed) throw new Error('Browser manager is closed');
      initializing = (async () => {
      await directory(stateDir, true, true);
      await directory(join(stateDir, 'profiles'), true, true);
      await directory(join(stateDir, 'run'), true, true);
      let saved = [];
      let file;
      try {
        file = await open(stateFile, constants.O_RDONLY | constants.O_NOFOLLOW);
        const info = await file.stat();
        if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077) !== 0 || info.size > 262_144) {
          throw new Error('Invalid browser state file');
        }
        saved = JSON.parse(await file.readFile('utf8'));
      } catch (error) { if (error.code !== 'ENOENT') throw new Error('Could not read private browser state', { cause: error }); }
      finally { await file?.close(); }
      if (!Array.isArray(saved) || saved.length > MAX_INSTANCES) throw new Error('Invalid browser state');
      for (const entry of saved) {
        if (!entry || typeof entry !== 'object' || typeof entry.profile !== 'string' ||
            !isAbsolute(entry.profile) || resolve(entry.profile) !== entry.profile ||
            typeof entry.createdAt !== 'string' || !Number.isFinite(Date.parse(entry.createdAt)) ||
            typeof entry.desired !== 'boolean') throw new Error('Invalid browser state');
        const name = checkedName(entry.name);
        if (records.has(name)) throw new Error('Duplicate browser name in state');
        records.set(name, { name, profile: entry.profile, startUrl: checkedUrl(entry.startUrl),
          ...(entry.label === undefined ? {} : { label: instanceLabel(entry.label) }),
          createdAt: entry.createdAt, desired: entry.desired, state: 'stopped' });
      }
      initialized = true;
      for (const record of records.values()) {
        if (closed) break;
        if (record.desired) await enqueue(() => startRecord(record)).catch(() => {});
      }
      })();
      return initializing;
    },
    list() { return [...records.values()].map(publicRow).sort((a, b) => a.name.localeCompare(b.name)); },
    start(name, options = {}) {
      checkedName(name);
      const startUrl = options.url === undefined ? undefined : checkedUrl(options.url);
      return enqueue(async () => {
        assertOpen();
        let record = records.get(name);
        if (record?.state === 'running') {
          if (options.profile !== undefined || options.url !== undefined) throw new Error('Stop this browser before changing its configuration');
          return publicRow(record);
        }
        if (!record && records.size >= MAX_INSTANCES) throw new Error('Browser limit reached (32)');
        let profile = record?.profile ?? join(stateDir, 'profiles', name);
        if (options.profile !== undefined) {
          if (typeof options.profile !== 'string' || !isAbsolute(options.profile) || resolve(options.profile) !== options.profile) throw new Error('Profile must be an absolute normalized directory');
          profile = await directory(options.profile);
        } else await directory(profile, !record, !record);
        if (profile !== join(stateDir, 'profiles', name)) await profileAvailable(profile);
        const previous = record && { profile: record.profile, startUrl: record.startUrl, desired: record.desired };
        record ??= { name, createdAt: new Date().toISOString(), state: 'stopped' };
        Object.assign(record, { profile, startUrl: startUrl ?? record.startUrl ?? 'about:blank', desired: true });
        records.set(name, record);
        try { await save(); }
        catch (error) {
          if (!error.stateReplaced) {
            if (previous) Object.assign(record, previous);
            else records.delete(name);
          }
          throw error;
        }
        return startRecord(record);
      });
    },
    stop(name) {
      checkedName(name);
      return enqueue(async () => {
        assertOpen();
        const record = records.get(name);
        if (!record) throw new Error('Unknown browser');
        const previousDesired = record.desired;
        record.desired = false;
        try { await save(); }
        catch (error) {
          if (!error.stateReplaced) record.desired = previousDesired;
          throw error;
        }
        const desktop = record.desktop;
        record.desktop = undefined;
        await desktop?.close();
        if (record.runtimeDir) await rm(record.runtimeDir, { recursive: true, force: true });
        record.state = 'stopped';
        record.error = undefined;
        return publicRow(record);
      });
    },
    rename(name, value) {
      checkedName(name);
      const label = instanceLabel(value);
      return enqueue(async () => {
        assertOpen();
        const record = records.get(name);
        if (!record) throw new Error('Unknown browser');
        const previous = record.label;
        record.label = label;
        try { await save(); }
        catch (error) {
          if (!error.stateReplaced) {
            if (previous === undefined) delete record.label;
            else record.label = previous;
          }
          throw error;
        }
        return publicRow(record);
      });
    },
    setViewport(name, value) {
      checkedName(name);
      const viewport = browserViewport(value);
      return enqueue(async () => {
        assertOpen();
        const record = records.get(name);
        if (!record) throw new HttpError(404, 'Unknown browser');
        if (record.state !== 'running' || !record.desktop) throw new HttpError(409, 'Start this browser before changing its display');
        // Always apply an explicit request: it can recover a previous partially failed native resize.
        await record.desktop.resize(viewport);
        record.viewport = viewport;
        // Display geometry is live state: viewer reconnects retain it; a new desktop starts wide.
        return publicRow(record);
      });
    },
    async getNavigation(name) {
      checkedName(name);
      const record = records.get(name);
      if (!record) throw new HttpError(404, 'Unknown browser');
      const desktop = record.desktop;
      if (closed || record.state !== 'running' || !desktop) return { canGoBack: null };
      try {
        const state = await desktop.getNavigation();
        // A late snapshot from a stopped/replaced desktop must never enable navigation.
        return { canGoBack: !closed && record.state === 'running' && record.desktop === desktop &&
          typeof state?.canGoBack === 'boolean' ? state.canGoBack : null };
      } catch { return { canGoBack: null }; }
    },
    /** A loopback CDP endpoint for a running browser (lib/cdp-bridge.mjs); local control socket only. */
    async openCdpBridge(name) {
      checkedName(name);
      const record = records.get(name);
      if (!record) throw new HttpError(404, 'Unknown browser');
      const desktop = record.desktop;
      if (closed || record.state !== 'running' || !desktop) throw new HttpError(409, 'Start this browser before attaching to it');
      if (typeof desktop.openCdpBridge !== 'function') throw new HttpError(501, 'This platform has no CDP bridge');
      return desktop.openCdpBridge();
    },
    getSocket(name) {
      checkedName(name);
      const record = records.get(name);
      return !closed && record?.state === 'running' ? record.desktop?.socketPath ?? null : null;
    },
    getPage(name) {
      checkedName(name);
      const record = records.get(name);
      return !closed && record?.state === 'running' && record.desktop?.transport === 'page'
        ? record.desktop : null;
    },
    async close() {
      if (closing) return closing;
      closed = true;
      lifetime.abort();
      closing = (async () => {
      await initializing?.catch(() => {});
      await tail;
      await Promise.all([...records.values()].map(async (record) => {
        const desktop = record.desktop;
        record.desktop = undefined;
        await desktop?.close();
        if (record.runtimeDir) await rm(record.runtimeDir, { recursive: true, force: true });
        record.state = 'stopped';
      }));
      })();
      return closing;
    },
  };
}
