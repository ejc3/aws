import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {renderPortal, renderMdmCredentialsPortal} from './portal-ui.mjs';
import {handlePortalRequest, signAwsRequest} from './portal-backend.mjs';

const env = {
  OWNER_EMAIL: 'owner@example.test', ACCESS_AUD: 'control-portal-aud', AWS_REGION: 'us-west-1',
  AWS_ACCESS_KEY_ID: 'DUMMYPORTALACCESS', AWS_SECRET_ACCESS_KEY: 'DUMMYPORTALSECRET',
  AWS_SESSION_TOKEN: 'DUMMYPORTALSESSION', CONTROL_LAMBDA_NAME: 'patchdoctor',
  TABLE_NAME: 'patchdoctor', LOG_GROUP: '/aws/codebuild/patchdoctor', PROJECT_NAME: 'patchdoctor',
};
const mutationId = '00fbb359-bf9e-4cbe-9455-009956024fa4';
const caseId = 'a'.repeat(64);
const otherCase = 'b'.repeat(64);
const now = () => Date.parse('2026-11-01T17:00:00Z');
const ownerAccess = {aud: env.ACCESS_AUD, getIdentity: async () => ({email: env.OWNER_EMAIL})};
function request(path = '/', {method = 'GET', body, headers = {}} = {}) {
  return new Request('https://portal.example.test' + path, {method,
    headers: {...(method === 'POST' ? {origin: 'https://portal.example.test', 'content-type': 'application/json'} : {}), ...headers},
    ...(body === undefined ? {} : {body: typeof body === 'string' ? body : JSON.stringify(body)})});
}
function json(value, options) { return new Response(JSON.stringify(value), options); }
function record(value) { return {Item: {body: {S: JSON.stringify(value)}}}; }
function transport(handler) {
  const calls = [];
  const fetch = async (url, options) => {
    assert.equal(options.method, 'POST');
    assert.equal(options.redirect, 'manual');
    assert.equal(options.signal.aborted, false);
    assert.match(options.headers.get('authorization'), /^AWS4-HMAC-SHA256 Credential=DUMMYPORTALACCESS\//);
    assert.equal(options.headers.get('x-amz-security-token'), 'DUMMYPORTALSESSION');
    const endpoint = new URL(url);
    assert.equal(endpoint.protocol, 'https:');
    assert.ok(['lambda.us-west-1.amazonaws.com', 'dynamodb.us-west-1.amazonaws.com', 'logs.us-west-1.amazonaws.com'].includes(endpoint.host));
    const call = {url, target: options.headers.get('x-amz-target'), payload: JSON.parse(options.body)};
    calls.push(call);
    return handler(call);
  };
  return {fetch, calls};
}
async function invoke(path, options = {}, settings = env, handler = () => { throw new Error('unexpected AWS call'); }) {
  const io = transport(handler);
  const diagnostics = [];
  const access = Object.hasOwn(options, 'access') ? options.access : ownerAccess;
  const response = await handlePortalRequest(request(path, options), settings, () => '<!doctype html><title>Private</title>',
    {fetch: io.fetch, now, access, diagnostic: value => diagnostics.push(value)});
  return {status: response.status, headers: response.headers, body: await response.json(), calls: io.calls, diagnostics};
}

test('SigV4 matches the published AWS SDK DynamoDB signing fixture', async () => {
  // https://github.com/aws/aws-sdk-js/blob/master/test/signers/v4.spec.js
  const headers = await signAwsRequest({url: 'https://localhost/', service: 'dynamodb', region: 'region',
    headers: {'x-amz-content-sha256': '3128b8d4f3108b3e1677a38eb468d1c6dec926a58eaea235d034b9c71c3864d4',
      'x-amz-target': 'DynamoDB_20111205.ListTables', 'x-amz-user-agent': 'aws-sdk-js/0.1'},
    body: JSON.stringify({ExclusiveStartTableName: 'bår'}),
    credentials: {accessKeyId: 'akid', secretAccessKey: 'secret', sessionToken: 'session'}, now: new Date(1935346573456)});
  assert.equal(headers.get('authorization'), 'AWS4-HMAC-SHA256 Credential=akid/20310430/region/dynamodb/aws4_request, '
    + 'SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token;x-amz-target;x-amz-user-agent, '
    + 'Signature=31fac5ed29db737fbcafac527470ca6d9283283197c5e6e94ea40ddcec14a9c1');
});

test('shell and every API require Cloudflare platform context, exact audience and owner before AWS', async () => {
  for (const path of ['/', '/doctor', '/api/status', '/api/doctor/cases', '/api/doctor/events', '/api/doctor/control', '/api/setup/start']) {
    const reply = await handlePortalRequest(new Request('https://portal.example.test' + path), env, () => 'secret shell');
    assert.equal(reply.status, 401, path);
    assert.doesNotMatch(await reply.text(), /secret shell/);
    const stranger = await invoke(path, {access: {aud: env.ACCESS_AUD, getIdentity: async () => ({email: 'other@example.test'})}});
    assert.equal(stranger.status, 403, path);
    assert.equal(stranger.calls.length, 0);
    for (const access of [undefined, null, {aud: env.ACCESS_AUD},
      {aud: 'another-app', getIdentity: async () => { throw new Error('wrong audience must not fetch identity'); }}]) {
      const forged = await invoke(path, {access, headers: {'oai-authenticated-user-id': 'owner-123',
        'oai-authenticated-user-email': env.OWNER_EMAIL, 'cf-access-authenticated-user-email': env.OWNER_EMAIL,
        'cf-access-jwt-assertion': 'forged'}});
      assert.ok([401, 403].includes(forged.status), path);
      assert.equal(forged.calls.length, 0);
      assert.doesNotMatch(JSON.stringify(forged.body), /secret shell|wrong audience/);
    }
  }
  for (const settings of [{...env, OWNER_EMAIL: ''}, {...env, ACCESS_AUD: ''}]) {
    const unset = await invoke('/api/status', {}, settings);
    assert.equal(unset.status, 503);
    assert.equal(unset.calls.length, 0);
  }
});

test('shell serves only the owner and never embeds server configuration', async () => {
  const response = await handlePortalRequest(request(), env, () => '<!doctype html><title>Private</title>', {access: ownerAccess});
  assert.equal(response.status, 200);
  assert.equal(await response.text(), '<!doctype html><title>Private</title>');
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.match(response.headers.get('content-security-policy'), /connect-src 'self'/);
  const doctor = await handlePortalRequest(request('/doctor'), env, path => 'Private ' + path, {access: ownerAccess});
  assert.equal(doctor.status, 200);
  assert.equal(await doctor.text(), 'Private /doctor');
  let awsCalls = 0;
  const render = path => path === '/doctor' ? renderPortal() : renderMdmCredentialsPortal();
  for (const path of ['/', '/doctor']) {
    const page = await handlePortalRequest(request(path), env, render, {access: ownerAccess,
      fetch: async () => { awsCalls++; throw new Error('shell must not call AWS'); }});
    assert.equal(page.status, 200);
    assert.equal(await page.text(), render(path));
  }
  assert.equal(awsCalls, 0);
});

test('only the verified context supplies identity, independent of caller headers', async () => {
  const response = await handlePortalRequest(request('/', {headers: {'oai-authenticated-user-email': 'other@example.test'}}),
    env, () => 'owner shell', {access: {aud: env.ACCESS_AUD, getIdentity: async () => ({email: ' OWNER@example.test '})}});
  assert.equal(response.status, 200);
  for (const getIdentity of [async () => { throw new Error('NEVERRETURNTHIS'); }, async () => ({}),
    async () => ({email: 42}), async () => ({email: 'other@example.test'})]) {
    const reply = await invoke('/api/status', {access: {aud: env.ACCESS_AUD, getIdentity}});
    assert.equal(reply.status, 403);
    assert.equal(reply.calls.length, 0);
    assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS/);
  }
});

