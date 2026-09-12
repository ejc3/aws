import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../../browser-manager.tf', import.meta.url), 'utf8');
const macSource = readFileSync(new URL('../../browser-manager-mac.tf', import.meta.url), 'utf8');

function block(kind, type, name) {
  const match = source.match(new RegExp(`^${kind} "${type}" "${name}" \\{\\n.*?^\\}`, 'ms'));
  assert.ok(match, `missing ${kind} ${type}.${name}`);
  return match[0];
}

test('AWS connector rotation preserves the remotely managed tunnel identity', () => {
  const secret = block('resource', 'random_bytes', 'browser_manager_tunnel_secret');
  assert.match(secret, /length\s*=\s*32\b/);
  assert.match(secret, /keepers\s*=\s*\{\s*rotation\s*=\s*"\d{4}-\d{2}-\d{2}"\s*\}/);
  const tunnel = block('resource', 'cloudflare_zero_trust_tunnel_cloudflared', 'browser_manager');
  assert.match(tunnel, /config_src\s*=\s*"cloudflare"/);
  assert.match(tunnel, /tunnel_secret\s*=\s*random_bytes\.browser_manager_tunnel_secret\.base64/);
  assert.match(tunnel, /prevent_destroy\s*=\s*true/);
  assert.doesNotMatch(tunnel, /replace_triggered_by|ignore_changes|local-exec|remote-exec/);
  assert.doesNotMatch(source, /resource "random_id" "browser_manager_tunnel_secret"/);
  assert.doesNotMatch(macSource, /random_bytes\.browser_manager_tunnel_secret\./);
});

test('replacement token is read after the PATCH and published only to its existing secret', () => {
  const token = block('data', 'cloudflare_zero_trust_tunnel_cloudflared_token', 'browser_manager');
  assert.match(token, /tunnel_id\s*=\s*cloudflare_zero_trust_tunnel_cloudflared\.browser_manager\.id/);
  assert.match(token, /depends_on\s*=\s*\[cloudflare_zero_trust_tunnel_cloudflared\.browser_manager\]/);
  const version = block('resource', 'aws_secretsmanager_secret_version', 'browser_manager_tunnel_token');
  assert.match(version, /secret_id\s*=\s*aws_secretsmanager_secret\.browser_manager_tunnel_token\.id/);
  assert.match(version, /secret_string\s*=\s*data\.cloudflare_zero_trust_tunnel_cloudflared_token\.browser_manager\.token/);
  const output = source.match(/^output "browser_manager_tunnel_token" \{\n.*?^\}/ms)?.[0];
  assert.ok(output);
  assert.match(output, /sensitive\s*=\s*true/);
});
