import { execFile, spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { writeFile, chmod } from 'node:fs/promises';
import { appendFileSync } from 'node:fs';
import { connect } from 'node:net';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import { VNC_PIXEL_RATIO } from './frame-viewport.mjs';

const execute = promisify(execFile);
const desktopMode = `${1440 * VNC_PIXEL_RATIO}x${900 * VNC_PIXEL_RATIO}`;
const navigationHelper = fileURLToPath(new URL('./native-navigation.py', import.meta.url));
// xprop can read these numeric hints, but writes them as CARDINAL instead of WM_SIZE_HINTS.
// This fixed native call preserves the original type and every field; no script comes from callers.
const WRITE_HINTS = `import ctypes as c,sys
x=c.CDLL('libX11.so.6');x.XOpenDisplay.restype=c.c_void_p
d=c.c_void_p(x.XOpenDisplay(None))
if not d.value: raise RuntimeError('Private display unavailable')
p=x.XInternAtom(d,b'WM_NORMAL_HINTS',0);t=x.XInternAtom(d,b'WM_SIZE_HINTS',0)
v=[int(s) for s in sys.argv[2].split(',')];a=(c.c_long*len(v))(*v)
x.XChangeProperty(d,int(sys.argv[1],16),p,t,32,0,c.byref(a),len(v))
x.XSync(d,0);x.XCloseDisplay(d)`;

// Xauthority wire record: FamilyWild selects this private cookie regardless of hostname. The server
// loads the cookie before display allocation; clients receive the same cookie with the chosen number.
function authority(display, cookie) {
  const fields = ['', display, 'MIT-MAGIC-COOKIE-1', cookie].map((value) => {
    const bytes = Buffer.isBuffer(value) ? value : Buffer.from(value);
    const size = Buffer.alloc(2);
    size.writeUInt16BE(bytes.length);
    return Buffer.concat([size, bytes]);
  });
  return Buffer.concat([Buffer.from([255, 255]), ...fields]);
}

export function vncArguments({ display, authFile, socketPath }) {
  return [
    '-norc', '-display', `:${display}`, '-auth', authFile,
    // LibVNCServer has its own IPv6 port; x11vnc's -no6 does not disable it.
    '-unixsock', socketPath, '-rfbport', '0', '-rfbportv6', '0', '-no6', '-forever', '-shared',
    '-xrandr', 'resize',
    '-nopw', '-noremote', '-nocmds', '-novncconnect', '-input', 'KMBC', '-quiet',
  ];
}

// Chrome-for-Testing is fingerprinted and blocked by some bot protections (e.g. Akamai on the
// My-Verizon login) even with a normal user-agent string. These are the launch arguments Playwright
// applies by default; a browser started with them (plus the Client-Hint mask below) presents like a
// mainstream Chrome and clears that gate. --no-startup-window is omitted: the desktop opens a window
// with the start URL.
const PLAYWRIGHT_ARGS = [
  '--disable-field-trial-config', '--disable-background-networking', '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows', '--disable-back-forward-cache', '--disable-breakpad',
  '--disable-client-side-phishing-detection', '--disable-component-extensions-with-background-pages',
  '--disable-component-update', '--disable-default-apps', '--disable-dev-shm-usage', '--disable-extensions',
  '--disable-features=AvoidUnnecessaryBeforeUnloadCheckSync,DestroyProfileOnBrowserClose,DialMediaRouteProvider,GlobalMediaControls,HttpsUpgrades,LensOverlay,MediaRouter,PaintHolding,ThirdPartyStoragePartitioning,BlockOriginHeaderModificationOnRedirect,Translate,AutoDeElevate,OptimizationHints',
  '--allow-pre-commit-input', '--disable-hang-monitor', '--disable-ipc-flooding-protection',
  '--disable-popup-blocking', '--disable-prompt-on-repost', '--disable-renderer-backgrounding',
  // NB: Playwright's default args include --metrics-recording-only, but we drop it here. That
  // flag makes Chrome record UMA metrics to <profile>/DeferredBrowserMetrics/*.pma and never
  // upload or prune them; harmless for Playwright's per-test browsers but our browsers are
  // long-lived, so it grows unbounded (a real incident: ~112GB / 27k files filled the root
  // disk). --disable-background-networking (above) already stops metrics upload. Command-line
  // flags are not visible to a page, so this does not change the anti-bot fingerprint.
  '--force-color-profile=srgb', '--password-store=basic', '--use-mock-keychain',
  '--no-service-autorun', '--export-tagged-pdf', '--disable-search-engine-choice-screen', '--disable-infobars',
  '--disable-sync', '--enable-unsafe-swiftshader', '--no-sandbox',
];

// maskChromeUserAgent hides the remaining Chrome-for-Testing identity — the sec-ch-ua Client-Hint
// brands and navigator.userAgentData that launch flags cannot change — via a CDP UA-metadata
// override, and clears navigator.webdriver. It speaks CDP over the private inherited debugging pipe
// (fds 3/4): a pipe, never a TCP port, consistent with the macOS transport. Best-effort — any pipe
// failure leaves the browser exactly as launched.
// isAppSchemeRedirect is true for a URL whose scheme is a custom app scheme the desktop
// browser cannot follow (vsfapp:, myapp:, …) — i.e. a well-formed scheme that is NOT one of
// the web/browser-internal schemes. These are the OAuth "open in the app" deep links we
// capture to a file so a headless operator can read a redirect the desktop swallows.
export function isAppSchemeRedirect(u) {
  return typeof u === 'string'
    && /^[a-z][a-z0-9+.-]*:/i.test(u)
    && !/^(https?|wss?|ftp|about|chrome|chrome-extension|devtools|data|blob|file|view-source|javascript):/i.test(u);
}

function maskChromeUserAgent(child, url, captureFile) {
  try {
    const writer = child.stdio[3], reader = child.stdio[4];
    if (!writer || !reader) return;
    const userAgent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
    const userAgentMetadata = {
      brands: [{ brand: 'Not_A Brand', version: '8' }, { brand: 'Chromium', version: '131' }, { brand: 'Google Chrome', version: '131' }],
      fullVersion: '131.0.6778.86',
      fullVersionList: [{ brand: 'Not_A Brand', version: '8.0.0.0' }, { brand: 'Chromium', version: '131.0.6778.86' }, { brand: 'Google Chrome', version: '131.0.6778.86' }],
      platform: 'Windows', platformVersion: '15.0.0', architecture: 'x86', model: '', mobile: false, bitness: '64', wow64: false,
    };
    let id = 0, buffer = Buffer.alloc(0);
    const masked = new Set();
    const send = (method, params = {}, sessionId) => {
      try { writer.write(`${JSON.stringify({ id: ++id, method, params, ...(sessionId ? { sessionId } : {}) })}\0`); } catch { /* pipe closed */ }
    };
    writer.on('error', () => {});
    reader.on('error', () => {});
    reader.on('data', (part) => {
      try {
        buffer = Buffer.concat([buffer, part]);
        let end;
        while ((end = buffer.indexOf(0)) !== -1) {
          const packet = buffer.subarray(0, end); buffer = buffer.subarray(end + 1);
          let message;
          try { message = JSON.parse(packet.toString('utf8')); } catch { continue; }
          if (message.method === 'Target.attachedToTarget') {
            const sessionId = message.params?.sessionId;
            const type = message.params?.targetInfo?.type;
            if (sessionId && type === 'page' && !masked.has(sessionId)) {
              masked.add(sessionId);
              send('Emulation.setUserAgentOverride', { userAgent, userAgentMetadata }, sessionId);
              send('Page.addScriptToEvaluateOnNewDocument', { source: "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});" }, sessionId);
              send('Page.enable', {}, sessionId);
              // Capture custom-scheme redirects the browser cannot follow (e.g. an OAuth flow
              // that ends at vsfapp://…?code=…): the URL appears as a requestWillBeSent we log
              // to a file, so a headless operator can read a deep-link the desktop can't open.
              if (captureFile) send('Network.enable', {}, sessionId);
              if (url && url !== 'about:blank') send('Page.navigate', { url }, sessionId);
            }
            if (sessionId) send('Runtime.runIfWaitingForDebugger', {}, sessionId);
          } else if (captureFile && message.method === 'Network.requestWillBeSent') {
            if (isAppSchemeRedirect(message.params?.request?.url)) {
              try { appendFileSync(captureFile, message.params.request.url + '\n'); } catch { /* best-effort */ }
            }
          }
        }
      } catch { /* ignore malformed frame */ }
    });
    send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
  } catch { /* masking is best-effort */ }
}

/** Only these fixed programs are launched. Native subprocesses never inherit a shell or CDP port. */
export async function launchDesktop({ runtimeDir, profile, browserBin, url, signal }) {
  const children = [];
  const failed = Promise.withResolvers();
  // A rejection can precede the readiness race (for example, spawn ENOENT).
  void failed.promise.catch(() => {});
  let closing;
  const resizeAbort = new AbortController();
  let resizeTail = Promise.resolve();
  let navigationRead;
  let navigationWait;
  let navigationResolve;
  const close = () => {
    if (closing) return closing;
    closing = (async () => {
      signal.removeEventListener('abort', aborted);
      resizeAbort.abort();
      await resizeTail;
      navigationResolve?.({ canGoBack: null });
      const browser = children.find(({ label }) => label === 'Browser');
      if (browser) {
        // Chromium handles SIGINT as normal AttemptExit; SIGTERM takes the shorter SessionEnding
        // path. Keep X and renderer children alive until profile data and singleton locks are flushed.
        if (browser.child.exitCode === null && browser.child.signalCode === null) browser.child.kill('SIGINT');
        await Promise.race([browser.done, delay(5_000, undefined, { ref: false })]);
        kill(browser.child, 'SIGKILL');
        await browser.done;
      }
      const desktop = children.filter((entry) => entry !== browser).toReversed();
      for (const { child } of desktop) kill(child, 'SIGTERM');
      await Promise.race([Promise.all(desktop.map(({ done }) => done)), delay(2_000, undefined, { ref: false })]);
      for (const { child } of desktop) kill(child, 'SIGKILL');
      await Promise.all(children.map(({ done }) => done));
    })();
    return closing;
  };
  const aborted = () => failed.reject(new Error('Desktop start cancelled'));
  signal.addEventListener('abort', aborted, { once: true });
  if (signal.aborted) aborted();
  function launch(label, bin, args, env, stdio = 'ignore') {
    if (signal.aborted) throw new Error('Desktop start cancelled');
    const child = spawn(bin, args, {
      env,
      detached: true,
      stdio,
    });
    const done = new Promise((resolve) => {
      child.once('error', () => {
        failed.reject(new Error(`${label} could not start`));
        resolve();
      });
      child.once('exit', (code) => {
        failed.reject(new Error(`${label} exited${code === null ? '' : ` (status ${code})`}`));
        resolve();
      });
    });
    children.push({ label, child, done });
    return child;
  }

  const authFile = join(runtimeDir, 'Xauthority');
  const socketPath = join(runtimeDir, 'vnc.sock');
  const cookie = randomBytes(16);
  try {
    await writeFile(authFile, authority('0', cookie), { mode: 0o600, flag: 'wx' });
    const x = launch('Xvfb', '/usr/bin/Xvfb', [
      '-displayfd', '3', '-screen', '0', `${desktopMode}x24`, '-nolisten', 'tcp',
      '-auth', authFile, '-noreset',
    ], process.env, ['ignore', 'ignore', 'ignore', 'pipe']);
    const display = await Promise.race([
      failed.promise,
      delay(15_000, undefined, { ref: false }).then(() => { throw new Error('Desktop display timed out'); }),
      new Promise((resolve, reject) => {
        let text = '';
        x.stdio[3].on('data', (part) => {
          text += part;
          if (/^\d{1,5}\n$/.test(text)) resolve(text.trim());
          else if (text.length > 8 || text.includes('\n')) reject(new Error('Invalid display allocation'));
        });
      }),
    ]);
    await writeFile(authFile, authority(display, cookie), { mode: 0o600 });
    const env = { ...process.env, DISPLAY: `:${display}`, XAUTHORITY: authFile,
      DBUS_SESSION_BUS_ADDRESS: `unix:path=${join(runtimeDir, 'bus.sock')}`, GSETTINGS_BACKEND: 'memory' };
    delete env.WAYLAND_DISPLAY;
    delete env.AT_SPI_BUS_ADDRESS;
    delete env.NO_AT_BRIDGE;
    // A separate owned bus keeps accessibility away from other desktops and user-wide settings.
    // Chromium depends on the native bus lifetime; individual metadata query failures are optional.
    const bus = launch('Accessibility bus', '/usr/bin/dbus-daemon', [
      '--session', '--nofork', `--address=${env.DBUS_SESSION_BUS_ADDRESS}`, '--print-address=3',
    ], env, ['ignore', 'ignore', 'ignore', 'pipe']);
    try {
      await Promise.race([
        new Promise((resolve, reject) => {
          bus.once('error', reject);
          bus.once('exit', () => reject(new Error('Accessibility bus unavailable')));
          bus.stdio[3].once('data', resolve);
        }),
        delay(2_000, undefined, { ref: false }).then(() => { throw new Error('Accessibility bus timed out'); }),
      ]);
      await execute('/usr/bin/gdbus', ['call', '--session', '--dest', 'org.a11y.Bus',
        '--object-path', '/org/a11y/bus', '--method', 'org.freedesktop.DBus.Properties.Set',
        'org.a11y.Status', 'IsEnabled', '<true>'], { env, timeout: 2_000, maxBuffer: 1024, signal: resizeAbort.signal });
    } catch { /* Native navigation remains unknown; do not interrupt the browser. */ }
    launch('Window manager', '/usr/bin/openbox', ['--sm-disable'], env);
    launch('VNC server', '/usr/bin/x11vnc', vncArguments({ display, authFile, socketPath }), env);
    const browser = launch('Browser', browserBin, [
      `--user-data-dir=${profile}`, '--ozone-platform=x11', '--no-first-run',
      '--no-default-browser-check', '--force-renderer-accessibility=basic',
      `--force-device-scale-factor=${VNC_PIXEL_RATIO}`,
      // Present as a mainstream desktop Chrome: the default Chrome-for-Testing UA is
      // fingerprinted and blocked by some sites' bot protection (e.g. Akamai on Verizon login).
      '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
      '--disable-blink-features=AutomationControlled',
      ...PLAYWRIGHT_ARGS,
      '--remote-debugging-pipe',
      // Launch blank so no unmasked request reaches a bot-protected origin; the mask navigates.
      '--start-maximized', '--window-size=1440,900', 'about:blank',
    ], env, ['ignore', 'ignore', 'ignore', 'pipe', 'pipe']);
    maskChromeUserAgent(browser, url, join(runtimeDir, 'app-redirects.log'));
    let navigationReader;
    const getNavigation = () => {
      if (closing || signal.aborted || resizeAbort.signal.aborted) return Promise.resolve({ canGoBack: null });
      if (!navigationRead) {
        navigationRead = new Promise(resolve => { navigationResolve = resolve; })
          .finally(() => { navigationRead = undefined; navigationResolve = undefined; });
        navigationWait = Promise.race([navigationRead, delay(2_000, { canGoBack: null }, { ref: false })]);
        if (!navigationReader) {
          // Keep the AT-SPI client alive until AFTER Chrome exits (the shared close path above).
          // Removing its last listener during Chromium's re-entrant key dispatch can crash Chrome.
          navigationReader = launch('Native navigation reader', '/usr/bin/python3',
            [navigationHelper, String(browser.pid)], env, ['pipe', 'pipe', 'ignore']);
          let output = '';
          navigationReader.stdout.on('data', chunk => {
            output += chunk.toString('utf8');
            if (output.length > 1024) {
              output = '';
              navigationReader.stdout.pause();
              failed.reject(new Error('Native navigation response exceeded its limit'));
              return;
            }
            if (!output.endsWith('\n')) return;
            try {
              const value = JSON.parse(output).canGoBack;
              navigationResolve?.({ canGoBack: !closing && !signal.aborted && typeof value === 'boolean' ? value : null });
            } catch { navigationResolve?.({ canGoBack: null }); }
            output = '';
          });
          navigationReader.once('exit', () => navigationResolve?.({ canGoBack: null }));
          navigationReader.once('error', () => navigationResolve?.({ canGoBack: null }));
          navigationReader.stdin.on('error', () => failed.reject(new Error('Native navigation reader unavailable')));
        }
        navigationReader.stdin.write('read\n');
      }
      // A slow read is unknown, not a reason to disconnect/recreate the AT-SPI subscription.
      // Keep the single in-flight request so repeated callers cannot queue unbounded work.
      return navigationWait;
    };
    let activeMode = desktopMode;
    const modes = new Map();
    const originalHints = new Map();
    async function resizeDesktop(viewport) {
      const { mode, width, height } = viewport;
      if (!Number.isInteger(width) || !Number.isInteger(height) ||
          (mode === 'desktop' ? width !== 1440 || height !== 900
            : mode !== 'phone' || width < 320 || width > 500 || height < 480 || height > 900)) {
        throw new Error('Invalid browser viewport');
      }
      let deadline = Date.now() + 15_000;
      const command = async (bin, args) => {
        if (closing || signal.aborted || resizeAbort.signal.aborted || Date.now() >= deadline) {
          throw new Error('Browser resize cancelled or timed out');
        }
        return (await execute(bin, args, { env, signal: resizeAbort.signal,
          timeout: Math.min(2_000, deadline - Date.now()), maxBuffer: 65_536 })).stdout;
      };
      const hints = async (id) => {
        const raw = await command('/usr/bin/xprop', ['-id', id, '-f', 'WM_NORMAL_HINTS', '32c', ' = $0+\\n', 'WM_NORMAL_HINTS']);
        const match = /^WM_NORMAL_HINTS\(WM_SIZE_HINTS\) = ([\d, ]+)\s*$/.exec(raw);
        const values = match?.[1].split(',').map(Number);
        if (values?.length !== 18 || values.some(v => !Number.isInteger(v) || v < 0 || v > 0xffffffff)) {
          throw new Error('Browser window size hints are unavailable');
        }
        return values;
      };
      const writeHints = (id, values) => command('/usr/bin/python3', ['-c', WRITE_HINTS, id, values.join(',')]);
      // Match only windows belonging to this exact live child on its private authenticated display.
      const listing = await command('/usr/bin/wmctrl', ['-lp']);
      const windows = listing.split('\n').flatMap(line => {
        const match = /^(0x[\da-f]+)\s+-?\d+\s+(\d+)\s/i.exec(line);
        return match && Number(match[2]) === browser.pid ? [match[1]] : [];
      });
      if (!windows.length || windows.length > 32) throw new Error('Browser windows are not ready for resizing');
      const before = new Map();
      for (const id of windows) before.set(id, await hints(id));
      const previousMode = activeMode;
      const savedBefore = new Map(originalHints);
      try {
        let nextMode = desktopMode;
        if (mode === 'phone') {
          // At most two custom modes exist. Prepare the inactive slot so rollback retains the old one.
          nextMode = [...modes].find(([, entry]) => entry.size === `${width}x${height}`)?.[0]
            ?? (activeMode === 'bm-phone-0' ? 'bm-phone-1' : 'bm-phone-0');
          if (modes.get(nextMode)?.size !== `${width}x${height}`) {
            if (modes.has(nextMode)) {
              if (modes.get(nextMode).attached) {
                await command('/usr/bin/xrandr', ['--delmode', 'screen', nextMode]);
                modes.get(nextMode).attached = false;
              }
              await command('/usr/bin/xrandr', ['--rmmode', nextMode]);
              modes.delete(nextMode);
            }
            // API sizes remain logical; scale the raster/timings together to retain refresh rate.
            await command('/usr/bin/xrandr', ['--newmode', nextMode, String(30 * VNC_PIXEL_RATIO ** 2),
              ...[width, width + 10, width + 50, width + 90, height, height + 6, height + 16, height + 56]
                .map(value => String(value * VNC_PIXEL_RATIO))]);
            modes.set(nextMode, { size: `${width}x${height}`, attached: false });
          }
          if (!modes.get(nextMode).attached) {
            await command('/usr/bin/xrandr', ['--addmode', 'screen', nextMode]);
            modes.get(nextMode).attached = true;
          }
          for (const id of windows) {
            if (!originalHints.has(id)) originalHints.set(id, before.get(id));
            await command('/usr/bin/wmctrl', ['-ir', id, '-b', 'add,maximized_vert,maximized_horz']);
            // EWMH requests are asynchronous; let Chrome publish its maximized-window hints first.
            await delay(100);
            // Chromium's 500px minimum otherwise crops a 390px desktop. Keep browser chrome and
            // every other ICCCM hint; removing only PMinSize gives a genuinely narrow page layout.
            const narrow = await hints(id);
            narrow[0] &= ~16;
            await writeHints(id, narrow);
          }
        }
        await command('/usr/bin/xrandr', ['--output', 'screen', '--mode', nextMode]);
        if (mode === 'desktop') {
          await delay(100);
          for (const id of windows) {
            await command('/usr/bin/wmctrl', ['-ir', id, '-b', 'add,maximized_vert,maximized_horz']);
            if (originalHints.has(id)) await writeHints(id, originalHints.get(id));
          }
          originalHints.clear();
        } else {
          for (const id of originalHints.keys()) if (!windows.includes(id)) originalHints.delete(id);
        }
        activeMode = nextMode;
      } catch (cause) {
        // Restore the last usable screen and the precise pre-call hints; shutdown always wins.
        deadline = Date.now() + 5_000;
        let rolledBack = true;
        try {
          await command('/usr/bin/xrandr', ['--output', 'screen', '--mode', previousMode]);
          for (const [id, values] of before) await writeHints(id, values);
        } catch { rolledBack = false; }
        originalHints.clear();
        for (const [id, values] of savedBefore) originalHints.set(id, values);
        throw new Error(rolledBack ? 'Browser resize failed; previous size restored'
          : 'Browser resize failed; its current size could not be confirmed', { cause });
      }
    }
    const resize = (viewport) => {
      const request = { ...viewport };
      const task = resizeTail.then(() => resizeDesktop(request));
      resizeTail = task.catch(() => {});
      return task;
    };
    await Promise.race([
      failed.promise,
      (async () => {
        const deadline = Date.now() + 15_000;
        while (Date.now() < deadline) {
          if (signal.aborted || closing) throw new Error('Desktop start cancelled');
          if (await readySocket(socketPath)) {
            await chmod(socketPath, 0o600);
            // Catch Chrome's immediate profile-lock/sandbox startup failures before advertising it.
            await delay(300);
            return;
          }
          await delay(50);
        }
        throw new Error('Desktop connection timed out');
      })(),
    ]);
    if (signal.aborted) throw new Error('Desktop start cancelled');
    return { socketPath, close, closed: failed.promise.catch(close), resize, getNavigation };
  } catch (error) {
    await close();
    throw error;
  }
}

function kill(child, signal) {
  if (!child.pid) return;
  // Every group was created by this process. No PID or process group is ever recovered from disk.
  // Signal the group even after its leader exits: Chrome renderer children can otherwise survive it.
  try { process.kill(-child.pid, signal); } catch (error) { if (error.code !== 'ESRCH') throw error; }
}

function readySocket(path) {
  return new Promise((resolve) => {
    const socket = connect(path);
    const finish = (ready) => { socket.destroy(); resolve(ready); };
    socket.setTimeout(300, () => finish(false));
    socket.once('error', () => finish(false));
    socket.once('data', (bytes) => finish(bytes.toString('ascii').startsWith('RFB ')));
  });
}
