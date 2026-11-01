import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile, writeFile, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createServer} from 'node:net';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {setTimeout as delay} from 'node:timers/promises';
import {fileURLToPath} from 'node:url';

// Pinned workerd runs both services locally. No outbound network service is configured.
test('native Worker transport accepts AWS requests and refuses redirects without following them', {timeout: 15000}, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'control-portal-workerd-'));
  let child;
  let exit;
  let output = '';
  try {
    const server = createServer();
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const port = server.address().port;
    await new Promise(resolve => server.close(resolve));
    const backend = await readFile(new URL('./portal-backend.mjs', import.meta.url), 'utf8');
    const configuration = {OWNER_EMAIL: 'owner@example.test', ACCESS_AUD: 'test-portal-aud',
      AWS_REGION: 'us-west-1', AWS_ACCESS_KEY_ID: 'DUMMYPORTALACCESS', AWS_SECRET_ACCESS_KEY: 'DUMMYPORTALSECRET',
      CONTROL_LAMBDA_NAME: 'patchdoctor', TABLE_NAME: 'patchdoctor', LOG_GROUP: '/aws/codebuild/patchdoctor'};
    await writeFile(join(directory, 'portal.mjs'), backend + `
const configuration = ${JSON.stringify(configuration)};
export default {fetch(request) {
  if (new URL(request.url).pathname === '/test-ready') return new Response('ready');
  const access = {aud: configuration.ACCESS_AUD, getIdentity: async () => ({email: configuration.OWNER_EMAIL})};
  return handlePortalRequest(request, configuration, () => 'private shell', {access});
}};
`);
    await writeFile(join(directory, 'aws.mjs'), `
export default {async fetch(request) {
  const body = await request.json();
  if (body.action === 'pause_new_runs') return Response.redirect('https://never-contact.invalid/', 307);
  if (new URL(request.url).host !== 'lambda.us-west-1.amazonaws.com') return Response.json({});
  return Response.json({connected: true, armed: false, paused: false,
    models: {doctor: 'us.anthropic.claude-opus-5-5', triage: 'us.anthropic.claude-haiku-5-5'},
    limits: {worker_minutes: 60, model_allowance_usd: 25, case_allowance_usd: 50, starts_per_day: 4}});
}};
`);
    await writeFile(join(directory, 'config.capnp'), `
using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [
    (name = "portal", worker = (modules = [(name = "portal.mjs", esModule = embed "portal.mjs")],
      compatibilityDate = "2026-07-02", globalOutbound = "aws")),
    (name = "aws", worker = (modules = [(name = "aws.mjs", esModule = embed "aws.mjs")],
      compatibilityDate = "2026-07-02"))
  ],
  sockets = [(name = "http", address = "127.0.0.1:${port}", http = (), service = "portal")]
);
`);
    const binary = process.env.WORKERD_BIN || fileURLToPath(new URL('./node_modules/workerd/bin/workerd', import.meta.url));
    child = spawn(binary, ['serve', join(directory, 'config.capnp')], {detached: true,
      env: {HOME: directory, PATH: '/usr/bin:/bin', TMPDIR: directory, AWS_EC2_METADATA_DISABLED: 'true',
        AWS_CONFIG_FILE: '/dev/null', AWS_SHARED_CREDENTIALS_FILE: '/dev/null'}, stdio: ['ignore', 'pipe', 'pipe']});
    exit = once(child, 'exit');
    child.stdout.on('data', value => { output = (output + value).slice(-8192); });
    child.stderr.on('data', value => { output = (output + value).slice(-8192); });
    const origin = 'http://127.0.0.1:' + port;
    const deadline = Date.now() + 5000;
    let ready = false;
    while (Date.now() < deadline && child.exitCode === null) {
      try { ready = (await fetch(origin + '/test-ready', {signal: AbortSignal.timeout(500)})).ok; }
      catch { /* Wait only for the owned local test process to bind its socket. */ }
      if (ready) break;
      await delay(25);
    }
    assert.equal(ready, true, output);
    // Original Sites headers make the old transport regression reach fetch too; production ignores them.
    const headers = {'oai-authenticated-user-email': configuration.OWNER_EMAIL};
    const status = await fetch(origin + '/api/status', {headers});
    const body = await status.json();
    assert.equal(status.status, 200, JSON.stringify(body));
    assert.equal(body.doctor.connected, true);
    assert.equal(body.doctor.armed, false);
    const refused = await fetch(origin + '/api/doctor/control', {method: 'POST', headers: {...headers,
      origin, 'content-type': 'application/json'}, body: JSON.stringify({action: 'pause',
      mutationId: '00fbb359-bf9e-4cbe-9455-009956024fa4'})});
    const refusal = await refused.json();
    assert.equal(refused.status, 502);
    assert.deepEqual(refusal.diagnostic, {stage: 'response', service: 'lambda', operation: 'InvokeFunction', errorClass: 'Error'});
    assert.doesNotMatch(JSON.stringify(refusal), /DUMMY|never-contact|authorization/i);
  } finally {
    if (child && child.exitCode === null) process.kill(-child.pid, 'SIGTERM');
    if (exit) await exit;
    await rm(directory, {recursive: true, force: true});
  }
});