test('missing AWS configuration is an honest pending status and mutations are unavailable', async () => {
  const settings = {...env, AWS_SECRET_ACCESS_KEY: ''};
  const status = await invoke('/api/status', {}, settings);
  assert.equal(status.status, 200);
  assert.equal(status.body.doctor.connected, false);
  assert.equal(status.body.doctor.armed, false);
  assert.equal(status.body.checks[0].status, 'pending');
  assert.equal(status.calls.length, 0);
  const action = await invoke('/api/doctor/control', {method: 'POST', body: {action: 'arm', mutationId}}, settings);
  assert.equal(action.status, 503);
  assert.equal(action.calls.length, 0);
});

test('real status and durable setup record are projected to the public contract', async () => {
  const reply = await invoke('/api/status', {}, env, call => {
    if (call.target) {
      assert.equal(call.payload.ConsistentRead, true);
      if (call.payload.Key.pk.S !== 'portal#setup') return json({});
      return json(record({microsoftConnected: true, phoneVerified: true, subscription: {existingLicense: true},
        access_token: 'NEVERRETURNTHIS'}));
    }
    assert.deepEqual(call.payload, {action: 'status'});
    return json({connected: true, armed: true, paused: true,
      models: {doctor: 'us.anthropic.claude-opus-5-5', triage: 'us.anthropic.claude-haiku-5-5'}, limits: {worker_minutes: 45,
      model_allowance_usd: 20, case_allowance_usd: 40, starts_per_day: 3}, worker_capability: 'NEVERRETURNTHIS'});
  });
  assert.equal(reply.status, 200);
  assert.equal(reply.body.doctor.armed, false);
  assert.deepEqual(reply.body.doctor.limits, {attemptUsd: 20, caseUsd: 40, startsPerDay: 3, minutes: 45});
  assert.equal(reply.body.setup.microsoftConnected, true);
  assert.equal(reply.body.setup.apnsConfigured, false);
  assert.equal(reply.body.setup.subscription.existingLicense, true);
  assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS|DUMMY/);
});

