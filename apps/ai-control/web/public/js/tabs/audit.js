// Audit: sudo commands per host and user, ai-box's recorded sessions with replay, and localai's activity.
// Replay shows the command line first; the output is one tap away and is shown in a <pre> as plain text.

import { h, badge, mount } from '../dom.js';
import { clock, str, bytes } from '../format.js';
import { AUDIT_HOSTS, AUDIT_USERS, AUDIT_RANGES, REPLAY_CAP, validSessionId } from '../state.js';
import { card, sectionError, emptyNote, loadingNote, kv, picker, syncSelect, isObj } from '../ui.js';

function tzOf(state) {
  return state.summary && isObj(state.summary.server) ? state.summary.server.timeZone : undefined;
}

function listBody(part, what, render) {
  if (part.error && !part.items) return sectionError(what, part.error);
  if (!part.items) return loadingNote(`Loading ${what.toLowerCase()}…`);
  if (!part.items.length) return emptyNote(`No ${what.toLowerCase()} in this period.`);
  return render(part.items.filter(isObj));
}

// Sudo rows carry `event`: "command" (ran), "refused" (sudo said no), "auth_fail" (wrong password). Anything
// but "command" stands out with an icon + word badge, and the list opens with a count.
const EVENTS = {
  refused: { key: 'down', word: 'Refused' },
  auth_fail: { key: 'crit', word: 'Wrong password' },
};

export function eventBadge(event) {
  if (event === 'command' || event === undefined || event === null) return null;
  const e = Object.prototype.hasOwnProperty.call(EVENTS, event) ? EVENTS[event] : { key: 'warn', word: 'Unusual entry' };
  return badge(e.key, e.word);
}

export const AMBIGUOUS_HELP = "This line's details can't be trusted on their own. The recorded session is authoritative.";

/** `ambiguous` counts only when it is exactly true (a non-bool is false). */
const isAmbiguous = (c) => c.ambiguous === true;

/** "3 refused, 1 wrong-password attempt, 1 needs checking in this period." or null when nothing stands out. */
export function sudoSummary(items) {
  let refused = 0;
  let auth = 0;
  let other = 0;
  let check = 0;
  for (const c of items) {
    if (c.event === 'refused') refused += 1;
    else if (c.event === 'auth_fail') auth += 1;
    else if (c.event !== 'command' && c.event !== undefined && c.event !== null) other += 1;
    if (isAmbiguous(c)) check += 1;
  }
  if (!refused && !auth && !other && !check) return null;
  const parts = [];
  if (refused) parts.push(`${refused} refused`);
  if (auth) parts.push(`${auth} wrong-password ${auth === 1 ? 'attempt' : 'attempts'}`);
  if (other) parts.push(`${other} unusual ${other === 1 ? 'entry' : 'entries'}`);
  if (check) parts.push(`${check} ${check === 1 ? 'needs' : 'need'} checking`);
  return `${parts.join(', ')} in this period.`;
}

function sudoList(items, tz, now) {
  const summary = sudoSummary(items);
  return [
    summary
      ? h('p', { class: 'audit-flags', role: 'status' }, badge(items.some((c) => c.event === 'auth_fail') ? 'crit' : 'warn', 'Attention'), ' ', summary)
      : h('p', { class: 'note' }, `All ${items.length} ${items.length === 1 ? 'entry was an allowed command' : 'entries were allowed commands'}.`),
    h(
      'ul',
      { class: 'cmd-list' },
      items.map((c) => {
        const flag = eventBadge(c.event);
        const check = isAmbiguous(c);
        return h(
          'li',
          { class: ['cmd', (flag || check) && 'cmd-flagged'] },
          flag || check ? h('div', { class: 'cmd-flag' }, flag, check ? badge('unknown', 'Check session recording') : null) : null,
          check ? h('p', { class: 'cmd-help' }, AMBIGUOUS_HELP) : null,
          h('p', { class: 'cmd-line mono' }, str(c.command, '(no command recorded)')),
          h('p', { class: 'cmd-meta' }, `${clock(c.ts, { timeZone: tz, now })} · as ${str(c.runas, 'root')}`, c.cwd ? h('span', null, ' · in ', h('span', { class: 'mono' }, str(c.cwd))) : null),
        );
      }),
    ),
  ];
}

