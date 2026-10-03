# bambu-downloader — MakerWorld backup

> **⚠️ Vibe-coded, personal-use project.** This was built largely by chatting
> with an AI coding assistant, to scratch one itch: keeping local copies of the
> MakerWorld models I care about before they get removed, hidden or censored.
> It runs on my own home server and does what I need. It is published as-is,
> **with no support, no roadmap and no promises** — issues and PRs may well go
> unanswered. Read the code before you trust it with your Bambu account.
>
> Forked from [sebasdoes/bambu-downloader](https://github.com/sebasdoes/bambu-downloader)
> and changed quite a bit since. Not affiliated with Bambu Lab / MakerWorld;
> use it with your own account and within their terms of service.

A self-hosted container with a web UI that:

1. **Signs in to MakerWorld** with your Bambu account (email + password, with
   email-code or TOTP 2FA — or paste an existing access token)
2. **Downloads models by URL** as `.3mf` files (type detected from content)
3. **Follows collections** (e.g. your Favorites) and syncs them on a schedule:
   new models are downloaded automatically, nothing is downloaded twice
4. **Gives up politely on dead models**: designs that were removed, made
   private or have no print profile (STL/CAD only) are skipped instead of being
   retried forever — and listed with the reason
5. **Library** with server-side search, filters and a download button per model;
   **Activity log** stored in SQLite with retention
6. **Home Assistant friendly**: per-collection stats endpoint plus an optional
   read-only API key
7. **PWA**: installable, and accepts shared MakerWorld links (Android share target)

## Quick start (compose)

```bash
docker compose up -d --build     # or: podman compose up -d --build
```

Then open **http://localhost:8008** (the compose file maps host port 8008 to
the container's 8080 — change `ports:` if you prefer another one), go to
**Settings**, sign in, and add a collection under **Collections**.

- **Rootless podman:** uncomment `userns_mode: keep-id` in `docker-compose.yml`
  so the bind-mounted `downloads/` and `data/` are writable.
- **Docker:** leave it commented and make the dirs writable for the
  container's uid 1000: `mkdir -p data downloads && sudo chown -R 1000:1000 data downloads`.
- Models land in `./downloads/<collection-id>-<title>/<design-id>-<title>/` as
  `<Model_title>__<Profile_name>.3mf` (the collection folder follows a rename
  on MakerWorld) next to a
  `cover.webp`; state (including your Bambu token) lives in `./data/`.
- Updating: `git pull && docker compose up -d --build`. Schema migrations run
  automatically on start, and the DB is copied to `data/backup/` first.

Plain `podman run` works too:

```bash
podman build -t bambu-downloader .
podman run -d --name bambu-downloader --userns=keep-id \
  -p 8008:8080 -v ./downloads:/app/downloads:Z -v ./data:/app/data:Z \
  bambu-downloader
```

### Troubleshooting: "attempt to write a readonly database"

The container runs as an unprivileged user (UID 1000 by default). With **rootless podman**, container UID 1000 does *not* map to your host UID — bind-mounted `./data` ends up owned by "nobody" from the container's perspective, and SQLite can't write. Two fixes:

```bash
# Option A (simplest): keep-id — your host uid appears inside the container
podman run -d --name bambu-downloader --userns=keep-id \
  -p 8008:8080 -v ./downloads:/app/downloads:Z -v ./data:/app/data:Z bambu-downloader
# (compose: uncomment userns_mode: keep-id in docker-compose.yml)

# Option B: chown the host dirs into your subuid range (container uid 1000)
podman unshare chown -R 1000:1000 ./data ./downloads
```

Note: `podman unshare chown` makes the dirs owned by your subuid range — `ls -l` on the host will look odd afterwards; that's expected.

## Using it

- **Collections tab:** follow a collection by URL, or pick one of your own
  from "Your MakerWorld collections" (refreshed every 60 min by default; the
  interval is editable there). Each followed collection shows its last and
  next sync; "Sync now" runs one immediately (a sync already in progress is
  never started twice).
- **Print profiles:** per followed collection choose what a sync downloads —
  the **default profile**, **the author's profiles**, or **all profiles**
  (including community ones; a popular model can have dozens). In the
  Library, "⚙ Profiles" on a model lists its profiles on MakerWorld (name,
  author, a *community* tag) and downloads extra ones next to the file you
  already have. The list is one request, reused for
  `BND_PROFILES_CACHE_MINUTES`.
- **Skipped models:** after `BND_MAX_DOWNLOAD_ATTEMPTS` failures caused by the
  model itself — or right away when it has no print profile — syncs stop
  trying it. The card lists the reason; "Retry on next sync" resets it.
- **Removed/hidden designs:** MakerWorld still counts them in a collection but
  no longer lists them, so they can't be downloaded; the UI shows them as
  "+ N removed or hidden on MakerWorld".
- **Signing out** keeps your files, library and followed collections; syncs
  pause until you sign in again.
- Unfollowing a collection asks whether to keep or delete its files.

## Security notes

- **Meant for a trusted LAN.** Don't expose it to the internet. On a shared
  network set `BND_API_KEY` (and enter it in the UI's Settings tab).
- `BND_READ_API_KEY` is a second, read-only key for dashboards: it opens GET
  endpoints only (status, stats, library, log, file downloads).
- Your Bambu Cloud **access token is stored in plaintext in SQLite**
  (`./data/`). Anyone who can read that volume can act as your MakerWorld
  account — protect it accordingly.
- The token and password are never logged; remote content is escaped in the
  UI; the app runs as an unprivileged user (uid 1000).

## Configuration

Environment variables (all optional — defaults shown):

| Variable | Default | Purpose |
|---|---|---|
| `BND_API_KEY` | *(unset)* | When set, every `/api/*` request must carry `X-API-Key: <value>` — protects the token from other LAN users |
| `BND_READ_API_KEY` | *(unset)* | Optional second key that only opens read-only (GET) endpoints — give this one to Home Assistant & co. Needs `BND_API_KEY` to be set |
| `BND_DOWNLOAD_DIR` | `/app/downloads` | Where models are saved |
| `BND_DATA_DIR` | `/app/data` | State dir (DB path derives from this unless overridden) |
| `BND_DB_PATH` | `/app/data/bambu_downloader.db` | SQLite state |
| `BND_PORT` | `8080` | Port the container healthcheck probes. The server itself always listens on 8080 inside the container — change the host side in `ports:` instead |
| `BND_SCHEDULER_INTERVAL_SECONDS` | `300` | How often the scheduler checks for due collections |
| `BND_SYNC_INTERVAL_MINUTES` | `360` | Default sync interval for newly added collections |
| `BND_MY_COLLECTIONS_REFRESH_MINUTES` | `60` | Default refresh interval of the "Your MakerWorld collections" list (min 15). Changeable in the UI (Collections tab), which then takes precedence |
| `BND_DOWNLOAD_DELAY_SECONDS` | `3` | Pause between downloads inside a sync (anti rate-limit) |
| `BND_EVENT_RETENTION_DAYS` | `30` | Activity-log entries older than this are pruned (hourly). `0` = keep forever |
| `BND_EVENT_MAX_ROWS` | `5000` | …and only the newest N are kept (~1 MB of SQLite). `0` = no cap |
| `BND_MAX_DOWNLOAD_ATTEMPTS` | `3` | Syncs skip a model after this many failures caused by the model itself (404 / private / no download URL); a design with **no print profile** (STL/CAD only) is skipped after the first failure. Network, CAPTCHA and sign-in problems don't count. Skipped models are listed in Collections with a Retry button. `0` = retry forever |
| `BND_PROFILES_CACHE_MINUTES` | `15` | How long Library → Profiles reuses a model's profile list before asking MakerWorld again. `0` = always ask |

## Home Assistant

`GET /api/collections/stats` lists every followed collection;
`GET /api/collections/<id>/stats` returns one as a flat object:

```json
{"collection_id": 12345, "title": "Favorites", "enabled": true, "syncing": false,
 "total": 273, "present": 268, "skipped": 3, "missing": 2,
 "last_sync_at": "2026-09-25T21:30:00+00:00", "last_sync_status": "ok", "last_sync_new": 4}
```

`total`/`present`/`missing` are as of the collection's last sync (`null` before
the first one). With `BND_API_KEY` set, give HA the read-only `BND_READ_API_KEY`
— it can read everything but can't sign in, download or unfollow.

```yaml
# configuration.yaml — one REST sensor per collection
rest:
  - resource: http://<host>:8008/api/collections/12345/stats
    headers:
      X-API-Key: !secret bambu_downloader_read_key   # omit without BND_API_KEY
    scan_interval: 300
    sensor:
      - name: "MakerWorld Favorites"
        unique_id: bambu_downloader_12345
        value_template: "{{ value_json.present }}"
        unit_of_measurement: models
        json_attributes: [total, missing, skipped, syncing, last_sync_at, last_sync_status]
```

## How it works (reverse-engineered endpoints)

The app talks to the same backend MakerWorld's web UI uses:

- `api.bambulab.com` for login + download URLs (not behind Cloudflare challenge)
- `makerworld.com/api/v1/design-service/...` for public metadata and collection listings
- Authenticated calls use the Bambu Cloud `access_token` as a Bearer token

## CI: prebuilt container image

A GitHub Actions workflow (`.github/workflows/container-image.yml`) builds the image on every push and publishes it to **GitHub Container Registry** — no secrets to configure, it uses the built-in `GITHUB_TOKEN`:

- Push to `main` → `ghcr.io/<owner>/<repo>:latest` (+ `:main`, `:sha`)
- Tag `v1.2.3` → `:1.2.3` (+ `:1.2`)
- Pull requests → build-only (validates the Dockerfile, no push)
- Multi-arch: `linux/amd64` and `linux/arm64` (works on a Pi/NAS)

Pull the prebuilt image instead of building locally:

```bash
podman pull ghcr.io/<owner>/bambu_downloader:latest
```

The package inherits the repo's visibility: **public repo → public image** (anyone can pull, no login). **Private repo → private image** — pull with `podman login ghcr.io` using a PAT with `read:packages`. If the first workflow run on a private repo fails to push the package, check that "Workflow permissions" in repo Settings → Actions is set to "Read and write permissions", or re-run the workflow after the package is created.

## Local development

Requirements: **Python 3.13** (what the image and CI use) and git. Docker or
podman only if you want to test the container.

```bash
git clone https://github.com/majkel17/bambu-downloader.git
cd bambu-downloader
python3 --version            # needs 3.13
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

Run the app with state in the repo (the defaults point at `/app/...`, which
only exist in the container; `downloads/`, `data/` and `.venv/` are git-ignored):

```bash
BND_DOWNLOAD_DIR=./downloads BND_DATA_DIR=./data \
  .venv/bin/uvicorn app.main:app --reload --port 8080
```

Open http://localhost:8080. It talks to the real MakerWorld once you sign in,
so keep `BND_DOWNLOAD_DELAY_SECONDS` sane while experimenting.

Checks (the same ones CI runs on every push/PR):

```bash
.venv/bin/pytest                     # offline test suite, no network needed
.venv/bin/ruff check app tests
.venv/bin/ruff format --check app tests   # drop --check to auto-format
```

Things worth knowing before changing code:

- **Layout:** `app/makerworld.py` (API client), `app/downloader.py` (downloads,
  syncs, activity log), `app/scheduler.py` (background loop), `app/db.py`
  (SQLite schema + migrations), `app/routes.py` (REST API), `app/static/`
  (vanilla-JS UI, no build step). Deeper notes live in [`docs/`](docs/README.md).
- **Schema changes** go into `DB_SCHEMA` for new databases *and* the ALTER
  list in `Database.__init__` for existing ones (both are idempotent).
- **UI changes:** bump `CACHE_VERSION` in `app/static/sw.js` (and the version
  pinned in `tests/test_thumb.py`), otherwise installed PWAs keep the old UI.
  The page runs under a strict CSP: no inline handlers — wire events in `app.js`.
- **Tests:** the `app_client` fixture reloads the app modules, so in tests
  refer to exception classes through the module (`app.downloader.NotFoundError`)
  at call time rather than importing them at the top of the file.

## License

[MIT](LICENSE) — do what you like with it. The one condition is keeping the
copyright notice: if you build on this, credit
**[majkel17/bambu-downloader](https://github.com/majkel17/bambu-downloader)**.
The original upstream code by [sebasdoes](https://github.com/sebasdoes/bambu-downloader)
was published without a license; this covers the changes made in this fork.