test('mutation origin must exactly match and JSON is required before any AWS call', async () => {
  for (const headers of [{origin: 'https://other.example.test'}, {origin: ''}, {'content-type': 'text/plain'}]) {
    const reply = await invoke('/api/doctor/control', {method: 'POST', body: {action: 'arm', mutationId}, headers});
    assert.ok([403, 415].includes(reply.status));
    assert.equal(reply.calls.length, 0);
  }
});

test('body bound, UUID, action and field allowlist prevent broker or extra arguments', async () => {
  for (const body of ['x'.repeat(4097), {}, null, [], {action: 'finish', mutationId},
    {action: 'arm', mutationId: 'not-a-uuid'}, {action: 'arm', mutationId, script: 'NEVER_EXECUTE'},
    {action: 'arm', mutationId, case_id: caseId}, {action: 'recheck', mutationId, case_id: 'invalid'}]) {
    const reply = await invoke('/api/doctor/control', {method: 'POST', body});
    assert.equal(reply.status, 400, JSON.stringify(body).slice(0, 80));
    assert.equal(reply.calls.length, 0);
  }
});

test('controls invoke only the fixed controller and preserve the stable UUID', async () => {
  for (const action of ['pause', 'arm', 'self_test', 'recheck']) {
    const expected = action === 'pause' ? 'pause_new_runs' : action;
    const reply = await invoke('/api/doctor/control', {method: 'POST', body: {action, mutationId}}, env, call => {
      assert.equal(new URL(call.url).pathname, '/2015-03-31/functions/patchdoctor/invocations');
      assert.equal(call.target, null);
      assert.deepEqual(call.payload, {action: expected, mutationId, ...(action === 'pause' ? {paused: true} : {})});
      return json({state: 'completed', mutationId, action: expected, armed: false, worker_capability: 'NEVERRETURNTHIS'});
    });
    assert.equal(reply.status, 200);
    assert.equal(reply.body.acknowledged, true);
    assert.equal(reply.calls.length, 1);
    assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS|DUMMY/);
  }
});

test('uncertain, mismatched or empty controller receipt cannot become success', async () => {
  for (const value of [{}, {state: 'uncertain', mutationId, action: 'arm'},
    {state: 'completed', mutationId: 'another-id', action: 'arm'}, {state: 'completed', mutationId, action: 'self_test'},
    {refused: true, error_type: 'NEVERRETURNTHIS'}]) {
    const reply = await invoke('/api/doctor/control', {method: 'POST', body: {action: 'arm', mutationId}}, env, () => json(value));
    assert.equal(reply.status, 409);
    assert.equal(reply.body.acknowledged, undefined);
    assert.equal(reply.calls.length, 1);
    assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS/);
  }
});

