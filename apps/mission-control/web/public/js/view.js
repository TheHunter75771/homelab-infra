// The page: header, status banner, the eight cards and the notifications panel. createView(actions) builds
// the frame once and returns {el, update(state, now)}; update() re-draws the parts that hold data. Section
// elements and the AI card's question box are built once, so a refresh never loses what Noah typed, the
// card he jumped to, or the control he has focused.
//
// State (built by app.js; see initialState there):
//   cards: model.normalizeCards() result or null, cardsFail, cardsFrom ('live'|'snapshot'|null), cardsSavedAt
//   briefing: model.normalizeBriefing() result or null, briefingFail, briefingFrom, briefingSavedAt
//   loading: bool; push: {phase, blocked, isIOS, busy, message}; highlight: card id or null

import { h, mount, icon, badge, extLink, hashLink, radarMark } from './dom.js';
import { CARD_ORDER, CARD_META, STATUS_WORD, MAX_QUESTION, askUrl, ago, longDate, failText } from './model.js';

const cardId = (id) => `card-${id}`;

function updatedLine(ms, now) {
  return ms ? h('span', { class: 'updated' }, `Updated ${ago(ms, now)}`) : null;
}

function tapThrough(id, href) {
  const label = CARD_META[id].linkLabel;
  if (!label) return null;
  return extLink(href, { class: 'tap' }, h('span', null, label), icon('external'), h('span', { class: 'sr-only' }, ' (opens in a new window)'));
}

function itemList(items) {
  if (!items.length) return null;
  return h(
    'ul',
    { class: 'items' },
    items.map((it) =>
      h(
        'li',
        { class: ['item', it.status && `st-${it.status}`] },
        it.status ? h('span', { class: 'item-st' }, icon(it.status), h('span', { class: 'sr-only' }, `${STATUS_WORD[it.status]}: `)) : null,
        h('span', { class: 'item-label' }, it.label),
        it.value ? h('span', { class: 'item-value' }, it.value) : null,
      ),
    ),
  );
}

function note(kind, ...children) {
  return h('p', { class: ['note', `note-${kind}`] }, icon(kind === 'error' ? 'down' : kind === 'setup' ? 'setup' : 'unknown'), h('span', null, ...children));
}

/** The status and body of one server card (everything but the AI card's question box). */
export function cardBody(id, card, state, now) {
  const cardsLoaded = Boolean(state.cards);
  let status = card ? card.status : 'unknown';
  const parts = [];
  if (!cardsLoaded) {
    parts.push(
      state.cardsFail
        ? note('unknown', `Can't reach Mission Control's server (${failText(state.cardsFail)}). This card fills in when it answers.`)
        : note('unknown', 'Loading…'),
    );
  } else if (!card.present) {
    parts.push(note('unknown', "The server didn't report this card."));
  } else {
    if (card.headline) parts.push(h('p', { class: 'headline' }, card.headline));
    else if (id === 'home' && status === 'setup') parts.push(h('p', { class: 'headline' }, 'Connect Home Assistant'));
    if (status === 'setup' && id === 'home') parts.push(note('setup', 'Add a Home Assistant long-lived token on ai-box, and this card will show your entities.'));
    if (card.error) parts.push(note('error', `Couldn't read this source: `, card.error));
    parts.push(itemList(card.items));
    if (card.moreCount) parts.push(h('p', { class: 'more' }, `and ${card.moreCount} more`));
    if (id === 'alerts' && !card.error && !card.items.length && !card.headline) parts.push(h('p', { class: 'headline' }, 'No active alerts.'));
    if (!card.headline && !card.error && !card.items.length && id !== 'alerts') parts.push(note('unknown', 'Nothing reported.'));
  }
  const link = id === 'ai' ? (card?.link || state.cards?.config.openWebuiUrl) : card?.link;
  const foot = [card?.present ? updatedLine(card.updatedAt, now) : null, card?.present || id === 'ai' ? tapThrough(id, link) : null].filter(Boolean);
  return { status, parts, foot };
}

