import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {renderPortal, renderMdmCredentialsPortal} from './portal-ui.mjs';

const html = renderPortal();
const mdmPanel = renderMdmCredentialsPortal();
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const mutationId = '11111111-2222-4333-8444-555555555555';
const outboxKey = 'patchdoctor-control-outbox-v1';

function memoryStorage() {
  const values = new Map();
  return {getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
}

class Element {
  constructor(id = '') { this.id = id; this.textContent = ''; this.hidden = false; this.disabled = false; this.handlers = {}; this.dataset = {}; this.children = []; this.classList = {add() {}}; }
  setAttribute() {}
  addEventListener(name, handler) { this.handlers[name] = handler; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  focus() {}
}

async function browser(fetcher, {storage = memoryStorage(), uuid = mutationId} = {}) {
  const elements = new Map();
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new Element(match[1]));
  const controls = ['arm', 'pause', 'self_test', 'recheck'].map(action => {
    const element = new Element(); element.dataset.control = action; element.disabled = true; return element;
  });
  const context = {AbortController, Date, Promise, crypto: {randomUUID: () => uuid},
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    localStorage: storage,
    document: {visibilityState: 'visible',
      getElementById(id) {
        assert.ok(!id.startsWith('mdm-'), 'Doctor activity must not read or rewrite MDM connection status');
        assert.ok(elements.has(id), 'UI referenced a missing element: ' + id);
        return elements.get(id);
      },
      createElement: () => new Element(),
      querySelectorAll: selector => { assert.equal(selector, '[data-control]'); return controls; },
    }, fetch: fetcher,
  };
  vm.runInNewContext(script, context, {timeout: 1000});
  await new Promise(resolve => setImmediate(resolve));
  return {elements, controls, storage};
}

function reply(value, status = 200) { return {ok: status >= 200 && status < 300, status, headers: {get: () => 'application/json'}, json: async () => value}; }


test('root MDM page is static and truthful, without a wizard, credential form or dead-end control', () => {
  assert.match(mdmPanel, /<h1>MDM credentials\.<\/h1>/);
  assert.match(mdmPanel, /role="status">Not connected/);
  assert.match(mdmPanel, /Microsoft organization administrator sign-in is still needed/);
  assert.match(mdmPanel, /The Intune account connection has not been configured here/);
  assert.doesNotMatch(mdmPanel, /<script|<button|<form|<input|<ol|role="tab|PatchDoctor|fetch\(|setInterval|\$96|APNs|push certificate|enrollment|subscription|adminconsent/i);
  assert.match(mdmPanel, /<html lang="en">/);
  assert.match(mdmPanel, /name="viewport"/);
  assert.match(mdmPanel, /href="#main"/);
  assert.match(mdmPanel, /prefers-reduced-motion:reduce/);
  assert.equal((html.match(/data-control="/g) || []).length, 4);
});

test('Doctor API failures leave the independent MDM status available and repair controls disabled', async () => {
  const calls = [];
  const {elements, controls} = await browser(async path => { calls.push(path); throw new Error('offline fixture'); });
  assert.deepEqual(calls, ['/api/status', '/api/doctor/cases', '/api/doctor/events']);
  assert.match(mdmPanel, /role="status">Not connected/);
  assert.doesNotMatch(mdmPanel, /status-error|offline fixture/);
  assert.equal(elements.get('status-error').hidden, false);
  assert.ok(controls.every(button => button.disabled));
  assert.equal(elements.get('doctor-badge').textContent, 'Status unavailable');
});

test('Doctor keeps its real arm action and commissioning gate without requiring MDM setup data', async () => {
  const posts = [];
  let armed = false;
  const fetcher = async (path, options) => {
    if (options.method === 'POST') {
      assert.equal(path, '/api/doctor/control');
      assert.equal(options.credentials, 'same-origin');
      assert.equal(options.redirect, 'error');
      posts.push(JSON.parse(options.body)); armed = true;
      return reply({ok: true, acknowledged: true, completed: true, action: 'arm', mutationId, message: 'Repairs enabled.'});
    }
    if (path === '/api/status') return reply({access: {protected: true}, doctor: {
      connected: true, armed, selfTest: {canArm: true}, limits: {attemptUsd: 25, caseUsd: 50, startsPerDay: 4, minutes: 60},
    }});
    return reply(path.endsWith('/cases') ? {cases: []} : {events: []});
  };
  const {elements, controls} = await browser(fetcher);
  const arm = controls.find(button => button.dataset.control === 'arm');
  assert.equal(arm.disabled, false);
  await arm.handlers.click();
  assert.deepEqual(posts, [{action: 'arm', mutationId}]);
  assert.equal(elements.get('doctor-badge').textContent, 'Repairs enabled');
  assert.equal(arm.disabled, true);
});

function doctorReply(path) {
  if (path === '/api/status') return reply({access: {protected: true}, doctor: {
    connected: true, armed: false, selfTest: {canArm: true}, limits: {},
  }});
  return reply(path.endsWith('/cases') ? {cases: []} : {events: []});
}

test('lost acknowledgement survives refresh and browser reload; explicit retry reuses the paid request ID', async () => {
  const storage = memoryStorage(), posts = [], savedBeforePost = [], receipts = new Map();
  let paidStarts = 0, loseAck = true;
  const fetcher = async (path, options) => {
    if (options.method !== 'POST') return doctorReply(path);
    const body = JSON.parse(options.body);
    savedBeforePost.push(JSON.parse(storage.getItem(outboxKey)));
    posts.push(body);
    if (!receipts.has(body.mutationId)) {
      paidStarts++;
      receipts.set(body.mutationId, {ok: true, acknowledged: true, completed: true, action: body.action, mutationId: body.mutationId});
    }
    if (loseAck) { loseAck = false; throw new Error('lost response after server commit'); }
    return reply(receipts.get(body.mutationId));
  };
  const first = await browser(fetcher, {storage});
  await first.controls.find(button => button.dataset.control === 'self_test').handlers.click();
  assert.equal(paidStarts, 1);
  const savedAfterLostAck = storage.getItem(outboxKey);
  await first.elements.get('refresh').handlers.click();
  const blockedAfterRefresh = first.controls.every(button => button.disabled);
  const reloaded = await browser(fetcher, {storage, uuid: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'});
  assert.equal(posts.length, 1, 'reload must not automatically replay');
  const blockedAfterReload = reloaded.controls.every(button => button.disabled);
  const retry = reloaded.elements.get('doctor-retry');
  // Before the outbox fix, the only retry offered after reload was another self-test.
  await (retry ?? reloaded.controls.find(button => button.dataset.control === 'self_test')).handlers.click();
  assert.equal(paidStarts, 1);
  assert.deepEqual(posts, [{action: 'self_test', mutationId}, {action: 'self_test', mutationId}]);
  assert.deepEqual(savedBeforePost, posts, 'outbox must be persisted before every POST');
  assert.deepEqual(JSON.parse(savedAfterLostAck), {action: 'self_test', mutationId});
  assert.equal(blockedAfterRefresh, true, 'status refresh cannot clear an unconfirmed request');
  assert.equal(blockedAfterReload, true);
  assert.equal(storage.getItem(outboxKey), null);
  assert.equal(reloaded.elements.get('doctor-pending').hidden, true);
  assert.equal(reloaded.controls.find(button => button.dataset.control === 'self_test').disabled, false);
});

test('storage failure refuses a mutation before sending it', async () => {
  let posts = 0;
  const storage = {getItem: () => null, setItem: () => { throw new Error('storage denied'); }, removeItem() {}};
  const view = await browser(async (path, options) => {
    if (options.method === 'POST') posts++;
    return doctorReply(path);
  }, {storage});
  await view.controls.find(button => button.dataset.control === 'self_test').handlers.click();
  assert.equal(posts, 0);
  assert.ok(view.controls.every(button => button.disabled));
  assert.match(view.elements.get('doctor-message').textContent, /request was not sent/);
});

test('an ordinary successful HTTP response does not clear the outbox without a durable acknowledgement', async () => {
  for (const response of [{ok: true, action: 'recheck', message: 'Still processing.'},
    {ok: true, acknowledged: true, completed: true, action: 'recheck', mutationId: 'wrong-request'}]) {
    const storage = memoryStorage();
    const view = await browser(async (path, options) => options.method === 'POST' ? reply(response) : doctorReply(path), {storage});
    await view.controls.find(button => button.dataset.control === 'recheck').handlers.click();
    assert.deepEqual(JSON.parse(storage.getItem(outboxKey)), {action: 'recheck', mutationId});
    assert.ok(view.controls.every(button => button.disabled));
    assert.equal(view.elements.get('doctor-retry').disabled, false);
  }
});

test('the exact durable failed self-test acknowledgement clears its request while preserving the failure and arm gate', async () => {
  const storage = memoryStorage();
  const view = await browser(async (path, options) => {
    if (options.method === 'POST') return reply({ok: false, acknowledged: true, completed: true,
      action: 'self_test', mutationId, error: 'commissioning_failed', message: 'Haiku self-test failed.'}, 409);
    if (path === '/api/status') return reply({doctor: {connected: true, armed: false, selfTest: {canArm: false}}});
    return doctorReply(path);
  }, {storage});
  await view.controls.find(button => button.dataset.control === 'self_test').handlers.click();
  assert.equal(storage.getItem(outboxKey), null);
  assert.equal(view.elements.get('doctor-pending').hidden, true);
  assert.match(view.elements.get('doctor-message').textContent, /Haiku self-test failed/);
  assert.match(view.elements.get('doctor-message').className, /error/);
  assert.equal(view.controls.find(button => button.dataset.control === 'arm').disabled, true);
  assert.equal(view.controls.find(button => button.dataset.control === 'self_test').disabled, false);
});

test('uncertain or mismatched HTTP409 responses never clear a saved request', async () => {
  for (const response of [
    {ok: false, acknowledged: true, completed: true, action: 'self_test', mutationId, error: 'control_uncertain'},
    {ok: false, acknowledged: true, completed: true, action: 'self_test', mutationId: 'wrong-request', error: 'commissioning_failed'},
    {ok: false, acknowledged: true, completed: false, action: 'self_test', mutationId, error: 'commissioning_failed'},
  ]) {
    const storage = memoryStorage();
    const view = await browser(async (path, options) => options.method === 'POST' ? reply(response, 409) : doctorReply(path), {storage});
    await view.controls.find(button => button.dataset.control === 'self_test').handlers.click();
    assert.deepEqual(JSON.parse(storage.getItem(outboxKey)), {action: 'self_test', mutationId});
    assert.ok(view.controls.every(button => button.disabled));
  }
});

test('partial activity is disclosed for backend truncation and the visible fifteen-event limit', async () => {
  for (const events of [{events: [], truncated: true}, {events: Array.from({length: 16}, () => ({type: 'self_test'}))}]) {
    const view = await browser(async path => path.endsWith('/events') ? reply(events) : doctorReply(path));
    assert.match(view.elements.get('events').children[0].textContent, /Partial activity: some recent events are not shown/);
  }
});
