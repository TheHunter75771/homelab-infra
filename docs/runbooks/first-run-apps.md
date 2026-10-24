# Runbook: first run of the media apps (clean start)

After the first successful `playbooks/site.yml`, every app is running with **empty** settings.
Nothing was restored. Role `stacks` wrote a few presets before the first start (URL bases, forms
login, SABnzbd folders and host check, the Jellyfin base URL and known proxy). This runbook covers
the rest, in order, about 30–45 minutes.

**Where:** a device on the tailnet. All admin URLs use the server's tailnet name over HTTPS
(`tailscale serve`). Below, `<ts>` is the value of `tailnet_fqdn` from `inventory/host_vars/`
(the full name, e.g. `<host>.<tailnet>.ts.net`). The short name and plain IPs will not work.

| App | URL |
|---|---|
| Jellyfin | `https://<ts>/watch` |
| SABnzbd | `https://<ts>/sabnzbd` |
| Sonarr | `https://<ts>/sonarr` |
| Radarr | `https://<ts>/radarr` |
| Prowlarr | `https://<ts>/prowlarr` |
| Jellyseerr | `https://<ts>:8443` |
| Push core (alerts) | `https://<ts>/` |

Inside Docker the apps reach each other by container name: `jellyfin:8096`, `sonarr:8989`,
`radarr:7878`, `prowlarr:9696`, `sabnzbd:8080`. Use those names (not `<ts>`) wherever one app asks
for another's address. The URL base still applies, e.g. `/sonarr`.

Paths inside the containers: the media drive is `/data` (`/data/tv`, `/data/movies`,
`/data/downloads/complete`). SABnzbd's scratch space is `/incomplete-downloads` (on the NVMe).

---

## 1. Jellyfin: `https://<ts>/watch`

1. Startup wizard → language → **create the admin user** with a strong password. Jellyfin is on the
   internet through the tunnel.
2. **Add Media Library** twice:
   - Content type **Shows**, folder **`/data/tv`**.
   - Content type **Movies**, folder **`/data/movies`**.
   Set metadata language/country, leave the rest at defaults.
3. **Remote access**: **Allow remote connections** = **on** (tunnel and tailnet clients are not
   "LAN" to Jellyfin). **Automatic port mapping** = **off**.
4. Finish the wizard, then sign in.
5. **Dashboard → Playback → Transcoding**:
   - Hardware acceleration **Intel QuickSync (QSV)**, QSV device **`/dev/dri/renderD128`**.
   - Hardware decoding: tick **H264, HEVC, HEVC 10bit, VP9** (the iGPU has no AV1 decode, so leave it off).
   - **Enable hardware encoding** on. Leave *low-power encoding* off. Save.
