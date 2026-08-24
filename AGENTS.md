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