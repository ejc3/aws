// Server-only AWS transport. Browser responses contain only the DTOs below.
const portalEncoder = new TextEncoder();
const PORTAL_REGION = 'us-west-1';
const PORTAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const PORTAL_CASE = /^[0-9a-f]{64}$/;
const PORTAL_ACTIONS = new Set(['pause', 'arm', 'self_test', 'recheck']);
const PORTAL_TOOLS = new Set(['Read', 'Edit', 'Write', 'Bash', 'Agent', 'WebFetch', 'WebSearch', 'Glob', 'Grep']);
const PORTAL_EVENTS = new Set(['claude_event', 'tool_use', 'usage', 'checkpoint_restored',
  'checkpoint_acknowledged', 'claude_exited', 'private_stream_drain_expired',
  'final_checkpoint_failed', 'case_finished', 'worker_failed', 'unparsed_private_event']);
const PORTAL_STATES = new Set(['pending', 'running', 'starting', 'start-uncertain',
  'daily-budget-wait', 'awaiting-native-closure', 'deferred', 'blocked-no-CVE',
  'blocked-no-current-critical-target', 'blocked-start-rejected', 'budget-exhausted',
  'resolved', 'patched', 'mitigated', 'blocked', 'self-test-passed', 'failed']);

class PortalError extends Error {
  constructor(status, code, message, diagnostic) {
    super(message); this.status = status; this.code = code; this.diagnostic = diagnostic;
  }
}
function portalFail(status, code, message, diagnostic) { throw new PortalError(status, code, message, diagnostic); }
function portalJson(body, status = 200) {
  return new Response(JSON.stringify(body), {status, headers: {
    'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store',
    'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer',
  }});
}
async function portalOwner(env, access) {
  const ownerEmail = typeof env.OWNER_EMAIL === 'string' ? env.OWNER_EMAIL.trim().toLowerCase() : '';
  const audience = typeof env.ACCESS_AUD === 'string' ? env.ACCESS_AUD.trim() : '';
  if (!ownerEmail || !audience) portalFail(503, 'owner_setup_missing', 'The portal owner access is not configured yet.');
  // Cloudflare supplies this context only to the directly authenticated Worker invocation.
  if (!access || typeof access.getIdentity !== 'function') portalFail(401, 'sign_in_required', 'Sign in through Cloudflare Access to continue.');
  if (access.aud !== audience) portalFail(403, 'owner_only', 'This portal is available only to its owner.');
  let identity;
  try { identity = await access.getIdentity(); }
  catch { portalFail(403, 'owner_only', 'This portal is available only to its owner.'); }
  if (typeof identity?.email !== 'string' || identity.email.trim().toLowerCase() !== ownerEmail) {
    portalFail(403, 'owner_only', 'This portal is available only to its owner.');
  }
}
function portalDiagnostic(stage, service, operation, error) {
  const classes = new Set(['Error', 'TypeError', 'RangeError', 'SyntaxError', 'AbortError',
    'NotSupportedError', 'DataError', 'OperationError']);
  return {stage, service, operation, errorClass: classes.has(error?.name) ? error.name : 'Error'};
}
function portalEncode(value) {
  return encodeURIComponent(value).replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
}
function portalHex(value) { return Array.from(new Uint8Array(value), x => x.toString(16).padStart(2, '0')).join(''); }
async function portalHash(value) { return portalHex(await crypto.subtle.digest('SHA-256', portalEncoder.encode(value))); }
async function portalHmac(key, value) {
  const imported = await crypto.subtle.importKey('raw', typeof key === 'string' ? portalEncoder.encode(key) : key,
    {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']);
  return crypto.subtle.sign('HMAC', imported, portalEncoder.encode(value));
}

export async function signAwsRequest({url, service, region, method = 'POST', headers = {}, body = '', credentials, now = new Date()}) {
  const target = new URL(url);
  if (target.protocol !== 'https:' || target.username || target.password) throw new Error('HTTPS AWS endpoint required');
  const signed = new Headers(headers);
  const amzDate = now.toISOString().replace(/[:-]|\.\d{3}/g, '');
  signed.set('host', target.host);
  signed.set('x-amz-date', amzDate);
  if (credentials.sessionToken) signed.set('x-amz-security-token', credentials.sessionToken);
  const payloadHash = await portalHash(body);
  if (service === 's3') signed.set('x-amz-content-sha256', payloadHash);
  const entries = [...signed.entries()].map(([key, value]) => [key.toLowerCase(), value.trim().replace(/\s+/g, ' ')])
    .sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
  const names = entries.map(([key]) => key).join(';');
  const query = [...target.searchParams].map(([key, value]) => [portalEncode(key), portalEncode(value)])
    .sort(([ak, av], [bk, bv]) => ak < bk ? -1 : ak > bk ? 1 : av < bv ? -1 : av > bv ? 1 : 0)
    .map(([key, value]) => key + '=' + value).join('&');
  const path = service === 's3' ? target.pathname : target.pathname.split('/').map(portalEncode).join('/');
  const canonical = [method.toUpperCase(), path || '/', query,
    entries.map(([key, value]) => `${key}:${value}\n`).join(''), names, payloadHash].join('\n');
  const date = amzDate.slice(0, 8);
  const scope = `${date}/${region}/${service}/aws4_request`;
  let key = await portalHmac('AWS4' + credentials.secretAccessKey, date);
  for (const part of [region, service, 'aws4_request']) key = await portalHmac(key, part);
  const signature = portalHex(await portalHmac(key, `AWS4-HMAC-SHA256\n${amzDate}\n${scope}\n${await portalHash(canonical)}`));
  signed.set('authorization', `AWS4-HMAC-SHA256 Credential=${credentials.accessKeyId}/${scope}, SignedHeaders=${names}, Signature=${signature}`);
  return signed;
}

async function portalReadJson(response, limit = 1048576) {
  if (Number(response.headers.get('content-length')) > limit) throw new Error('response exceeds bound');
  const reader = response.body?.getReader();
  if (!reader) throw new Error('missing response body');
  const chunks = []; let total = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > limit) { await reader.cancel(); throw new Error('response exceeds bound'); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const data = new Uint8Array(total); let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(data));
}

function portalConfig(env) {
  const missing = ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'CONTROL_LAMBDA_NAME', 'TABLE_NAME', 'LOG_GROUP']
    .filter(key => typeof env[key] !== 'string' || !env[key].trim());
  if (missing.length) portalFail(503, 'service_setup_missing', 'The private AWS connection is not configured yet.');
  if (env.AWS_REGION !== PORTAL_REGION || !/^[A-Za-z0-9-_]{1,64}(?::[A-Za-z0-9-_]+)?$/.test(env.CONTROL_LAMBDA_NAME)
      || !/^[A-Za-z0-9_.-]{3,255}$/.test(env.TABLE_NAME) || !/^\/[A-Za-z0-9_./#-]{1,255}$/.test(env.LOG_GROUP)) {
    portalFail(503, 'service_setup_invalid', 'The private AWS connection needs its configuration checked.');
  }
  return {credentials: {accessKeyId: env.AWS_ACCESS_KEY_ID, secretAccessKey: env.AWS_SECRET_ACCESS_KEY,
    sessionToken: env.AWS_SESSION_TOKEN}, control: env.CONTROL_LAMBDA_NAME, table: env.TABLE_NAME, logGroup: env.LOG_GROUP};
}

class PortalAws {
  constructor(env, fetcher, now, diagnostic) {
    this.config = portalConfig(env); this.fetcher = fetcher; this.now = now; this.diagnostic = diagnostic;
  }
  async call(service, operation, payload) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    let stage = 'sign';
    try {
      const isLambda = service === 'lambda';
      const url = isLambda
        ? `https://lambda.${PORTAL_REGION}.amazonaws.com/2015-03-31/functions/${portalEncode(this.config.control)}/invocations`
        : `https://${service}.${PORTAL_REGION}.amazonaws.com/`;
      const body = JSON.stringify(payload);
      const headers = isLambda ? {'content-type': 'application/json', 'x-amz-invocation-type': 'RequestResponse'}
        : {'content-type': service === 'logs' ? 'application/x-amz-json-1.1' : 'application/x-amz-json-1.0',
          'x-amz-target': `${service === 'logs' ? 'Logs_20140328' : 'DynamoDB_20120810'}.${operation}`};
      const signed = await signAwsRequest({url, service, region: PORTAL_REGION, headers, body,
        credentials: this.config.credentials, now: new Date(this.now())});
      stage = 'fetch';
      // workerd supports manual/follow only. Manual keeps signed credentials off redirect targets.
      const reply = await this.fetcher(url, {method: 'POST', headers: signed, body, redirect: 'manual', signal: controller.signal});
      stage = 'response';
      if (!reply.ok || reply.headers.has('x-amz-function-error')) throw new Error('AWS operation failed');
      stage = 'decode';
      const result = await portalReadJson(reply);
      if (!result || typeof result !== 'object' || Array.isArray(result) || result.refused || result.errorMessage) {
        portalFail(409, 'control_refused', 'PatchDoctor refused this request. Refresh its status before trying again.');
      }
      return result;
    } catch (error) {
      if (error instanceof PortalError) throw error;
      const diagnostic = portalDiagnostic(stage, service, operation || 'InvokeFunction', error);
      try { this.diagnostic(diagnostic); } catch { /* Diagnostics cannot change the original result. */ }
      portalFail(502, 'service_unavailable', 'The private service did not confirm this request. Refresh before trying again.', diagnostic);
    } finally { clearTimeout(timeout); }
  }
  control(payload) { return this.call('lambda', '', payload); }
  ddb(operation, payload) { return this.call('dynamodb', operation, {TableName: this.config.table, ...payload}); }
  async record(pk) {
    const result = await this.ddb('GetItem', {Key: {pk: {S: pk}}, ConsistentRead: true});
    if (!result.Item) return null;
    if (typeof result.Item.body?.S !== 'string') throw new PortalError(502, 'record_invalid', 'A private service record needs checking.');
    try { return JSON.parse(result.Item.body.S); }
    catch { portalFail(502, 'record_invalid', 'A private service record needs checking.'); }
  }
}

