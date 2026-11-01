import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {renderPortal, renderMdmCredentialsPortal} from './portal-ui.mjs';

const html = renderPortal();
const mdmPanel = renderMdmCredentialsPortal();
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const mutationId = '11111111-2222-4333-8444-555555555555';

class Element {
  constructor(id = '') { this.id = id; this.textContent = ''; this.hidden = false; this.disabled = false; this.handlers = {}; this.dataset = {}; this.children = []; this.classList = {add() {}}; }
  setAttribute() {}
  addEventListener(name, handler) { this.handlers[name] = handler; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  focus() {}
}

async function browser(fetcher) {
  const elements = new Map();
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new Element(match[1]));
  const controls = ['arm', 'pause', 'self_test', 'recheck'].map(action => {
    const element = new Element(); element.dataset.control = action; element.disabled = true; return element;
  });
  const context = {AbortController, Date, Promise, crypto: {randomUUID: () => mutationId},
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    localStorage: {getItem: () => null, setItem() {}},
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
  return {elements, controls};
}

function reply(value) { return {ok: true, headers: {get: () => 'application/json'}, json: async () => value}; }


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
      return reply({ok: true, acknowledged: true, message: 'Repairs enabled.'});
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
