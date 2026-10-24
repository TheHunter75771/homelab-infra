// Smoke test: the fake API serves public/ over HTTP; the real app modules boot in a linkedom DOM and every
// route renders, for every scenario, at phone and desktop width, without console errors. Then the
// designed states (loading, backend unreachable, offline from the saved snapshot) and the main actions.
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import { createFakeApi } from './fake-api.mjs';
import { openApp } from './browser.mjs';
import { createCacheStorage } from './helpers.mjs';

let fake;
let base;
before(async () => {
  fake = createFakeApi({ scenario: 'normal', stepMs: 30, logEveryMs: 50, tickMs: 200 });
  base = await fake.listen();
});
after(() => fake.close());

const ROUTES = [
  ['', 'Overview'],
  ['apps', 'Apps'],
  ['apps/radarr', 'Radarr'],
  ['drive', 'Drive & Gate'],
  ['media', 'Media'],
  ['alerts', 'Alerts'],
  ['uptime', 'Uptime & drills'],
  ['settings', 'Settings'],
  ['more', 'More'],
];

// What each scenario must show somewhere on a given route (spot checks of the designed states).
const EXPECT = {
  normal: {
    '': ['All systems normal', '8 apps healthy · drive 34%', '2.64 TB free', 'Nightly OK', '8 / 8 healthy', 'Box uptime'],
    apps: ['Update to 6.1.1.10360-ls303', 'Pinned version 6.1.1.10360-ls303 is newer than the running image'],
    drive: ['Next check', 'next boot', 'Full around', 'Gate history', 'powercycle', 'A re-run only acts when it has failed or been lost.'],
    media: ['Arrival (2016)', 'Direct play', '912 / 1000 API calls today', 'Requests · 2 pending', 'Usenet provider', 'Edit dates in Settings'],
    alerts: ['Media drive back', 'Resolved', '3 subscribed', 'Sends a push to 3 devices.'],
    uptime: ['99.95%', 'Monthly uptime per app', 'Media-drive failures per month', 'later'],
    settings: ['Disk warning at', 'Subscription renewal dates', 'API keys', 'Pinned versions', 'dashboard 2.0.0'],
  },
  bad: {
    '': ['1 issue', 'Media drive failed after a power-cycle. 8 apps held.', 'failed', '2.64 TB free*', 'Last known · drive offline', 'Reboot needed', '0 / 8 healthy', '8 held by the gate'],
    apps: ['Held by gate', 'Held by the boot gate until the media drive is back.'],
    'apps/radarr': ["Logs aren't available: held by the boot gate until the media drive is back."],
    drive: ['Next automatic retry', 'Re-run check now', 'Apps held', 'last known'],
    media: ['Jellyfin is held by the gate.', 'SABnzbd is held by the gate.', 'Missing: drive offline'],
    alerts: ['Media drive failed', 'Active'],
  },
  'media-lost': {
    '': ['Media drive lost at 13:48. 6 apps held.', '2 / 8 healthy', '6 held by the gate'],
    drive: ['media-lost', 'Next automatic retry'],
  },
  'docker-down': {
    '': ["Docker isn't running."],
    apps: ['Stopped', 'not running', "Docker isn't running."],
  },
  clean: {
    '': ['All systems normal', 'drive empty · first backup tonight', '3.98 TB free', 'No media yet', 'No backup yet', 'First nightly run at 03:30', 'Not pulled to the PC yet'],
    drive: ['The forecast starts after 7 days of samples. No media on the drive yet.', 'Not enough data yet'],
    media: ['Nothing playing right now.', 'Queue is empty.', 'Add the Jellyseerr API key in Settings.', 'Open Settings', 'None set up yet', 'No renewal dates yet.'],
    alerts: ['No alerts yet', 'Subscribe this phone to get alerts', '0 subscribed', 'Subscribe a device first.'],
  },
  sab: {
    '': ["SABnzbd's API isn't answering.", 'not answering'],
    media: ["SABnzbd isn't answering", 'Timed out after 5 s at 14:19. The container is running, so this may clear on its own.', 'Not answering', 'Rechecks automatically every 30 s.'],
  },
  troubled: {
    '': ['6 issues', 'Radarr is down.', 'Sonarr is unhealthy.', 'Media drive is 92% full.', 'Last run failed'],
    apps: ['Down', 'Unhealthy'],
  },
  rehearsal: {
    '': ['Rehearsal mode is on. Drill pushes go to test devices only.'],
    alerts: ['Rehearsal mode: only test devices receive drill pushes. This test goes to every device.'],
  },
  remote: {
    '': ['Remote'],
    apps: ['Update is only available on the tailnet.'],
    drive: ['Only available on the tailnet'],
  },
  // What the real server sends for its own scenarios (test/server-summaries).
  'real-normal': { '': ['1 issue', 'Usenet provider renews in 5 days.'], drive: ['Full around Jan 7, 2028'] },
  'real-bad-night': { '': ['Media drive failed after a power-cycle. 8 apps held.', 'Reboot needed'], apps: ['Held by gate'] },
  'real-media-lost': { '': ['Media drive lost at 14:02. 6 apps held.'], apps: ['Held by gate', 'Healthy'] },
  'real-one-app-down': { '': ['Radarr is down.'], media: ["SABnzbd isn't answering", 'Rechecks automatically every 30 s.'] },
  'real-docker-inactive': { '': ["Docker isn't running."], apps: ['Stopped', "Docker isn't running."] },
  'real-clean': { '': ['No backup yet'] },
};