test('lost response has no automatic mutation replay and no raw exception disclosure', async () => {
  const reply = await invoke('/api/doctor/control', {method: 'POST', body: {action: 'arm', mutationId}}, env,
    () => { throw new Error('NEVERRETURNTHIS https://signed.example.test/?token=secret'); });
  assert.equal(reply.status, 502);
  assert.equal(reply.calls.length, 1);
  assert.match(reply.body.message, /Refresh/);
  assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS|signed.example/);
  assert.deepEqual(reply.body.diagnostic, {stage: 'fetch', service: 'lambda', operation: 'InvokeFunction', errorClass: 'Error'});
  assert.deepEqual(reply.diagnostics, [reply.body.diagnostic]);
});

test('transport diagnostics expose only fixed stage, service, operation and allowlisted error class', async () => {
  for (const [handler, expected] of [
    [() => { throw new TypeError('NEVERRETURNTHIS'); }, {stage: 'fetch', errorClass: 'TypeError'}],
    [() => { const error = new Error('NEVERRETURNTHIS'); error.name = 'secret=NEVERRETURNTHIS'; throw error; },
      {stage: 'fetch', errorClass: 'Error'}],
    [() => json({message: 'NEVERRETURNTHIS'}, {status: 403}), {stage: 'response', errorClass: 'Error'}],
    [() => new Response('NEVERRETURNTHIS'), {stage: 'decode', errorClass: 'SyntaxError'}],
  ]) {
    const reply = await invoke('/api/setup/start', {method: 'POST', body: {}}, env, handler);
    assert.equal(reply.status, 502);
    assert.deepEqual(reply.body.diagnostic, {...expected, service: 'dynamodb', operation: 'GetItem'});
    assert.deepEqual(reply.diagnostics, [reply.body.diagnostic]);
    assert.doesNotMatch(JSON.stringify([reply.body, reply.diagnostics]), /NEVERRETURNTHIS|DUMMY|https:/);
    assert.equal(reply.calls.length, 1);
  }
});

test('case query uses sparse index and strong pointers; only safe DTO fields leave the server', async () => {
  const reply = await invoke('/api/doctor/cases', {}, env, call => {
    if (call.target.endsWith('.Query')) {
      assert.equal(call.payload.IndexName, 'active-cases');
      assert.equal(call.payload.Limit, 30);
      return json({Items: [{pk: {S: 'case#' + caseId}}], LastEvaluatedKey: {pk: {S: 'private'}}});
    }
    const pk = call.payload.Key.pk.S;
    if (pk === 'active-case') return json(record({case_id: otherCase}));
    if (pk === 'host-operation') return json({});
    assert.ok(['case#' + caseId, 'case#' + otherCase].includes(pk));
    return json(record({case_id: pk.slice(5), cve: 'CVE-2026-12345', status: 'running', updated_at: 1793552400,
      targets: [{name: 'fcvm-metal-arm', region: 'us-west-1', instance_id: 'i-never-show'}],
      summary: 'token=NEVERRETURNTHIS', findings: [{private: 'NEVERRETURNTHIS'}], worker_capability_hash: 'NEVERRETURNTHIS'}));
  });
  assert.equal(reply.status, 200);
  assert.equal(reply.body.cases.length, 2);
  assert.equal(reply.body.truncated, true);
  assert.equal(reply.body.scope, 'active');
  assert.deepEqual(reply.body.cases[0].targets, [{name: 'fcvm-metal-arm', region: 'us-west-1'}]);
  assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS|instance_id|worker_capability|findings/);
});

