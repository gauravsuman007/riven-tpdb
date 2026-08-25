# Riven (adult-only TPDB fork) — agent notes

## Goal
Standalone, adult-only Riven fork backed directly by ThePornDB (TPDB). No
Whisparr dependency. Regular movies/TV must never appear.

## Architecture decisions (so far)
- Metadata source: TPDB REST API (`https://api.theporndb.net`, `Authorization:
  Bearer <token>`). Client: `src/program/apis/tpdb_api.py`.
- Adult scenes and movies both map to Riven `Movie` items (flat, mirrors
  Whisparr's own scene-as-movie model).
- Adult-only is enforced at two points:
  1. `IndexerService.run()` (`services/indexers/__init__.py`) only resolves items
     with a `tpdb_id`; everything else is skipped.
  2. Mainstream content providers (Trakt/Overseerr/Listrr/Mdblist/PlexWatchlist)
     were removed from `program.py` `Services`, `types.py`, and
     `content/__init__.py` (which now exports only `TPDBContent`). Their .py files
     still exist under `services/content/` but are unused (candidate for cleanup).
- Content provider: `services/content/tpdb_content.py` (`TPDBContent`, key `tpdb`)
  subscribes to TPDB **sites** and emits `MediaItem({"tpdb_id": ...,
  "requested_by": "tpdb"})` stubs. Only content service in the fork.
- `MediaItem` gained `tpdb_id`, `site_id`, `site_name`, `performers` (JSON)
  columns. Migration: `src/alembic/versions/...8c71d4e9a2f3_add_tpdb_metadata.py`.
- Mapping is pure/dependency-free in `services/indexers/tpdb_mapping.py`
  (dict -> Movie dict) so it is unit-testable in isolation.
- `tpdb_id` was added to `db_functions.item_exists_by_any_id` and
  `event_manager.item_exists_in_queue`/`add_item` so TPDB items dedupe on their
  TPDB id just like imdb/tmdb/tvdb ids.
- `MediaItem.is_adult` property == `bool(tpdb_id)`.
- Scraping (Phase 4): adult content is Newznab category 6000 ("XXX"/"Adult").
  `services/scrapers/categories.py` adds `is_adult_category()` and
  `select_category_ids()` so Prowlarr recognizes adult indexers and maps TPDB
  items to the `xxx` category. Jackett skips appending the release year for
  adult items (adult trackers match exact title). Stremio scrapers already
  no-op for items without an imdb_id, so they never touch adult items.

## TPDB JSON contract (verified against the LIVE API 2026-08-22)
- Images (`posters`/`background`/`background_back`): `{full, large, medium, small}`
  (all string URLs; served as `image/jpeg`).
- Site: `id` is an INT; canonical string id is `uuid`. `name`, `parent`,
  `network` (both `{id:int, name, uuid, ...}`).
- Performer (scene.performers[]): `id` (UUID str), `name`, `extras{gender}`,
  `face`, `image`, plus many extras.
- Tag (scene.tags[]): `id` (int), `uuid`, `name`.
- Director: `id` is an INT in the live API (despite the plugin C# model typing
  it string); `name`.
- Scene: `id` (UUID str), `title`, `rating` (can be 0 = unrated), `date`
  ("YYYY-MM-DD"), `duration` (sec), `site`, `performers`, `directors`, `tags`,
  `poster`, `posters`, `background`, `background_back`.
- IMPORTANT: search endpoints (`/scenes?parse=`, `/movies?parse=`) return a flat
  list with `site_id` (INT) at top level and NO nested `site`. Only the detail
  endpoints (`/scenes/{id}`, `/movies/{id}`) return the nested `site`,
  `performers`, `tags`. The indexer uses the DETAIL endpoints, so mapping always
  sees the full shape.
- Listing by site: `GET /scenes?site=<site_uuid>&page=<N>` works (the `site`
  param is NOT `site_id`). Page size is fixed at 20; `limit` is IGNORED.
  `performers=` / `tags=` filters were probed and returned null (unsupported).
  List order is NOT strictly by date.
- API models use `extra="allow"` so `model_dump()` never drops fields the
  mapping depends on.

## Testing
- `python3 -m py_compile <changed files>` for syntax.
- Unit suites (self-contained, stub framework deps): `src/tests/test_tpdb_phase2.py`
  (mapping/API/indexer), `src/tests/test_tpdb_phase3.py` (content service), and
  `src/tests/test_tpdb_phase4.py` (adult scraper categories, stdlib-only).
  Run with a venv that has pydantic/sqlalchemy/httpx/requests/lxml/loguru (+ h2).
- Live TPDB calls need `TPDB_API_TOKEN`; the REST endpoints return 401 without it.
- Full app boot requires `pyfuse3` (needs system FUSE headers + pkg-config),
  which `uv sync` cannot install in this sandbox.

## Collections and AVN awards
- A **Collection** is a browsable list beside the library, not inside it.
  `CollectionEntry.media_item_id` is null until an entry is actually requested,
  and that null is the whole design: 8,815 award entries exist as catalogue rows
  while the library holds only what was asked for. Models:
  `src/program/media/collection.py`; migration `...b7e4a2f19c05_add_collections`.
- Award corpus: `services/awards/avn.py` parses Wikipedia's per-ceremony
  articles (4th/1987 through the current one, auto-detected by probing upward).
  Verified live: 39 collections, 11,672 entries, 8,815 naming a work, 2,792
  winners, 6,943 distinct titles, ~2.4s to build.
- Only **winners** are persisted by default (`content.awards.include_nominees`
  is False). Nominees are ~9,000 of the ~11,700 entries and most of the
  resolution cost. Turning the flag off prunes nominees already stored, except
  any that were already requested -- deleting those would orphan a title that
  is in the library.
- Three article layouts exist and all three are needed: `{{Award category|...}}`
  inline cells (39th+); `!` header rows with entries in the *following* row
  (older, needs positional cell mapping -- hence `awards/wikitable.py`); and
  "Additional award winners" bullet lists *outside* any table, which alone carry
  2,595 winners across 33 ceremonies.
- Parser traps, all of which produced wrong data before being fixed:
  a `<ref name="AVN-mag" />` citation parses as a quoted work title unless refs
  are stripped first; bold/italic quote markers survive into titles unless
  stripped *after* studio extraction (studio detection needs those markers);
  older person categories write "Person, Title" bare, which is only safe to
  comma-split once the category is known to be a person award.
- Matching (`services/awards/matching.py`) is the *reverse* of
  `scrapers/adult_matching.py`: catalogue-vs-catalogue, not release-vs-catalogue.
  Bar is `ACCEPT_SCORE = 6.0`, and title similarity alone maxes at 5.0, so a
  bare title match can never be accepted on its own.
- IMPORTANT: resolution needs TWO passes per entry. `/movies?q=` returns the
  flat shape (no nested `site`, no `performers`), so scoring search results
  directly leaves studio and cast permanently unset and nothing ever matches.
  `_resolve_one` shortlists on title, then fetches `/movies/{id}` for the top 3.
- Resumability is a property of the rows: `match_state` *is* the checkpoint.
  Every batch commits, so a restart resumes at the first pending entry. A TPDB
  outage breaks out of the loop rather than marking the backlog unmatched.
- Only winners are auto-requested (`content.awards.auto_request_winners`), and
  `request_matched_winners` is bounded per run so the first sync trickles into
  the pipeline instead of flooding it.
- Frontend (separate repo, `../riven-tpdb-frontend`): shelf on the library page
  (`lib/components/collections-shelf.svelte`), detail at
  `(protected)/collections/[key]/`. The collections endpoints are hand-typed in
  `lib/collections.ts` because `providers/riven.ts` is OpenAPI-generated and
  needs a running backend to regenerate.
- Tests: `src/tests/test_awards.py` (parser + matcher, stdlib-only) and
  `src/tests/test_awards_service.py` (service against real SQLite, skips without
  sqlalchemy).

## Adult Empire (the ranking source TPDB cannot provide)
- `services/recommendations/adultempire.py`. A storefront knows what TPDB does
  not: what sells, what is trending, what customers scored, and what they
  bought together.
- ACCESS RULES, these are not incidental:
  - The site shows an age/terms interstitial to *browser* user agents. Do NOT
    click it -- that button accepts the site's Terms & Conditions, which is not
    ours to accept. The client identifies honestly as `Riven-TPDB-Crawler/1.0`
    and the site serves the real page. `_get` raises if it ever sees the
    interstitial, so swapping in a browser UA fails loudly instead of
    silently routing through a terms acceptance.
  - Do not impersonate Googlebot. It works, but it is impersonation; the
    site's robots.txt is `User-agent: *`, so an honest bot is already welcome.
  - robots.txt disallows every `/Search` path. Nothing here searches -- the
    sitemap and browse listings give the same reach and are allowed.
  - One request/second, single threaded. It is a shop, not an API.
- Surfaces, all verified live:
  - `/all-time-bestselling-porn-movies.html` -- 48/page, 579 pages (~27,800
    titles). Rank 1 is *Pirates*, which is correct, so the order is real.
  - `/best-selling-porn-movies.html`, `/trending-porn-movies.html` -- 490 pages
    each; what is moving now.
  - `/new-release-porn-movies.html`.
  - Detail pages carry `rating-stars-avg` (a real audience score), studio,
    production year, release date, length and full cast.
  - "Customers Who Bought This Product Also Bought" -- collaborative
    filtering, behaviourally different from TPDB `/similar` (metadata
    similarity).
- Cost model: listings are cheap (1 request per 48 titles, rank is position and
  appears nowhere else in the markup); ratings/studio/cast need one detail
  request per title. Top 1,000 all-time is ~21 + 1,000 requests, ~17 min.
- IMPORTANT for matching: studio coverage here is ~100%, against ~2% in the AVN
  winners corpus. Adult Empire titles therefore clear the matcher's bar far
  more easily (title + studio + year + cast, versus title + year alone).
- No JSON-LD anywhere; parsing is regex over the card and detail markup.
- Tests: `src/tests/test_adultempire.py` (stdlib only, trimmed fixtures).

## The brochure (Adult Empire as a first-class source)
- `/brochure` is a browsing surface: one horizontally scrolling shelf per
  ranked listing, served whole by `GET /collections/brochure/shelves` so the
  page paints in one round trip rather than one per row.
- Listings are MIRRORED locally, not fetched on page load -- the client is rate
  limited to 1 req/s, so a live fetch per shelf would make the page unusable.
  `services/recommendations/brochure.py` syncs listings (cheap, 48/request) and
  enriches details (one request per title, resumable: `rating is null` is the
  marker).
- KEY ARCHITECTURAL POINT: a brochure title needs no TPDB record to be usable.
  `MediaItem.adultempire_id` is a second, independent identifier, and
  `services/indexers/adultempire_indexer.py` builds a full `Movie` from the
  cached entry with ZERO network calls -- title, studio (as `site_name`), year,
  release date, runtime and cast are all the scrapers need. TPDB enrichment
  (`recommendations/enrichment.py`) runs later and is purely additive.
- `IndexerService` routes on the identifier: `tpdb_id` first (richer), then
  `adultempire_id`. TPDB wins when both are present so a reindex never falls
  back to the sparser brochure data.
- TRAP: `MediaItem.is_adult` and `scrapers/shared._is_adult_item` must BOTH
  accept `adultempire_id`. They gate the Newznab XXX category; missing the
  Adult Empire case sends brochure titles to the indexers as mainstream films,
  in the wrong categories, and they silently find nothing.
- TRAP: enrichment must request `"/{id}/"`, NOT `"/{id}/{slug}.html"`. A wrong
  slug still answers 200 but serves a page with none of the product markup, so
  enrichment quietly finds nothing at all.
- The detail page at `/brochure/[id]` carries the same controls as a TPDB
  title: Play, Request, candidate releases (`ItemManualScrape`, addressed by
  `adultempire_id`) and direct-site search (`DirectSearch`, which already
  matched on title alone). `resolve_media_item` builds a transient Movie from
  the cached entry, so candidates list BEFORE anything is requested.
- `GET /items/library_states` takes `adultempire_ids` as well as `tpdb_ids`, so
  the brochure page renders files, sizes and releases from the same shape the
  TPDB page uses. An enriched title answers to both ids.
- `CollectionEntry` gained `external_source`/`external_id` (identity at the
  source), `rank`, `rating`, `duration_minutes`, `released_at`. `match_state`
  is `self_sourced` for these: distinct from `matched`, which asserts a TPDB
  record was actually found. `entry.actionable` is the requestable test.
- The AVN resolver must stay scoped to `Collection.source == "avn"` or it will
  burn TPDB calls resolving brochure entries that never needed it.
- `providers/riven.ts` is OpenAPI-generated; the new query params were added to
  it by hand. Regenerating against a running backend produces the same thing.

## Sources evaluated and rejected for ranking/awards
- TPDB has no ranking of any kind: `rating` is 0 on every record, `order_by`
  and `sort` are accepted but ignored, and there is no popularity or view field.
  Do not try to build "top rated" or "trending" on it.
- `awards.avn.com` is authoritative but only covers 2019+ and its year switcher
  is client-side, so each year needs a browser.
- XBIZ and XRCO are each ONE Wikipedia page using a fourth layout
  (`=== Category ===` headings, `* YEAR: Winner, ''Title'' (Studio)` bullets),
  winners only. XBIZ: 361 titles, 84% not already AVN winners. XRCO: 259
  titles, 61% new. Both worth adding as new `Collection.source` values.
- Grabby / Venus / Hot d'Or / Feminist Porn Awards: one sparse page each
  (6-31 bullets); not worth a parser.
- Wikidata's AVN statements are almost entirely performers (221 humans against
  21 films) -- useless for titles.