function sessionList(items, tz, now, onOpen) {
  return h(
    'ul',
    { class: 'cmd-list' },
    items.map((sess) => {
      const ok = validSessionId(sess.id);
      return h(
        'li',
        { class: 'cmd session' },
        h('p', { class: 'cmd-line mono' }, str(sess.command, '(no command recorded)')),
        h('p', { class: 'cmd-meta' }, `${clock(sess.ts, { timeZone: tz, now })} · as ${str(sess.runas, 'root')}`, sess.cwd ? h('span', null, ' · in ', h('span', { class: 'mono' }, str(sess.cwd))) : null),
        ok
          ? h('button', { type: 'button', class: 'btn', onclick: () => onOpen(sess) }, 'Replay')
          : h('p', { class: 'note' }, badge('warn', 'Can\'t replay'), ' This session has an id AI Control won\'t send back to the server.'),
      );
    }),
  );
}

export function replayPanel(replay, tz, now, actions) {
  const sess = replay.session || {};
  const data = replay.data || {};
  const output = typeof data.output === 'string' ? data.output : null;
  const outBytes = output !== null ? new TextEncoder().encode(output).length : 0;
  const cut = data.output_truncated === true || data.truncated === true || outBytes >= REPLAY_CAP;
  const parts = [
    kv([
      ['Started', clock(sess.ts, { timeZone: tz, now })],
      ['Ran as', str(data.runas ?? sess.runas, 'root')],
      ['Folder', h('span', { class: 'mono' }, str(data.cwd ?? sess.cwd))],
      ['Terminal', str(data.tty ?? sess.tty)],
    ]),
    h('p', { class: 'sub' }, 'Command'),
    h('pre', { class: 'replay-cmd' }, str(data.command ?? sess.command, '(no command recorded)')),
  ];
  if (replay.error) parts.push(sectionError('The replay', replay.error));
  if (replay.output && output !== null && !replay.loading) {
    parts.push(
      h('p', { class: 'sub' }, `Output (${bytes(outBytes)})`),
      h('p', { class: ['cap-note', cut && 'cut'], role: 'note' }, cut ? 'Output was cut off. The full recording stays on ai-box.' : `Replays are capped at ${bytes(REPLAY_CAP)}; this one is complete.`),
      data.output_damaged === true ? h('p', { class: 'note' }, badge('warn', 'Damaged'), ' Part of this recording couldn\'t be read, so some output may be missing.') : null,
      h('pre', { class: 'replay-out', id: 'replay-output', tabindex: '0', 'aria-label': 'Session output' }, output || '(no output)'),
    );
  } else if (replay.output && replay.loading) {
    // The same button (same id), marked busy, so focus stays put until the output arrives.
    parts.push(h('button', { type: 'button', class: 'btn', id: 'replay-show-output', 'aria-disabled': 'true', 'aria-busy': 'true' }, 'Loading the output…'));
  } else if (replay.output && output === null && !replay.error) {
    parts.push(emptyNote('The server sent no output for this session.'));
  } else if (!replay.output && !(replay.error && !replay.data)) {
    // No "Show output" when the session itself couldn't be read (e.g. 422 "this session log is damaged").
    parts.push(h('button', { type: 'button', class: 'btn', id: 'replay-show-output', onclick: actions.replayOutput }, 'Show output'));
  }
  parts.push(h('button', { type: 'button', class: 'btn btn-quiet', id: 'replay-close', onclick: actions.closeReplay }, 'Back to the list'));
  return card('Session replay', parts, { id: 'au-replay', cls: 'replay', focusHead: true });
}