test('events parse only authored metadata and discard raw content, errors and unknown tools', async () => {
  const reply = await invoke('/api/doctor/events', {}, env, call => {
    assert.equal(call.target, 'Logs_20140328.FilterLogEvents');
    assert.equal(call.payload.logGroupName, '/aws/codebuild/patchdoctor');
    assert.equal(call.payload.startTime, now() - 3600000);
    return json({events: [
      {timestamp: now(), message: JSON.stringify({event: 'tool_use', tool: 'Bash', command: 'NEVERRETURNTHIS', token: 'NEVERRETURNTHIS'})},
      {timestamp: now(), message: 'NEVERRETURNTHIS raw log'},
      {timestamp: now(), message: JSON.stringify({event: 'NEVERRETURNTHIS', tool: 'Bash'})},
      {timestamp: now(), message: JSON.stringify({event: 'worker_failed', error_type: 'NEVERRETURNTHIS'})},
      {timestamp: now(), message: JSON.stringify({event: 'usage', input_tokens: 12, output_tokens: 3, raw: 'NEVERRETURNTHIS'})},
      {timestamp: now(), message: JSON.stringify({event: 'tool_use', tool: 'NEVERRETURNTHIS'})},
    ]});
  });
  assert.equal(reply.status, 200);
  assert.equal(reply.body.events.length, 4);
  assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS|"command"|"error_type"|"token"/);
  assert.equal(reply.body.events[0].tool, 'Bash');
});

test('arbitrary or incomplete successful Lambda JSON is not healthy status', async () => {
  for (const status of [{foo: 'bar'}, {connected: true, armed: true},
    {connected: true, armed: true, paused: false, models: {doctor: 'wrong-model'}, limits: {worker_minutes: 60}}]) {
    const reply = await invoke('/api/status', {}, env, call => json(call.target ? {} : status));
    assert.equal(reply.status, 502);
    assert.equal(reply.body.error, 'status_invalid');
    assert.equal(reply.body.doctor, undefined);
  }
});

test('recent activity follows bounded pages, includes newest page and signals truncation', async () => {
  let pages = 0;
  const reply = await invoke('/api/doctor/events', {}, env, call => {
    pages++;
    assert.equal(call.payload.nextToken, pages === 1 ? undefined : 'page-' + pages);
    return json({events: [{eventId: String(pages), timestamp: now() - 1000 * (6 - pages),
      message: JSON.stringify({event: 'case_finished', raw: 'NEVERRETURNTHIS'})}], nextToken: 'page-' + (pages + 1)});
  });
  assert.equal(pages, 5);
  assert.equal(reply.body.events.length, 5);
  assert.equal(reply.body.events[0].timestamp, new Date(now() - 1000).toISOString());
  assert.equal(reply.body.truncated, true);
  assert.equal(reply.body.windowMinutes, 60);
  assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS|page-/);
});

test('setup requires actual configuration and selects real consent/enrollment URLs from durable state', async () => {
  const missing = await invoke('/api/setup/start', {method: 'POST', body: {}}, env, () => json({}));
  assert.equal(missing.status, 503);
  assert.equal(missing.body.error, 'microsoft_setup_missing');
  for (const connected of [false, true]) {
    const settings = {...env, MICROSOFT_ADMIN_CONSENT_URL: 'https://login.microsoftonline.com/tenant/adminconsent?client_id=public-id',
      INTUNE_ENROLLMENT_URL: 'https://portal.manage.microsoft.com/'};
    const reply = await invoke('/api/setup/start', {method: 'POST', body: {}}, settings,
      () => json(record({microsoftConnected: connected})));
    assert.equal(reply.status, 200);
    assert.equal(reply.body.step, connected ? 'enroll' : 'microsoft');
    assert.equal(reply.body.url, connected ? settings.INTUNE_ENROLLMENT_URL : settings.MICROSOFT_ADMIN_CONSENT_URL);
  }
});

test('setup refuses insecure or credential-bearing destinations and caller-supplied URLs', async () => {
  for (const url of ['http://example.test/', 'https://name:password@example.test/', 'javascript:alert(1)']) {
    const reply = await invoke('/api/setup/start', {method: 'POST', body: {}}, {...env, MICROSOFT_ADMIN_CONSENT_URL: url}, () => json({}));
    assert.equal(reply.status, 503);
  }
  const reply = await invoke('/api/setup/start', {method: 'POST', body: {url: 'https://other.test/'}});
  assert.equal(reply.status, 400);
  assert.equal(reply.calls.length, 0);
});

