# Role `dashboard_desktop`

Desktop app for the media server's Dashboard v2, applied to the workstation by
`playbooks/gaming-pc.yml` (no become, local connection). `files/homelab-dashboard-desktop` is a
small WebKitGTK window on `https://{{ tailnet_fqdn }}/` of the first `media_servers` host. It has
no browser chrome, its own dock entry and a launcher result named "Media Server".

It installs into the desktop user's home only. It installs no packages and never uses sudo:
- `~/.local/bin/homelab-dashboard-desktop` (0755)
- `~/.local/share/applications/homelab-dashboard.desktop`, checked with `desktop-file-validate` when that tool exists
- `~/.local/share/icons/hicolor/{512x512,scalable}/apps/homelab-dashboard.{png,svg}`, copied from `apps/dashboard/web/public/icons`
- `~/.config/homelab-dashboard/app.conf` (0600). The role writes only its `url` key.

It needs `python3-gobject` and `webkit2gtk4.1`. The role asserts that both import and names the
`dnf` command if they don't. At run time the app keeps its website data in
`~/.local/share/homelab-dashboard` and `~/.cache/homelab-dashboard`: the service worker, its
cache and cookies. That data is what lets the dashboard show its offline screen while the box is down.

## Decisions
- **`url` belongs to the role, everything else to the app.** `ini_file` sets only `url`. A new
  file starts with just that key, and an existing one keeps the size, position and zoom the app
  saved. If the tailnet name changes, the next run updates the app instead of leaving it pointed at
  a dead name. The app never writes `url`: on close it re-reads the file and updates only its
  window keys, so a run while the window is open sticks. A URL given on the command line is used
  once and never saved.
- **Navigation.** Only the dashboard origin (https, that host, port 443) loads in the window,
  including same-origin `target=_blank` and `window.open`. Other http(s) links go to the default
  browser (the admin apps on :8443/:8444, GitHub runbooks, anything else), but only after a real
  user gesture. WebKit labels a script's `a.click()` or a dispatched `MouseEvent` LINK_CLICKED
  too, so the navigation type is not trusted. A foreign navigation without a gesture (a script, a
  redirect, an iframe) is dropped, so a page can't open browser tabs. Every other scheme is
  dropped and never handed to `xdg-open`.
  - WebKit itself blocks `file:` from an https page.
  - `javascript:` is never a navigation in WebKit; it runs as page script. The dashboard's CSP
    (`script-src 'self'`) blocks it.
  - A clicked external link may get a WebKit preconnect (TCP/TLS, no request) before the policy
    hands it to the browser.
- **Permissions are all denied,** and so is `navigator.permissions.query()`. That includes
  notifications: WebKitGTK has no Web Push (`PushManager` is undefined), so the dashboard reports
  push as unsupported here, and the alerts reach the phone instead.
- **Downloads go to the browser only from the dashboard origin:** non-displayable responses,
  `Content-Disposition: attachment` and `<a download>`. WebKit reports no gesture for a download,
  so one from anywhere else is dropped.
- **Private data.** The app creates its config, website-data and cache directories 0700, and
  tightens an existing one of its own that group or others can reach.
- **TLS** errors fail the load with an error page and are never bypassed. The web process runs
  sandboxed (bubblewrap).
- **Caches are refreshed, never created.** The handlers run `gtk-update-icon-cache` and
  `update-desktop-database` only when the user cache already exists. A user `icon-theme.cache`
  that nothing keeps current hides icons added later, and Steam writes game icons into this same
  hicolor tree without refreshing it. Without a cache, GTK and the shell scan the directories.
- **One window.** A second launch exits with "already running", because two instances would share
  one website-data directory. The launcher sets `SingleMainWindow=true`.
- Wayland neither reports nor sets window positions, so the app remembers position on X11
  sessions only. Size, maximized state and zoom are remembered everywhere.

## Verify
- `homelab-dashboard-desktop --selftest` prints `ok <title>` and exits 0, or prints
  `fail <reason>` and exits 1.
  - It needs a desktop session (`DISPLAY` or `WAYLAND_DISPLAY`) but never shows a window.
  - It uses throwaway website data, so a cached shell can't pass for a live server.
  - Its output never names the host.
- Tests (no display needed): `python3 -m unittest discover -s roles/dashboard_desktop/tests -v`.
- A `GLib-GIO-WARNING ... Error releasing name ... Sandboxed.WebProcess` line on stderr at exit
  is the sandboxed web process shutting down. It is harmless.