function briefingBody(state, now) {
  const b = state.briefing;
  const parts = [];
  let status = 'unknown';
  if (!b) {
    parts.push(state.briefingFail ? note('error', `Couldn't load today's briefing (${failText(state.briefingFail)}).`) : note('unknown', 'Loading…'));
    return { status: state.briefingFail ? 'down' : 'unknown', parts, foot: [] };
  }
  status = 'ok';
  const date = longDate(b.date);
  if (b.headline || date) parts.push(h('p', { class: 'brief-date' }, b.headline || date));
  if (b.summary) parts.push(h('p', { class: 'headline' }, b.summary));
  if (state.briefingFail) {
    status = 'warn';
    parts.push(note('error', `Couldn't refresh the briefing (${failText(state.briefingFail)}). This is the copy from ${ago(state.briefingSavedAt, now)}.`));
  }
  if (!b.sections.length && !b.summary) parts.push(note('unknown', 'Nothing in the briefing yet.'));
  for (const sec of b.sections) {
    parts.push(h('div', { class: 'brief-sec' }, h('h3', null, sec.title), sec.items.length ? itemList(sec.items) : h('p', { class: 'muted' }, sec.empty)));
  }
  return { status, parts, foot: [updatedLine(b.generatedAt, now)].filter(Boolean) };
}

// Open WebUI only PREFILLS the message box from ?q= (it never sends it), and applies ?models= only when that
// model is in Noah's list. So the wording says "open … ready to send", never "ask" or "send" as if done.
const HELP_ON = 'Opens your question in Open WebUI, ready to send.';

function askBox(actions) {
  const input = h('input', {
    id: 'ask-q',
    type: 'text',
    name: 'q',
    autocomplete: 'off',
    autocapitalize: 'sentences',
    enterkeyhint: 'go',
    maxlength: String(MAX_QUESTION),
    placeholder: 'Ask anything',
    'aria-describedby': 'ask-help',
  });
  const btn = h('button', { type: 'submit', class: 'btn btn-primary', id: 'ask-go' }, icon('external'), h('span', null, 'Open in AI'));
  const help = h('p', { id: 'ask-help', class: 'help' }, HELP_ON);
  const form = h(
    'form',
    {
      class: 'ask',
      role: 'search',
      'aria-label': 'Open a question in Open WebUI',
      novalidate: true,
      onsubmit: (e) => {
        e.preventDefault();
        if (btn.getAttribute('aria-disabled') === 'true') return;
        const url = askUrl(form.base, input.value);
        if (url) actions.openExternal?.(url);
      },
    },
    h('label', { for: 'ask-q', class: 'sr-only' }, 'Your question for the AI'),
    h('div', { class: 'ask-row' }, input, btn),
    help,
  );
  form.base = null;
  form.setBase = (base) => {
    form.base = base;
    const ok = Boolean(askUrl(base, ''));
    if (ok) btn.removeAttribute('aria-disabled');
    else btn.setAttribute('aria-disabled', 'true');
    help.textContent = ok
      ? HELP_ON
      : "Open in AI is off: the server hasn't sent a usable Open WebUI address.";
  };
  return form;
}

const iosSteps = () => [
  [icon('share'), 'In Safari, tap ', h('strong', null, 'Share'), ' (the square with an arrow).'],
  [icon('plusSquare'), 'Choose ', h('strong', null, 'Add to Home Screen'), ', then ', h('strong', null, 'Add'), '.'],
  [radarMark(), 'Open ', h('strong', null, 'Mission Control'), ' from your Home Screen, not from Safari.'],
  [icon('alert'), 'Come back to this panel and tap ', h('strong', null, 'Enable notifications'), '.'],
];

