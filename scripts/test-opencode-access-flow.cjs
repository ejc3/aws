#!/usr/bin/env node
'use strict';
// Isolated ARM-only browser regression. All identities, cookies and upstream credentials are synthetic.
// Requires preinstalled nginx plus Playwright with cached Chromium/WebKit; never installs dependencies.
// PLAYWRIGHT_CORE_PATH=/absolute/playwright-core node scripts/test-opencode-access-flow.cjs \
//   --proxy scripts/nextjs-ct/ct/opencode-web-proxy --report /tmp/opencode-auth-report.json
const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn, execFileSync } = require('node:child_process');
const args = process.argv.slice(2);
const options = {};
for (let i = 0; i < args.length; i += 2) {
  const name = args[i]; const value = args[i + 1];
  if (!['--proxy', '--report'].includes(name) || !value || value.startsWith('--') || options[name]) throw new Error('Usage: --proxy SCRIPT --report OUTPUT');
  options[name] = value;
}
if (!options['--proxy'] || !options['--report']) throw new Error('Require --proxy SCRIPT and --report OUTPUT');
const proxyFile = path.resolve(options['--proxy']);
const reportFile = path.resolve(options['--report']);
const { chromium, webkit } = require(process.env.PLAYWRIGHT_CORE_PATH || 'playwright-core');
process.umask(0o077);
const env = { ...process.env, AWS_EC2_METADATA_DISABLED: 'true', AWS_CONFIG_FILE: '/dev/null', AWS_SHARED_CREDENTIALS_FILE: '/dev/null', PYTHONDONTWRITEBYTECODE: '1' };
for (const name of ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE']) delete env[name];
const children = [];
const servers = [];
const contexts = [];
const sha256 = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const report = { syntheticOnly: true, externalIdentityUsed: false, testedSourceSHA256: Object.fromEntries([proxyFile, path.join(path.dirname(proxyFile), 'opencode-web-service-worker.js'), path.join(path.dirname(proxyFile), 'opencode-web-access-refresh.html')].map((file) => [path.basename(file), sha256(file)])), browsers: [], checks: [] };
let root;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function port() { const s = net.createServer(); await new Promise((r) => s.listen(0, '127.0.0.1', r)); const p = s.address().port; await new Promise((r) => s.close(r)); return p; }
async function listen(handler) { const s = http.createServer(handler); servers.push(s); await new Promise((r) => s.listen(0, '127.0.0.1', r)); return s; }
function check(name, condition) { assert.ok(condition, name); report.checks.push(name); }
function body(req) { return new Promise((resolve) => { const chunks = []; req.on('data', (x) => chunks.push(x)); req.on('end', () => resolve(Buffer.concat(chunks).toString())); }); }
function request(url, headers = {}) { return new Promise((resolve, reject) => { const r = http.get(url, { headers }, (res) => { const chunks = []; res.on('data', (x) => chunks.push(x)); res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) })); }); r.on('error', reject); }); }
function pipeline(req, res, target, rewrite = {}) {
  const headers = { ...req.headers, ...rewrite };
  const outgoing = http.request({ hostname: '127.0.0.1', port: target, path: req.url, method: req.method, headers }, (incoming) => { res.writeHead(incoming.statusCode, incoming.headers); incoming.pipe(res); });
  outgoing.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end(); });
  res.on('close', () => outgoing.destroy()); req.pipe(outgoing);
}
async function startNginx(config, runtime) {
  await fsp.mkdir(runtime, { recursive: true }); await fsp.writeFile(path.join(runtime, 'nginx.conf'), config);
  const child = spawn('/usr/sbin/nginx', ['-p', runtime + '/', '-c', path.join(runtime, 'nginx.conf'), '-g', 'daemon off;'], { env, stdio: ['ignore', 'ignore', 'pipe'] });
  children.push(child); let error = ''; child.stderr.on('data', (b) => { error += b.toString(); });
  await pause(180); if (child.exitCode !== null) throw new Error('Isolated nginx failed: ' + error.slice(-1000));
}
const fixtureHtml = Buffer.from('<!doctype html><meta charset="utf-8"><title>OpenCode synthetic fixture</title><main><h1 id="status">Loading</h1><input id="draft" aria-label="Draft"></main><script src="/app.js"></script>');
const fixtureApp = Buffer.from(`(async()=>{const status=document.getElementById('status');try{const i=await fetch('/api/info');await i.json();const r=await fetch('/api/event');const reader=r.body.getReader();const first=await reader.read();await reader.cancel();if(new TextDecoder().decode(first.value).includes('server.connected'))status.textContent='Home All Projects';}catch{}navigator.serviceWorker.register('/sw.js').catch(()=>{});})();`);
const oldWorker = Buffer.from(`const CACHE='workbox-precache-v2-'+self.registration.scope;self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/app.js'])).then(()=>self.skipWaiting())));self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(u.origin!==self.location.origin)return;if(e.request.mode==='navigate'&&!/^\\/(?:api|auth)(?:\\/|$)/.test(u.pathname)){e.respondWith(caches.open(CACHE).then(c=>c.match('/')).then(r=>r||fetch(e.request)));return;}if(u.pathname==='/app.js')e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));});`);
async function main() {
  root = await fsp.mkdtemp('/tmp/opencode-auth-regression-');
  const state = { fixed: false, loginHits: 0, infoRequests: 0, denyInfo: false, infoDelay: 0, foreignHeaders: [] };
  const nativeAuth = 'Basic ' + Buffer.from('opencode:synthetic-only-password').toString('base64');
  const upstream = await listen(async (req, res) => {
    if (req.headers.authorization !== nativeAuth) { res.writeHead(401); res.end(); return; }
    const p = new URL(req.url, 'http://fixture').pathname;
    if (p === '/api/info') { state.infoRequests++; await pause(state.infoDelay); res.writeHead(state.denyInfo ? 401 : 200, { 'content-type': 'application/json' }); res.end(state.denyInfo ? '{}' : '{"version":"2.1.2"}'); return; }
    if (p === '/api/denied') { res.writeHead(401, { 'content-type': 'application/json' }); res.end('{}'); return; }
    if (p === '/api/echo') { const b = await body(req); res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ method: req.method, body: b, preserve: req.headers['x-preserve'] ?? null, xhr: req.headers['x-requested-with'] ?? null })); return; }
    if (p === '/api/event') { res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' }); res.flushHeaders(); res.write('data: {"type":"server.connected"}\n\n'); const t = setTimeout(() => res.end(': later\n\n'), 2000); res.on('close', () => clearTimeout(t)); return; }
    if (p === '/') { res.writeHead(200, { 'content-type': 'text/html' }); res.end(fixtureHtml); return; }
    if (p === '/app.js') { res.writeHead(200, { 'content-type': 'application/javascript' }); res.end(fixtureApp); return; }
    if (p === '/sw.js') { res.writeHead(200, { 'content-type': 'application/javascript', 'cache-control': 'no-store' }); res.end(oldWorker); return; }
    res.writeHead(404); res.end();
  });
  const oldPort = await port(); const fixedPort = await port(); const upstreamPort = upstream.address().port;
  const oldRuntime = path.join(root, 'old'); const fixedRuntime = path.join(root, 'fixed');
  await fsp.mkdir(oldRuntime, { recursive: true }); await fsp.mkdir(fixedRuntime, { recursive: true });
  const spec = { hostname: '127.0.0.1', port: upstreamPort, proxy_port: fixedPort };
  const render = `import importlib.machinery,json,pathlib,sys\nm=importlib.machinery.SourceFileLoader('opencode_proxy_under_test',sys.argv[1]).load_module()\nr=pathlib.Path(sys.argv[2]);s=json.loads(sys.argv[3])\n` +
    `\nif hasattr(m,'prepare_assets'):m.prepare_assets(r)\nelif hasattr(m,'write_public_assets'):m.write_public_assets(r)\nelif hasattr(m,'write_assets'):m.write_assets(r)\n` +
    `sys.stdout.write(m.configuration(s,'synthetic-only-password','127.0.0.2',r))\n`;
  const fixedConfig = execFileSync('python3', ['-c', render, proxyFile, fixedRuntime, JSON.stringify(spec)], { env, encoding: 'utf8' }).replace(`listen ${fixedPort};`, `listen 127.0.0.1:${fixedPort};`);
  const oldConfig = `pid ${oldRuntime}/nginx.pid;error_log stderr warn;events{worker_connections 128;}http{access_log off;server{listen 127.0.0.1:${oldPort};location /{proxy_pass http://127.0.0.1:${upstreamPort};proxy_http_version 1.1;proxy_set_header Authorization "${nativeAuth}";proxy_buffering off;}}}`;
  await startNginx(oldConfig, oldRuntime); await startNginx(fixedConfig, fixedRuntime);
  let origin;
  const loginServer = await listen((req, res) => {
    const parsed = new URL(req.url, 'http://synthetic-login'); state.loginHits++;
    const candidate = parsed.searchParams.get('next') || '/'; const next = candidate.startsWith('/') && !candidate.startsWith('//') ? candidate : '/';
    if (req.method === 'POST') { res.writeHead(302, { 'set-cookie': 'synthetic_access=valid; HttpOnly; SameSite=Lax; Path=/', location: origin + next }); res.end(); }
    else { res.writeHead(200, { 'content-type': 'text/html', 'cache-control': 'no-store', 'access-control-allow-origin': '*' }); res.end('<!doctype html><title>Synthetic Access sign-in</title><h1>Sign in</h1><form method="post" action="/login?next=' + encodeURIComponent(next) + '"><button id="login">Use synthetic existing session</button></form>'); }
  });
  const loginOrigin = `http://127.0.0.1:${loginServer.address().port}`;
  const edge = await listen((req, res) => {
    const parsed = new URL(req.url, 'http://fixture');
    if (!/(?:^|;\s*)synthetic_access=valid(?:;|$)/.test(req.headers.cookie || '')) {
      if (parsed.pathname.startsWith('/api/') && req.headers['x-requested-with']) { res.writeHead(401, { 'content-type': 'application/json' }); res.end('{"error":"access-session-expired"}'); }
      else { res.writeHead(302, { location: loginOrigin + '/login?next=' + encodeURIComponent(req.url) }); res.end(); }
      return;
    }
    if (parsed.pathname === '/api/fail') { req.socket.destroy(); return; }
    const rewrite = { host: '127.0.0.1' };
    if (req.headers.origin === `http://127.0.0.1:${edge.address().port}`) rewrite.origin = 'https://127.0.0.1';
    pipeline(req, res, state.fixed ? fixedPort : oldPort, rewrite);
  });
  origin = `http://127.0.0.1:${edge.address().port}`;
  const foreign = await listen((req, res) => { state.foreignHeaders.push({ xhr: req.headers['x-requested-with'] ?? null, cookie: !!req.headers.cookie }); res.writeHead(200, { 'content-type': 'application/json', 'access-control-allow-origin': '*' }); res.end('{}'); });
  check('native upstream still rejects unauthenticated requests', (await request(`http://127.0.0.1:${upstreamPort}/api/info`)).status === 401);
  check('proxy rejects foreign Host', (await request(`http://127.0.0.1:${fixedPort}/api/info`, { host: 'evil.example' })).status === 421);
  check('proxy rejects foreign Origin', (await request(`http://127.0.0.1:${fixedPort}/api/info`, { host: '127.0.0.1', origin: 'https://evil.example' })).status === 403);
  const firstByte = await new Promise((resolve, reject) => { const start = Date.now(); const r = http.get(`http://127.0.0.1:${fixedPort}/api/event`, { headers: { host: '127.0.0.1' } }, (res) => { res.once('data', (b) => { resolve({ ms: Date.now() - start, connected: b.toString().includes('server.connected') }); r.destroy(); }); }); r.on('error', (e) => { if (e.code !== 'ECONNRESET') reject(e); }); });
  check('SSE initial frame remains unbuffered under2seconds', firstByte.connected && firstByte.ms < 2000);
  for (const [name, engine] of [['chromium', chromium], ['webkit', webkit]]) {
    state.fixed = false; state.denyInfo = false; state.infoDelay = 0;
    const context = await engine.launchPersistentContext(path.join(root, 'profile-' + name), { headless: true, viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true }); contexts.push(context);
    const page = await context.newPage(); const browserResult = { browser: name, steps: [] }; report.browsers.push(browserResult);
    const step = (text) => browserResult.steps.push(text);
    await context.addCookies([{ name: 'synthetic_access', value: 'valid', url: origin }]);
    await page.goto(origin); await page.waitForFunction(() => document.getElementById('status')?.textContent === 'Home All Projects');
    await page.evaluate(async () => { await navigator.serviceWorker.ready; if (!navigator.serviceWorker.controller) await new Promise((r) => navigator.serviceWorker.addEventListener('controllerchange', r, { once: true })); localStorage.setItem('syntheticDraftSentinel', 'keep'); await caches.open('synthetic-unrelated-cache'); });
    const other = await context.newPage(); await other.goto(origin); await other.locator('#draft').fill('unsaved synthetic draft'); await other.evaluate(() => { window.syntheticNoReload = 91473; });
    step('old cached app boots with valid session');
    await context.clearCookies(); await page.reload(); await page.waitForTimeout(600);
    assert.equal(new URL(page.url()).pathname, '/'); assert.equal(await page.locator('#status').textContent(), 'Loading'); step('expired cookie reproduces old cached endlessLoading');
    state.fixed = true;
    await page.goto(origin + '/auth/access-refresh'); await page.locator('#login').waitFor(); await page.locator('#login').click();
    await page.waitForURL(origin + '/'); await page.waitForFunction(() => document.getElementById('status')?.textContent === 'Home All Projects');
    await page.waitForFunction(async () => navigator.serviceWorker.controller && !(await caches.keys()).some((x) => x.startsWith('workbox-precache-v2-')));
    assert.equal(await page.evaluate(() => localStorage.getItem('syntheticDraftSentinel')), 'keep');
    assert.ok((await page.evaluate(() => caches.keys())).includes('synthetic-unrelated-cache'));
    step('one-time protected auth route recovers oldcache and updatedworker controls');
    const html = await request(origin + '/', { cookie: 'synthetic_access=valid' }); const js = await request(origin + '/app.js', { cookie: 'synthetic_access=valid' });
    assert.deepEqual(html.body, fixtureHtml); assert.deepEqual(js.body, fixtureApp); step('originalHTMLandJSbytes remain unchanged');
    await page.evaluate(async () => { const r = await navigator.serviceWorker.getRegistration(); await r.update(); }); await page.waitForTimeout(300);
    assert.equal(await other.locator('#draft').inputValue(), 'unsaved synthetic draft'); assert.equal(await other.evaluate(() => window.syntheticNoReload), 91473); step('workerupdate does not reload otherunsavedtab');
    assert.ok(await page.evaluate(async () => (await navigator.serviceWorker.getRegistration())?.active?.state === 'activated'));
    // Force-refresh can bypass control for this document while its registration is already active.
    await page.addInitScript(() => {
      if (location.pathname === '/auth/access-refresh' && location.search === '?synthetic-uncontrolled') {
        Object.defineProperty(navigator.serviceWorker, 'controller', { configurable: true, get: () => null });
        sessionStorage.setItem('syntheticUncontrolledRecovery', String(navigator.serviceWorker.controller === null));
      }
    });
    await page.goto(origin + '/auth/access-refresh?synthetic-uncontrolled', { waitUntil: 'domcontentloaded' });
    const resumedUncontrolled = await Promise.race([
      page.waitForURL(origin + '/', { timeout: 8000 }).then(() => true),
      page.locator('#retry:not([hidden])').waitFor({ state: 'visible', timeout: 8000 }).then(() => false),
    ]);
    assert.ok(resumedUncontrolled, 'an uncontrolled recovery document must resume through its already active worker');
    assert.equal(await page.evaluate(() => sessionStorage.getItem('syntheticUncontrolledRecovery')), 'true');
    assert.ok(await page.evaluate(() => !!navigator.serviceWorker.controller));
    step('uncontrolled recovery resumes through active worker and root is controlled');
    const echo = await page.evaluate(async () => { const r = await fetch(new Request(location.origin + '/api/echo', { method: 'POST', headers: { 'content-type': 'text/plain', 'x-preserve': 'yes' }, body: 'synthetic-body' })); return r.json(); });
    assert.deepEqual(echo, { method: 'POST', body: 'synthetic-body', preserve: 'yes', xhr: 'XMLHttpRequest' });
    const aborted = await page.evaluate(async () => { const c = new AbortController(); c.abort(); try { await fetch('/api/info', { signal: c.signal }); return false; } catch (e) { return e.name === 'AbortError'; } }); assert.ok(aborted); step('Requestbodyheadersandabort preserved');
    await page.evaluate(async (url) => { await fetch(url); }, `http://127.0.0.1:${foreign.address().port}/api/echo`);
    assert.deepEqual(state.foreignHeaders.at(-1), { xhr: null, cookie: false }); step('crossorigin requests get no authheader orcookie changes');
    const beforeDenied = state.loginHits; state.infoDelay = 100;
    const denied = await page.evaluate(async () => Promise.all(Array.from({ length: 4 }, () => fetch('/api/denied').then((r) => r.status)))); state.infoDelay = 0;
    assert.deepEqual(denied, [401, 401, 401, 401]); assert.equal(new URL(page.url()).pathname, '/'); assert.equal(state.loginHits, beforeDenied); step('unrelated401withhealthyinfo never redirects');
    const beforeFailure = state.loginHits; await page.evaluate(async () => { try { await fetch('/api/fail'); } catch {} }); await page.waitForTimeout(200); assert.equal(state.loginHits, beforeFailure); step('networkfailure never causes authloop');
    await context.clearCookies(); const beforeExpiry = state.loginHits;
    await page.evaluate(() => { void fetch('/api/info').catch(() => {}); }); await page.waitForURL(/\/login\?/); await page.waitForTimeout(300); assert.equal(state.loginHits, beforeExpiry + 1); step('APIexpiry navigates originatingclient once to realauthflow');
    assert.equal(await other.locator('#draft').inputValue(), 'unsaved synthetic draft'); assert.equal(await other.evaluate(() => window.syntheticNoReload), 91473);
    await page.locator('#login').click(); await page.waitForURL(origin + '/'); await page.waitForFunction(() => document.getElementById('status')?.textContent === 'Home All Projects'); step('login resumes Home with firstSSEframe');
    state.denyInfo = true; const beforeRecovery = state.loginHits; await page.goto(origin + '/auth/access-refresh'); await page.waitForTimeout(700); assert.equal(new URL(page.url()).pathname, '/auth/access-refresh'); assert.equal(state.loginHits, beforeRecovery); assert.ok(await page.locator('button').count(), 'recovery renders a manual retry action'); state.denyInfo = false; step('recoverypage401 staysfriendly without selfredirectloop');
    await context.clearCookies(); await page.goto(origin + '/'); await page.waitForURL(/\/login\?/); step('expiredfuturetopnavigation immediately reaches login');
    await context.close(); contexts.splice(contexts.indexOf(context), 1);
  }
  report.ok = true;
}
main().catch((error) => { report.ok = false; report.error = error.message; process.exitCode = 1; }).finally(async () => {
  for (const c of contexts) await c.close().catch(() => {});
  for (const child of children) child.kill('SIGTERM');
  await pause(150);
  for (const child of children) if (child.exitCode === null) child.kill('SIGKILL');
  for (const s of servers) { s.closeAllConnections(); await new Promise((r) => s.close(r)); }
  if (root) await fsp.rm(root, { recursive: true, force: true });
  report.cleaned = true;
  await fsp.writeFile(reportFile, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  console.log(JSON.stringify(report));
});
