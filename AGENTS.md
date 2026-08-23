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

## Next phases (planned)
- Recommendations (TPDB-based) + performer/tag catalogs (TPDB public list
  endpoint only filters by `site`, so performer/tag catalogs need another
  source or a local cache).
- Frontend "add" flow with TPDB title search.