async function renderRoute(scenario, route, title, width) {
  fake.setScenario(scenario);
  const b = await openApp({ base, hash: `#/${route}`, width });
  try {
    await b.until(() => b.$('.title')?.textContent === title && !b.$('[aria-busy="true"]'), `${scenario} ${route} ${width}`);
    const want = EXPECT[scenario]?.[route] || [];
    await b.until(() => want.every((w) => b.text().includes(w)), `${scenario}/${route}@${width}: ${want.filter((w) => !b.text().includes(w)).join(' | ')}`).catch((err) => {
      throw new Error(`${err.message}\n--- page text ---\n${b.text().slice(0, 1500)}`);
    });
    if (width >= 900) {
      assert.ok(b.$('.sidebar'), 'desktop: sidebar');
      assert.equal(b.$('.tabbar'), null);
    } else {
      assert.ok(b.$('.tabbar'), 'mobile: tab bar');
      assert.equal(b.$('.sidebar'), null);
    }
    assert.ok(b.$('.access'), 'access badge');
    // status is never colour alone: every chip carries an icon and a word
    for (const chip of b.$$('.chip')) {
      assert.ok(chip.querySelector('svg') || chip.textContent.trim(), 'chip has icon or word');
      assert.ok(chip.textContent.trim().length > 0, `chip has a word: ${chip.outerHTML.slice(0, 80)}`);
    }
    // every disabled action has a visible reason somewhere near it, or explains itself
    for (const btn of b.$$('button[disabled]')) assert.ok(btn.getAttribute('title'), `disabled button has a reason: ${btn.textContent}`);
    assert.deepEqual(b.errors, [], `${scenario}/${route}@${width}: console errors`);
  } finally {
    await b.close();
  }
}

for (const scenario of Object.keys(EXPECT)) {
  test(`every route renders: ${scenario}, phone (390)`, async () => {
    for (const [route, title] of ROUTES) await renderRoute(scenario, route, title, 390);
  });
}
for (const scenario of ['normal', 'bad', 'clean', 'sab', 'remote', 'real-normal', 'real-bad-night']) {
  test(`every route renders: ${scenario}, desktop (1440)`, async () => {
    for (const [route, title] of ROUTES) {
      if (route === 'more') continue; // phone-only screen; still reachable, checked on the phone
      await renderRoute(scenario, route, title, 1440);
    }
  });
}