function focusById(root, id) {
  const stack = [root];
  while (stack.length) {
    const n = stack.pop();
    if (n.nodeType === 1 && n.getAttribute('id') === id) {
      n.focus();
      return true;
    }
    for (const c of n.childNodes) stack.push(c);
  }
  return false;
}

export function createAudit(actions = {}) {
  let lastReplay = null;
  const noop = () => {};
  const act = { auditOptions: noop, openReplay: noop, replayOutput: noop, closeReplay: noop, refreshAudit: noop, ...actions };
  const host = picker('Machine', AUDIT_HOSTS, 'aibox', (v) => act.auditOptions({ host: v }), 'audit-host');
  const user = picker('Account', AUDIT_USERS.map((u) => ({ id: u, label: u })), 'claude', (v) => act.auditOptions({ user: v }), 'audit-user');
  const range = picker('Period', AUDIT_RANGES, AUDIT_RANGES[0].id, (v) => act.auditOptions({ range: v }), 'audit-range');
  const refresh = h('button', { type: 'button', class: 'btn', id: 'audit-refresh', onclick: () => act.refreshAudit() }, 'Refresh');
  const controls = h('div', { class: 'controls', role: 'group', 'aria-label': 'Audit filters' }, host.el, user.el, range.el, refresh);
  const data = h('div', { class: 'tab-data' });
  const el = h('div', { class: 'tab-body' }, controls, data);
  return {
    el,
    update(state, now = Date.now()) {
      const a = state.audit;
      syncSelect(host.select, a.host);
      syncSelect(user.select, a.user);
      syncSelect(range.select, a.range);
      const tz = tzOf(state);
      const prev = lastReplay;
      lastReplay = a.replay;
      if (a.replay) {
        mount(data, replayPanel(a.replay, tz, now, act));
        // Focus follows the action: Replay → the replay heading; Show output → the output once it's there
        // (or the heading if the output couldn't be shown).
        // A failed load (e.g. 422, a damaged session log) → the heading, so the error right under it is read next.
        const outputArrived = a.replay.output && !a.replay.loading && (!prev || !prev.output || prev.loading);
        const failed = a.replay.error && !a.replay.loading && (!prev || !prev.error || prev.loading);
        if (!prev) focusById(data, 'au-replay-h');
        else if (failed) focusById(data, 'au-replay-h');
        else if (outputArrived) focusById(data, 'replay-output') || focusById(data, 'au-replay-h');
        return;
      }
      const hostLabel = (AUDIT_HOSTS.find((x) => x.id === a.host) || AUDIT_HOSTS[0]).label;
      const cards = [card(`Sudo commands by ${a.user} on ${hostLabel}`, listBody(a.sudo, 'Commands', (items) => sudoList(items, tz, now)), { id: 'au-sudo' })];
      if (a.host === 'aibox') {
        const skipped = a.sessions.skipped > 0
          ? h('p', { class: 'note', role: 'status' }, badge('warn', 'Skipped'), ` ${a.sessions.skipped} session ${a.sessions.skipped === 1 ? "log couldn't be read and was" : "logs couldn't be read and were"} skipped.`)
          : null;
        cards.push(card(`Recorded sessions (${a.user})`, [skipped, listBody(a.sessions, 'Sessions', (items) => sessionList(items, tz, now, act.openReplay))], { id: 'au-sess' }));
        if (a.user === 'localai') {
          cards.push(
            card(
              'Other activity by localai',
              listBody(a.localai, 'Activity', (items) =>
                h(
                  'ul',
                  { class: 'cmd-list' },
                  items.map((e) => h('li', { class: 'cmd' }, h('p', { class: 'cmd-line' }, str(e.message, '')), h('p', { class: 'cmd-meta' }, `${clock(e.ts, { timeZone: tz, now })} · ${str(e.unit, str(e.comm))}`))),
                ),
              ),
              { id: 'au-local' },
            ),
          );
        }
      } else {
        cards.push(h('p', { class: 'note' }, 'Recorded sessions (replay) are kept on ai-box only. The gaming PC shows its sudo commands from the journal.'));
      }
      mount(data, cards);
    },
  };
}
