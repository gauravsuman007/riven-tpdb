<div align="center">
  <h1>Riven TPDB</h1>
  <p>
    An <strong>adult-only</strong> fork of
    <a href="https://github.com/rivenmedia/riven">Riven</a> that runs entirely on
    <a href="https://theporndb.net/">ThePornDB (TPDB)</a> — no Whisparr.
  </p>
</div>

> **Status:** experimental work-in-progress. The TPDB metadata, content,
> scraping, discovery and recommendation layers are implemented and unit-tested
> against the live TPDB API, and the companion frontend fork sources every
> browse surface from TPDB.

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
- ✅ **Discovery & recommendations** — `/api/v1/tpdb/*` serves newest movies and
  scenes, full-text search, performers, sites, the tag vocabulary, per-title
  related lists, and a recommendations feed seeded from your TPDB collection,
  your library, or your site subscriptions.
- ✅ **Requesting** — TPDB titles can be queued into Riven by UUID
  (`POST /api/v1/items/add` with `tpdb_ids`).
- ⚠️ **Ranking** — TPDB exposes no popularity or rating signal. Its ordering
  parameters are silently ignored and `rating` is 0 on every record, so nothing
  here is sorted by popularity. Feeds are newest-first and recommendations are
  derived from relatedness, not from ranking.

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
instead of environment variables -- see [Web UI](#5-web-ui-optional) below for
how to run it.

### 3. Point Riven at adult trackers

- **Prowlarr**: add your adult indexers, then set `RIVEN_SCRAPING_PROWLARR_URL`
  and `RIVEN_SCRAPING_PROWLARR_API_KEY`.
- **Jackett**: same idea with `RIVEN_SCRAPING_JACKETT_URL` and
  `RIVEN_SCRAPING_JACKETT_API_KEY`.

The fork now recognizes Newznab adult categories (`XXX`/`Adult`, category 6000),
so adult trackers are searched correctly instead of being silently dropped.

**Prowlarr and Jackett are the only scrapers that work for adult content**, and
at least one of them is required for anything to download. The Stremio-style
scrapers -- Torrentio, Comet, MediaFusion, AIOStreams, Orionoid -- address
content purely by IMDb id. TPDB titles have no IMDb id, so those scrapers could
only ever return nothing, and they have been removed from this fork along with
Rarbg, whose query hardcodes `ncategory:XXX`. Zilean remains alongside Prowlarr
and Jackett since it searches by title, though its DMM hashlists skew
mainstream.

### 4. Pick a debrid service

Riven does not download torrents itself -- it hands infohashes to a debrid
service, which caches them and streams the files back through RivenVFS. One of
these must be configured or nothing will ever play:

| Service     | Setting                             |
| ----------- | ----------------------------------- |
| Real-Debrid | `RIVEN_DOWNLOADERS_REAL_DEBRID_*`   |
| AllDebrid   | `RIVEN_DOWNLOADERS_ALL_DEBRID_*`    |
| Debrid-Link | `RIVEN_DOWNLOADERS_DEBRID_LINK_*`   |
| TorBox      | `RIVEN_DOWNLOADERS_TORBOX_*`        |

TorBox support is specific to this fork -- upstream removed its downloader in
October 2025, before RivenVFS, and this is a port to the current interface
rather than a revert. TorBox mints playable URLs from a `(torrent_id, file_id)`
pair and those URLs expire, so entries store a synthetic `torbox://` reference
that is resolved to a fresh CDN URL whenever the VFS needs one.

> A qBittorrent-emulating bridge (decypharr, rdt-client) is not a substitute for
> any of these: Riven talks to debrid providers over their own REST APIs, not a
> torrent client API.

#### Uncached torrents

Upstream Riven is **cached-only**: it asks the provider whether a torrent is
already held (instant availability) and discards it otherwise. That assumption
comes from mainstream content, where popular releases are usually in a
provider's cache. Adult releases from adult trackers usually are not, so on this
fork the cached-only rule rejects almost everything -- titles sit in `Scraped`
with every stream blacklisted.

This fork therefore asks the provider to *fetch* an uncached torrent and
re-checks it later, instead of throwing it away:

| Setting                                | Default | Meaning                                            |
| -------------------------------------- | ------- | -------------------------------------------------- |
| `downloaders.download_uncached`         | `true`  | Ask the provider to fetch torrents it has not cached |
| `downloaders.uncached_poll_minutes`     | `10`    | Minutes between re-checks while it downloads        |
| `downloaders.uncached_max_wait_hours`   | `24`    | Give up this long after the provider started        |

This uses your provider's download quota. Set `download_uncached` to `false` to
restore upstream's cached-only behaviour.

### 5. Web UI (optional)

The image in this repository is the **backend only** -- it serves a JSON API and
has no web pages. Browsing to it directly returns `{"detail":"Not Found"}`; that
is the API 404ing on `/`, not a broken deployment. The interactive API docs live
at `http://<host>:8080/docs`.

The web UI is a separate application, [`riven-frontend`][riven-frontend]. It is
included in `docker-compose.yml` and needs two secrets in your `.env`:

```bash
# Backend api_key -- printed on first start, and stored in data/settings.json
BACKEND_API_KEY=<backend api_key>
# Session signing secret
AUTH_SECRET=$(openssl rand -base64 32)
```

Then browse to `http://localhost:3000` and register an account. Set
`ENABLE_EMAIL_PASSWORD_SIGNUP=false` afterwards to close registration.

Use the fork, [`riven-tpdb-frontend`][riven-tpdb-frontend]
(`ghcr.io/gauravsuman007/riven-tpdb-frontend:latest`). Upstream's frontend
browses TMDB, so its home page, search and list pages show mainstream movies and
TV regardless of what this backend serves. The fork sources every browse surface
from the TPDB endpoints instead and adds a TPDB detail page with "Request" and
"Add to TPDB collection" actions.

The settings form is generated at runtime from the backend's JSON schema
(`/api/v1/settings/schema`), so this fork's TPDB settings appear there
automatically.

> If you do run upstream's image, use the `:dev` tag, not `:latest`. At the time
> of writing `:latest` is a much older build that predates RivenVFS: it reads
> settings keys this fork no longer has (`symlink.*`, `downloaders.torbox.*`)
> and its settings pages fail with HTTP 500 as a result.

[riven-frontend]: https://github.com/rivenmedia/riven-frontend
[riven-tpdb-frontend]: https://github.com/gauravsuman007/riven-tpdb-frontend

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

## Direct-scrape scraper plugins

Besides scraping torrent indexers, Riven can search streaming sites directly
("Watch from a site" on a title's detail page) and play a scene straight from
the site's own CDN -- no torrent, no debrid step. The eight built-in sites
(tnaflix, eporner, hqporner, paradisehill, tubepornclassic, xfreehd, upornia,
iporntv) live in `src/program/services/directscrapers/`, and Settings ->
Plugins lets you switch any of them off, or add your own for a site none of
them cover.

### Adding a scraper

Drop a Python file into the `plugins/` folder (mapped to `/riven/plugins`
inside the container by `docker-compose.yml`), then click "Rescan folder" on
Settings -> Plugins. No rebuild, no restart. Your file needs one class:

```python
from program.services.directscrapers.base import DirectScraper
from program.services.directscrapers.models import DirectVideo, DirectSource


class MySiteScraper(DirectScraper):
    key = "mysite"          # unique; also what /resolve is keyed on
    name = "My Site"        # shown in the UI
    base_url = "https://mysite.example"

    def search(self, query: str, limit: int = 20) -> list[DirectVideo]:
        ...  # hit the site's search page/API, return up to `limit` results

    def resolve(self, video_id: str) -> list[DirectSource]:
        ...  # given one of your own video_ids, return its playable renditions
```

That is the entire contract -- one class, two methods. `DirectScraper.__init__`
already gives you `self.session`, a `requests.Session` with a real browser
User-Agent and (if the user has turned it on) automatic routing through their
configured VPN; use it instead of `requests` directly so your traffic honours
that setting.

**`search(query, limit)` -> `list[DirectVideo]`**, best match first, at most
`limit` items. Fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `site` | `str` | Your scraper's `key` |
| `video_id` | `str` | Whatever identifies this video on your site -- passed back to `resolve()` unchanged |
| `title` | `str` | As the site wrote it. Matching against the library happens later; do not pre-filter here |
| `page_url` | `str` | Link to the video's page on the site |
| `thumbnail` | `str \| None` | |
| `duration` | `int \| None` | Seconds. `None`, never `0`, when the site does not say |
| `resolution` | `str \| None` | One of `"2160p"`, `"1440p"`, `"1080p"`, `"720p"`, `"576p"`, `"480p"`, `"360p"`, or `None` if genuinely unknown -- do not guess from an "HD" badge, that is what `hd` is for |
| `size` | `int \| None` | Bytes, if the site states it up front |
| `views` | `int \| None` | |
| `hd` | `bool` | The site showed an HD badge -- a claim, not a measurement |

Do not filter or rank results yourself. `DirectScraperService` runs your
`search()` for several phrasings of the same query and scores everything
centrally against the library's title, cast and studio
(`services/directscrapers/ranking.py`) -- see
[`AGENTS.md`](./AGENTS.md#direct-scrape-matching-tube-sites) for why a naive
title-only filter lets a lot of false positives through, and what this
project settled on instead.

**`resolve(video_id)` -> `list[DirectSource]`**, best quality first. Called
fresh on every playback request -- these URLs are frequently short-lived or
IP-bound, so nothing here is cached. Fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `url` | `str` | The actual media URL |
| `label` | `str` | Shown in a quality picker, e.g. `"1080p"` or `"HD"` |
| `resolution` | `str \| None` | Same scale as above |
| `size` | `int \| None` | Bytes |
| `mime_type` | `str` | Defaults to `"video/mp4"` -- set it explicitly if your site serves HLS (`"application/vnd.apple.mpegurl"`) or anything else |
| `headers` | `dict[str, str]` | Extra headers the URL needs to actually load -- most commonly `Referer`. Travels with the URL rather than being reconstructed by whoever plays it |

### Rules a plugin must follow

- **One `DirectScraper` subclass per file.** A file can define helper
  functions and classes freely; only the first `DirectScraper` subclass is
  registered as a scraper (a second is reported as an error, not silently
  ignored).
- **`key` cannot collide with a built-in.** `tnaflix`, `eporner`, and the
  other seven are reserved; a plugin using one of those keys is rejected with
  a visible error in Settings -> Plugins rather than silently overriding it.
- **Never raise out of `search()` or `resolve()` for an ordinary "no
  results."** Return an empty list. Raise only for something the caller
  should actually surface as an error (the site being unreachable, for
  instance) -- `DirectScraperService` already treats one scraper failing as
  normal and keeps the others' results.
- **A broken plugin cannot take the app down.** A syntax error, a missing
  import, a constructor that raises -- all of it is caught at load time,
  recorded as a per-file error visible in Settings -> Plugins, and the rest
  of the scrapers keep working. Fix the file and click "Rescan folder" again.

Look at `src/program/services/directscrapers/hqporner.py` or `xfreehd.py` for
a complete, working example of the shape above.

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
contract, and [`MAINTAINING.md`](./MAINTAINING.md) for how to pull updates from
upstream Riven into this fork.

```bash
# what would an upstream update actually touch here?
./scripts/upstream-report.sh
```

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