test('mobile back buttons: app → Apps, Drive/Uptime/Settings → More; desktop has none for sidebar screens', async () => {
  fake.setScenario('normal');
  const b = await openApp({ base, hash: '#/drive', width: 390 });
  try {
    await b.until(() => b.$('.back'), 'back button');
    assert.equal(b.$('.back').getAttribute('href'), '#/more');
    await b.go('#/apps/sonarr');
    await b.until(() => b.$('.title')?.textContent === 'Sonarr');
    assert.equal(b.$('.back').getAttribute('href'), '#/apps');
    assert.equal(b.$('.tab[aria-current="page"]').textContent.trim(), 'Apps');
  } finally {
    await b.close();
  }
  const d = await openApp({ base, hash: '#/drive', width: 1440 });
  try {
    await d.until(() => d.$('.title')?.textContent === 'Drive & Gate');
    assert.equal(d.$('.back'), null);
    assert.equal(d.$('.nav-item[aria-current="page"]').textContent.trim(), 'Drive & Gate');
  } finally {
    await d.close();
  }
});

test('loading: skeletons and "Connecting" until the first summary', async () => {
  fake.setScenario('normal');
  fake.setHang(true);
  const b = await openApp({ base });
  try {
    await b.until(() => b.$('[aria-busy="true"]'), 'skeleton');
    assert.equal(b.$$('.skel.tile-sk').length, 5);
    assert.match(b.text('.meta'), /Connecting/);
    assert.deepEqual(b.errors, []);
  } finally {
    fake.setHang(false);
    await b.close();
  }
});

test('backend unreachable: stale banner, last-known data stays, actions paused with the reason', async () => {
  fake.setScenario('normal');
  const b = await openApp({ base, hash: '#/apps', width: 390 });
  try {
    await b.until(() => b.text().includes('Update to 6.1.1.10360-ls303'));
    fake.setDown(true);
    await b.app.live.refresh();
    await b.until(() => b.text().includes("Can't reach the dashboard backend."), 'stale banner');
    assert.match(b.text(), /Showing data from \d\d:\d\d\. Actions are paused\. Retrying every 10 s\./);
    assert.match(b.text('.meta'), /Stale\s*·\s*updated \d\d:\d\d/);
    assert.ok(b.text().includes('Update to 6.1.1.10360-ls303'), 'data still on screen');
    assert.ok(b.button('Restart').disabled);
    assert.ok(b.text().includes('Backend unreachable. Actions are paused.'));
    fake.setDown(false);
    await b.app.live.refresh();
    await b.until(() => !b.text().includes("Can't reach the dashboard backend."), 'recovers');
    assert.match(b.text('.meta'), /Live/);
    assert.deepEqual(b.errors, []);
  } finally {
    fake.setDown(false);
    await b.close();
  }
});

test('offline: a cold start without the server shows the snapshot saved on this device', async () => {
  const caches = createCacheStorage();
  fake.setScenario('bad');
  const first = await openApp({ base, caches });
  try {
    await first.until(() => first.text().includes('1 issue'));
    await first.until(async () => (await (await caches.open('homelab-data-v1')).match('/__snapshot')) !== undefined, 'snapshot saved');
  } finally {
    await first.close();
  }
  fake.setDown(true);
  const b = await openApp({ base, caches });
  try {
    await b.until(() => b.text().includes("Can't reach the server"), 'offline screen');
    assert.match(b.text(), /Last known state at \d\d:\d\d: media drive failed after a power-cycle\. 8 apps held\./);
    assert.match(b.text(), /Saved on this device · \d\d:\d\d/);
    const rows = b.$$('.off-row').map((r) => [...r.querySelectorAll('span')].map((x) => x.textContent.trim()));
    assert.deepEqual(rows.map((r) => r.slice(0, 2)), [
      ['Boot gate', 'failed'],
      ['Apps', '0 running · 8 held'],
      ['Last backup', rows[2][1]],
    ]);
    assert.ok(b.$$('.off-row svg').length === 3, 'each row has a status icon');
    assert.ok(b.text().includes('Alerts still arrive by push.'));
    assert.ok(b.text().includes("outside Docker, so the boot gate doesn't take it down"), 'ADR 0013 copy');
    fake.setDown(false);
    fake.setScenario('normal');
    b.click(b.button('Try again'));
    await b.until(() => b.text().includes('All systems normal'), 'back online after Try again');
    assert.deepEqual(b.errors, []);
  } finally {
    fake.setDown(false);
    await b.close();
  }
  fake.setDown(true);
  const empty = await openApp({ base, caches: createCacheStorage() });
  try {
    await empty.until(() => empty.text().includes('Nothing has been saved on this device yet'), 'offline without snapshot');
  } finally {
    fake.setDown(false);
    await empty.close();
  }
});