- XBIZ has no per-ceremony articles, only one summary page.

## Next phases (planned)
- Recommendations (TPDB-based) + performer/tag catalogs (TPDB public list
  endpoint only filters by `site`, so performer/tag catalogs need another
  source or a local cache).
- Frontend "add" flow with TPDB title search.
## Sessions: read this before changing anything
Several Claude Code sessions work in this repo at once. **This file is the only
shared memory between them** -- it is tracked, so it travels with the branch and
is visible to every session and to CI. `CLAUDE.md` is gitignored and is only a
pointer to this file; do not put facts there.

Two rules keep the sessions from diverging:
1. Before starting work, `git log --oneline -10` and skim the section below.
   Another session may have already landed what you are about to write.
2. After landing a change that another session would be wrong without --
   a new deployment step, a renamed service, a trap that cost you an hour --
   append it here in the same commit.

## Deployment
The server is `hellonfire@192.168.2.100` (key-based; no ssh alias). The compose
stack is a full checkout at `/home/hellonfire/Server/riven-tpdb`.

    ssh 192.168.2.100 'cd /home/hellonfire/Server/riven-tpdb && \
      docker compose pull riven-tpdb riven-tpdb-frontend && \
      docker compose up -d riven-tpdb riven-tpdb-frontend'

- **Name the services explicitly.** A bare `docker compose up -d` also recreates
  `riven_postgres`, which an app deploy has no reason to touch.
