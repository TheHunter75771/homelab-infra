// The watched containers (CORRECTIONS.md §1): what they are for, where to open them, and whether the gate's
// drop-out watcher stops them on `media-lost` (§2). The helper's `ps` is the source of truth for what runs;
// this catalog only adds labels and is the fallback list while Docker is held.
//
// `open`: {port, path} on the tailnet name (`tailscale serve` HTTPS), or null. ADR 0015: 443 serves only the
// dashboard; the path-based admin apps are on 8444 (their own origin), Jellyseerr on 8443 at /.

export const DEFAULT_APPS = Object.freeze({
  jellyfin: { label: 'Jellyfin', purpose: 'Streams the library to TVs, phones and the web', open: { port: 8444, path: '/watch' }, stack: 'media', mediaGated: true },
  jellyseerr: { label: 'Jellyseerr', purpose: 'Takes requests for new movies and shows', open: { port: 8443, path: '/' }, stack: 'media', mediaGated: true },
  sonarr: { label: 'Sonarr', purpose: 'Finds and grabs TV episodes', open: { port: 8444, path: '/sonarr' }, stack: 'media', mediaGated: true },
  radarr: { label: 'Radarr', purpose: 'Finds and grabs movies', open: { port: 8444, path: '/radarr' }, stack: 'media', mediaGated: true },
  prowlarr: { label: 'Prowlarr', purpose: 'Manages the indexers for Sonarr and Radarr', open: { port: 8444, path: '/prowlarr' }, stack: 'media', mediaGated: true },
  sabnzbd: { label: 'SABnzbd', purpose: 'Downloads from Usenet', open: { port: 8444, path: '/sabnzbd' }, stack: 'media', mediaGated: true },
  cloudflared: { label: 'cloudflared', purpose: 'Public tunnel for /watch', open: null, stack: 'edge', mediaGated: false },
  'wol-relay': { label: 'wol-relay', purpose: 'Wakes the gaming PC from the tailnet', open: null, stack: 'utils', mediaGated: false },
});

// Base URLs on the host (every admin UI is published on 127.0.0.1 only) and where their API keys live.
export const DEFAULT_INTEGRATIONS = Object.freeze({
  jellyfin: { baseUrl: 'http://127.0.0.1:8096/watch' },
  jellyseerr: { baseUrl: 'http://127.0.0.1:5055' },
  sonarr: { baseUrl: 'http://127.0.0.1:8989/sonarr', keyFile: 'sonarr/config.xml' },
  radarr: { baseUrl: 'http://127.0.0.1:7878/radarr', keyFile: 'radarr/config.xml' },
  prowlarr: { baseUrl: 'http://127.0.0.1:9696/prowlarr', keyFile: 'prowlarr/config.xml' },
  sabnzbd: { baseUrl: 'http://127.0.0.1:8080/sabnzbd', keyFile: 'sabnzbd/sabnzbd.ini' },
});

export const APP_NAME_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
