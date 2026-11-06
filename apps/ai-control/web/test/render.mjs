// Test helper: loads the app's modules (from public/, or from AICTL_WEB_PUBLIC for the mutation check),
// builds app states from fixtures, and renders every tab plus the shell into the DOM shim.

import path from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { installDom } from './dom-shim.mjs';
import * as fx from './fixtures.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const PUBLIC = process.env.AICTL_WEB_PUBLIC ? path.resolve(process.env.AICTL_WEB_PUBLIC) : path.resolve(HERE, '..', 'public');

export async function loadModules() {
  const imp = (p) => import(pathToFileURL(path.join(PUBLIC, 'js', p)).href);
  const [state, shell, overview, routing, models, audit, logs] = await Promise.all([
    imp('state.js'),
    imp('shell.js'),
    imp('tabs/overview.js'),
    imp('tabs/routing.js'),
    imp('tabs/models.js'),
    imp('tabs/audit.js'),
    imp('tabs/logs.js'),
  ]);
  return { state, shell, tabs: { overview: overview.createOverview, routing: routing.createRouting, models: models.createModels, audit: audit.createAudit, logs: logs.createLogs } };
}

/**
 * A full app state: the summary plus every side request loaded.
 * `data` overrides: { history, requests, logSources, logSourcesError, logs, sudo, sessions, localai, user, conn,
 *   replay: {session, data, output} }
 */
export function buildState(mods, summary, data = {}) {
  const { reduce, initialState, auditKey } = mods.state;
  let s = initialState();
  if (summary !== undefined) s = reduce(s, { type: 'summary/loaded', summary, at: fx.NOW });
  s = reduce(s, { type: 'history/loaded', points: data.history ?? fx.history(), at: fx.NOW });
  s = reduce(s, { type: 'requests/loaded', items: data.requests ?? fx.requests(), at: fx.NOW });
  if (data.logSourcesError) s = reduce(s, { type: 'logs/sources/failed', error: data.logSourcesError });
  else s = reduce(s, { type: 'logs/sources/loaded', data: data.logSources ?? fx.logSources() });
  if (s.logs.source) s = reduce(s, { type: 'logs/loaded', source: s.logs.source, lines: s.logs.lines, data: data.logs ?? fx.logs(), at: fx.NOW });
  if (data.user) s = reduce(s, { type: 'audit/options', user: data.user });
  for (const [part, items] of [
    ['sudo', data.sudo ?? fx.sudo()],
    ['sessions', data.sessions ?? fx.sessionsReply()],
    ['localai', data.localai ?? fx.localai()],
  ])
    s = reduce(s, { type: 'audit/loaded', part, key: auditKey(s, part), data: items, at: fx.NOW });
  if (data.replay) {
    s = reduce(s, { type: 'replay/open', session: data.replay.session });
    if (data.replay.output) s = reduce(s, { type: 'replay/output' });
    s = reduce(s, { type: 'replay/loaded', session: data.replay.session, data: data.replay.data });
  }
  if (data.conn) s = reduce(s, { type: 'conn', mode: data.conn });
  return s;
}

/** Renders the shell and all five tabs for `state`; returns { name: rootElement }. */
export function renderAll(mods, state) {
  const doc = installDom();
  const out = {};
  const shell = mods.shell.createShell({ onTab() {} });
  shell.update(state, fx.NOW);
  const shellRoot = doc.createElement('div');
  shellRoot.append(shell.header, shell.banner, shell.main, shell.nav);
  out.shell = shellRoot;
  for (const [name, create] of Object.entries(mods.tabs)) {
    const tab = create({});
    tab.update({ ...state, tab: name }, fx.NOW);
    out[name] = tab.el;
  }
  return out;
}

export { fx };
