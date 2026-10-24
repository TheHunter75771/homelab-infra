import { html, Icon, Dot } from '../ui.js';

export function More({ view }) {
  const items = [
    { href: '#/drive', icon: 'hard-drive', label: 'Drive & Gate', dot: view.dots.drive },
    { href: '#/uptime', icon: 'pulse', label: 'Uptime & drills', dot: null },
    { href: '#/settings', icon: 'gear-six', label: 'Settings & about', dot: null },
  ];
  return html`<nav class="more-list" aria-label="More">
    ${items.map(
      (n) => html`<a class="more-item" href=${n.href}>
        <${Icon} name=${n.icon} /><span class="nav-label">${n.label}</span>
        <${Dot} tone=${n.dot} label=${n.dot ? (n.dot === 'fail' ? 'has a failure' : 'needs attention') : undefined} />
        <${Icon} name="caret-right" cls="chev" />
      </a>`,
    )}
  </nav>`;
}
