# Xtream Filter Proxy

A self-hosted proxy that sits between IPTV Smarters Pro (or any Xtream-Codes
client) and an upstream Xtream-Codes provider, filtering the catalog by
regex rules on title and audio language — without the client ever knowing
it isn't talking to the provider directly.

## How it works

- Point Smarters at this proxy's URL instead of the provider's, keeping the
  original username/password.
- List endpoints (`get_live_streams`, `get_vod_streams`, `get_series`, and
  the `*_categories` variants) are served **entirely from a local SQLite
  cache**, filtered in-process — no upstream request happens on these calls,
  so filtering stays fast (well under a second, even for 150k+ VOD entries).
- A background sync job periodically refreshes that cache from the
  provider's list endpoints and detects new/removed titles.
- A separate crawler worker fetches per-title audio-track metadata
  (`get_vod_info` / `get_series_info`, with an optional `ffprobe` fallback)
  so the language filter has something to match against.
- Stream playback (`/live/`, `/movie/`, `/series/`) is a 302 redirect to the
  real provider URL — no video traffic passes through the proxy.
- Login, `get_vod_info`, `get_series_info`, and any unrecognized action are
  passed through to upstream unmodified.

## Setup

```bash
cp config.example.yaml config.yaml
# edit config.yaml: upstream.base_url / username / password
docker compose up -d
```

Point IPTV Smarters at `http://<host>:8080` with the same username/password
as your provider. The Web UI is at the same address (`/`).

### Running without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

`ffprobe` is optional — install it on the host/image if you want the
fallback prober; the app detects its presence at runtime and simply skips
that step if it's missing.

## Configuration

All settings live in `config.yaml` (see `config.example.yaml` for the full
annotated reference) and are also editable from the Web UI's Settings page —
changes are written back to the same file and applied immediately for
filters; crawler timing parameters take effect on the worker's next cycle.

Key sections:
- `upstream` — provider URL/credentials, timeout, user-agent
- `crawler` — request pacing, connection-slot safety margin, sync interval,
  and log rotation (`log_max_age_days`, `log_max_rows` — the `crawl_log`
  table is trimmed after every sync so it doesn't grow unbounded over months
  of uptime)
- `crawl_schedule` — one or more day/time windows the crawler is allowed to
  run in (or `enabled: false` to run continuously)
- `title_filters` — per-kind (`live`/`vod`/`series`) include/exclude regex
  lists, matched against the title (optionally also the category name)
- `audio_filters` — per-kind (`vod`/`series` only) include/exclude regex
  lists matched against each probed audio track, plus `on_unknown`
  (`keep`/`drop`) for items that haven't been probed successfully yet

All regexes are case-sensitive — use an inline `(?i)` if you want
case-insensitive matching. Invalid regexes are rejected on save with an
error message.

## Web UI

- **Dashboard** — crawler status, per-kind probe progress, pause/resume,
  manual sync, reset-all-probes, recent log
- **Settings** — upstream, crawler tuning, all filter regex fields
- **Filter Preview** — try a title/audio regex combination against the
  live cache and see counts plus sample titles on both sides, without
  touching the saved config
- **Catalog** — searchable table of cached items with probe status and
  detected audio tracks; supports forcing a re-probe of a single item

After changing filters, refresh the playlist inside IPTV Smarters — it
caches the catalog locally and won't pick up changes until you do.

## Limits of the audio-language detection

This is the most fragile part of the system, by design of the ecosystem it
talks to:

- **Not all panels expose audio metadata via the API.** Many Xtream panels'
  `get_vod_info` / `get_series_info` responses omit `audio`/`streams`
  entirely — in that case the only way to learn the language is the
  `ffprobe` fallback, which opens a real connection to the stream and reads
  its header. If `ffprobe` is disabled (the default) or unavailable, such
  items fall back to `on_unknown` behavior (default: `keep`, so nothing is
  hidden by accident before it's been probed).
- **Language tags are free text set by the uploader**, e.g. `ger`, `deu`,
  `german`, `Deutsch`, or sometimes nothing at all — the regex approach
  exists specifically so you can adapt to whatever vocabulary your
  provider's metadata actually uses; there's no universal normalization.
- **Series are probed once per season**, using the first episode with usable
  audio info; the result is applied to the whole series. If a season's
  audio composition genuinely varies episode-to-episode, this can miss it.
- **ffprobe consumes a real provider connection** for the duration of the
  probe. On accounts with a low `max_connections` (some providers issue
  just one), the crawler's connection-slot check (`reserve_slots`) may
  correctly refuse to probe at all rather than risk kicking a live stream —
  this is intentional, not a bug. Raise `max_connections` on the provider
  side, or accept that such accounts will mostly rely on the API-provided
  metadata (when present) instead of ffprobe.

## Tests

```bash
source .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest tests/ -v
```

`tests/test_audio_parser.py` covers the audio-track parser against several
realistic (and some deliberately malformed) `get_vod_info` response shapes,
since that parsing code is the most error-prone part of the project.