test('5xx from the proxy (dashboard down, box up): stale during a session, offline screen on a cold start', async () => {
  for (const status of [502, 503, 504]) {
    const caches = createCacheStorage();
    fake.setScenario('normal');
    const b = await openApp({ base, caches, width: 390 });
    try {
      await b.until(() => b.text().includes('All systems normal'));
      await b.until(async () => (await (await caches.open('homelab-data-v1')).match('/__snapshot')) !== undefined, 'snapshot saved');
      fake.setApiError(status);
      await b.app.live.refresh();
      await b.until(() => b.text().includes("Can't reach the dashboard backend."), `${status}: stale banner`);
      assert.ok(b.text().includes('All systems normal'), `${status}: the last data stays on screen`);
    } finally {
      await b.close();
    }
    const cold = await openApp({ base, caches, width: 390 });
    try {
      await cold.until(() => cold.text().includes("Can't reach the server"), `${status}: offline screen`);
      assert.match(cold.text(), /Last known state at \d\d:\d\d: all systems normal\./);
      assert.deepEqual(cold.errors, []);
    } finally {
      fake.setApiError(0);
      await cold.close();
    }
  }
});

test('Open links go to the app ports on this host (ADR 0015)', async () => {
  fake.setScenario('normal');
  const b = await openApp({ base, hash: '#/apps', width: 390 });
  try {
    await b.until(() => b.$$('.app-card').length === 8);
    const open = Object.fromEntries(b.$$('.app-card').map((c) => [c.querySelector('.app-name b').textContent, c.querySelector('a.btn[target="_blank"]')?.getAttribute('href') ?? null]));
    assert.deepEqual(open, {
      Jellyfin: 'http://127.0.0.1:8444/watch',
      Jellyseerr: 'http://127.0.0.1:8443/',
      Sonarr: 'http://127.0.0.1:8444/sonarr',
      Radarr: 'http://127.0.0.1:8444/radarr',
      Prowlarr: 'http://127.0.0.1:8444/prowlarr',
      SABnzbd: 'http://127.0.0.1:8444/sabnzbd',
      cloudflared: null,
      'wol-relay': null,
    });
    for (const a of b.$$('.app-card a.btn[target="_blank"]')) assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
  } finally {
    await b.close();
  }
});

test('restart: optimistic Starting chip, toasts, then healthy', async () => {
  fake.setScenario('normal');
  const b = await openApp({ base, hash: '#/apps', width: 1440 });
  try {
    await b.until(() => b.$$('.app-card').length === 8);
    const card = b.$$('.app-card').find((c) => c.textContent.includes('Sonarr'));
    b.click(b.button('Restart', card));
    await b.until(() => b.text('.toast-wrap').includes('Restarting Sonarr…'), 'first toast');
    await b.until(() => b.text('.toast-wrap').includes('Sonarr is healthy again'), 'done toast');
    assert.ok(fake.requests.includes('POST /api/apps/sonarr/restart'));
    assert.deepEqual(b.errors, []);
  } finally {
    await b.close();
  }
});

test('update: confirm dialog, three progress steps, success toast, the Update button goes away', async () => {
  fake.setScenario('normal');
  const b = await openApp({ base, hash: '#/apps/radarr', width: 390 });
  try {
    await b.until(() => b.button('Update to 6.1.1.10360-ls303'));
    b.click(b.button('Update to 6.1.1.10360-ls303'));
    await b.until(() => b.$('[role="dialog"]'), 'dialog');
    assert.equal(b.text('#dlg-title'), 'Update Radarr to 6.1.1.10360-ls303?');
    assert.equal(b.text('#dlg-body'), 'Pulls linuxserver/radarr:6.1.1.10360-ls303, recreates the container and waits for its health check. Radarr is down for about a minute.');
    b.click(b.button('Update', b.$('[role="dialog"]')));
    await b.until(() => b.$('.steps'), 'progress steps');
    assert.deepEqual(b.$$('.step').map((s) => s.textContent.replace(/\s*\((done|in progress|waiting)\)\s*$/, '').trim()), ['Pulling image', 'Recreating container', 'Waiting for health check']);
    await b.until(() => b.text('.toast-wrap').includes('Radarr updated to 6.1.1.10360-ls303 and healthy'), 'toast');
    await b.until(() => !b.button('Update to 6.1.1.10360-ls303'), 'no more update');
    assert.equal(b.$('[role="dialog"]'), null);
    assert.deepEqual(b.errors, []);
  } finally {
    await b.close();
  }
});

