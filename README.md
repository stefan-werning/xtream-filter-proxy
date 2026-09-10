# Xtream Filter Proxy

A self-hosted proxy that sits between your IPTV player and an Xtream-Codes
provider, and hands the player a **filtered** catalog — filtered by regex
rules on the title, by audio language, and by category.

The player never knows it isn't talking to the provider directly.

Typical use: a provider bundles 150 000 movies in a dozen languages, and you
only ever watch the German ones. Instead of scrolling past everything else,
you point your player at this proxy and see only what matches your rules.

**Works with any Xtream-Codes client**, e.g. **Smarters Pro**, **TiviMate**,
or anything else offering an "Xtream Codes API" login type. Nothing
player-specific is required.

> **Note:** This project only filters and forwards what your own provider
> account already serves you. It contains no content, no provider list, and
> no way to access anything you aren't already paying for.

---

## Table of contents

- [How it works](#how-it-works)
- [Quick start (Docker)](#quick-start-docker)
- [Raspberry Pi setup](#raspberry-pi-setup)
- [Connecting your player](#connecting-your-player)
- [Configuration](#configuration)
- [Web UI](#web-ui)
- [How audio-language detection works (and its limits)](#how-audio-language-detection-works-and-its-limits)
- [Running without Docker](#running-without-docker)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Tests](#tests)

---

## How it works

- You point your player at this proxy's URL instead of the provider's.
- **List endpoints** (`get_live_streams`, `get_vod_streams`, `get_series`,
  and the `*_categories` variants) are served **entirely from a local SQLite
  cache** and filtered in-process. No upstream request happens on these
  calls, so browsing stays fast even with 150k+ VOD entries.
- A **background sync job** periodically refreshes that cache from the
  provider's list endpoints and detects new/removed titles.
- A **crawler worker** fetches per-title audio-track metadata
  (`get_vod_info` / `get_series_info`, with an optional `ffprobe` fallback)
  so the language filter has something to match against.
- **Stream playback** (`/live/`, `/movie/`, `/series/`) is a 302 redirect to
  the real provider URL — no video traffic passes through the proxy, so it
  adds no bandwidth cost and no transcoding load.
- **Login and unrecognized actions** are passed through to upstream
  unmodified, so player features you don't filter keep working.

```
   ┌────────────┐   Xtream API    ┌──────────────────┐   Xtream API   ┌──────────┐
   │  Player    │ ──────────────► │  Filter Proxy    │ ─────────────► │ Provider │
   │ (Smarters  │ ◄────────────── │  (this project)  │ ◄───────────── │          │
   │  TiviMate) │  filtered lists │  SQLite + rules  │   full lists   └──────────┘
   └────────────┘                 └──────────────────┘
          │                                                                 ▲
          └───────────── video streams (302 redirect, direct) ──────────────┘
```

---

## Quick start (Docker)

Requirements: Docker with the Compose plugin.

```bash
git clone https://github.com/stefan-werning/xtream-filter-proxy.git
cd xtream-filter-proxy

mkdir -p data
cp config.example.yaml data/config.yaml
# edit data/config.yaml: set upstream.base_url / username / password

docker compose up -d
```

Open `http://<host>:8080/` for the Web UI.

The container reads its config from `data/config.yaml` (the `data/`
directory is a bind mount, so the config and the SQLite database live on the
host and survive rebuilds). If that file doesn't exist on first start, the
bundled example is copied there automatically — but then you still have to
put your real credentials in and restart.

> `data/` and `config.yaml` are git-ignored, so your credentials never end
> up in a commit.

---

## Raspberry Pi setup

Runs comfortably on a **Pi 3, 4 or 5** with 64-bit Raspberry Pi OS
(Debian-based). A Pi 3 with 1 GB RAM is enough — the proxy itself is light;
only the initial crawl takes a while (see the note at the end).

**1. Install Docker** (skip if already present):

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker "$USER"
```

Log out and back in (or reboot) so the group change takes effect. Verify
with `docker ps` — it should work without `sudo`.

**2. Clone and configure:**

```bash
git clone https://github.com/stefan-werning/xtream-filter-proxy.git
cd xtream-filter-proxy

mkdir -p data
cp config.example.yaml data/config.yaml
nano data/config.yaml     # set upstream.base_url / username / password
```

**3. Build and start:**

```bash
docker compose up -d
```

The first build takes roughly **5–10 minutes on a Pi 3** (it installs
`ffmpeg` for the ffprobe fallback) and a couple of minutes on a Pi 4/5.
Later rebuilds are much faster thanks to Docker's layer cache.

**4. Verify:**

```bash
docker compose logs -f      # Ctrl-C to stop following
curl -s localhost:8080/api/crawler/status
```

Then open `http://<pi-ip>:8080/` from any device on your network.

### Pi notes

- **Autostart** is already handled: `restart: unless-stopped` in
  `docker-compose.yml` brings the container back after a reboot or crash.
- **Wired Ethernet is preferable** to Wi-Fi for a device that's syncing
  catalogs and (optionally) probing streams around the clock, but Wi-Fi
  works.
- **Use a decent power supply.** A Pi 3 wants a solid 5 V / 2.5 A supply;
  undervoltage shows up as random SD-card corruption, which is not fun with
  a database on it.
- **Shutdown is graceful by design.** `stop_grace_period: 140s` in
  `docker-compose.yml` matches uvicorn's `--timeout-graceful-shutdown 130`
  so `docker compose down` never kills the crawler mid-probe. That matters
  if your provider only allows one concurrent connection: a hard kill can
  leave that single slot stuck as "in use" upstream for a while.
- **The initial crawl is slow, on purpose.** With `max_connections: 1` the
  crawler probes one title at a time and waits for the connection slot;
  tens of thousands of titles can take days of wall-clock time. Use
  `crawl_schedule` to confine it to hours you don't watch TV, and use the
  Categories tab to exclude categories you don't care about — excluded
  categories are skipped entirely, which is by far the biggest speedup
  available.

---

## Connecting your player

In your player, add a playlist/profile of type **Xtream Codes API** (the
exact wording differs per app) with:

| Field | Value |
| --- | --- |
| Server / Portal URL | `http://<host>:8080` |
| Username | anything (see below) |
| Password | anything (see below) |

**About the credentials:** the proxy does not verify them. It always
authenticates upstream using the credentials from `config.yaml`. Any values
work — using your real provider credentials just keeps things recognizable.

⚠️ The flip side: **anyone who can reach the proxy's port can use it**, with
no password. Keep it on your LAN, or put authentication in front of it
(reverse proxy with basic auth, VPN, firewall rule) before exposing it to
the internet.

---

## Configuration

All settings live in `config.yaml`. See
[`config.example.yaml`](config.example.yaml) for the full annotated
reference.

Most settings are also editable from the Web UI's **Settings** page. The UI
writes changes back to the very same file — there is no second, hidden
configuration store. Filter changes apply immediately; crawler timing
parameters take effect on the worker's next cycle.

The listen address and port are **not** taken from `config.yaml` — they come
from how uvicorn is started (the `ports:` mapping in `docker-compose.yml`, or
the `--port` flag when running directly).

| Section | What it controls |
| --- | --- |
| `upstream` | Provider URL, credentials, timeout, user-agent |
| `database` | SQLite file path |
| `ffprobe` | Enable/disable the ffprobe fallback prober, its timeout and binary path |
| `crawler` | Request pacing, connection-slot safety margin (`reserve_slots`), sync interval, retention (`purge_after_days`) and log rotation (`log_max_age_days`, `log_max_rows`) |
| `crawl_schedule` | Day/time windows the crawler may run in (or `enabled: false` to run continuously) |
| `title_filters` | Per-kind (`live`/`vod`/`series`) include/exclude regex lists, matched against the title (optionally also the category name) |
| `category_filters` | Per-kind list of category IDs to hide entirely — easiest to manage from the Categories tab |
| `audio_filters` | Per-kind (`vod`/`series` only) include/exclude regex matched against each probed audio track, plus `on_unknown` |

### Filter semantics

- An item passes if it matches **at least one** `include` pattern (or
  `include` is empty) **and** matches **no** `exclude` pattern.
- `exclude` always wins over `include`.
- All regexes are **case-sensitive** — write `(?i)` inline if you want
  case-insensitive matching, e.g. `(?i)\bgerman\b`.
- Invalid regexes are rejected on save, with an error message.
- `audio_filters.on_unknown` decides what happens to titles the crawler
  hasn't successfully probed yet:
  - `keep` (default) — show them until proven otherwise. Nothing disappears
    by accident while the crawl is still running.
  - `drop` — show only titles with a confirmed matching audio track. Gives
    a clean list immediately, but hides a lot until the crawl catches up.

---

## Web UI

Reachable at `http://<host>:8080/`.

- **Dashboard** — crawler status, per-kind progress, ETA, pause/resume,
  manual sync, retry failed probes, reset all probes, recent log with
  explanations of each status term. Updates live over Server-Sent Events
  (`/api/events`) — status changes and new log lines appear as the crawler
  produces them, with no polling. Falls back to polling if the SSE
  connection can't be established.
- **Settings** — upstream, crawler tuning, crawl schedule, all filter regex
  fields.
- **Categories** — every category with its item count; uncheck one to hide
  it and stop the crawler wasting probes on it.
- **Filter Preview** — try a title/audio regex combination against the live
  cache and see counts plus sample titles from both sides, without touching
  the saved config.
- **Catalog** — searchable table of cached items with probe status and
  detected audio tracks. Supports forcing a re-probe of a single item, and
  "always show" overrides that bypass all filters for a specific title.
- **Delivered List** — exactly what your player receives right now for the
  selected type, after all filters. Useful for verifying that a filter did
  what you expected.

After changing filters, **refresh the playlist inside your player** — these
apps cache the catalog locally and won't pick up changes until you do.

---

## How audio-language detection works (and its limits)

This is the most fragile part of the system, by nature of the ecosystem it
talks to. Two sources are used, in this order:

1. **The provider API** (`get_vod_info` / `get_series_info`) — fast, no
   connection slot needed, but not always present or complete.
2. **`ffprobe` fallback** (optional, off by default) — opens a real
   connection to the stream and reads its header. Accurate, but slow and it
   occupies one of your account's connection slots.

Known limitations:

- **Not all panels expose audio metadata via the API.** Many omit
  `audio`/`streams` entirely. Without `ffprobe`, such items fall back to
  `on_unknown`.
- **Language tags are free text set by the uploader** — `ger`, `deu`,
  `german`, `Deutsch`, or nothing at all. The regex approach exists
  precisely so you can adapt to whatever vocabulary your provider uses;
  there is no universal normalization.
- **Series are probed once**, using the first episode with usable audio
  info; the result is applied to the whole series. If audio composition
  genuinely varies episode to episode, this can miss it.
- **ffprobe consumes a real provider connection** for the duration of the
  probe. On accounts with a low `max_connections` (some issue exactly one),
  the crawler's slot check (`reserve_slots`) may correctly refuse to probe
  rather than risk interrupting a live stream — that's intentional, not a
  bug.
- **Some API responses are incomplete rather than absent** — e.g. one audio
  track reported when the file actually has several. When the API result
  doesn't match your include rules, the crawler deliberately re-checks with
  ffprobe instead of trusting it (status `deferred` in the UI).

---

## Running without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# edit config.yaml

uvicorn app.main:app --host 0.0.0.0 --port 8080 --timeout-graceful-shutdown 130
```

Requires Python 3.11+. `ffprobe` (from FFmpeg) is optional — install it if
you want the fallback prober; the app detects its presence at runtime and
skips that step when it's missing.

Run a **single** uvicorn worker (the default). The crawler and the live
dashboard updates assume one process; with `--workers N > 1` you'd get N
crawlers fighting over the connection slot and the SSE stream would only
see events from whichever worker handled a given request.

Convenience scripts are included: `./start.sh`, `./stop.sh`, `./restart.sh`.
They run the app on port **8099** by default (override with `PORT=8080
./start.sh`) and write logs to `data/uvicorn.log`. `stop.sh` waits for a
running probe to finish rather than killing it, for the connection-slot
reason described above.

---

## Updating

```bash
cd xtream-filter-proxy
git pull
docker compose build
docker compose up -d
```

Your `data/` directory (config + database) is untouched by this.

---

## Troubleshooting

**The player still shows the unfiltered list.**
These apps cache aggressively. Refresh the playlist in the app; if that
doesn't help, delete the playlist entry and re-add it. Check the proxy log
(`docker compose logs`) — if you see only a bare `player_api.php` login and
no `get_vod_streams` call, the player is serving you its own cache and never
asked the proxy for the catalog.

**Everything is hidden / the list is nearly empty.**
Most likely `audio_filters.on_unknown: drop` combined with a crawl that
hasn't finished yet. Switch to `keep`, or check the Dashboard's "hidden by …"
breakdown to see which filter stage is removing items.

**The crawler says `outside window`.**
That's `crawl_schedule` doing its job — it's outside the configured hours.
Adjust the window in Settings, or set schedule to off.

**Probes keep failing with `error`.**
Usually a busy or unreachable provider. They're retried automatically with
backoff; "Retry failed probes" on the Dashboard requeues them all at once.

**Everything is slow on a Pi.**
Expected during the initial crawl. Exclude categories you don't need, and
confine crawling to off-hours via `crawl_schedule`.

---

## Tests

```bash
source .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest tests/ -v
```

`tests/test_audio_parser.py` covers the audio-track parser against several
realistic (and some deliberately malformed) `get_vod_info` response shapes,
since that parsing code is the most error-prone part of the project.