- **The host also runs UPSTREAM `riven` / `riven-frontend`** (`spoked/riven`).
  Those are a different application. Always target `riven-tpdb`.
- Compose service names are `riven-tpdb`, `riven-tpdb-frontend`,
  `riven_postgres`; container names are `riven-tpdb`, `riven-tpdb-frontend`,
  `riven-db`. Postgres differs between the two -- compose name in compose
  commands, container name in `docker exec` / `docker logs`.
- Ports on the host: backend **8089**, frontend **3001** (not 8080/3000).
- **`:latest` is built from `main` only.** The workflow tags `latest` with
  `enable={{is_default_branch}}`; a `ci/**` push publishes a branch tag that the
  compose file does not reference. Deploying from a branch therefore re-pulls
  whatever main last built and looks like a successful no-op. Merge to main
  first, or override the tag deliberately and say so.
- Backend and frontend are two repos with two independent CI runs; the frontend
  usually finishes a minute or two later. Check both:
  `gh run list --limit 1 --json databaseId,status` then `gh run watch <id> --exit-status`.
- Migrations run automatically at startup ("Database migrations completed
  successfully" in the log). Never run alembic by hand.
- The API answers ~20-30s after the container starts; probing sooner returns a
  misleading 502. Wait with an until-loop on `http://127.0.0.1:3001/api/v1/`.
- Verifying without the api_key: `local_access` is on for loopback and the
  frontend proxies with the key injected, so from the server
  `curl -s http://127.0.0.1:3001/api/v1/items?limit=30` returns real data.
- `RIVEN_FORCE_ENV=true` is set on the server. Any `RIVEN_*` env var silently
  overwrites the UI-saved setting on every start.

## Running the tests
These suites are plain scripts with a local `check(name, cond)` harness, not
pytest. On the server:

    docker exec riven-tpdb env PYTHONPATH=/riven/src \
      /riven/.venv/bin/python /riven/src/tests/<name>.py

Locally they need only sqlalchemy/pydantic/loguru in a throwaway venv; each one
prints `SKIP:` and exits 0 if a dependency is missing.

## User collections (distinct from source catalogues)
- The same `Collection` model backs three different things, told apart by
  `source`: `avn` (award ballots), `adultempire` (storefront listings), and
  `user` (hand-curated lists). Only `user` collections are editable; the router
  rejects edits to the others, because a source catalogue is rebuilt on every
  sync and an edit to one would silently vanish.
- The library page's Collections shelf shows `source=user` **only**. Forty award
  years in that row would bury the two or three lists the user actually made.
- **Adding a title to a collection does not request it.** No add path touches
  the event manager. A collection is what you are interested in; the library is
  what you own. An add adopts an existing MediaItem if there is one, and never
  creates one.
- User entries have a null `category`, so the `(collection_id, title, category)`
  unique constraint does not protect them -- NULL never equals NULL in SQL.
  Dedupe happens in `_existing_entry`, keyed on whichever id the title was
  added by.
- Adding an Adult Empire entry runs a TPDB lookup so it lands with the same
  artwork and ids a TPDB title has. A miss is not an error: the entry keeps its
  `external_id`, stays `self_sourced`, and is still requestable.
- **TPDB has exactly one collection per account** -- a flat "collected" flag,
  no named lists. So `content.collections.sync_to_tpdb` can only mirror
  *membership*, not which collection a title is in. It is also one-way:
  `user/collection` exposes GET/HEAD/POST and no DELETE, so removing a title
  locally cannot un-collect it upstream. Off by default.
- The collection write is keyed on the **integer `_id`**, not the uuid stored
  everywhere else. `TpdbApi.numeric_id()` reads it from the raw payload because
  pydantic does not surface underscore-prefixed keys as extra fields, however
  permissive the model config is.

## The AVN page
- `/avn` is its own browsing surface, a row per ceremony year, newest first --
  not a collections shelf. `GET /collections/avn/overview` enumerates years from
  `content.awards.first_year` to the current year *from the settings*, so a year
  the corpus has not reached yet still gets a row marked `status: "fetching"`
  ("Data being fetched"). A page that grows downwards while a sync runs reads as
  breakage, not as progress.
- `POST /collections/avn/enable` does two things and needs both: it saves the
  setting (so the switch survives a restart and matches Settings → Content →
  Awards) **and** calls `ProgramScheduler.refresh_content_jobs()`. Saving alone
  leaves a switch that reads "on" while nothing runs until the next restart.
- `refresh_content_jobs()` deliberately does *not* re-run `_schedule_functions`:
  every job that registers carries `next_run_time=now`, so a settings change
  would immediately fire the vacuum and the library retry as a side effect. It
  touches only the four awards/brochure jobs, adds or removes them, and drops
  the cached service instances (they read their settings at construction).

## Shared TPDB lookup
`services/recommendations/tpdb_lookup.resolve_movie()` is the single two-pass
search-then-detail matcher. Both the brochure enricher and the collections
service call it. Do not re-implement it: scoring the flat `/movies?q=` records
directly leaves studio and cast unset, the score never clears `ACCEPT_SCORE`,
and nothing ever matches -- silently.
