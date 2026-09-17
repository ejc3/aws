import assert from 'node:assert/strict';
import { test } from 'node:test';
import { isAppSchemeRedirect } from '../lib/desktop.mjs';

test('captures custom app-scheme deep links the desktop cannot follow', () => {
  assert.equal(isAppSchemeRedirect('vsfapp://com.verizon.familybase.parent/signin?code=abc&state=x'), true);
  assert.equal(isAppSchemeRedirect('myapp:callback?token=1'), true);
  assert.equal(isAppSchemeRedirect('com.example.app://oauth'), true);
});

test('ignores ordinary web and browser-internal URLs', () => {
  for (const u of [
    'https://secure.verizon.com/signin',
    'http://127.0.0.1:8080/cb',
    'wss://example.com/socket',
    'about:blank',
    'chrome://password-manager/passwords',
    'chrome-extension://abcd/page.html',
    'devtools://devtools/bundled/inspector.html',
    'data:text/html,hi',
    'blob:https://x/9f',
    'file:///etc/hosts',
    'view-source:https://x',
    'javascript:void(0)',
  ]) {
    assert.equal(isAppSchemeRedirect(u), false, u);
  }
});

test('ignores malformed / non-string input', () => {
  assert.equal(isAppSchemeRedirect(undefined), false);
  assert.equal(isAppSchemeRedirect(''), false);
  assert.equal(isAppSchemeRedirect('/relative/path'), false);
  assert.equal(isAppSchemeRedirect('not a url'), false);
});