test('stop: confirm dialog (bottom sheet on the phone), then "Stopped on purpose" with Start', async () => {
  fake.setScenario('normal');
  const b = await openApp({ base, hash: '#/apps/sonarr', width: 390 });
  try {
    await b.until(() => b.button('Stop'));
    b.click(b.button('Stop'));
    await b.until(() => b.$('[role="dialog"]'));
    assert.equal(b.text('#dlg-title'), 'Stop Sonarr?');
    assert.match(b.text('#dlg-body'), /stays stopped until you start it again/);
    b.click(b.button('Stop', b.$('[role="dialog"]')));
    await b.until(() => b.text('.toast-wrap').includes('Sonarr stopped. It stays down until you start it.'));
    await b.until(() => b.text().includes('Stopped on purpose') && b.button('Start'), 'stopped state');
    b.click(b.button('Start'));
    await b.until(() => b.text('#dlg-title') === 'Start Sonarr?');
    b.click(b.button('Cancel'));
    await b.until(() => !b.$('[role="dialog"]'), 'cancel closes');
    assert.deepEqual(b.errors, []);
  } finally {
    await b.close();
  }
});

test('gate re-run (bad night): corrected copy, server steps, failure toast', async () => {
  fake.setScenario('bad');
  const b = await openApp({ base, hash: '#/drive', width: 1440 });
  try {
    await b.until(() => b.button('Re-run check now') && !b.button('Re-run check now').disabled);
    assert.match(b.text('.next-row'), /Next automatic retry\s*\d:\d\d/);
    b.click(b.button('Re-run check now'));
    await b.until(() => b.$('[role="dialog"]'));
    assert.equal(b.text('#dlg-title'), 'Re-run the media-drive check?');
    assert.equal(b.text('#dlg-body'), 'Re-checks the media drive now: detect, verify and mount at /media. Never reboots. Apps stay held until it passes.');
    b.click(b.button('Run check'));
    await b.until(() => b.$('.steps'));
    assert.deepEqual(b.$$('.step').map((s) => s.textContent.replace(/\s*\((done|in progress|waiting)\)\s*$/, '').trim()), ['Requested', 'Checking the drive (detect, verify, mount /media)']);
    await b.until(() => b.text('.toast-wrap').includes('Check failed: the drive is still not verified. Next automatic retry in 5 min.'));
    assert.deepEqual(b.errors, []);
  } finally {
    await b.close();
  }
});

test('log viewer: streamed lines with levels, filter, empty filter result, pause and follow', async () => {
  fake.setScenario('normal');
  const b = await openApp({ base, hash: '#/apps/radarr', width: 1440 });
  try {
    await b.until(() => b.$$('.log-line').length >= 12, 'tail');
    assert.ok(b.$('.lv-ERR') && b.$('.lv-WRN') && b.$('.lv-DBG'));
    await b.until(() => b.$$('.log-line').length > 13, 'follows new lines');
    assert.match(b.text('.log-foot'), /Following live/);
    b.type(b.$('.log-tools input'), 'err');
    await b.until(() => b.$$('.log-line').length === 1 && /1 lines match/.test(b.text('.log-foot')));
    b.type(b.$('.log-tools input'), 'zzz');
    await b.until(() => b.text('.log-body').includes('No lines match “zzz”.'));
    b.type(b.$('.log-tools input'), '');
    b.click(b.button('Pause'));
    await b.until(() => /Paused/.test(b.text('.log-foot')) && b.button('Follow'));
    const frozen = b.$$('.log-line').length;
    await new Promise((r) => setTimeout(r, 200));
    assert.equal(b.$$('.log-line').length, frozen, 'paused freezes the view');
    assert.match(b.text('.log-foot'), /\d+ new/);
    b.click(b.button('Follow'));
    await b.until(() => b.$$('.log-line').length > frozen);
    assert.deepEqual(b.errors, []);
  } finally {
    await b.close();
  }
});

