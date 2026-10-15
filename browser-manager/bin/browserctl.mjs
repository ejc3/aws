#!/usr/bin/env node
import { request } from 'node:http';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { instanceLabel, instanceName, startUrl } from '../lib/auth.mjs';

const usage = 'browserctl start <name> [--url https://example.com] [--profile /absolute/local/profile]\n' +
  'browserctl list\nbrowserctl stop <name>\nbrowserctl url <name>\nbrowserctl rename <name> "Display label"\n' +
  'browserctl cdp <name>      print a loopback CDP endpoint for the running browser (Playwright connect_over_cdp)\n' +
  'browserctl job <name> <job.py> [args...]   run a job in the running browser (bin/browser-job.py; jobs/)\n' +
  'Stopping retains the browser profile and login. No delete command.';

try {
  const [command, name, ...args] = process.argv.slice(2);
  if (!command || command === '--help' || command === 'help') { console.log(usage); process.exit(0); }
  if (command === 'job') {
    // Hand off to the job runner beside this file; it asks this CLI for the endpoint itself.
    const { spawnSync } = await import('node:child_process');
    const { fileURLToPath } = await import('node:url');
    const runner = fileURLToPath(new URL('./browser-job.py', import.meta.url));
    const python = process.env.BM_JOB_PYTHON || join(homedir(), 'pw-venv/bin/python');
    const { existsSync } = await import('node:fs');
    if (!existsSync(python)) {
      throw new Error(`browserctl job needs Python with Playwright at ${python} (or set BM_JOB_PYTHON):\n` +
        '  python3 -m venv ~/pw-venv && ~/pw-venv/bin/pip install playwright\n(no browser download: jobs drive the manager\'s browser)');
    }
    const result = spawnSync(python, [runner, name, ...args], { stdio: 'inherit' });
    process.exit(result.status ?? 1);
  }
  if (!['start', 'stop', 'url', 'list', 'rename', 'cdp'].includes(command)) throw new Error(usage);
  if (command !== 'list') instanceName(name);
  else if (name) throw new Error(usage);
  const body = command === 'start' ? { name } : {};
  if (command === 'rename') {
    if (args.length !== 1) throw new Error(usage);
    body.label = instanceLabel(args.shift());
  }
  for (let i = 0; i < args.length; i += 2) {
    const key = args[i];
    const value = args[i + 1];
    if (command !== 'start' || !value || !['--url', '--profile'].includes(key)) throw new Error(usage);
    if (key === '--url') body.url = startUrl(value);
    else if (value.startsWith('/')) body.profile = value;
    else throw new Error('--profile must be an absolute local path');
  }
  const path = command === 'start' ? '/api/browsers' : ['stop', 'rename', 'cdp'].includes(command)
    ? `/api/browsers/${name}/${command}` : '/api/browsers';
  const mutation = ['start', 'stop', 'rename', 'cdp'].includes(command);
  const data = await new Promise((resolve, reject) => {
    const req = request({ socketPath: join(process.env.BM_STATE_DIR || join(homedir(), '.local/state/browser-manager'), 'control.sock'),
      path, method: mutation ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json' }, timeout: 30000 }, res => {
      let result = '';
      res.on('data', chunk => { result += chunk; if (result.length > 1024 * 1024) res.destroy(new Error('Response too large')); });
      res.on('error', reject);
      res.on('end', () => {
        try { const value = JSON.parse(result); if (res.statusCode !== 200) throw new Error(value.error || 'Request failed'); resolve(value); }
        catch (error) { reject(error); }
      });
    });
    req.on('error', error => reject(new Error(error.code === 'ENOENT' || error.code === 'ECONNREFUSED'
      ? 'Browser manager is not running. Start browser-manager.service first.' : 'Could not reach browser manager')));
    req.on('timeout', () => req.destroy(new Error('Request timed out')));
    req.end(mutation ? JSON.stringify(body) : undefined);
  });
  if (command === 'list') {
    console.log(data.browsers.length ? data.browsers.map(b => `${b.name}\t${b.label ?? b.name}\t${b.state}\t${b.url}`).join('\n') : 'No browsers yet. Run browserctl start <name>.');
  } else if (command === 'url') {
    const browser = data.browsers.find(b => b.name === name);
    if (!browser) throw new Error('No browser registered with that name');
    console.log(browser.url);
  } else if (command === 'cdp') console.log(data.wsEndpoint);
  else if (command === 'rename') console.log(`${data.browser.label}: ${data.browser.url}`);
  else console.log(`${data.state}: ${data.url}`);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
