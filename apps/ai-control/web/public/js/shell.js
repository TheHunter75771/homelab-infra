// The frame around the tabs: header (name, connection, who you are), the offline banner, and the tab bar.

import { h, icon, badge } from './dom.js';
import { duration, clock, str } from './format.js';
import { TABS, connText, dataAgeMs } from './state.js';
import { isObj } from './ui.js';

export const TAB_LABELS = { overview: 'Overview', routing: 'Routing', models: 'Models', audit: 'Audit', logs: 'Logs' };

/**
 * The banner text while the summary can't be refreshed, or null. Two cases: ai-box didn't answer at all
 * (network), or it answered with an HTTP error or a summary that can't be read (server).
 */
export function bannerText(state, now = Date.now()) {
  if (state.conn !== 'offline') return null;
  const f = state.failure || { kind: 'network', detail: null };
  const lead = f.kind === 'server' ? `ai-box answered with an error${f.detail ? ` (${f.detail})` : ''}.` : "Can't reach ai-box.";
  if (!state.summary) return `${lead} This device has no saved copy yet. It will load by itself when ai-box is back.`;
  const age = dataAgeMs(state, now);
  const tz = isObj(state.summary.server) ? state.summary.server.timeZone : undefined;
  const when = typeof state.summaryAt === 'number' ? ` (${clock(state.summaryAt, { timeZone: tz, now })})` : '';
  const ageText = age === null ? 'an unknown time' : age < 5000 ? 'a moment' : duration(age);
  return `${lead} Showing what it reported ${ageText} ago${when}. This updates by itself when ai-box is back.`;
}

export function createShell({ onTab } = {}) {
  const conn = h('span', { class: 'conn' });
  const who = h('span', { class: 'who' });
  const header = h('header', { class: 'top' }, h('h1', { class: 'brand' }, 'AI Control'), h('div', { class: 'top-right' }, conn, who));
  const bannerText_ = h('p');
  const banner = h('div', { class: 'banner', role: 'status', 'aria-live': 'polite', hidden: true }, icon('offline'), bannerText_);
  const tabButtons = {};
  const nav = h(
    'nav',
    { class: 'tabbar', 'aria-label': 'Sections' },
    TABS.map((t) => {
      const b = h('button', { type: 'button', class: 'tab', id: `tab-${t}`, onclick: () => onTab && onTab(t) }, icon(t), h('span', { class: 'tab-label' }, TAB_LABELS[t]));
      tabButtons[t] = b;
      return b;
    }),
  );
  const main = h('main', { id: 'main', class: 'main', tabindex: '-1' });
  const title = h('h2', { class: 'tab-title', id: 'tab-title' });
  main.appendChild(title);

  return {
    header,
    banner,
    nav,
    main,
    title,
    update(state, now = Date.now()) {
      const key = state.conn === 'live' || state.conn === 'polling' ? 'ok' : state.conn === 'offline' ? 'down' : 'unknown';
      conn.replaceChildren(badge(key, connText(state, now)));
      const id = state.summary && isObj(state.summary.server) ? state.summary.server.identity : null;
      const name = isObj(id) ? str(id.name, '') || str(id.login, '') : '';
      who.replaceChildren(...(name ? [icon('user'), h('span', { class: 'who-name' }, name)] : []));
      const msg = bannerText(state, now);
      banner.hidden = !msg;
      bannerText_.textContent = msg || '';
      for (const t of TABS) {
        if (t === state.tab) tabButtons[t].setAttribute('aria-current', 'page');
        else tabButtons[t].removeAttribute('aria-current');
      }
      title.textContent = TAB_LABELS[state.tab] || '';
    },
  };
}