function portalText(value, length = 1000) {
  if (typeof value !== 'string') return '';
  if (/PRIVATE KEY|(?:AKIA|ASIA)[A-Z0-9]{16}|sk-ant-|github_pat_|ghp_|Bearer\s|(?:password|secret|token|access.?key)\s*[=:]/i.test(value)) return '[Sensitive details withheld]';
  return value.replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, length);
}
function portalTime(value) {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) return new Date(value < 1e12 ? value * 1000 : value).toISOString();
  if (typeof value === 'string' && /^\d{4}-\d\d-\d\dT/.test(value) && Number.isFinite(Date.parse(value))) return new Date(value).toISOString();
  return null;
}
function portalCase(value) {
  if (!value || !PORTAL_CASE.test(value.case_id)) return null;
  return {id: value.case_id, cve: /^CVE-\d{4}-\d{4,10}$/.test(value.cve) || value.cve === 'SELF_TEST' ? value.cve : null,
    status: PORTAL_STATES.has(value.status) ? value.status : 'unknown',
    targets: (Array.isArray(value.targets) ? value.targets : []).slice(0, 30).map(target => ({
      name: /^[a-zA-Z0-9_.-]{1,80}$/.test(target.name) ? target.name : 'Managed host',
      region: /^us-(?:east|west)-[12]$/.test(target.region) ? target.region : null,
    })), updatedAt: portalTime(value.updated_at), summary: portalText(value.summary),
    triage: value.triage && typeof value.triage === 'object' ? {
      decision: ['run_doctor', 'defer'].includes(value.triage.decision) ? value.triage.decision : 'unknown',
      category: ['waiting_for_vendor', 'not_applicable', 'already_mitigated', 'needs_investigation'].includes(value.triage.category) ? value.triage.category : 'unknown',
    } : null};
}
function portalSetup(record) {
  const value = record && typeof record === 'object' ? record : {};
  const result = {};
  for (const key of ['microsoftConnected', 'apnsConfigured', 'mdmEnrolled', 'cloudflareEnrolled', 'phoneVerified', 'macVerified']) result[key] = value[key] === true;
  result.subscription = {annualUsd: 96, existingLicense: value.subscription?.existingLicense === true};
  return result;
}
function portalSelfTest(config, selfCase) {
  const haiku = config?.haiku_self_test;
  const haikuPassed = haiku?.ok === true;
  const nativeFailed = ['FAILED', 'FAULT', 'STOPPED', 'TIMED_OUT'].includes(selfCase?.build_status);
  const opusStatus = nativeFailed ? 'failed' : PORTAL_STATES.has(selfCase?.status) ? selfCase.status : 'not-run';
  return {haikuPassed, haikuCheckedAt: portalTime(haiku?.checked_at),
    opusStatus, opusCheckedAt: portalTime(selfCase?.updated_at),
    canArm: haikuPassed && selfCase?.status === 'self-test-passed'
      && selfCase?.outcome === 'self-test-passed' && selfCase?.build_status === 'SUCCEEDED'};
}
function portalDoctor(value) {
  const source = value.doctor && typeof value.doctor === 'object' ? value.doctor : value;
  const limits = source.limits || {};
  if (source.connected !== true || typeof source.armed !== 'boolean' || typeof source.paused !== 'boolean'
      || source.models?.doctor !== 'us.anthropic.claude-opus-5-5' || source.models?.triage !== 'us.anthropic.claude-haiku-5-5'
      || ['model_allowance_usd', 'case_allowance_usd', 'starts_per_day', 'worker_minutes'].some(key =>
        typeof limits[key] !== 'number' || !Number.isFinite(limits[key]) || limits[key] <= 0)) {
    portalFail(502, 'status_invalid', 'PatchDoctor did not return a verified status. Refresh before using its controls.');
  }
  return {connected: true, armed: source.armed === true && source.paused !== true,
    triageModel: 'Haiku 5.5', model: 'Opus 5.5', limits: {attemptUsd: limits.model_allowance_usd,
      caseUsd: limits.case_allowance_usd, startsPerDay: limits.starts_per_day, minutes: limits.worker_minutes}};
}