test('bad configuration and AWS failure never return secret payloads', async () => {
  const wrongRegion = await invoke('/api/doctor/cases', {}, {...env, AWS_REGION: 'us-east-1'});
  assert.equal(wrongRegion.status, 503);
  assert.equal(wrongRegion.calls.length, 0);
  for (const response of [() => json({errorMessage: 'NEVERRETURNTHIS'}, {headers: {'x-amz-function-error': 'Unhandled'}}),
    () => json({__type: 'AccessDenied', message: 'NEVERRETURNTHIS'}, {status: 403}),
    () => new Response('NEVERRETURNTHIS', {status: 307, headers: {location: 'https://other.example.test/'}}),
    () => new Response('NEVERRETURNTHIS', {headers: {'content-length': '1048577'}})]) {
    const reply = await invoke('/api/status', {}, env, response);
    assert.equal(reply.status, 502);
    assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS|DUMMY|other.example/);
  }
});


const validDoctor = {connected: true, armed: false, paused: false,
  models: {doctor: 'us.anthropic.claude-opus-5-5', triage: 'us.anthropic.claude-haiku-5-5'},
  limits: {worker_minutes: 60, model_allowance_usd: 25, case_allowance_usd: 50, starts_per_day: 4}};

test('commissioning status uses exact durable records and redacts all configuration internals', async () => {
  const selfKey = 'case#' + await (async () => {
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode('PatchDoctor/SELF_TEST'));
    return Buffer.from(bytes).toString('hex');
  })();
  for (const [haiku, opus, expected] of [
    [false, 'self-test-passed', false], [true, 'pending', false], [true, 'running', false],
    [true, 'self-test-passed', true], [true, 'NEVERRETURNTHIS', false],
  ]) {
    const keys = [];
    const reply = await invoke('/api/status', {}, env, call => {
      if (!call.target) return json(validDoctor);
      const pk = call.payload.Key.pk.S; keys.push(pk);
      assert.equal(call.payload.ConsistentRead, true);
      if (pk === 'config') return json(record({haiku_self_test: {ok: haiku, checked_at: now() / 1000,
        error_type: 'NEVERRETURNTHIS'}, worker_capability_hash: 'NEVERRETURNTHIS', secret: 'NEVERRETURNTHIS'}));
      if (pk === selfKey) return json(record({mode: 'self-test', status: opus, outcome: 'self-test-passed',
        build_status: 'SUCCEEDED', updated_at: now() / 1000,
        capability: 'NEVERRETURNTHIS', summary: 'NEVERRETURNTHIS'}));
      assert.equal(pk, 'portal#setup'); return json({});
    });
    assert.equal(reply.status, 200);
    assert.deepEqual(new Set(keys), new Set(['config', selfKey, 'portal#setup']));
    assert.deepEqual(reply.body.doctor.selfTest, {haikuPassed: haiku, haikuCheckedAt: new Date(now()).toISOString(),
      opusStatus: opus === 'NEVERRETURNTHIS' ? 'not-run' : opus,
      opusCheckedAt: new Date(now()).toISOString(), canArm: expected});
    assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS|capability|secret|haiku_self_test/);
  }
});

test('commissioning missing records cannot enable repairs', async () => {
  const reply = await invoke('/api/status', {}, env, call => json(call.target ? {} : validDoctor));
  assert.equal(reply.status, 200);
  assert.deepEqual(reply.body.doctor.selfTest, {haikuPassed: false, haikuCheckedAt: null,
    opusStatus: 'not-run', opusCheckedAt: null, canArm: false});
});

test('completed failed Haiku commissioning is an informative refusal rather than a success acknowledgment', async () => {
  const reply = await invoke('/api/doctor/control', {method: 'POST', body: {action: 'self_test', mutationId}}, env,
    () => json({state: 'completed', action: 'self_test', mutationId,
      haiku: {ok: false, error_type: 'NEVERRETURNTHIS', reason: 'NEVERRETURNTHIS'}}));
  assert.equal(reply.status, 409);
  assert.equal(reply.body.error, 'commissioning_failed');
  assert.equal(reply.body.acknowledged, undefined);
  assert.match(reply.body.message, /Haiku.*failed.*Opus.*not queued/);
  assert.doesNotMatch(JSON.stringify(reply.body), /NEVERRETURNTHIS|error_type/);
  assert.equal(reply.calls.length, 1);
});

