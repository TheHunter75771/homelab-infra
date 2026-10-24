// Hash routes. Every dashboard window stays on path "/", so the service worker's exact-path window
// reuse for push clicks finds it, and /sonarr, /radarr etc. (same origin) are never confused with it.

export const SCREENS = ['overview', 'apps', 'app', 'drive', 'media', 'alerts', 'uptime', 'settings', 'more'];

export function parseHash(hash) {
  const raw = String(hash || '').replace(/^#\/?/, '');
  const [head, ...rest] = raw.split('/').filter(Boolean);
  if (!head) return { name: 'overview' };
  if (head === 'apps' && rest[0]) {
    let id = rest[0];
    try {
      id = decodeURIComponent(id);
    } catch {
      // keep the raw segment
    }
    return { name: 'app', id };
  }
  if (SCREENS.includes(head) && head !== 'app') return { name: head };
  return { name: 'overview' };
}

export function toHash(route) {
  if (!route || route.name === 'overview') return '#/';
  if (route.name === 'app') return `#/apps/${encodeURIComponent(route.id || '')}`;
  return `#/${route.name}`;
}

// Where the header's back button goes. Drive, Uptime and Settings live under More on mobile.
export function parentOf(route, mobile) {
  if (route.name === 'app') return { name: 'apps' };
  if (mobile && ['drive', 'uptime', 'settings'].includes(route.name)) return { name: 'more' };
  return null;
}

export function titleOf(route, appLabel) {
  return (
    {
      overview: 'Overview',
      apps: 'Apps',
      app: appLabel || route.id || 'App',
      drive: 'Drive & Gate',
      media: 'Media',
      alerts: 'Alerts',
      uptime: 'Uptime & drills',
      settings: 'Settings',
      more: 'More',
    }[route.name] || 'Overview'
  );
}

export const sameRoute = (a, b) => a.name === b.name && (a.id || '') === (b.id || '');
