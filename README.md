<div align="center">
  <h1>Riven TPDB</h1>
  <p>
    An <strong>adult-only</strong> fork of
    <a href="https://github.com/rivenmedia/riven">Riven</a> that runs entirely on
    <a href="https://theporndb.net/">ThePornDB (TPDB)</a> — no Whisparr.
  </p>
</div>

> **Status:** experimental work-in-progress. The fork compiles, and the TPDB
> metadata, content, and scraping layers are implemented and unit-tested against
> the live TPDB API. Recommendations and performer/tag catalogs are not yet
> implemented.

---

## What this is

Riven TPDB is [Riven](https://github.com/rivenmedia/riven) re-targeted for adult
content. Riven is a Plex torrent-streaming stack (metadata → scraping →
Debrid → symlinks); this fork replaces its mainstream metadata/content sources
with [ThePornDB](https://theporndb.net/) so the whole pipeline is keyed on TPDB
ids instead of IMDb/TMDB.

It does **not** depend on Whisparr. Metadata comes straight from TPDB's REST
API, the same database Whisparr and Stash use.

The core guarantee of the fork:

> **Adult titles only.** Mainstream movies and TV never enter the library —
> the indexer only resolves items that carry a `tpdb_id`, and every mainstream
> content provider (Trakt/Overseerr/Listrr/Mdblist/Plex Watchlist) has been
> removed.

## How it maps to Riven

| Riven concept            | Riven TPDB equivalent                                       |
| ------------------------ | ----------------------------------------------------------- |
| Movie / Show             | Adult **scene** or **movie** (both stored as a `Movie`)     |
| Metadata indexer         | `TpdbApi` → `services/indexers/tpdb_indexer.py`             |
| Content providers        | `services/content/tpdb_content.py` (TPDB site subscriptions)|
| Scrapers                 | Prowlarr + Jackett (adult trackers, searched by title)      |
| Media server             | Plex / Jellyfin / Emby (unchanged)                          |

## Status

- ✅ **Metadata** — TPDB scenes/movies index correctly (title, year, rating,
  site, performers, genres, poster, backgrounds).
- ✅ **Content** — subscribe to TPDB sites (studios/networks); new scenes are
  picked up and flow into the indexer.
- ✅ **Scraping** — Prowlarr/Jackett recognize adult (Newznab "XXX") categories
  and search adult trackers by scene title.
- ⏳ **Recommendations** — not yet implemented.
- ⏳ **Performer/tag catalogs** — not yet implemented (TPDB's public list
  endpoint only filters by `site`).

## Getting started

### 1. Get a TPDB API token

Create an account at <https://theporndb.net> and generate an API token.

### 2. Run with Docker Compose (recommended)

Copy `.env.example` to `.env`, then set `RIVEN_TPDB_API_TOKEN` (and optionally
`RIVEN_CONTENT_TPDB_SITES`):

```bash
cp .env.example .env
# edit .env -> RIVEN_TPDB_API_TOKEN=<your-tpdb-token>
docker compose up -d
```

This starts Riven + PostgreSQL and pulls
`ghcr.io/gauravsuman007/riven-tpdb:latest`. See
[`docker-compose.yml`](./docker-compose.yml).

You can also run a single container directly:

```bash
docker run -d \
  -e RIVEN_FORCE_ENV=true \
  -e RIVEN_TPDB_API_TOKEN="<your-tpdb-token>" \
  -e RIVEN_CONTENT_TPDB_ENABLED=true \
  -e 'RIVEN_CONTENT_TPDB_SITES=["<site-uuid-1>","<site-uuid-2>"]' \
  -v ./data:/riven/data \
  -p 8080:8080 \
  ghcr.io/gauravsuman007/riven-tpdb:latest
```

You can also configure everything from the Riven web UI (**Settings → TPDB**)
instead of environment variables.

### 3. Point Riven at adult trackers

- **Prowlarr**: add your adult indexers, then set `RIVEN_SCRAPING_PROWLARR_URL`
  and `RIVEN_SCRAPING_PROWLARR_API_KEY`.
- **Jackett**: same idea with `RIVEN_SCRAPING_JACKETT_URL` and
  `RIVEN_SCRAPING_JACKETT_API_KEY`.

The fork now recognizes Newznab adult categories (`XXX`/`Adult`, category 6000),
so adult trackers are searched correctly instead of being silently dropped.

## Environment variables

Settings are loaded from `settings.json`; set `RIVEN_FORCE_ENV=true` to override
them from the environment. TPDB-specific variables:

| Variable                          | Description                                        |
| --------------------------------- | -------------------------------------------------- |
| `RIVEN_TPDB_API_TOKEN`            | ThePornDB API token (Bearer)                       |
| `RIVEN_TPDB_API_BASE_URL`         | TPDB API base URL (default `https://api.theporndb.net`) |
| `RIVEN_TPDB_ENABLED`              | Enable TPDB metadata integration                   |
| `RIVEN_CONTENT_TPDB_ENABLED`      | Enable TPDB site subscriptions                     |
| `RIVEN_CONTENT_TPDB_SITES`        | JSON array of site UUIDs to subscribe to           |
| `RIVEN_CONTENT_TPDB_MAX_PAGES`    | Pages (20 scenes each) to fetch per site/run       |
| `RIVEN_CONTENT_TPDB_UPDATE_INTERVAL` | Seconds between site subscription runs (default 3600) |
| `RIVEN_DATABASE_HOST`             | PostgreSQL connection string (full SQLAlchemy URL) |
| `RIVEN_SCRAPING_PROWLARR_ENABLED` | Enable the Prowlarr scraper                        |
| `RIVEN_SCRAPING_PROWLARR_URL`     | Prowlarr URL (default `http://localhost:9696`)     |
| `RIVEN_SCRAPING_PROWLARR_API_KEY` | Prowlarr API key                                   |
| `RIVEN_SCRAPING_JACKETT_ENABLED`  | Enable the Jackett scraper                         |
| `RIVEN_SCRAPING_JACKETT_URL`      | Jackett URL (default `http://localhost:9117`)      |
| `RIVEN_SCRAPING_JACKETT_API_KEY`  | Jackett API key                                    |

## Docker images

A GitHub Actions workflow (`.github/workflows/docker-build-multiarch.yml`)
builds and pushes multi-arch images to the GitHub Container Registry on every
push to `main` and on version tags:

| Label  | Docker platform    |
| ------ | ------------------ |
| arm64  | `linux/arm64`      |
| x64    | `linux/amd64`      |

> **arm32 (`linux/arm/v7`) and x86 (`linux/386`) are not built in CI.** Their
> native dependencies (`lxml`, `psycopg2`, `psutil`, ...) publish no 32-bit
> wheels, so they would have to compile from source under QEMU emulation —
> making each build take an hour or more. `rapidfuzz` (via `rank-torrent-name`)
> ships no 32-bit x86 binaries at all, so 32-bit x86 is unsupported upstream.

Pull with: `ghcr.io/gauravsuman007/riven-tpdb:latest`.

## Development

See [`AGENTS.md`](./AGENTS.md) for architecture decisions and the live TPDB API
contract.

```bash
uv sync
# unit tests (self-contained; no full app boot required)
uv run python src/tests/test_tpdb_phase2.py   # metadata + indexer
uv run python src/tests/test_tpdb_phase3.py   # content service
uv run python src/tests/test_tpdb_phase4.py   # adult scraper categories
# live tests (hit the real TPDB API)
TPDB_API_TOKEN=<token> uv run python src/tests/test_tpdb_phase3.py
```

## License

This is a fork of [Riven](https://github.com/rivenmedia/riven) and is released
under the same license — see [`LICENSE.md`](./LICENSE.md).