/** The notifications panel's contents for a push state. */
export function pushBody(push, hasKey, actions) {
  const p = push || { phase: 'checking' };
  const busy = Boolean(p.busy);
  const button = (id, cls, ic, label, onclick, disabled = false) =>
    h('button', { type: 'button', id, class: ['btn', cls], 'aria-disabled': busy || disabled ? 'true' : null, onclick: () => (busy || disabled ? undefined : onclick?.()) }, icon(ic), h('span', null, label));
  const out = [];
  if (p.phase === 'checking') out.push(h('p', null, 'Checking this device…'));
  else if (p.phase === 'blocked' && p.blocked === 'ios-browser') {
    out.push(
      h('p', null, 'On iPhone and iPad, notifications only work once Mission Control is on your Home Screen (iOS 16.4 or later).'),
      h('ol', { class: 'steps' }, iosSteps().map((step) => h('li', null, ...step))),
    );
  } else if (p.phase === 'blocked' && p.blocked === 'denied') {
    out.push(
      h('p', null, 'Notifications are blocked for Mission Control on this device.'),
      h(
        'ul',
        { class: 'plain' },
        h('li', null, h('strong', null, 'iPhone: '), 'Settings → Notifications → Mission Control → Allow Notifications.'),
        h('li', null, h('strong', null, 'Android or desktop: '), 'site settings (the icon left of the address) → Notifications → Allow, then reload.'),
      ),
    );
  } else if (p.phase === 'blocked' && p.blocked === 'insecure') {
    out.push(h('p', null, "Notifications need the secure address: open Mission Control through its https:// tailnet address."));
  } else if (p.phase === 'blocked' && p.blocked === 'worker') {
    out.push(h('p', null, "Mission Control's background worker didn't start, so notifications can't work yet. Reload the page to try again."));
  } else if (p.phase === 'blocked') {
    out.push(h('p', null, "This browser can't receive web push. On iPhone, use Safari and add Mission Control to the Home Screen."));
  } else if (p.phase === 'off') {
    out.push(h('p', null, 'Get the 05:30 briefing and alerts on this device.'));
    if (p.isIOS) out.push(h('p', { class: 'help' }, 'iPhone will ask once. Choose Allow.'));
    if (!hasKey) out.push(note('unknown', "The server hasn't sent its push key yet. Refresh, then try again."));
    out.push(h('div', { class: 'btn-row' }, button('push-on', 'btn-primary', 'alert', 'Enable notifications', actions.enablePush, !hasKey)));
  } else if (p.phase === 'on') {
    out.push(
      h('p', { class: 'on-line' }, badge('ok', 'On'), h('span', null, 'Notifications are on for this device.')),
      h('div', { class: 'btn-row' }, button('push-test', 'btn-primary', 'send', 'Send a test', actions.testPush), button('push-off', 'btn-quiet', 'bellOff', 'Turn off', actions.disablePush)),
    );
  }
  return out;
}