test('alerts: remove a device (fail-coloured confirm), send a test push', async () => {
  fake.setScenario('normal');
  const b = await openApp({ base, hash: '#/alerts', width: 390 });
  try {
    await b.until(() => b.text().includes('3 subscribed'));
    b.click(b.$$('.dev')[0].querySelector('button'));
    await b.until(() => b.$('[role="dialog"]'));
    assert.equal(b.text('#dlg-title'), 'Remove iPhone?');
    assert.ok(b.$('.dialog.tone-fail'));
    b.click(b.button('Remove', b.$('[role="dialog"]')));
    await b.until(() => b.text('.toast-wrap').includes('Device removed. It will stop getting pushes.'));
    await b.until(() => b.text().includes('2 subscribed'));
    b.click(b.button('Send test push'));
    await b.until(() => b.text('.toast-wrap').includes('Test push sent to 2 devices'));
    assert.deepEqual(b.errors, []);
  } finally {
    await b.close();
  }
});

test('runbooks: external links when the summary has them, a plain list otherwise; no Retry on a failed media tile', async () => {
  fake.setScenario('normal');
  const b = await openApp({ base, hash: '#/settings', width: 1440 });
  try {
    await b.until(() => b.$$('section[aria-label="Runbooks"] a').length === 2, 'runbook links');
    for (const a of b.$$('section[aria-label="Runbooks"] a')) {
      assert.equal(a.getAttribute('target'), '_blank');
      assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
      assert.match(a.getAttribute('href'), /^https:\/\//);
    }
  } finally {
    await b.close();
  }
  fake.setScenario('clean');
  const c = await openApp({ base, hash: '#/settings', width: 1440 });
  try {
    await c.until(() => c.text().includes('In docs/runbooks/ of the homelab-infra repo.'), 'plain list');
    assert.equal(c.$$('section[aria-label="Runbooks"] a').length, 0);
  } finally {
    await c.close();
  }
  fake.setScenario('sab');
  const s = await openApp({ base, hash: '#/media', width: 390 });
  try {
    await s.until(() => s.text().includes('Rechecks automatically every 30 s.'));
    assert.equal(s.button('Retry'), undefined);
    assert.deepEqual(s.errors, []);
  } finally {
    await s.close();
  }
});

test('settings: thresholds and renewal dates save in the server\'s shape', async () => {
  fake.setScenario('normal');
  const b = await openApp({ base, hash: '#/settings', width: 1440 });
  try {
    // linkedom doesn't reflect input.type to the attribute, so select by structure
    await b.until(() => b.$('.field-row input') && b.text().includes('set · ends c0de'));
    b.change(b.$('.field-row input'), '85');
    await b.until(() => b.text('.toast-wrap').includes('Disk warning saved'));
    assert.equal(fake.settings.thresholds.diskWarnPct, 85);
    b.change(b.$('.field-row input'), '12');
    await new Promise((r) => setTimeout(r, 50));
    assert.equal(fake.settings.thresholds.diskWarnPct, 85, 'out of range is not sent');
    const form = b.$$('form.sub-form')[0];
    const [nameInput, dateInput] = form.querySelectorAll('input');
    b.type(nameInput, 'Indexer D');
    b.type(dateInput, '2027-01-15');
    await new Promise((r) => setTimeout(r, 20)); // a frame between typing and pressing Add, as for a person
    form.dispatchEvent(new b.window.Event('submit', { bubbles: true, cancelable: true }));
    await b.until(() => b.text('.toast-wrap').includes('Renewal added'));
    assert.ok(fake.settings.subscriptions.some((s) => s.name === 'Indexer D' && s.date === '2027-01-15'));
    await b.until(() => b.text().includes('Indexer D'));
    assert.deepEqual(b.errors, []);
  } finally {
    await b.close();
  }
});
