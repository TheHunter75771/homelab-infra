// Shared UI primitives. Preact + htm (vendored ESM, no build step).

import { h, Fragment } from '../vendor/preact.mjs';
import { useState, useEffect, useRef } from '../vendor/hooks.mjs';
import htm from '../vendor/htm.mjs';
import { ICONS } from './icons.js';

export const html = htm.bind(h);
export { Fragment };

export const SPRITE = '/icons/phosphor.svg';

// Phosphor icon from the vendored sprite. Decorative unless `label` is given.
export function Icon({ name, size, spin = false, cls = '', label }) {
  if (!ICONS.has(name)) console.error(`unknown icon: ${name}`);
  const a11y = label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': 'true' };
  return html`<svg class=${`icon${spin ? ' spin' : ''}${cls ? ` ${cls}` : ''}`} width=${size} height=${size} focusable="false" ...${a11y}><use href=${`${SPRITE}#${name}`} /></svg>`;
}

// Status chip: color + icon + word, never color alone.
export function Chip({ tone = 'idle', icon, label, mono = false, spin = false, big = false, plain = false }) {
  return html`<span class=${`chip tone-${tone}${mono ? ' mono' : ''}${big ? ' big' : ''}${plain && !icon ? ' plain' : ''}`}>
    ${icon && html`<${Icon} name=${icon} spin=${spin} />`}${label}
  </span>`;
}

export function Dot({ tone, cls = 'dot', label }) {
  if (!tone) return null;
  return html`<span class=${`${cls} tone-${tone}`} role=${label ? 'img' : undefined} aria-label=${label}></span>`;
}

let reasonSeq = 0;
export function useId(prefix = 'r') {
  const ref = useRef(null);
  if (!ref.current) ref.current = `${prefix}${(reasonSeq += 1)}`;
  return ref.current;
}

// Visible reason under a disabled action (hover titles don't exist on phones).
export function Reason({ id, text }) {
  if (!text) return null;
  return html`<span class="reason" id=${id}><${Icon} name="lock-simple" />${text}</span>`;
}

// Button or link. `reason` disables it and swaps the icon for a lock when `lock` is set.
export function Btn({
  icon,
  label,
  onClick,
  primary = false,
  lg = false,
  danger = false,
  reason = null,
  lock = false,
  href,
  newTab = false,
  describedBy,
  cls = '',
  type = 'button',
  ariaLabel,
  busy = false,
  faded = false,
}) {
  const disabled = Boolean(reason) || busy;
  const classes = `btn${primary && !disabled ? ' primary' : ''}${lg ? ' lg' : ''}${danger ? ' danger' : ''}${faded ? ' faded' : ''}${cls ? ` ${cls}` : ''}`;
  const shownIcon = reason && lock ? 'lock-simple' : busy ? 'circle-notch' : icon;
  const inner = html`${shownIcon && html`<${Icon} name=${shownIcon} spin=${busy && !reason} />`}${label}`;
  if (href && !disabled) {
    return html`<a class=${classes} href=${href} target=${newTab ? '_blank' : undefined} rel=${newTab ? 'noopener noreferrer' : undefined} aria-label=${ariaLabel}>${inner}</a>`;
  }
  return html`<button type=${type} class=${classes} onClick=${disabled ? undefined : onClick} disabled=${disabled} aria-describedby=${describedBy} aria-label=${ariaLabel} title=${reason || undefined}>${inner}</button>`;
}

export function Bar({ pct, tone, cls = '' }) {
  const w = Math.max(0, Math.min(100, Number(pct) || 0));
  return html`<span class=${`bar${tone === 'warn' ? ' warn' : ''}${cls ? ` ${cls}` : ''}`} role="presentation"><span style=${{ width: `${w}%` }}></span></span>`;
}

export const toneVar = (tone) => (tone ? `tone-${tone}` : '');

// Ticks every `ms` while mounted; returns Date.now().
export function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

export function useMedia(query) {
  const get = () => Boolean(globalThis.matchMedia?.(query)?.matches);
  const [match, setMatch] = useState(get);
  useEffect(() => {
    const mq = globalThis.matchMedia?.(query);
    if (!mq) return undefined;
    const on = () => setMatch(mq.matches);
    on();
    mq.addEventListener?.('change', on);
    return () => mq.removeEventListener?.('change', on);
  }, [query]);
  return match;
}

export const DESKTOP_QUERY = '(min-width: 900px)';

// Link to a hash route.
export function hrefFor(route) {
  if (!route || route.name === 'overview') return '#/';
  if (route.name === 'app') return `#/apps/${encodeURIComponent(route.id || '')}`;
  return `#/${route.name}`;
}