export function createView(actions = {}) {
  const dateEl = h('p', { class: 'top-date' });
  const refreshBtn = h('button', { type: 'button', class: 'icon-btn', id: 'refresh', 'aria-label': 'Refresh', onclick: () => actions.refresh?.() }, icon('refresh'));
  const header = h(
    'header',
    { class: 'top' },
    h('div', { class: 'brand' }, radarMark(), h('div', null, h('h1', null, 'Mission Control'), dateEl)),
    h('div', { class: 'top-actions' }, hashLink('#notify', { class: 'icon-btn', 'aria-label': 'Notifications settings' }, icon('alert')), refreshBtn),
  );
  const banner = h('div', { class: 'banner', role: 'status', 'aria-live': 'polite', hidden: true });

  const sections = {};
  const ask = askBox(actions);
  for (const id of CARD_ORDER) {
    const meta = CARD_META[id];
    const heading = h('h2', { id: `${cardId(id)}-h`, tabindex: '-1' }, meta.title);
    const st = h('span', { class: 'card-st' });
    const head = h('div', { class: 'card-head' }, icon(meta.icon, 'card-icon'), heading, st);
    const body = h('div', { class: 'card-body' });
    const foot = h('div', { class: 'card-foot' });
    const el = h('section', { class: ['card', `card-${id}`], id: cardId(id), 'aria-labelledby': `${cardId(id)}-h` }, head, id === 'ai' ? ask : null, body, foot);
    sections[id] = { el, heading, st, body, foot };
  }
  const grid = h('div', { class: 'grid', id: 'cards' }, CARD_ORDER.map((id) => sections[id].el));

  const pushPanel = h('div', { class: 'card-body' });
  const pushMsg = h('p', { class: 'push-msg', role: 'status', 'aria-live': 'polite' });
  const notifyHeading = h('h2', { id: 'notify-h', tabindex: '-1' }, 'Notifications on this device');
  const notify = h(
    'section',
    { class: 'card card-notify', id: 'notify', 'aria-labelledby': 'notify-h' },
    h('div', { class: 'card-head' }, icon('alert', 'card-icon'), notifyHeading),
    pushPanel,
    pushMsg,
  );
  const main = h('main', { class: 'main', id: 'main' }, grid, notify);
  const el = h('div', { class: 'frame' }, hashLink('#main', { class: 'skip' }, 'Skip to the cards'), header, banner, main);

  function update(state, now = Date.now()) {
    const focusedId = globalThis.document?.activeElement?.id || null;

    dateEl.textContent = longDate(state.briefing?.date) || longDate(new Date(now).toLocaleDateString('en-CA'));
    refreshBtn.setAttribute('aria-busy', state.loading ? 'true' : 'false');

    // banner: the server itself can't be read
    if (state.cardsFail) {
      banner.hidden = false;
      const why = state.cardsFail.kind === 'network' ? "Can't reach Mission Control" : `Mission Control answered with an error (${failText(state.cardsFail)})`;
      mount(banner, icon('offline'), h('span', null, state.cards && state.cardsFrom === 'snapshot' ? `${why}. Showing what it reported ${ago(state.cardsSavedAt, now)}.` : `${why}. Cards fill in when it's back.`));
    } else {
      banner.hidden = true;
      mount(banner);
    }

    for (const id of CARD_ORDER) {
      const sec = sections[id];
      const r = id === 'briefing' ? briefingBody(state, now) : cardBody(id, state.cards?.cards[id] ?? null, state, now);
      mount(sec.st, badge(r.status, STATUS_WORD[r.status]));
      mount(sec.body, ...r.parts);
      mount(sec.foot, ...r.foot);
      sec.foot.hidden = !r.foot.length;
      sec.el.classList.toggle('flash', state.highlight === id);
      sec.el.setAttribute('aria-busy', state.loading && !state.cards ? 'true' : 'false');
    }
    ask.setBase(state.cards?.config.openWebuiUrl ?? null);

    mount(pushPanel, ...pushBody(state.push, Boolean(state.cards?.config.vapidPublicKey), actions));
    pushPanel.setAttribute('aria-busy', state.push?.busy ? 'true' : 'false');
    const msg = state.push?.message;
    pushMsg.textContent = msg ? msg.text : '';
    pushMsg.className = ['push-msg', msg ? `msg-${msg.kind === 'error' ? 'error' : 'ok'}` : ''].join(' ').trim();

    if (focusedId && globalThis.document?.activeElement?.id !== focusedId) {
      const again = globalThis.document.getElementById(focusedId);
      if (again && typeof again.focus === 'function') again.focus();
    }
  }

  /** Scroll to a card and move focus to its heading (a notification tap, or #card-x in the address). */
  function focusCard(id) {
    const sec = id === 'notify' ? { el: notify, heading: notifyHeading } : sections[id];
    if (!sec) return;
    const still = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
    sec.el.scrollIntoView?.({ block: 'start', behavior: still ? 'auto' : 'smooth' });
    sec.heading.focus({ preventScroll: true });
  }

  return { el, update, focusCard, sections };
}