test('actual portal rendering keeps Enable disabled until commissioning passes and retains Pause while armed', () => {
  const html = renderPortal();
  assert.match(html, /id="doctor-self-test"/);
  const source = html.slice(html.indexOf('    function renderStatus()'), html.indexOf('    function caseStatus('));
  assert.ok(source.startsWith('    function renderStatus()'));
  for (const [proof, label, canArm] of [
    [{haikuPassed: false, canArm: false}, 'Not run', false],
    [{haikuPassed: false, haikuCheckedAt: new Date(now()).toISOString(), canArm: false}, 'Haiku check failed', false],
    [{haikuPassed: true, opusStatus: 'pending', canArm: false}, 'Opus queued', false],
    [{haikuPassed: true, opusStatus: 'running', canArm: false}, 'Opus running', false],
    [{haikuPassed: true, opusStatus: 'self-test-passed', canArm: true}, 'Passed', true],
  ]) {
    for (const armed of [false, true]) {
      const elements = new Map();
      const element = () => ({classList: {add() {}}, replaceChildren() {}, replaceWith() {}, append() {}});
      const buttons = ['arm', 'pause', 'self_test', 'recheck'].map(control => ({dataset: {control}, disabled: true}));
      const byId = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
      vm.runInNewContext(source + '\nrenderStatus();', {
        state: {status: {doctor: {connected: true, armed, selfTest: proof}, setup: {subscription: {existingLicense: true}}}},
        byId, document: {querySelectorAll: () => buttons}, setupSteps: [], node: element,
        text: value => typeof value === 'string' ? value : '', amount: () => 'example',
      }, {timeout: 1000});
      assert.equal(buttons[0].disabled, armed || !canArm);
      assert.equal(buttons[1].disabled, !armed);
      assert.match(byId('doctor-self-test').textContent, new RegExp(label));
    }
  }
});


test('commissioning requires matching Opus outcome and native successful build proof', async () => {
  for (const [extra, expected, status] of [
    [{outcome: 'self-test-passed', build_status: 'FAILED'}, false, 'failed'],
    [{outcome: 'self-test-passed', build_status: 'TIMED_OUT'}, false, 'failed'],
    [{outcome: 'self-test-passed'}, false, 'self-test-passed'],
    [{build_status: 'SUCCEEDED'}, false, 'self-test-passed'],
    [{outcome: 'blocked', build_status: 'SUCCEEDED'}, false, 'self-test-passed'],
    [{outcome: 'self-test-passed', build_status: 'SUCCEEDED'}, true, 'self-test-passed'],
  ]) {
    const reply = await invoke('/api/status', {}, env, call => {
      if (!call.target) return json(validDoctor);
      const pk = call.payload.Key.pk.S;
      if (pk === 'config') return json(record({haiku_self_test: {ok: true, checked_at: now() / 1000}}));
      if (pk.startsWith('case#')) return json(record({mode: 'self-test', status: 'self-test-passed', ...extra}));
      assert.equal(pk, 'portal#setup'); return json({});
    });
    assert.equal(reply.status, 200);
    assert.equal(reply.body.doctor.selfTest.canArm, expected, JSON.stringify(extra));
    assert.equal(reply.body.doctor.selfTest.opusStatus, status);
  }
});


test('commissioning follows only the current controller case pointer and refuses malformed pointers', async () => {
  for (const pointer of ['c'.repeat(64), '../../config', ['c'.repeat(64)], null]) {
    const keys = [];
    const reply = await invoke('/api/status', {}, env, call => {
      if (!call.target) return json({...validDoctor, self_test_case_id: pointer});
      const pk = call.payload.Key.pk.S; keys.push(pk);
      if (pk === 'config') return json(record({haiku_self_test: {ok: true}}));
      if (pk === 'case#' + pointer) return json(record({status: 'pending', mode: 'self-test'}));
      assert.equal(pk, 'portal#setup'); return json({});
    });
    if (typeof pointer === 'string' && pointer.length === 64) {
      assert.equal(reply.status, 200);
      assert.equal(reply.body.doctor.selfTest.opusStatus, 'pending');
      assert.equal(reply.body.doctor.selfTest.canArm, false);
      assert.ok(keys.includes('case#' + pointer));
      assert.equal(keys.filter(key => key.startsWith('case#')).length, 1);
      assert.doesNotMatch(JSON.stringify(reply.body), new RegExp(pointer));
    } else {
      assert.equal(reply.status, 502);
      assert.equal(reply.body.error, 'status_invalid');
      assert.deepEqual(keys, []);
    }
  }
});
