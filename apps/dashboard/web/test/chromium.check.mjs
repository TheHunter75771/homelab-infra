// Opt-in real-browser smoke (`npm run test:browser`): headless Chromium loads every route from the fake API,
// which sends the server's CSP, and fails on any console error, uncaught exception or CSP violation.
// Skips when no Chromium/Chrome binary is installed.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { createFakeApi } from './fake-api.mjs';

const BIN = ['chromium-browser', 'chromium', 'google-chrome-stable', 'google-chrome'].find((b) => {
  try {
    execFileSync('which', [b], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
});

function chrome(url, width, height) {
  return new Promise((resolve, reject) => {
    const args = ['--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars', `--window-size=${width},${height}`, '--virtual-time-budget=4000', '--enable-logging=stderr', '--v=0', '--dump-dom', url];
    const p = spawn(BIN, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    let err = '';
    p.stdout.on('data', (d) => (out += d));
    p.stderr.on('data', (d) => (err += d));
    const t = setTimeout(() => p.kill('SIGKILL'), 60000);
    p.on('close', () => {
      clearTimeout(t);
      const console = err.split('\n').filter((l) => /:CONSOLE[:(]/.test(l) || /Content Security Policy|Refused to/.test(l));
      resolve({ dom: out, console });
    });
    p.on('error', reject);
  });
}

let fake;
let base;
before(async () => {
  if (!BIN) return;
  fake = createFakeApi({ scenario: 'normal' });
  base = await fake.listen();
});
after(() => fake?.close());

const CASES = [
  ['normal', ['', 'apps', 'apps/radarr', 'drive', 'media', 'alerts', 'uptime', 'settings', 'more']],
  ['bad', ['', 'apps', 'drive', 'media', 'alerts']],
  ['clean', ['', 'drive', 'alerts']],
  ['sab', ['media']],
];

for (const [scenario, routes] of CASES) {
  for (const [w, h] of [
    [390, 844],
    [1440, 900],
  ]) {
    test(`chromium: ${scenario} @${w}`, { skip: !BIN && 'no Chromium found' }, async () => {
      fake.setScenario(scenario);
      for (const r of routes) {
        const { dom, console } = await chrome(`${base}/#/${r}`, w, h);
        assert.deepEqual(console, [], `${scenario} /#/${r} @${w}: console output`);
        assert.match(dom, /class="title"/, `${scenario} /#/${r} rendered`);
        assert.doesNotMatch(dom, /aria-busy="true"/, `${scenario} /#/${r} still loading`);
      }
    });
  }
}

// --- interactive checks over the DevTools protocol --------------------------------------------------------

async function devtools(width, height) {
  const fs = await import('node:fs');
  const os = await import('node:os');
  const path = await import('node:path');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-chrome-'));
  const port = 9300 + Math.floor(Math.random() * 500);
  const proc = spawn(BIN, ['--headless=new', '--no-sandbox', '--disable-gpu', `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, `--window-size=${width},${height}`, 'about:blank'], { stdio: 'ignore' });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let targets;
  for (let i = 0; i < 100 && !targets; i += 1) {
    try {
      targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
    } catch {
      await sleep(100);
    }
  }
  const ws = new WebSocket(targets.find((t) => t.type === 'page').webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener('open', r));
  let seq = 0;
  const waiting = new Map();
  const problems = [];
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data);
    if (m.id && waiting.has(m.id)) {
      waiting.get(m.id)(m);
      waiting.delete(m.id);
    }
    if (m.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(m.params.type)) problems.push(m.params.args.map((a) => a.value ?? a.description).join(' '));
    if (m.method === 'Runtime.exceptionThrown') problems.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  });
  const send = (method, params = {}) =>
    new Promise((r) => {
      seq += 1;
      waiting.set(seq, r);
      ws.send(JSON.stringify({ id: seq, method, params }));
    });
  const run = async (expr) => (await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true })).result?.result?.value;
  await send('Runtime.enable');
  await send('Page.enable');
  return {
    send,
    run,
    sleep,
    problems,
    close() {
      ws.close();
      proc.kill('SIGKILL');
      fs.rmSync(dir, { recursive: true, force: true });
    },
  };
}

test('chromium: the service worker controls the page, and a cold start with the server gone shows the offline screen', { skip: !BIN && 'no Chromium found' }, async () => {
  const own = createFakeApi({ scenario: 'bad' });
  const url = await own.listen();
  const b = await devtools(390, 844);
  try {
    await b.send('Page.navigate', { url: `${url}/#/` });
    await b.sleep(2500);
    const sw = await b.run(`(async () => { const r = await navigator.serviceWorker.getRegistration(); const name = (await caches.keys()).find((n) => n.startsWith('homelab-shell-')); return { scope: r && r.scope, script: r && r.active && r.active.scriptURL, shell: name ? (await (await caches.open(name)).keys()).length : 0 }; })()`);
    assert.equal(sw.scope, `${url}/`);
    assert.equal(sw.script, `${url}/sw.js`);
    assert.ok(sw.shell >= 40, 'shell precached');
    await b.send('Page.reload');
    await b.sleep(2000);
    assert.equal(await b.run('!!navigator.serviceWorker.controller'), true);
    await own.close(); // static files and API both gone
    await b.send('Page.reload');
    await b.sleep(3000);
    const off = await b.run(`({ h: document.querySelector('.offline h1')?.textContent, line: document.querySelector('.off-line')?.textContent, font: document.fonts.check('500 26px Inter') })`);
    assert.equal(off.h, "Can't reach the server");
    assert.match(off.line, /^Last known state at \d\d:\d\d: media drive failed after a power-cycle\. 8 apps held\.$/);
    assert.equal(off.font, true, 'Inter from the cache');
    assert.deepEqual(b.problems.filter((p) => !/Failed to load resource/.test(p)), []);
  } finally {
    b.close();
    await own.close().catch(() => {});
  }
});

test('chromium: dialogs trap Tab, close on Escape and give focus back; destructive ones start on Cancel', { skip: !BIN && 'no Chromium found' }, async () => {
  fake.setScenario('bad');
  const b = await devtools(1440, 900);
  try {
    await b.send('Page.navigate', { url: `${base}/#/drive` });
    await b.sleep(2000);
    const r = await b.run(`(async () => {
      const key = (k, shift) => document.dispatchEvent(new KeyboardEvent('keydown', { key: k, shiftKey: !!shift, bubbles: true }));
      const name = () => document.activeElement.textContent.trim();
      const opener = [...document.querySelectorAll('button')].find((x) => x.textContent.includes('Re-run check now'));
      opener.focus(); opener.click();
      await new Promise((r) => setTimeout(r, 300));
      const out = { initial: name() };
      key('Tab'); out.tab = name();
      key('Tab', true); out.shiftTab = name();
      const box = document.querySelector('[role=dialog]').getBoundingClientRect();
      out.width = Math.round(box.width);
      key('Escape');
      await new Promise((r) => setTimeout(r, 200));
      out.closed = !document.querySelector('[role=dialog]');
      out.back = name();
      location.hash = '#/alerts';
      await new Promise((r) => setTimeout(r, 500));
      document.querySelector('.dev button').click();
      await new Promise((r) => setTimeout(r, 300));
      out.destructive = name();
      return out;
    })()`);
    assert.deepEqual(r, { initial: 'Run check', tab: 'Cancel', shiftTab: 'Run check', width: 420, closed: true, back: 'Re-run check now', destructive: 'Cancel' });
    assert.deepEqual(b.problems, []);
  } finally {
    b.close();
  }
});

// --- review fixes -----------------------------------------------------------------------------------------

test('chromium: at 390 px every interactive element on every route is at least 44 × 44', { skip: !BIN && 'no Chromium found' }, async () => {
  const b = await devtools(390, 844);
  const small = [];
  try {
    await b.send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
    for (const scenario of ['normal', 'bad', 'clean', 'sab', 'troubled', 'real-one-app-down']) {
      fake.setScenario(scenario);
      for (const r of ['', 'apps', 'apps/radarr', 'drive', 'media', 'alerts', 'uptime', 'settings', 'more']) {
        await b.send('Page.navigate', { url: `${base}/#/${r}` });
        await b.sleep(900);
        const found = await b.run(`(() => [...document.querySelectorAll('a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])')]
          .map((el) => ({ el, r: el.getBoundingClientRect() }))
          .filter(({ el, r }) => r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden')
          .filter(({ r }) => r.width < 44 || r.height < 44)
          .map(({ el, r }) => el.tagName + ' "' + (el.textContent.trim() || el.getAttribute('aria-label') || el.className).slice(0, 30) + '" ' + Math.round(r.width) + 'x' + Math.round(r.height)))()`);
        for (const f of found) small.push(`${scenario} /#/${r}: ${f}`);
      }
    }
    assert.deepEqual(small, []);
    assert.deepEqual(b.problems, []);
  } finally {
    b.close();
  }
});

test('chromium: warning and failure dots differ in shape and say which they are', { skip: !BIN && 'no Chromium found' }, async () => {
  fake.setScenario('troubled'); // apps: a failure dot; drive: a warning dot
  const b = await devtools(1440, 900);
  try {
    await b.send('Page.navigate', { url: `${base}/#/` });
    await b.sleep(1200);
    const dots = await b.run(`[...document.querySelectorAll('.nav-item .dot')].map((d) => ({ nav: d.closest('a').querySelector('.nav-label').textContent, label: d.getAttribute('aria-label'), radius: getComputedStyle(d).borderRadius, transform: getComputedStyle(d).transform }))`);
    const apps = dots.find((d) => d.nav === 'Apps');
    const drive = dots.find((d) => d.nav === 'Drive & Gate');
    assert.equal(apps.label, 'failure');
    assert.equal(apps.radius, '50%');
    assert.equal(drive.label, 'warning');
    assert.notEqual(drive.transform, 'none', 'a warning is a diamond');
  } finally {
    b.close();
  }
});

for (const status of [502, 503, 504]) {
  test(`chromium: dashboard down behind the proxy (${status} for everything): the cached shell opens the offline screen`, { skip: !BIN && 'no Chromium found' }, async () => {
    const own = createFakeApi({ scenario: 'bad' });
    const url = await own.listen();
    const b = await devtools(390, 844);
    try {
      await b.send('Page.navigate', { url: `${url}/#/` });
      await b.sleep(2500);
      await b.send('Page.reload');
      await b.sleep(1500);
      assert.equal(await b.run('!!navigator.serviceWorker.controller'), true);
      own.setProxyError(status);
      await b.send('Page.reload');
      await b.sleep(2500);
      const off = await b.run(`({ h: document.querySelector('.offline h1')?.textContent, line: document.querySelector('.off-line')?.textContent, font: document.fonts.check('500 26px Inter'), sprite: !!document.querySelector('.off-ring use') })`);
      assert.equal(off.h, "Can't reach the server");
      assert.match(off.line, /^Last known state at \d\d:\d\d: media drive failed after a power-cycle\. 8 apps held\.$/);
      assert.equal(off.font, true);
      assert.deepEqual(b.problems.filter((p) => !/Failed to load resource|status of 50\d/.test(p)), []);
    } finally {
      b.close();
      await own.close();
    }
  });
}

test('chromium: focus stays in the dialog while the progress steps run', { skip: !BIN && 'no Chromium found' }, async () => {
  const own = createFakeApi({ scenario: 'normal', stepMs: 1500 });
  const url = await own.listen();
  const b = await devtools(1440, 900);
  try {
    await b.send('Page.navigate', { url: `${url}/#/apps/radarr` });
    await b.sleep(1500);
    const r = await b.run(`(async () => {
      const key = (k, shift) => document.dispatchEvent(new KeyboardEvent('keydown', { key: k, shiftKey: !!shift, bubbles: true }));
      [...document.querySelectorAll('button')].find((x) => x.textContent.includes('Update to')).click();
      await new Promise((r) => setTimeout(r, 300));
      [...document.querySelectorAll('[role=dialog] button')].find((x) => x.textContent.trim() === 'Update').click();
      await new Promise((r) => setTimeout(r, 400));
      const box = document.querySelector('[role=dialog]');
      const out = { progress: !!document.querySelector('.steps'), afterConfirm: document.activeElement === box };
      key('Tab'); out.tab = document.activeElement === box;
      key('Tab', true); out.shiftTab = document.activeElement === box;
      return out;
    })()`);
    assert.deepEqual(r, { progress: true, afterConfirm: true, tab: true, shiftTab: true });
  } finally {
    b.close();
    await own.close();
  }
});

test('chromium: a deploy swaps the whole shell at once, never mid-page or under an open dialog', { skip: !BIN && 'no Chromium found' }, async () => {
  const fs = await import('node:fs');
  const os = await import('node:os');
  const path = await import('node:path');
  const { stamp } = await import('../scripts/stamp-sw.mjs');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-deploy-'));
  fs.cpSync(new URL('../public', import.meta.url), dir, { recursive: true });
  const own = createFakeApi({ scenario: 'normal', publicDir: dir });
  const url = await own.listen();
  const b = await devtools(1440, 900);
  try {
    await b.send('Page.navigate', { url: `${url}/#/drive` });
    await b.sleep(2500);
    await b.send('Page.reload');
    await b.sleep(1500);
    const v1 = await b.run(`caches.keys().then((k) => k.filter((n) => n.startsWith('homelab-shell-')))`);
    assert.equal(v1.length, 1);
    // the page is open on Sonarr with a confirm dialog up (in-page route change, no navigation)
    await b.run(`location.hash = '#/apps/sonarr'`);
    await b.sleep(800);
    await b.run(`(() => { window.__page = 'v1-page'; [...document.querySelectorAll('button')].find((x) => x.textContent.trim() === 'Stop').click(); })()`);
    await b.sleep(300);
    assert.equal(await b.run(`!!document.querySelector('[role=dialog]')`), true);
    // deploy v2: a changed module, restamped worker; the browser finds it on its next update check
    fs.appendFileSync(path.join(dir, 'js/format.js'), "\nglobalThis.__shellBuild = 'v2';\n");
    stamp(dir);
    await b.run(`navigator.serviceWorker.getRegistration().then((r) => r.update())`);
    await b.sleep(3000);
    const waiting = await b.run(`(async () => ({ page: window.__page, dialog: !!document.querySelector('[role=dialog]'), waiting: !!(await navigator.serviceWorker.getRegistration()).waiting, build: globalThis.__shellBuild || 'v1' }))()`);
    assert.deepEqual(waiting, { page: 'v1-page', dialog: true, waiting: true, build: 'v1' }, 'the new version waits while a dialog is open');
    await b.run(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))`);
    await b.sleep(7000);
    const after = await b.run(`(async () => ({ page: window.__page || 'reloaded', build: globalThis.__shellBuild || 'v1', shells: (await caches.keys()).filter((n) => n.startsWith('homelab-shell-')), title: document.querySelector('.title')?.textContent }))()`);
    assert.equal(after.page, 'reloaded', 'the page reloaded once idle');
    assert.equal(after.build, 'v2', 'and runs the new shell');
    assert.equal(after.shells.length, 1, 'one shell version cached');
    assert.notDeepEqual(after.shells, v1, 'the old version is gone');
    assert.equal(after.title, 'Sonarr');
    assert.deepEqual(b.problems, []);
  } finally {
    b.close();
    await own.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