6. **Dashboard → Networking**: check **Base URL** = `/watch` and **Known proxies** = `172.30.10.0/24`
   (both preset; don't change them).
7. TVs on the home network add the server as **`http://<lan_ip>:8096/watch`** (`lan_ip` from host_vars).
   Auto-discovery is not available (the container is not on host networking).

## 2. SABnzbd: `https://<ts>/sabnzbd`

1. The quick-start wizard opens (no news server yet): language → enter your **Usenet server**
   (host, port 563, **SSL on**, username, password, connections up to your plan's limit) → **Test
   server** → Next.
2. Folders are preset. Confirm on the last page or in **Config → Folders**:
   **Temporary Download Folder** `/incomplete-downloads`, **Completed Download Folder**
   `/data/downloads/complete`.
3. **Config → General → Security**: set a **Username and Password** (required: the UI is reachable
   from the whole tailnet). Save, then copy the **API Key** from the same page for Sonarr/Radarr.
4. **Config → Categories**: make sure categories **`tv`** and **`movies`** exist, with
   **Folder/Path** `tv` and `movies` (relative, so they land in `/data/downloads/complete/tv|movies`).
   Save each row.
5. Leave **Config → Sorting** off. Sonarr/Radarr do the renaming.

> "Access denied - Hostname verification failed" means you used a name other than `<ts>`.
> Use the full tailnet name (this goes away once a username/password is set).
> In **rehearsal** SABnzbd starts **paused** on purpose (`start_paused`, kept in step with `rehearsal`
> by Ansible). Don't resume it during a drill.

## 3. Sonarr: `https://<ts>/sonarr`

1. First visit shows **Authentication Required** (forms login is preset) → choose a username and
   password → Save.
2. **Settings → Media Management** (click *Show Advanced*):
   - **Root Folders → Add Root Folder** → `/data/tv`.
   - **Use Hardlinks instead of Copy** = on (default). Downloads and the library share the drive, so
     imports are instant hardlinks.
   - **Unmonitor Deleted Episodes** = **on** (deleting a file won't trigger a re-download).
   - Rename Episodes: on if you want tidy names (optional).
3. **Settings → Download Clients → + → SABnzbd**: Host **`sabnzbd`**, Port **`8080`**, URL Base
   **`/sabnzbd`**, API Key (from SABnzbd), Username/Password (from SABnzbd), Category **`tv`**,
   SSL off → **Test** → Save. No remote path mapping is needed: both containers see the same `/data`.
4. **Settings → Profiles**: keep one quality profile you actually want (e.g. *HD-1080p*) and set its
   **Upgrade Until**. Delete or ignore the 4K ones unless the drive is sized for them.
5. **Settings → General → Security**: copy the **API Key** (for Prowlarr and Jellyseerr).

## 4. Radarr: `https://<ts>/radarr`

Same as Sonarr, with these differences:
- Root folder **`/data/movies`**; **Unmonitor Deleted Movies** = on.
- Download client SABnzbd with Category **`movies`** (Host `sabnzbd`, Port `8080`, URL Base `/sabnzbd`).
- Copy the API key from **Settings → General**.

## 5. Prowlarr: `https://<ts>/prowlarr`

1. **Authentication Required** → username/password → Save.
2. **Indexers → Add Indexer** → your Usenet indexer(s) (Newznab) → API key → **Test** → Save.
3. **Settings → Apps → + → Sonarr**:
   - Prowlarr Server **`http://prowlarr:9696/prowlarr`**
   - Sonarr Server **`http://sonarr:8989/sonarr`**
   - API Key: Sonarr's. Sync Level **Full Sync** → Test → Save.
4. **Settings → Apps → + → Radarr**: Prowlarr Server `http://prowlarr:9696/prowlarr`, Radarr Server
   **`http://radarr:7878/radarr`**, Radarr's API key → Test → Save.
5. **Indexers** → **Sync App Indexers**. In Sonarr/Radarr, **Settings → Indexers** now lists them
   (marked *(Prowlarr)*). Don't add indexers in Sonarr/Radarr by hand.

## 6. Monitoring on a clean library (read before adding anything)

The library is empty and nothing knows what you "already have". The defaults will happily search for
every episode of every show you add. Start narrow and widen on purpose:

- **Adding a series** (Sonarr → *Add New*):
  - **Monitor**: *Future Episodes* for shows you follow as they air. *Pilot Episode* or
    *First Season* to sample a show. Avoid *All Episodes* for long-running shows.
  - **Untick "Start search for missing episodes"** until you have run one successful test grab
    (section 8). Then use *Search Monitored* per series deliberately.
- **Adding a movie** (Radarr): Monitor *Movie Only*, Minimum Availability **Released**. Untick
  "Start search" until the test grab works.
- **No import lists and no bulk adds** for the first week. Watch disk use in **System → Status**
  (Sonarr/Radarr) and the free space on `/data`.
- **Deleting something in Jellyfin** removes the file. The *Unmonitor Deleted* settings keep the
  *arr apps from fetching it again.
- **Jellyseerr requests** (next section) search immediately for whatever is requested. Keep
  auto-approve off so requests wait for you.

## 7. Jellyseerr: `https://<ts>:8443`

1. Setup → **Use your Jellyfin account**:
   - Jellyfin URL: hostname **`jellyfin`**, port **`8096`**, SSL off, URL base **`/watch`**.
   - Email, then the Jellyfin **admin** username/password → Sign in.
2. **Jellyfin libraries** → Sync → enable the **Shows** and **Movies** libraries → Start scan → Continue.
3. **Services → Add Radarr Server**: Default ✓, name, Hostname **`radarr`**, Port **`7878`**, SSL
   off, API key, URL Base **`/radarr`** → **Test**, then pick the quality profile, root folder
   **`/data/movies`**, Minimum Availability *Released* → Add.
4. **Add Sonarr Server**: Default ✓, Hostname **`sonarr`**, Port **`8989`**, API key, URL Base
   **`/sonarr`** → Test → quality profile, root folder **`/data/tv`**, **Season Folders** ✓ → Add.
   Finish setup.
5. **Settings → General**: **Application URL** `https://<ts>:8443`. Optional: **Settings →
   Jellyfin → External URL** `https://<public_watch_host>/watch`, so "Play on Jellyfin" links work
   off the tailnet.
6. **Settings → Users → Default Permissions**: *Request* only, **no Auto-Approve** (see section 6).

## 8. Smoke test

1. Sonarr: add one small, recent show with *Pilot Episode*, then **Search** it.
2. SABnzbd shows it downloading into `/incomplete-downloads`, then completing into
   `/data/downloads/complete/tv`.
3. Sonarr imports it to `/data/tv/...`. On the server, the link count shows the hardlink:
   `stat -c '%h %n' /media/tv/<Show>/*/*` → `2`.
4. Jellyfin shows the episode after a library scan (*Dashboard → Libraries → Scan*). Play it from a
   phone off the tailnet via `https://<public_watch_host>/watch` and check *Dashboard → Activity*
   for "(HW)" transcoding when forcing a lower quality.
5. Sonarr/Radarr **System → Status** shows no health warnings (download client, root folder, indexers).

## 9. Phone alerts (push core): `https://<ts>/`

On each phone (on the tailnet): open the page, set a label, **Subscribe**, then **Send test**.
On iPhone, first use **Share → Add to Home Screen**, open it from the home screen, and then
subscribe (iOS only allows Web Push for home-screen apps). For rehearsal drills, subscribe one
device with the label **`test`**.