async function portalMutation(request) {
  if (request.headers.get('origin') !== new URL(request.url).origin) portalFail(403, 'origin_refused', 'Open the portal directly before using its controls.');
  if (request.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') portalFail(415, 'json_required', 'Send this control as JSON.');
  try { return await portalReadJson(request, 4096); }
  catch { portalFail(400, 'body_invalid', 'The request must be valid JSON no larger than 4 KiB.'); }
}

export async function handlePortalRequest(request, env, renderPortal, dependencies = {}) {
  try {
    await portalOwner(env, dependencies.access);
    const url = new URL(request.url);
    if (request.method === 'GET' && ['/', '/doctor'].includes(url.pathname)) {
      return new Response(renderPortal(url.pathname), {headers: {'content-type': 'text/html; charset=utf-8',
        'cache-control': 'no-store', 'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer',
        'content-security-policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"}});
    }
    if (!url.pathname.startsWith('/api/')) return portalJson({error: 'not_found', message: 'This page does not exist.'}, 404);
    const fetcher = dependencies.fetch ?? globalThis.fetch.bind(globalThis);
    const now = dependencies.now ?? Date.now;
    const diagnostic = dependencies.diagnostic ?? (value => console.warn(JSON.stringify(value)));
    if (request.method === 'GET' && url.pathname === '/api/status') {
      try {
        const aws = new PortalAws(env, fetcher, now, diagnostic);
        const doctor = await aws.control({action: 'status'});
        const projected = portalDoctor(doctor);
        const source = doctor.doctor && typeof doctor.doctor === 'object' ? doctor.doctor : doctor;
        if (source.self_test_case_id !== undefined && (typeof source.self_test_case_id !== 'string' || !PORTAL_CASE.test(source.self_test_case_id))) {
          portalFail(502, 'status_invalid', 'PatchDoctor did not return a verified self-test identity.');
        }
        const selfCaseKey = 'case#' + (source.self_test_case_id ?? await portalHash('PatchDoctor/SELF_TEST'));
        const [setup, config, selfCase] = await Promise.all([
          aws.record('portal#setup'), aws.record('config'), aws.record(selfCaseKey),
        ]);
        return portalJson({doctor: {...projected, selfTest: portalSelfTest(config, selfCase)},
          setup: portalSetup(setup), access: {protected: true}, checks: []});
      } catch (error) {
        if (error instanceof PortalError && error.code === 'service_setup_missing') return portalJson({
          doctor: {connected: false, armed: false}, setup: portalSetup(null), access: {protected: true},
          checks: [{name: 'Private service connection', status: 'pending', message: error.message}],
        });
        throw error;
      }
    }
    if (request.method === 'GET' && url.pathname === '/api/doctor/cases') {
      const aws = new PortalAws(env, fetcher, now, diagnostic);
      const index = await aws.ddb('Query', {IndexName: 'active-cases', Limit: 30, ScanIndexForward: false,
        KeyConditionExpression: 'record_kind = :kind', ExpressionAttributeValues: {':kind': {S: 'active-case'}}});
      if (!Array.isArray(index.Items)) portalFail(502, 'cases_invalid', 'PatchDoctor did not return a case list.');
      const keys = new Set(index.Items.map(item => item.pk?.S).filter(pk => /^case#[0-9a-f]{64}$/.test(pk)));
      // Strong pointers cover the sparse index's eventual consistency without a table scan.
      const pointers = await Promise.all(['active-case', 'host-operation'].map(pk => aws.record(pk)));
      for (const pointer of pointers) if (PORTAL_CASE.test(pointer?.case_id)) keys.add('case#' + pointer.case_id);
      const cases = [];
      for (const key of keys) {
        const value = portalCase(await aws.record(key));
        if (value) cases.push(value);
      }
      cases.sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || ''));
      return portalJson({cases, scope: 'active', truncated: !!index.LastEvaluatedKey});
    }
    if (request.method === 'GET' && url.pathname === '/api/doctor/events') {
      const aws = new PortalAws(env, fetcher, now, diagnostic);
      const events = [], seen = new Set();
      let nextToken;
      const endTime = now(), startTime = endTime - 3600000;
      for (let page = 0; page < 5; page++) {
        const result = await aws.call('logs', 'FilterLogEvents', {logGroupName: aws.config.logGroup,
          startTime, endTime, limit: 50, filterPattern: '{ $.event = "*" }', ...(nextToken ? {nextToken} : {})});
        for (const event of (Array.isArray(result.events) ? result.events : []).slice(0, 50)) {
          if (event.eventId && seen.has(event.eventId)) continue;
          if (event.eventId) seen.add(event.eventId);
          let value; try { value = JSON.parse(event.message); } catch { continue; }
          if (!PORTAL_EVENTS.has(value?.event)) continue;
          const timestamp = portalTime(event.timestamp);
          if (!timestamp || event.timestamp < startTime || event.timestamp > endTime) continue;
          const row = {timestamp, type: value.event};
          if (PORTAL_TOOLS.has(value.tool)) row.tool = value.tool;
          for (const key of ['input_tokens', 'output_tokens', 'bytes']) if (Number.isSafeInteger(value[key]) && value[key] >= 0) row[key] = value[key];
          events.push(row);
        }
        const token = typeof result.nextToken === 'string' ? result.nextToken : undefined;
        if (!token) { nextToken = undefined; break; }
        if (token === nextToken) break;
        nextToken = token;
      }
      events.sort((a, b) => b.timestamp.localeCompare(a.timestamp));
      return portalJson({events, truncated: !!nextToken, windowMinutes: 60});
    }
    if (request.method === 'POST' && url.pathname === '/api/doctor/control') {
      const body = await portalMutation(request);
      if (!body || Array.isArray(body) || !PORTAL_ACTIONS.has(body.action) || !PORTAL_UUID.test(body.mutationId)
          || Object.keys(body).some(key => !['action', 'mutationId', 'case_id'].includes(key))
          || (body.case_id !== undefined && (body.action !== 'recheck' || !PORTAL_CASE.test(body.case_id)))) {
        portalFail(400, 'control_invalid', 'Choose one of the portal controls and provide its mutation ID.');
      }
      const action = body.action === 'pause' ? {action: 'pause_new_runs', paused: true} : {action: body.action};
      const result = await new PortalAws(env, fetcher, now, diagnostic).control({...action, mutationId: body.mutationId,
        ...(body.case_id ? {case_id: body.case_id} : {})});
      if (result.state !== 'completed' || result.mutationId !== body.mutationId || result.action !== action.action) {
        portalFail(409, 'control_unconfirmed', 'This action has not been durably confirmed. Refresh before trying again.');
      }
      if (body.action === 'self_test' && !PORTAL_CASE.test(result.case_id) && result.haiku?.ok === false) {
        portalFail(409, 'commissioning_failed', 'Haiku’s self-test failed, so the Opus self-test was not queued. Refresh status before trying again.');
      }
      const reply = {ok: true, action: body.action, acknowledged: true};
      if (PORTAL_CASE.test(result.case_id)) reply.caseId = result.case_id;
      for (const key of ['armed', 'paused']) if (typeof result[key] === 'boolean') reply[key] = result[key];
      reply.message = {pause: 'New repair runs are paused. Existing work continues.', arm: 'Repair activation was acknowledged. Refresh to see its current state.',
        self_test: 'The self-test request was acknowledged.', recheck: 'The recheck request was acknowledged.'}[body.action];
      return portalJson(reply);
    }
    if (request.method === 'POST' && url.pathname === '/api/setup/start') {
      const body = await portalMutation(request);
      if (!body || Array.isArray(body) || Object.keys(body).length) portalFail(400, 'setup_invalid', 'Start setup from the portal.');
      const setup = portalSetup(await new PortalAws(env, fetcher, now, diagnostic).record('portal#setup'));
      const key = setup.microsoftConnected ? 'INTUNE_ENROLLMENT_URL' : 'MICROSOFT_ADMIN_CONSENT_URL';
      let target;
      try { target = new URL(env[key]); } catch { /* Missing setup is a real pending state. */ }
      if (!target || target.protocol !== 'https:' || target.username || target.password) portalFail(503, 'microsoft_setup_missing',
        setup.microsoftConnected ? 'The tenant is connected. Its Company Portal enrollment link still needs to be configured.'
          : 'The Microsoft tenant connection is not configured yet. Connect the owner’s Intune tenant before enrollment.');
      return portalJson({url: target.href, step: setup.microsoftConnected ? 'enroll' : 'microsoft',
        message: setup.microsoftConnected ? 'Continue in Company Portal. Enrollment requires your consent.' : 'Continue to Microsoft to connect your tenant.'});
    }
    return portalJson({error: 'not_found', message: 'This API route is not available.'}, 404);
  } catch (error) {
    if (error instanceof PortalError) return portalJson({error: error.code, message: error.message,
      ...(error.diagnostic ? {diagnostic: error.diagnostic} : {})}, error.status);
    return portalJson({error: 'service_unavailable', message: 'The private service is temporarily unavailable. Refresh before trying again.'}, 502);
  }
}
