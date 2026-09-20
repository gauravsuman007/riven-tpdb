# Riven (adult-only TPDB fork) — agent notes

## Metadata providers: TPDB, then StashDB

`settings.metadata.providers` is an ordered list (`["tpdb", "stashdb"]` by
default). `program.services.recommendations.metadata_lookup.resolve_movie` is
the ONLY entry point callers should use -- `tpdb_lookup.resolve_movie` is now
the TPDB half of the chain, not a thing to call directly.

The chain moves on for two reasons, and both matter: no acceptable match, and
the provider failing outright (no key, unreachable, GraphQL error). A provider
that is disabled or has no credentials is **skipped**, not counted as a failed
attempt.

### The id must not be confused

`Match.provider` says which provider answered. Never store `Match.tpdb_id`
by hand -- call `metadata_lookup.assign_provider_id(target, match)`, which
routes it through `PROVIDER_ID_ATTRIBUTE` to the column that provider owns
(`tpdb_id` / `stashdb_id` / `adultempire_id`, all three present on both
MediaItem and CollectionEntry). Sharing `tpdb_id` would make every TPDB lookup
and dedupe silently wrong, with no way afterwards to tell where a value came
from.

`assign_provider_id` returns **False and writes nothing** when the provider is
unknown or the column is absent. That refusal is deliberate, but it surfaces
as "this provider never resolves anything" rather than as an error -- which is
how Adult Empire matches on collection entries were being dropped before
`CollectionEntry.adultempire_id` existed. `src/tests/test_mediaitem_ids.py`
now asserts the map and the two models agree.

`CollectionEntry.external_id` is **not** the same thing as
`CollectionEntry.adultempire_id`, even though both hold a product number:
`external_id` says the row *came from* the storefront (and the brochure paths
address it by that), while `adultempire_id` says a lookup *matched* it to a
product. Request and link paths must check both.

### StashDB specifics

- GraphQL, so **errors come back HTTP 200 in the body**. Anything checking
  `response.ok` alone reads a total failure as an empty result.
- **Every query needs the API key, search included.** An unauthenticated
  request answers 200 with `"not authorized"` -- not a 401 -- so a missing key
  looks exactly like a malformed query.
- Scene-oriented; there is no movie endpoint. One lookup, not TPDB's two.
- `searchScenes(term, limit)` returns full records, so candidates can be
  scored directly -- no flat/detail split to work around.
- **Whisparr v3 is not a reference.** It never queries StashDB; it consumes
  `api.whisparr.com/v3/` and receives generic Sonarr-shaped resources.

`stashdb_mapping` must keep producing the same keys as `tpdb_mapping`.
`src/tests/test_metadata_fallback.py` compares them directly and fails if they
drift.


## AVN entries: the italic is the title

The pre-2000 ceremony articles never name a studio and run the cast straight
into the title -- ``Nina Hartley, Herschel Savage; ''Amanda by Night II''``.
Read as plain text that yields a "title" naming two people and a film, and
throws the cast away. `avn._split_entry` now unwraps the line's bold first
(the wrapper's own quotes are why the trailing-studio pattern never fired on a
winner row), then takes a quoted segment, then an italic one.

**This is not cosmetic.** A perfect title alone scores 5.0 against
`ACCEPT_SCORE = 6.0`, so a title-only entry is unmatchable *in principle*, by
any provider. Cast is what clears the bar. `avn._cross_reference` therefore
pools cast and studio across every entry in the same ceremony naming the same
film -- the person categories know who was in "Angel Puss" even though the
"Best All-Sex Video" row does not. Scoped to one ceremony and keyed on the
normalised title, so two unrelated films sharing a title decades apart cannot
pool anything.

Measured over ceremonies 4-43: media entries carrying a cast went 226 -> 2267,
and titles still containing a stray `;` went 427 -> 39.

`AwardsService._resolve_one` calls the shared `resolve_movie` chain now, not
TPDB directly, so an award entry can resolve from Adult Empire or StashDB.
TPDB's *scene* index is kept as a last resort after the chain (a few
categories genuinely name a scene) -- `_resolve_scene`.


## Recommendations and list import: read the design doc first

`design/RECOMMENDATIONS-STRATEGY.md` holds the researched strategy for the
recommendation engine (scene / movie / studio) and the generic list-import
feature. It is dated 2026-09-10 and every source-access claim in it was
verified against the live site that day; re-check robots.txt before acting.

The headline, because it is the thing most likely to be re-litigated:
**Reddit cannot be harvested.** `robots.txt` is `User-agent: * / Disallow: /`,
and NSFW content has been unavailable through the Data API since 5 July 2023.
The import pipeline IS the Reddit integration. Do not build a crawler for it,
and do not build embedding search over titles -- see the doc for why that
contradicts the refuse-rather-than-guess stance the matcher is built on.

## The recommendation engine (built; steps 1, 2, 4 of the design doc)

Three modules under `program/services/recommendations/`, served by
`routers/secure/explore.py` at `/api/v1/explore/*`.

### facets.py -- the vocabulary

`MediaItem.genres` is one flat list mixing kinds of fact (`blowjob`,
`narrative` and `brown hair` in the same bag), which is why nothing could act
on it. `Facet(kind, category, value)` restores the kind, adopting StashDB's
grouped tag graph as canonical and normalising every other provider into it by
alias.

- **Normalisation refuses rather than guesses.** An unrecognised string becomes
  `GENRE / Unknown` carrying the original text, never a plausible-looking
  `Moods:` facet. A guessed facet would be matched by every mood intent
  forever with nothing to show it was invented -- the same stance as
  `assign_provider_id` writing nothing rather than the wrong column.
- The graph is **ingested, not invented**: `POST /explore/vocabulary/ingest`
  reads ~3,000 tags in ~30 calls and caches them as `tag_graph.json` in the
  StashDB cache dir. Until that has run there are no tag ids, so the scene
  engine reports itself unavailable instead of quietly serving newest-first
  results under an intent's name. The button is on the Explore page.

### intents.py -- what a person asks for

"Outdoor", "real plot", "believable" are named facet expressions with `any`
(pull), `all` (requirement), `none` (veto) and runtime/year bounds. Defaults in
`DEFAULT_INTENTS`; an operator's `intents.json` in the data dir merges **per
intent by name**, so retuning one does not fork the rest.

- **A bound excludes a known-and-outside value, and says nothing about an
  unknown one.** Making bounds strict (unknown = excluded) looked principled
  and emptied "Has a real plot" outright, because the award corpus carries no
  runtime at all. Era membership is kept honest by requiring a positive decade
  facet instead, which is the `any` list doing the filtering.
- **`Intent.evaluate` returns `None` when nothing in `any` matched, not 0.0.**
  It briefly did the latter, on the theory that "eligible but unwanted" was a
  useful fallback. Measured on the live catalogue with the vocabulary not yet
  ingested, four intents produced rails byte-identical to the unfiltered one,
  because every title was eligible for all of them. A row labelled "Outdoors"
  that is really "everything" is worse than no row -- and the router already
  drops empty rails.
- **A declared bound requires the fact it bounds to be known.** An unknown year
  is not evidence of falling inside a year range; that is how a 2020 release
  turned up under "The golden age".
- Every intent declares which `engines` it suits. "Real plot" is `movies` only:
  StashDB's corpus is modern amateur/gonzo scenes, so asking it that question
  returns confident nonsense.

### engine.py -- the two engines and the studio split

- **MovieEngine** ranks `CollectionEntry` rows already in the database
  (brochure shelves, AVN ballots, imported lists). No fetching, works with no
  provider configured. Signals: award density (per body -- AVN/XRCO/XBIZ are
  weighted apart because they disagree usefully), rating shrunk toward the
  corpus mean, bestseller rank as demand, recency, and library affinity.
  - **TRAP: rank once for all rails.** `/explore/rows` calls `rank_many`, not
    `rank` per row. The corpus is five figures of rows and ranking per rail
    re-reads all of it, plus the taste profile and award index, once per row
    on the page.
  - The award index is keyed on the **folded title**, not an id: an award
    corpus and a storefront share no identifier, and pooling must not wait on
    the awards service having resolved a provider match.
  - `CollectionEntry` carries no tags, so facets are assembled from the
    storefront categories (below), the **award category** ("Best Parody" is a
    genre claim by an editorial body), a **decade derived from the year**
    (`decade_facet`, restricted to 1970s-1990s because those are the themes
    StashDB's graph actually has), and the MediaItem's genres once requested.

### adultempire_categories.py -- where movie genres actually come from

**An Adult Empire product page carries no genre information at all.** Verified
on the live site: length, production year, studio, cast, UPC, disc count, and
every `Label=` on the page is navigation. This is why the theme and mood rails
came back empty -- there was nothing on a brochure entry to match against.

The genres exist only in the other direction: 507 browsable categories, each a
listing of the titles in it. So the index is built by reading the categories an
intent names and recording which products appear in them. Measured sizes:
Feature 12,238, Classic Plot 1,836 (literally the "real plot" signal), Classic
7,827, Outdoors 2,368, Romance 1,485, Parody 1,296, Comedy 815, Beach 277,
Vintage Porn 193.

- Category pages parse with the existing `parse_listing` -- same product-card
  markup as a studio listing, 48 per page, `?sort=bestseller&page=N`.
- **Only the first pages of each category.** Listings are demand-ordered and
  the brochure mirrors top-ranked titles, so the overlap is front-loaded. A
  full Feature crawl is 255 requests at one per second for twelve thousand
  titles we do not hold.
- A JSON file in the data dir, not a table: derived, rebuildable, no migration.
- The sync **runs in the background** and the endpoint returns immediately.
  Holding an HTTP response for a four-minute crawl times out at the proxy and
  reports failure while the crawl is still working. Poll
  `GET /explore/categories`.

**"Shot with care" is `engines=["scenes"]` on purpose.** StashDB has
`Moods:Artistic`; the movie corpus has no equivalent -- Adult Empire publishes
no such category, and no AVN category in the corpus names cinematography,
screenplay or direction (checked: zero rows). A movies rail for it would be
filled by whatever came closest, which is the confident wrong answer the engine
exists to avoid.
- **SceneEngine** asks StashDB's `queryScenes`, which facets server-side.
  - **TRAP: `INCLUDES`, never `INCLUDES_ALL`.** An intent's `any` list is a
    pull; requiring all eleven location tags demands a scene shot on a beach
    *and* a boat *and* a balcony, which returns nothing.
  - **TRAP: never send an exclusion on the tags criterion.** There is no
    exclusion key on it -- an `excludes` alongside `value` is rejected with
    HTTP 422, which `_query` raises and `rank` catches, so every intent
    carrying a `none` term came back empty with nothing but a warning in the
    log. Both scene rails were dead this way.
  - The server-side filter is a narrowing, not the judgement: `none` is
    applied locally on every returned scene, which is what the local
    evaluation was always for. The query over-fetches to pay for it.
- **StudioEngine** answers "best of X" without meaning "best-selling". Adult
  Empire carries a rating per title but will not order by it (hence
  `STUDIO_SORTS`), so the mirrored catalogue is re-ranked locally and returned
  as **two labelled rows** -- deep cuts (rates above the studio's *own*
  baseline, sells poorly) and popular -- rather than one blend that answers
  neither question.

Every result carries its `signals` and `reasons`, and the page prints them. A
recommendation nobody can interrogate is one nobody can correct.

### Ratings: where they come from, and the two zeros

`ratings.py` / `POST /explore/ratings/sync`. The measured facts, all of which
look like bugs until you know them:

- **TPDB writes a literal `0` on every record.** It exposes no ranking at all,
  so a stored 0 means "no ranking", not "rated zero" -- 60 of 68 library items
  held it. Anything checking `rating is None` alone will treat that 0 as a
  real score and show it, and will refuse to overwrite it.
- **An Adult Empire *listing* page carries no rating.** 48 of 48 rows on the
  all-time-bestsellers page came back with none, which is why only 35 of 2,577
  catalogue entries had one.
- **An Adult Empire *product* page does.** `rating-stars-avg`, out of five.
  Roughly two thirds of pages have one; the rest have no reviews, which the
  backfill counts as `unrated`, not `failed`.
- **The slug in a product URL is ignored.** `/700215/` and
  `/700215/anything-porn-movies.html` both return Pirates. That is what makes
  the backfill possible with no sitemap index and no slug reconstruction.

The backfill commits in batches of 25 rather than at the end, unlike the
category index -- a twelve-minute run that writes only on completion looks
like nothing is happening and loses everything to a restart.

**TRAP: the column cannot record "nobody reviewed this".** That and "we have
not looked" are both `rating IS NULL`, so without a record of the attempt a
third of the catalogue is re-fetched on every run and `pending` never reaches
zero. Those product ids go in `adultempire_unrated.json` beside the category
index -- derived, rebuildable, no migration -- and `?force=true` re-checks
them when a title has since been reviewed.

It only ever fills gaps in `year`/`duration_minutes`: a storefront disagreeing
with an award ballot is not grounds to overwrite the ballot. It does replace a
MediaItem's 0, which is not a score.

### Rail filtering and sorting

`engine.arrange()`, behind `min_rating` / `sort` on `/explore/recommendations`
and `/explore/rows`. Two rules that are choices, not accidents:

- **An unrated title never satisfies a minimum.** Most entries have no rating,
  and "no rating" is not evidence of a good one.
- **Sorting by rating does not hide the unrated** -- they sort last, still in
  score order. A sort is an ordering; turning it silently into a filter drops
  titles nobody asked to drop.

The filter runs after scoring and before the limit. A per-rail control
re-ranks the whole corpus through `/explore/recommendations` rather than
filtering the twenty items on screen: "the four titles in this row with four
stars" and "the catalogue's best four-star titles for this intent" are
different answers, and only the second is the one anyone means. `Rail.intent`
is carried in the response for this -- do not split `key` on a hyphen, which
works only until an intent name contains one.

A scene rail can only empty under a minimum: StashDB carries no audience
score at all.

Tests: `src/tests/test_recommendations.py` (stdlib-only, stubs the framework
deps; no network, no database).

Still unbuilt from the design doc: import v1/v2, the movie facet index, XBIZ /
XRCO corpora, Excalibur.

## A bare magnet cannot reach most swarms

`add_torrent` used to send `magnet:?xt=urn:btih:<hash>` and nothing else. With
no trackers the debrid provider has only the DHT, and a swarm that announces
solely to its own tracker has no DHT presence -- so the torrent stalls at
"no seeds" forever while the indexer reports a healthy count. Both numbers are
true; they describe different networks.

Pass `Stream.download_url` (the indexer's .torrent link, already fetched
during scraping to extract the infohash) and TorBox gets the announce list.
Measured on one release: 0 peers as a magnet, 4.3 MB/s and complete as a file.

TorBox **dedupes by infohash**: re-adding one it already holds returns the
existing torrent and ignores the file, so a torrent already stuck at 0 seeds
must be deleted before re-adding it with the file. Only TorBox is known to
accept the upload; the other providers take the argument and ignore it.

`Stream.privacy` records public/semiPrivate/private from Prowlarr and orders
reachable releases first. It is a safety net, not the fix -- with the torrent
file, a semi-private release downloads fine.

## Adult matching: what the score means

`adult_matching.evaluate()` scores site/date/performers/title and the result
is persisted as `Stream.rank` (score x 100). RTN's own rank is ~0 for
everything adult, which is why it cannot be used.

Changing acceptance rules WITHOUT measuring against the live library is the
trap. An outright year-conflict veto looked obviously right and discarded 24
of 67 in-use releases, nearly all correct: tracker dates and TPDB `aired_at`
disagree constantly. Run the evidence over every stored stream and look at
which currently-active downloads would be rejected before believing a rule.

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

## Studios (Adult Empire's per-studio ranked listings)
- `/studios` is the directory (a picker), `/studios/[id]` is one studio's
  ranked rows, and the brochure page shows only SAVED studios. Showing all
  hundred there would bury the two or three the user follows -- the same
  reasoning that keeps award years off the library's Collections shelf.
- The `Studio` table stores studios ONLY. A studio's titles are read live on
  every request and never mirrored: two ranked rows for a hundred studios is
  twenty thousand rows rebuilt weekly to serve pages mostly never opened, and
  a rank stored last Sunday is not the rank.
- Directory source is `?letter=all` on the three catalogue index pages
  (`/all-porn-movie-studios.html`, `/all-porn-video-studios.html`,
  `/all-blu-ray-studios.html`), unioned by id, ~800 each for movies/videos.
  TRAP: the studio SITEMAPS (`/sitemaps/studio*/sitemap.xml`) look like the
  sanctioned source and are what the feature originally used, but they cap out
  at ~100 -- a curated top slice, not the catalogue. Confirmed live: Pure
  Taboo (id 95179, a real working studio page, 242 titles) is absent from
  every sitemap and present in `?letter=all`. Confirmed non-paginated too --
  `&page=2` returns the identical set. robots.txt disallows `/Search` and
  `/AllSearch/Search` specifically; these index pages are not under either.
- Studio URL is `/{ae_id}/studio/{slug}.html`. Each card on the index page
  links its id TWICE (image, then title) -- `parse_studio_refs` dedupes.
  The movie index is read first so the winning slug is the `-porn-movies`
  form, which is the catalogue `parse_listing` is built around.
- Some studios genuinely have no Adult Empire page at all -- e.g. Bratty Sis
  (a TPDB "site" under the Nubiles network) never showed up under any name
  variant across all three catalogue indexes. That is a real gap in what the
  storefront carries, not a bug in the directory sync.
- `parse_listing` works UNCHANGED on studio pages -- same `product-card`
  markup. That is why studios needed no second parser; a divergence would show
  up as empty studio pages.
- Sorts: the page offers eight, but only `bestseller` and `trending` rank by
  demand and those are the only two `STUDIO_SORTS` allows. THERE IS NO RATING
  SORT. Adult Empire carries a rating per title (detail page only) but will not
  order by it, so there is no honest "Top Rated" row -- re-sorting the
  forty-eight bestsellers would be a top-rated list *of the bestsellers*.
- TRAP: Adult Empire studio pages have NO description and NO logo. Only an
  `<h1>`, a `data-tid` and an "N Results" count. All studio artwork and
  descriptions come from TPDB `/sites`, which is why `Studio` has both
  `refreshed_at` (storefront) and `tpdb_checked_at` (TPDB) -- the latter
  records the ATTEMPT, so studios TPDB has never heard of are not re-looked-up
  every run.
- TPDB site matching is EXACT on a normalised name, no fuzzy fallback
  (`studios.pick_site`). A search for "Evil Angel" returns twenty-two sites
  including "Mylf X Evil Angel", in TPDB's own order; a loose match hangs the
  wrong network's logo on a studio and nobody can see to report it.
- `_store` must NEVER write `saved`. A weekly sync that cleared saved studios
  is indistinguishable from data loss. Guarded by a test.
- Clicking a studio title POSTs to `/studios/titles/{product_id}`, which
  find-or-creates a `CollectionEntry` and returns its id; the frontend then
  goes to `/brochure/{entryId}`. It searches EVERY `source="adultempire"`
  collection first -- studio rows overlap the brochure shelves heavily, and two
  entries for one storefront id means two detail pages disagreeing about
  whether it was requested.
- The router holds ONE `StudioService` for the process. The 1 req/s pacing
  lives on the client instance, so building one per request resets it and
  turns a polite crawler into concurrent bursts.
- The directory sync is CRON, not interval (weekly, overnight): a several-
  minute crawl on an interval drifts to whenever the process last restarted.
  `ScheduledFunctionConfig` gained an optional `cron` key for this. It also
  runs once immediately when the table is empty, so enabling it does not leave
  the section blank until Sunday.

## Resolving brochure entries to TPDB (the "old detail page" bug)
- `/brochure/[id]` picks its view from `entry.tpdb_id`: set means redirect to
  the full TPDB page, null means render the storefront page. So an entry that
  was never resolved is stuck on the storefront view FOREVER.
- Until `BrochureService.resolve_batch` existed, `enrich_entry` ran only when a
  title was REQUESTED. Measured on the live database: 573 of 576 Adult Empire
  entries had `tpdb_id` null. This looked like "the TPDB page only works for
  new titles" -- it was really "it only works for requested ones".
- TRAP: `resolve_batch` selects on `matched_at IS NULL`, not on `tpdb_id IS
  NULL`. About one title in five has no TPDB record at all (bare one-word
  titles, pre-1980 releases); keying off the id alone re-asks TPDB about every
  known miss on every run, forever, and starves the never-tried entries.
- A miss stamps `matched_at` but KEEPS `match_state = self_sourced`. Demoting
  it to `unmatched` (as the awards path does) would make `actionable` false and
  take away a title that downloads perfectly from storefront metadata. An award
  entry with no TPDB record is a dead row; a storefront entry is not.
- It has its own timer, separate from `_enrich_brochure`. That one is paced by
  Adult Empire's 1 req/s courtesy delay, this one by TPDB's rate limit; sharing
  a timer makes each wait out the other's budget.

## VPN routing (Tailscale today, swappable)
- `program/services/vpn/` is a provider seam: `base.py` states the contract,
  `tailscale.py` implements it, `__init__.py` owns POLICY. Callers ask the
  SERVICE, never a provider, and never "is the VPN on" -- they ask whether a
  named purpose (`SCRAPING`, `STREAMING`) is routed. Adding WireGuard means one
  class plus one enum value.
- FAILS CLOSED, and this is the load-bearing property. If a purpose is routed
  and the tunnel is down, `proxy_for` raises `VpnUnavailable` and the route
  returns 503. It must NEVER fall back to a direct connection: someone routing
  scraper traffic is controlling where it appears to come from, and quietly
  using the host's address instead defeats the only reason the setting exists,
  invisibly. Guarded by tests.
- Only the streaming-site scrapers are ever routed. TPDB, the debrid
  providers, the indexers and the library scan always go out directly.
- TRAP: the proxy is applied in `_RoutedSession.request` (a `requests.Session`
  subclass), NOT in `DirectScraper._get`. `_get` looks like the obvious place
  and is wrong -- `iporntv` calls `self.session.head` directly to probe a
  rendition, and that request would go out around the tunnel while everything
  else went through it. The scraper still works and the video still plays; only
  the exit address is wrong, which is invisible. Overriding `request` covers
  every verb and every future call site by construction.
- TRAP: the proxy URL must be `socks5h://`, not `socks5://`. Plain `socks5`
  resolves hostnames locally, handing every scraped site to the host's own
  resolver -- exactly what routing the traffic was meant to avoid.
- The daemon is a SIDECAR container in USERSPACE mode (`TS_USERSPACE=true`),
  deliberately with no `NET_ADMIN` and no `/dev/net/tun`. Kernel mode captures
  the whole container's routing table and there would be no way to route only
  the scrapers. Do not "fix" this into kernel mode.
- Control (login, exit node) goes over `tailscaled`'s local API on its unix
  socket, shared between the containers by the `tailscale-sock` volume. That
  mount must be READ-WRITE on the backend: connecting to a unix socket needs
  write access, so `:ro` leaves status working and every control action
  failing. The local API is not a versioned public API, so every call in
  `tailscale.py` degrades to "unavailable" rather than raising.
- TRAP, found live on first deploy: the image's default socket is
  `/tmp/tailscaled.sock` INSIDE the tailscale container;
  `/var/run/tailscale/tailscaled.sock` is only a symlink to it, kept for
  host-mode compatibility. Sharing `/var/run/tailscale` alone shares the
  symlink, not the socket it points at, which is outside the shared volume and
  invisible to the backend -- status read "unreachable" even with the sidecar
  logged in and healthy. Fixed by setting `TS_SOCKET=/var/run/tailscale/tailscaled.sock`
  on the tailscale service, which makes the daemon bind its real socket inside
  the shared directory instead of leaving a dangling link to it.
- Exit nodes are only offered from peers with `ExitNodeOption`. Setting an id
  the daemon does not recognise is accepted silently and routes nothing, which
  is indistinguishable from a working tunnel -- so `set_exit_node` refuses
  unknown ids rather than passing them through.
- The VPN settings tab carries a custom control panel (`vpn-control.svelte`)
  alongside the generated form, because logging in and picking an exit node are
  actions against a running daemon, not values to save.
- TRAP, reported as "Generate login link does nothing": `VpnService.__init__`
  used to skip building a provider entirely unless `vpn.enabled` was already
  true, so clicking either login button before that toggle was flipped hit
  `self.provider is None` and returned a silent, error-free `state: disabled`
  -- indistinguishable from a successful click. Logging in, checking status
  and choosing an exit node are account-management actions that must work
  before there is any reason to enable routing, not after. Fixed by always
  building the provider; `enabled` (with `route_scraping`/`route_streaming`)
  still gates `routes()`/`proxy_for()`, which is the thing it should gate.
- TRAP, reported by the user as "two auth key fields" and "no login URL
  button": `TailscaleModel.tailscale` used to render in the generic schema
  form AND in the control panel, and the generic form's copy had a worse
  failure mode than duplication -- saving a key through it set
  `settings.vpn.tailscale.auth_key` without ever calling `/vpn/connect`, and
  the endpoint's old fallback (`body.auth_key or settings.tailscale.auth_key`)
  meant every later "Log in" attempt silently tried key auth instead of
  generating a URL, so the login-URL button never had a reason to appear.
  Fixed two ways: `HIDDEN_SECTIONS["vpn"] = {"tailscale"}` in
  `program/settings/visibility.py` removes the schema-rendered field entirely
  (`socket_path`/`proxy_url` are container wiring, not user settings, same
  reasoning as everywhere else in that module); `/vpn/connect` no longer
  substitutes a stored key for an omitted one -- whichever of the panel's two
  buttons was clicked ("Generate login link" vs "Connect with key") is exactly
  what runs, deterministically.
- The sidecar is OPTIONAL. Without it the backend works normally and the VPN
  tab reports "unreachable"; the repo's `docker-compose.yml` is the reference,
  and a deployment has to add the service to its own compose file.

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
- A stale FUSE mount left by an uncleanly-killed `riven-tpdb` is now cleared
  automatically by the `riven-tpdb-mountguard` sidecar in docker-compose.yml
  (see the long comment there). Read that before touching either half.
  - The failure this prevents: the `rshared` bind leaks the RivenVFS mount to
    the host, and a crash orphans it there. `restart: unless-stopped` then
    retries against the dead endpoint forever -- 118 consecutive failures
    overnight, with nothing in the backend's log, because the daemon refuses
    to create the container and the image never runs. The manual fix that
    used to be documented here (`umount -l .../library`) still works, but
    only helps someone who happens to be watching, which is why it recurred.
  - **A dead endpoint has two states and only one of them fails to stat.**
    Measured on the same orphaned mount: `ls -d .../library` succeeded while
    `ls -A .../library` returned ENOTCONN. So `[ -d ]`, `test -e` and `ls -d`
    all report a dead mount as healthy -- any check built on them silently
    does nothing. Always read the directory. The daemon hits the same split,
    which is why the backend sometimes fails to be created and sometimes
    starts onto a library it cannot read while reporting itself healthy.
  - The guard binds the PARENT directory, deliberately: docker never stats
    the children of a bind source, so the guard starts when the backend
    cannot, and `rshared` carries its unmount back out to the host.
  - `rshared` cannot simply be dropped to avoid all this -- it is what lets
    the Jellyfin service see a filesystem mounted inside another container.
  - `entrypoint.sh` carries the same check as a second line of defence, for
    the case where the daemon binds a dead endpoint through instead of
    refusing. It cannot replace the sidecar: in the refusing case it never
    runs at all.
- `RIVEN_FORCE_ENV=true` is set on the server. Any `RIVEN_*` env var silently
  overwrites the UI-saved setting on every start.

## Library sorting and grouping
- `SortOrderEnum` covers title, added-date, rating, release year and studio.
  `sort_type` (which enforces one sort per column) splits the value rather
  than assuming anything not "title" is a date -- the old form broke the
  moment a third column existed.
- **Every optional column sorts NULLS LAST.** Postgres puts NULL highest, so a
  descending rating sort would otherwise open with every unrated title, which
  is the opposite of what it asks for.
- **Grouping is a frontend rendering choice**, applied to the page the grid
  received; there is no `group` parameter on the backend and the loader strips
  it. What makes it correct is that choosing a group also sets the sort on the
  same key (`GROUP_SORT`), so consecutive runs in the page *are* the groups.
  They are built by appending in order, never by bucketing on the key --
  bucketing would merge two runs that a page boundary separated and claim a
  group was complete when it was not.
- A rating of 0 groups with the unrated. See the two zeros above.
- TRAP: `providers/riven.ts` is generated from the backend's OpenAPI spec and
  needs a running backend to regenerate, so it lags new query values. The zod
  schema in `schemas/items.ts` is the check that matters; keep any cast
  confined to the one field that drifted.

## Library search: studio and cast
- The search box matches a **title, its studio, or anyone in its cast**, and
  suggests all three as you type. `GET /api/v1/items/suggest?q=` answers from
  the LIBRARY ONLY -- it never touches TPDB, so a keystroke can never become a
  rate-limited upstream call.
- `MediaItem.performers` is a JSON array and a JSON array cannot be searched by
  substring: Postgres matches an exact element with a GIN index and nothing
  else. `ItemPerformer` (`program/media/item_performer.py`) is that column
  unnested, one row per (title, performer), which is what makes "type *ril*,
  see *Riley Reid*" possible at all. Studios need no such table -- `site_name`
  is already a column.
- Size is not the concern people expect: this indexes the library, not TPDB's
  catalogue. At ~3 performers per scene it is ~3 rows per OWNED title.
- The table is DERIVED, never authored. `sync_item_performers` is driven by
  `after_insert`/`after_update` mapper events on `MediaItem`, not by calls at
  the write sites -- `performers` is set by the TPDB indexer, the Adult Empire
  indexer and TPDB enrichment today, and a fourth site added later would
  otherwise silently stop updating suggestions. `rebuild_all()` reconstructs it
  from scratch at any time.
- TRAP: the `after_update` listener MUST gate on
  `inspect(target).attrs.performers.history.has_changes()`. Items are updated on
  every pipeline state transition; without the gate one state change becomes a
  delete plus three inserts for every title in flight.
- Migration `c5a8f30d9b71` backfills in SQL and adds pg_trgm GIN indexes on
  cast, `site_name` and `title`.
- TRAP, and it cost a deploy: **`alembic/env.py` runs migrations with
  `isolation_level="AUTOCOMMIT"`.** `connection.begin_nested()` (a SAVEPOINT)
  is invalid there and raises before the statement is even sent, so the
  optional-index loop created NONE of its four statements and swallowed the
  reason. Under AUTOCOMMIT there is no poisoned transaction to protect against
  in the first place -- a plain try/except per statement is both necessary and
  sufficient. Do not reach for a savepoint in a migration in this repo.
  `e2b7c40a9d16` repeats the statements for databases already stamped past the
  broken version; fixing a migration in place only helps one that has not run
  it yet.
- Everything about the indexes is a performance property only. Search is
  correct without them, so `IF NOT EXISTS` plus a swallowed failure is the
  right shape -- but verify with `SELECT indexname FROM pg_indexes WHERE
  indexname LIKE '%trgm%'` after deploying, because a silent skip looks
  exactly like success.
- `search` (substring, all three fields) and `performer=`/`site=` (exact,
  case-insensitive) are deliberately different params. Clicking "Riley Reid" in
  the dropdown sets the facet, not the text search: a substring search for a
  name would also return every title whose text happens to contain it. Typing
  clears the facet -- keeping both would silently show the intersection.
- Frontend: the dropdown is in the library page's search field, fed by a
  `suggest` remote command over `$lib/suggestions.ts` (hand-typed for the same
  reason as `collections.ts`). Two debounce timers on purpose: 150ms for the
  dropdown (a cheap local query, should feel live) and 300ms for the grid (a
  full navigation). Out-of-order responses are dropped by a request token --
  "ri" can answer after "riley" and would otherwise overwrite it.
- Tests: `src/tests/test_item_search.py` (SQLite, stdlib harness).

## Search matches names fuzzily (`program/utils/fuzzy.py`)
- `ilike('%term%')` is a substring test, not search. Measured against the live
  directory before this existed: "brazzers" found Brazzers, **"brazers" found
  nothing**, "evil angel" found twenty-two, **"evilangel" found nothing**. A
  viewer who gets an empty page concludes the studio is not carried.
- Two halves, and they are orthogonal. `matches()` widens the WHERE clause;
  `ranking()` orders what comes back so widening never costs precision at the
  top -- exact, then prefix, then substring, then merely-similar. Searching
  "vixen" must still offer *Vixen* above *Exotic Vixen Films*, and a test
  pins exactly that.
- **COLLAPSING** (strip everything but letters and digits) is the cheap half
  and catches more than trigrams do: it makes "evilangel", "Evil-Angel" and
  "evil angel" one string, and being exact it cannot introduce a wrong match.
  The OnlyFans index stores a collapsed form already -- the handle IS it --
  so that column is passed as `collapsed=` rather than collapsed again in SQL.
- **`word_similarity`, NOT `similarity`.** The plain form compares whole
  strings and punishes a name for being longer than the query, which is the
  normal case. Measured: "bangbross" vs "Bang Bros Productions" scores 0.24
  whole-string and 0.50 word-wise; "vixn" vs "Vixen" 0.375 against 0.60.
  Threshold is 0.5 -- pg_trgm's own 0.3 default starts returning names that
  merely share a syllable.
- Terms shorter than 4 characters skip the fuzzy clauses entirely. Two letters
  is a prefix someone is still typing, and fuzzy-matching it hits everything.
- **Trigrams are Postgres-only and the tests run on SQLite**, where those
  clauses are simply omitted. That is deliberate: search stays CORRECT
  everywhere and only gets cleverer where `pg_trgm` exists. Never make
  behaviour depend on an index -- the same rule as the library's trigram
  indexes above. `test_fuzzy_search.py` asserts SQLite has no trigrams and
  still answers.
- Indexes: `a7c4e9b21d38` (studio names) and the add-on's `0005_name_trgm`
  (account names and handles). Performance only, `IF NOT EXISTS`, failures
  swallowed -- and therefore **verify after deploying**, because a silent skip
  looks exactly like success.
- TRAP, in the add-on's account list: `order_by` APPENDS. The relevance
  ranking has to go on BEFORE the rail's own ordering, or the rail leads and
  relevance becomes a tiebreak nobody ever reaches.

## Search returns three kinds of thing, in three rows
- Titles, **studios** and **OnlyFans accounts**. A search box is asked for all
  three and used to answer with only the first.
- `/details/tpdb/movie/<uuid>` was being built for EVERY result including
  performers and TPDB "sites", so clicking a studio answered **404 "Title not
  found on TPDB"**. That is what "search is broken" looked like.
- **TPDB sites and studio pages are different id spaces.** `/studios/[id]` is
  keyed by Adult Empire's id; a TPDB site carries its own numeric id and a
  uuid, and neither can be rewritten into the other. So the Studios ROW is
  sourced from the studio DIRECTORY (`/api/v1/studios?search=`), which is the
  thing that actually has a page, rather than from TPDB's site search.
- Performers have no page in this app. Their honest destination is their work
  in the library, which the `performer=` facet already serves.
- The rows render ABOVE the titles and OUTSIDE the results branch, because
  they must show when no title matched: searching a studio whose films are not
  in the catalogue used to say "No results found" with the studio page one
  click away. That state now says "No titles matched".
- Frontend: `$lib/entity-search.ts` (hand-maintained, like `studios.ts` --
  the add-on's routes are never in the generated OpenAPI spec) and
  `components/search/entity-row.svelte`. Streamed, not awaited, so the title
  results never wait behind it. `allSettled`, because the add-on is optional
  and an empty studio directory is a normal state, not an error.

## Multi-file releases (playlists)
- A scene compilation arrives as ONE torrent holding five or six separate
  scenes, each a `MediaEntry` against the same title. Playback used to resolve
  `media_entries[0]` and call that the title, so the rest were downloaded,
  mounted and unreachable -- nothing in the UI ever said they existed.
- `MediaItem.media_parts` is the centre of the fix: the files of the CURRENT
  release, in filename order. `MediaItem.media_entry` is now just `parts[0]`.
- The grouping key is `MediaEntry.stream_infohash` against
  `active_stream.infohash`. This is not cosmetic: item 862 (Half His Age) held
  five files from the active torrent PLUS one orphan left by a swapped-out
  candidate, and `media_entry` was returning the orphan -- so the title played
  a file from a release it no longer used.
- TRAP: `active_stream` is an `ActiveStream` pydantic model, not a dict. The
  column is JSON and comes back through a TypeDecorator, so `.get("infohash")`
  raises AttributeError and every request 500s. Use `.infohash`.
- Every stream endpoint takes `?part=N` indexed into `media_parts`, defaulting
  to 0, so a single-file title produces byte-for-byte the URLs it always did.
  `playback_url.resolve(..., part=)` is where it lands.
- HLS sessions are keyed `"{item_id}:{part}"`, not by item. Sharing one would
  serve segments of one file under another's playlist. `SessionManager.segment`
  takes `session_key`, and DELETE with no `part` stops every part's session.
- `GET /stream/parts/{id}` is what a client builds a playlist from (always at
  least one entry). `GET /stream/playlist/{id}.m3u` is the same thing in the
  format external players read.
- Part titles come from the FILENAME ("Kristen Scott 2"), which is the only
  description of a part that exists. There is no per-file metadata and no
  index column; "Part 1" would throw away the one real label.
- Durations are never probed to build the list -- a six-part release would mean
  six ffprobe runs against remote URLs before the player could draw anything.
  They appear only where `media_metadata` was already filled in.
- External players get `/Videos/{guid}/playlist.m3u`, and every ENTRY carries
  the same play-session token the playlist request authenticated with. The
  player fetches entries itself with no cookie and no key, so an entry without
  the token means a playlist that opens and whose every track 401s. `.m3u`
  earns its place in the Android chooser exactly the way `.mp4` does.

## Only one file of a multi-file release reached the library

The playlist feature above was correct and had nothing to show. "Mistress
Maitland" (Deeper) is a 16.3 GiB torrent of four scenes; item 874 held ONE
4.1 GiB file -- `DEEPER_101429`, the last one in TorBox's listing order --
with the other three downloaded, mounted and unreachable. Reported as "the
player shows a 4 GB file and no way to reach the rest", which reads like a
player bug and is not one.

`Downloader._update_attributes` creates one `MediaEntry` per file and is
called ONCE PER FILE by `update_item_attributes`. Its non-candidate branch
opened with `item.filesystem_entries.clear()`, so every file deleted the file
before it. Nothing downstream could recover: the loss happened before anything
was persisted, so `media_parts` grouped a set of one, `/stream/parts` answered
with one entry, and the parts panel and the `.m3u` hand-off both correctly
declined to offer a playlist for a single-file title.

- `entry_selection.stale_entries()` is the rule now, and it lives in its own
  import-free module because the downloader package cannot be imported without
  a settings file, a database and RTN -- a rule in there is a rule nothing can
  test. Exactly two things are replaced: files of a DIFFERENT release (which
  is what the `clear()` was for, and must keep working, or `media_parts`
  groups on an infohash that is no longer active), and an existing entry for
  the SAME filename (so re-processing replaces rather than doubles).
- The candidate branch (`keep_existing=True`) had the same bug in a quieter
  form: its deactivation loop had no condition, so each file deactivated the
  sibling added just before it and a multi-file candidate ended up with one
  live file out of however many. It now deactivates only OTHER releases.
- TRAP when auditing this: an item whose files were lost this way looks
  completely healthy. `last_state` is Completed, the VFS mount works, playback
  works. The only symptom is that the torrent's size and the item's file size
  disagree. `select fe.media_item_id, count(*) from "FilesystemEntry" fe join
  "MediaEntry" me on me.id = fe.id group by 1` is how to find the survivors.
- Repairing an already-damaged item: `POST /api/v1/scrape/queue_release` with
  the SAME infohash re-pins and re-downloads it through the fixed code. Do NOT
  use `/items/reset` -- it blacklists the active stream, so the title comes
  back on a different release.
- Tests: `src/tests/test_entry_selection.py` (stdlib only), which also asserts
  the downloader still calls the rule and no longer clears per file.

### Re-pinning a release that is already active looped for ever

Found while repairing item 874 with `queue_release`, and it is a trap for
anyone doing the same repair. `downloading_stream_hash` means "a release is
pending", and `db_functions.retry_library` hands any item carrying one back to
the pipeline -- on purpose, so a pinned fetch survives a restart. Only the
CANDIDATE branch of `Downloader.run` cleared it, and `candidate_mode` is False
exactly when the pinned hash is already `active_stream.infohash`, which is what
pinning the same release a second time produces. So the item downloaded, kept
its pin, was handed straight back by that query, and downloaded again -- every
three seconds, indefinitely, reporting itself **Completed** the whole time.
`entry_selection.pin_satisfied()` now clears it, and only when the pinned
release is the one now active: a pin naming a different release is a candidate
fetch that has not happened yet, and clearing it would abandon the download.

### Known and NOT fixed: ordering, and only part 0 in the VFS

Both are designed but unbuilt -- see `design/MULTIFILE-RELEASES.md`, which
carries the part-ordering rules (measured over all 251 torrents in the TorBox
account), the VFS naming proposal, upstream's status, and the one damaged
title still awaiting repair (872, Island Fever 3, playing its trailer).

`media_parts` orders by filename, and `parts[0]` is both what plays first and
what the VFS mounts. Right for numbered scenes, wrong for a release with
extras.

#### Only part 0 of a release is in the VFS

`RivenVFS.add()` registers `item.media_entry`, which is `media_parts[0]`, so
the other parts are never mounted; and `naming.generate_clean_path()` builds
the filename from the ITEM, so all parts of one title would collide on a single
path even if they were. Item 874 has four playable parts and one file under
`/movies`.

This does not affect playback. The stream endpoints resolve a part straight
from the debrid provider (`playback_url.resolve(..., part=)`) and never consult
the VFS, so the web player, `/stream/parts`, the parts panel and the `.m3u`
hand-off all serve every part -- verified on 874: four parts, four distinct
durations and sizes, all streaming 206.

It is left alone deliberately. Naming per-part files means choosing a
convention a media server will read, and Jellyfin's multi-part convention
(`- part1`, `- cd1`) STACKS files as segments of one film, which a scene
compilation is not. See "Media-server masquerade": scanning the debrid VFS is
already the path this fork does not take.

## The request button: what the entry matcher was getting wrong
Audited by resolving 50 matched `CollectionEntry` rows against the live TPDB
record their `tpdb_id` names (2026-09-11). 47 of 50 landed on the right title.
The three that did not were three separate holes, all in
`program/services/awards/matching.py`, and all now closed and covered by
`src/tests/test_awards.py`:

- **The season was never read.** "Girlcore: Season 1" matched "Girlcore Season
  Two: Volume 1" at a score of 9.0. Both titles contain a 1 -- the entry's
  SEASON and the candidate's VOLUME -- and the volume check compared those two
  numbers and found them equal. `extract_season` is now a separate axis, and it
  reads number words ("Season Two") because series spell seasons out and number
  volumes in digits.
- **An unnumbered entry matched any instalment.** The volume check only fired
  when BOTH sides named a number, so "Black Ass Addiction" agreed with "Black
  Ass Addiction 5". Whether such a pair was accepted came down to title LENGTH:
  the identical shape was rejected for "Trans-Visions" (2 tokens) and accepted
  for this one (3), because the extra token moved `title_symmetry` either side
  of 0.7. Now `instalment_only_on_candidate` -- but volume 1 is excluded, since
  a first instalment is routinely written both ways.
- **Title plus year alone was a match.** The weights are tuned so a title tops
  out at 5.0 against `ACCEPT_SCORE = 6.0`, but the year bonus is worth exactly
  the missing 1.0 -- so an entry corroborated by nothing but a plausible date
  landed precisely ON the bar. That is how "Big Wet Asses" became "Big Black
  Wet Asses". With no studio and no cast agreeing, the titles must now be
  token-identical.

TRAP when tuning any of this: **most correct matches score exactly 6.0**, with
no studio and no cast, because catalogue entries usually carry neither. So
raising `ACCEPT_SCORE`, or demanding studio-or-cast outright, trades three wrong
matches for dozens of missing ones. The gates above were chosen because,
measured over those 50, they reject only the wrong ones.

Known and NOT fixed, because both are genuinely ambiguous rather than wrong:
an AVN performer-category entry whose title is a bare performer name ("Abella
Danger" -> "Danger: Abella"), and word-vs-digit tokens ("Season Two" and
"Season 2" are 67% alike, so that pair fails the title gate).

## Seeing what a manual scrape filtered out
`GET /api/v1/scrape?include_filtered=true` also returns the releases the adult
matcher REJECTED, each flagged `filtered` with `filter_reason` (the evidence
verbatim) and ranked on the SAME scale as the accepted ones, so a close call
reads as one. The Settings-free toggle is on the manual scrape results
toolbar.

- Worth having because `manual=True` already bypasses every other filter, so
  `_filter_adult_torrents` is the only thing left that can drop a candidate --
  and an empty pick list gave no way to tell "nothing was found" from
  "seventeen were found and discarded".
- They are still PICKABLE. The point is to let someone overrule a filter they
  can see, not to hide it better. They sort below every accepted release
  (`byRankAcceptedFirst`), so they can never be chosen by accident.
- TRAP: `Stream.filtered` / `Stream.filter_reason` are **`ClassVar`**, not
  columns. A bare `filtered: bool` annotation on a declarative class is read as
  a column declaration and raises `MappedAnnotationError` at import time -- the
  whole app fails to start, not just the scrape path.
- `_filter_adult_torrents` now returns three values (kept, evidence, dropped).
- Toggling the checkbox RE-SCRAPES. The rejected set is built server-side and
  was never sent, so there is nothing on screen to filter.

## Multi-part playback: two bugs, both outside the playlist code
The playlist feature below was correct; these were URLs that forgot the part.

- **The HTML player played part 0 for every track.** Every stream URL in
  `video-player.svelte` carries `partQuery` except the CDN hand-off, which
  fetched `/api/stream/{id}/direct` bare -- and the backend defaults a missing
  `part` to 0. So a six-scene release offered six entries and played the first
  one six times. Only when direct play was available, which is why it looked
  like the player ignoring the track list. Verified on item 862 (Half His Age):
  `/stream/direct/862?part=N` returns a different CDN URL per part, and
  `playback_info` a different duration and size, so the backend was never at
  fault.
- **The external player got one file.** Both native bridges
  (`openInExternalPlayer`, `playDirect`) address a video by Jellyfin ID, and an
  id names ONE stream. The playlist URL was built and fetched, then discarded
  in favour of the id. `openInExternal()` now drops the id when
  `external_url` reports `parts > 1`, so the hand-off goes over as
  `/Videos/{guid}/playlist.m3u` -- the only form that can carry a whole
  release.

## The lock screen hid the way out of the app
`html[data-app-locked] body>*` hid `#mux-launcher`, the back-to-apps button the
multiplexer injects into every page it proxies -- so someone who could not
remember the PIN was stranded on a television with no keyboard and no other
control. "Sign out instead" is not the same thing: it ends the session rather
than switching apps.

- The cover stylesheet in `hooks.server.ts` now exempts `#mux-launcher`, and
  the lock overlay drops to `z-index: 2147483646` so the launcher (2147483647
  while locked) is above it. Equal values would leave the winner to DOM order,
  and both elements are appended to `<body>` by scripts that race.
- It gives nothing away: the button navigates AWAY from the locked app, shows
  no content, and coming back re-locks because the stamp it reads is stale.

## Two silent failures in the TPDB manual match picker
Both had the same symptom -- "no results for anything" -- and either alone
was enough:
- The endpoint's `type` is a Literal of PLURALS (`scenes`/`movies`/
  `performers`/`sites`). The frontend sent `type=movie`, which FastAPI
  rejected with a 422 before TPDB was ever called.
- `/tpdb/search` answers with a BARE ARRAY. The component read
  `payload.results ?? payload.data ?? []` off it, got undefined twice, and
  rendered "Nothing found" over a good list of candidates.
The picker now searches BOTH collections: `IndexerService` resolves a tpdb_id
by trying `get_scene` then `get_movie`, so either kind of record is a valid
answer, and a library title that is really a scene was otherwise unmatchable.

## The tube-site scrapers are an add-on now

They were `program/services/directscrapers/`, `routers/secure/direct.py`,
`settings.direct_scraping` and a separate `riven-tpdb-scrapers` repo of plugin
files. The feature is now
[riven-addon-tubescraper](https://github.com/gauravsuman007/riven-addon-tubescraper),
including the twenty scrapers, which ship in that repo's `scrapers/` folder.
**The `./plugins` bind mount is gone from docker-compose.yml**: a scraper
change used to be a commit, an `scp` to that folder and a rescan -- three
places to get out of step -- and is now a commit plus Update in the add-ons
page. On the server the old folder is `plugins.superseded-by-tubescraper-addon`
(verified byte-identical to the archived repo before it was set aside).

TRAP, hit during the migration: **`settings.direct_scraping` did not survive.**
Extracting a feature moves its subtree to `settings.addons.<key>` and the old
one is dropped by `AppModel` validation on the first save, so any scraper that
had been disabled and any custom site order came back at their defaults, with
nothing reporting it. Exactly what happened to `onlyfans.enabled`. Check and
re-set anything that was not a default before extracting a feature, because
afterwards the old values are unrecoverable -- the only backup on the server
predated the feature entirely.

### The host owns no scraper code at all

Not the scrapers, not the ranking, not the `DirectScraper` contract. Every
copy of that contract is an add-on's, vendored as `<package>/scraper_api/`.

The first attempt kept the contract here as `program/services/scraper_plugins`
on the grounds that two add-ons share it. That was overruled: it exists only
to serve add-ons, so it belongs to them. **The intermediate state is also
instructive** -- moving it out the first time without keeping a copy broke the
OnlyFans add-on at startup (`No module named 'program.services.directscrapers'`),
because add-ons cannot import each other; either can be removed underneath the
other.

So both add-ons carry an identical copy, and **the copies are checked rather
than trusted**. `scraper_api/drift.py` compares this add-on's copy against
every other add-on's on the deployed machine, where both are installed side by
side, normalising only the owning package name. Each add-on's suite calls it,
and skips when it is installed alone.

That check is not optional bookkeeping. `_RoutedSession` lives in that package
and is where the VPN proxy is applied, so two copies that disagree break
nothing visible -- one add-on's scraper traffic simply starts leaving from the
wrong address. To change the contract, edit riven-addon-tubescraper (the
canonical copy) and run `scripts/sync-scraper-api.sh` from the other repo.

`test_vpn.py` now asserts only the boundary: no scraper package under
`program/services/`. The routed-session assertion itself lives with each copy.

### The host's half of playback

The frontend's `direct-play/[token]/[file]` route and `lib/server/bookmarks.ts`
stayed: they are the HOST's player infrastructure -- minting a URL an external
app can open, and filling in the overlay's description. They call the add-on
through `TUBE_API` in `lib/addons.ts`, the single place that knows its key.

If the add-on is not installed its API answers 404 and those paths degrade the
way they already do for an unreachable backend, and **the section on a title's
page does not appear at all** -- see the slots note below.

## Add-ons

Self-contained features the host loads from `/riven/addons`, one folder each.
`program/addons/` is the whole framework: `contract.py` is what an add-on is,
`loader.py` finds and starts them, `database.py` owns the schema-per-add-on
rule, `installer.py` clones them from git, `mounting.py` attaches their routes
to the running app. The management API is `routers/secure/addons.py`; the UI is
Settings → Plugins → Add-ons.

The OnlyFans performer index used to live in this repo and is now
[riven-addon-onlyfans](https://github.com/gauravsuman007/riven-addon-onlyfans),
which also absorbed the `onlyfans_scrapers` repo. Its traps live in that
repo's AGENTS.md.

### The rules that make it work

- **The folder name is the identity** — Postgres schema, route prefix and
  settings key. `manifest.key` must equal it or the host refuses to load,
  because the add-on's data would land somewhere its own uninstall would not
  look.
- **One Postgres schema per add-on, `alembic_version` included.** That is what
  makes `DROP SCHEMA <key> CASCADE` a complete uninstall. It is also why each
  add-on runs an INDEPENDENT alembic chain rather than a labelled branch off
  the host's: branches share one version table, so removing an add-on would
  leave a head alembic cannot resolve and the host would refuse to start, with
  nothing naming the folder that was deleted.
- **An add-on may reference host tables; the host must never reference an
  add-on's.** A host foreign key into the add-on's schema would be dropped by
  that CASCADE, turning a clean uninstall into a silent change to the host's
  own schema.
- **Add-on routes are `/api/v1/x/<key>`, not `/api/v1/addons/<key>`.** The
  latter is the management router's own prefix, and `mounting.detach_all`
  clears by prefix — the two overlapping would make removing an add-on delete
  the endpoints that manage add-ons.
- **Routes are mounted AFTER `Program.start()`**, in `main.py`. The registry is
  filled during start, so mounting at import time would publish nothing.
  FastAPI matches by walking a list per request, so adding routes to a live app
  genuinely works; the OpenAPI cache is the only thing that needs clearing.
- **`settings.addons` is an untyped dict, and `/settings/schema` splices the
  add-ons' own schemas into it.** `AppModel` is built at import time and
  add-ons are found long after, so there is nothing to generate a typed field
  from. The splice is what gives an add-on a real settings tab for free.
- **Installing an add-on runs someone else's code as Riven.** There is no
  sandbox, and the UI says so. The installer validates a temporary clone
  before anything is moved into place, which prevents accidents, not attacks.

### Traps found building it

- **`SET search_path` survives the connection pool.** An add-on migration sets
  it so a migration without an explicit `schema=` still lands in the add-on's
  schema. On a pooled connection the next borrower inherits it -- and the next
  borrower was the host, which died with `relation "MediaItem" does not exist`
  seconds after an add-on reported loading fine. Migrations run on a throwaway
  NullPool engine and use `SET LOCAL`; both guards are deliberate.
- **`str(engine.url)` redacts the password.** A migration config built from it
  reconnects as nobody and fails with a bare "password authentication failed"
  naming the add-on rather than the mistake. Use
  `render_as_string(hide_password=False)`, or hand alembic a live connection.
- **Never `from main import app` in a request handler.** It re-executes main.py
  in that worker thread and dies installing uvicorn's signal handlers. `main`
  binds the app into `mounting` at startup instead.
- **Forgetting an add-on's modules cannot be done by name.** Only the entry
  point is named after the key (`riven_addon_<key>`); everything it imports
  from its own folder is named by whatever the author called the package --
  `onlyfans` ships `onlyfans_addon`. Dropping the entry point alone made
  "update" look like it worked: the new `riven_addon.py` ran, imported the
  STALE cached package, and every line the update changed stayed unchanged
  until the next restart. The loader records the modules an add-on introduces
  at import time (`_introduced_modules`, containment-checked against the
  add-on's folder so it cannot unload a host dependency) and forgets those --
  for failed and disabled add-ons too, since "disable, update, enable" is the
  obvious way to update something.
- **`refresh_addon_jobs` must not check `scheduler.running`.** It is called at
  the end of `_schedule_functions`, which runs before `scheduler.start()`, so
  that check silently skipped every add-on job on every startup. Its
  registration line is INFO for the same reason: jobs that never registered and
  jobs that run and find nothing look identical otherwise.
- **`git` has to be in the runtime image** or every install fails with a
  FileNotFoundError whose entire message is `git`.
- **Extracting a feature resets its settings.** Its subtree moves from
  `settings.<key>` to `settings.addons.<key>`, and the old one is dropped by
  `AppModel` validation on the first save. Re-set anything that was not a
  default -- `onlyfans.enabled` came back false and the index quietly stopped.

### Slots: an add-on inside the host's own page

A page is the right shape for an add-on that owns a screen. It is the wrong
shape for one whose whole interface belongs *inside* something the host
already renders -- a panel on a title's page cannot be a route without tearing
the page in half. `AddonManifest.slots` is that second shape.

- **The vocabulary is the HOST's.** Two slots exist: `details` (a section on a
  title's page) and `settings` (beside the generated form in that add-on's own
  settings tab). An add-on naming a slot the host does not offer contributes
  nothing and is NOT an error -- the host must be free to retire a slot, and an
  add-on built against an older host has to degrade to "that section does not
  appear" rather than to a broken page.
- **The bundle exports `slots`, keyed by name**, rather than a default mount
  function. One bundle can therefore serve a page and several slots without the
  host guessing which it was handed.
- **Nothing is fetched unless a page with that slot renders**, and
  `$lib/addon-slots.ts` memoises the add-on list as the in-flight PROMISE, so
  several slots on one page share one request rather than racing.
- **A failed lookup is not cached.** Caching "there are no add-ons" because the
  backend was briefly unreachable would hide a working add-on's section until a
  full reload.
- **`AddonSlot` renders no wrapper until something mounts** -- `display: none`,
  not an empty div. A zero-height element still occupies a grid cell and still
  collects the surrounding layout's gap, which is how "the section does not show
  up" quietly becomes "the section is invisible and the page has a hole in it".
- **The settings slot is scoped with `only={key}`.** Without it every add-on's
  settings panel would render in every add-on's tab, since that tab is rendered
  once per add-on and the slot is otherwise filled by all of them.
- **`apply()` in `addon-control.svelte` calls `resetAddonSlots()`.** It is the
  one choke point every mutation passes through; without it, disabling an
  add-on leaves its section on every title's page until a reload, which reads
  as "the toggle did nothing".
- **The host bridge is one function, `host.play()`**, and should stay that way.
  Anything reachable over HTTP the add-on fetches itself. The player is the
  exception because it is in-page state owned by this app's Svelte runtime and
  the add-on's bundle carries a different one.
- TRAP: the generic add-on passthrough had to learn `x-accel-buffering` and
  `connection`. The tube scraper's search is server-sent events, and the
  dedicated proxy it replaced set both explicitly. Without them a buffering
  reverse proxy added later would hold every frame and deliver them together at
  the end -- which is exactly what streaming existed to avoid, and looks like a
  slow backend rather than a dropped header.

### The frontend side

- **`/x/[addon]/[...rest]`** dynamically imports the add-on's prebuilt
  `addon.js` and hands it a DOM node. The add-on brings its own Svelte runtime;
  two runtimes are only a problem if they share a component tree.
- **`api/v1/x/[...path]` is a byte passthrough** and had to exist. The generic
  `api/[...backendProxy]` ends with `json(await response.json())`, which
  decodes the body, drops the content type and cannot do Range — it was
  silently 500ing OnlyFans video playback long before add-ons existed, and
  would equally have broken every add-on bundle, stylesheet and proxied image.
- **Sidebar entries and settings tabs are data**, read from `/api/v1/addons`.
  Only the icon is resolved locally, since an icon is a component.


### An add-on can reach the television, as data

`riven-tv` is a second, JavaScript-free renderer for sets running engines from
about 2016. It **cannot run an add-on's `ui/addon.js`** any more than it can
run the frontend's own bundle — dynamic `import()` is Chromium 63 and that
target is 53 — so there is no version of "run the add-on's UI" on that
surface, now or later.

`AddonTv` on the manifest is how an add-on says what a television may draw
for it instead: `browse` for a screen of its own (`tv/browse`, `tv/detail`,
`tv/play`), `title` for a section inside the television's own title page
(`tv/title`). Both default off, because a TV screen is a second renderer to
keep working and most add-ons do not want one. The shapes are documented in
`docs/tv.md` in each add-on repository.

The host's part is only to carry the declaration: `loader.py` reads it,
`/api/v1/addons` reports it, and the frontend's `/api/tv/shell` filters it
down to what is installed, enabled and loaded before the television sees it.
**The host owns none of the shapes** — it neither validates nor renders them,
which is the same boundary it keeps around a slot an add-on names but does not
fill.

A flag is not a guarantee the endpoint works. `riven-tv` treats a missing or
malformed answer as "that section does not appear", never as an error, so an
add-on is free to be newer than the television it lands on.

## Rails: the host stores the ORDER, never the rows

A page's rows are arrangeable -- Home, Explore, and each add-on's own page --
and the arrangement lives in `RailLayout` (`program/rails`), served by
`/api/v1/rails/{page}` where page is "home", "explore" or "x/<addon>".

**The table holds keys and nothing else.** A rail's title and endpoint travel
with the code that draws it: `$lib/tv/manifest` for the frontend's own home
rows, the recommendation engine for Explore's ranked rows, `Addon.rails()`
for an add-on's. Store a title beside the order and a retitled row keeps its
old name on every deployment that ever saved a layout -- and the stale copy
is the one on screen.

Three rules, and each one has already been the bug:

* **Empty is not "everything off".** No saved layout means *never arranged*,
  which the surfaces read as "use your own defaults" and which picks up rows
  added by later updates. A layout that exists with every row off is a
  decision and is honoured forever. `DELETE /api/v1/rails/{page}` is the only
  way back from the second to the first.
* **A missing rail is skipped, never pruned.** Disable an add-on and its rows
  leave every page while their positions stay; re-enable it and they come
  back where they were. Verified live.
* **A new rail turns itself on.** `arrange()` appends catalogued rails the
  layout has never heard of -- otherwise every row an update adds is
  invisible to exactly the people who arranged their pages.

`arrange()` filters by page; `forEditing()` deliberately does NOT. Home and
Explore offer the whole catalogue, because offering every installed row is
what "add a row" means there; an add-on's own page is narrowed by passing
only its own rails in. Getting this backwards put OnlyFans performer rows on
Home uninvited -- fixed, but the shapes are one line apart.

**An add-on's rail endpoint answers CARDS, not the add-on's own shape:**
`{"items": [{id, title, subtitle, image, action}]}`, `action` being "open" or
"play". No card carries a URL -- this app addresses a performer as
`/x/onlyfans/<id>` and the television as a session-prefixed path with the id
in a query string, so a card naming one would be wrong on the other. It is
also a safety property: an add-on is third-party code, and a card that could
name a link would be naming it on a page carrying the viewer's session.

## Add-on capabilities are inferred, except the one that matters

`/api/v1/addons` reports "settings", "api", "jobs", "rails", "database",
"tv", "slots" by CALLING the add-on -- it has settings because
`settings_model()` answered. A manifest field restating that would be a
second truth able to disagree with the first, and the badge would be drawn
from the wrong one.

`"scrapers"` is declared, and has to be: the host owns no scraper code at
all. It is also the only capability with a consequence rather than a label --
it is the claim that this add-on's outbound traffic belongs in the tunnel the
VPN tab configures. What actually enforces that is `_RoutedSession` in the
shared `scraper_api`, and `tests/test_tube_scrapers.py` asserts every bundled
scraper goes through it and asks the VPN per purpose. The badge is the claim;
that test is the enforcement.

## A rail card opens the library's copy, when there is one

A recommendation is a `CollectionEntry`, and an entry links to a `MediaItem`
only when it was requested **through Riven** (`media_item_id`). A title that
reached the library any other way is never joined. Adult Empire's
`self_sourced` rows — 551 of them — make that visible: they carry title,
studio, year and cast, so they are requestable without resolving a TPDB id
and never acquire one, and the card for a film already in the library opened
the storefront listing it was mirrored from.

`library_links()` in `recommendations/engine.py` builds a folded-title index
of `MediaItem` and stamps `library_item_id` / `library_tpdb_id` onto each
recommendation. Three rules, each of which was a decision:

- **Nothing is written.** This decides where a card points, not what the
  library and the catalogue believe about each other. In particular it does
  not set `media_item_id`, which would remove the title from every rail —
  `rank_many` filters on it.
- **Never into `tpdb_id`.** That column is what the request path reads;
  filling it from the library would make a self-sourced entry look matched.
- **An ambiguous name matches nothing.** Name is the whole evidence: the
  storefront's year and TPDB's release date disagree by years on anything
  re-released (Cheerleaders is 2007 on one, 2014 on the other) and
  self-sourced rows have no cast. Two library items called *Family Cheaters*
  means this cannot say which, and the wrong film is worse than the
  storefront page it would replace.

`_name_key()` (letters and digits only) is deliberately **not** `_fold()`.
`_fold` is what collapses duplicate rows into one recommendation, and
widening it would silently merge titles the rails currently keep apart.

Tests: `src/tests/test_recommendations.py`.

### ...and so does every OTHER surface, through one function

The first version of this fix reached the Explore rails only. It was written as
a `href()` in `explore/+page.svelte` that checked the library fields before
falling through to `entryHref()` -- so the bestseller and trending shelves, the
home hero, the studio pages and the brochure page's own redirect all kept the
old answer, and the same owned title opened the storefront from any of them.
That is the shape to watch for: a rule about WHERE A TITLE LIVES, implemented
at one of the five places that draw a title.

There is now exactly one decision, `entryHref()` in `lib/collections.ts`, and
every surface routes through it. Its order is: the library item's own ids
first (`library_tpdb_id`, then `library_item_id`), then the entry's
(`tpdb_id`, then `media_item_id`), then the brochure page. The library's ids
are never mixed with the entry's -- an owned title whose media item carries no
TPDB id belongs on its riven page, because the entry's TPDB record would render
a page that cannot find the copy you own and offers to request it again.

- Backend: `CollectionEntryResponse` carries `library_item_id` /
  `library_tpdb_id`, stamped by `_entry_response` from the index
  `library_links()` builds. **Every** entry-serving endpoint passes it --
  collection detail, brochure shelves, the AVN overview and the single-entry
  lookup -- because each one feeds a card somebody clicks, and one left out is
  one surface still opening the storefront. `engine.link_for()` is the shared
  fold; do not write a second one.
- `explore/brochure/[id]/+page.server.ts` asks "is this page still the right
  one for this entry?" by comparing `entryHref(entry)` against its own URL,
  rather than re-testing `tpdb_id`. Re-deciding it locally is exactly how this
  page kept its old answer while every card linking to it learned a better one.
- Studio rows need nothing of their own: a studio title is promoted to an
  entry and then redirected through that same page.

## A wrong match must not be able to destroy correct artwork

"Pirates" (Digital Playground, 2005) sat in the library wearing the cover of
"Butthole Pirates" (Heatwave) -- with the RIGHT TPDB id beside it, which is
what made it invisible for weeks. Three separate rules had to be wrong at once,
and each is now enforced:

1. **`enrich_entry` REPLACED the storefront's poster with the match's.** A
   storefront row's cover is the cover of that exact product id and is right by
   construction; a match's is right only if the match is. It gap-fills now.
   The damage was permanent in practice: `build_movie` copies the entry's
   poster onto the MediaItem, and `AdultEmpireIndexer._apply` only ever fills
   gaps, so no later re-sync of the entry could undo it.
2. **The matcher accepted "Butthole Pirates" for "Pirates".** Fixed earlier by
   `MIN_TITLE_SYMMETRY` (see the matching section); containment alone cannot
   tell a film from someone else's parody of it.
3. **Re-matching corrected the id and kept the picture.** `if match.poster:` --
   the obvious form -- leaves the previous provider's artwork in place exactly
   when the new record has none, at the moment the id beside it changes. That
   is now `apply_match_poster()` in `awards/matching.py`: a poster from a
   metadata CDN the new record does not vouch for is CLEARED, and enrichment
   puts the storefront cover back in its place. A poster from anywhere else is
   left alone -- it was never the match's to begin with. The manual endpoint
   `POST /items/{id}/tpdb` already made this decision by hand; this is the same
   rule applied to the automatic path.

Tests: the poster rules in `src/tests/test_awards.py` and
`src/tests/test_brochure_tpdb_first.py` (which also asserts the shipped
`enrich_entry` still gap-fills, because that suite mirrors its body).

## Seeking a proxied file got it throttled by TorBox

Reported as "played fine, then too much seeking and it stopped with an error"
from an external player. The chain, all measured 2026-09-14 on item 871:

1. An external player seeks by opening a **new** range request and abandoning
   the old one (VLC also opens one for `moov` at the tail). Its URL is
   `/Videos/{id}/stream.{ext}` -> frontend -> `/api/v1/stream/file` -> CDN,
   so each seek was a new connection from this server to TorBox's CDN.
   `direct_debrid_handoff` does not change this: it is used by the in-page
   player only.
2. TorBox's CDN limits connections and request rate **per file** and answers
   **429**. It kept refusing that file for ~40 minutes, to a single request with
   nothing open, and to a **freshly minted link** for the same file, while
   other files on the account served 206. So a 429 is about the file, not the
   link or the account.
3. `/stream/file` re-minted on any status `< 500` -- 429 included -- and
   `playback_url.verify` treated 429 as dead, so the HLS/remux routes (which
   verify on **every** playlist and segment request) re-minted too. Each
   refusal cost a TorBox API call plus another CDN request on a file already
   over its limit.

The fix, in `program/services/streaming/upstream_guard.py`:

- **Per-file connection cap, newest wins** (`stream.max_upstream_connections_per_file`,
  default 2). At the cap the *oldest* upstream connection is closed: the
  player that seeked has stopped reading it, and refusing the new request
  would stall the seek. The iterator races each read against eviction, or an
  unread response holds the CDN socket until a timeout.
- **429 is a cooldown, never a re-mint.** Recorded per file; the next request
  gets `503` + `Retry-After` without touching the CDN. Honours the CDN's own
  `Retry-After`, else 30s doubling to 10 min, cleared on success. The
  frontend forwards `Retry-After`; dropping it makes a player retry at once.
- `resolve(check=True)` raises `ProviderThrottled` on 429, and the checked
  routes map it through the same throttle, so every route stops together.
- `verify` results are cached 60s. HLS was sending the CDN a probe per segment.

**Do not "simplify" 429 back into the re-mint branch.** It is the one status
below 500 that means *stop asking*.

Also: ffmpeg quotes its input URL in errors, and a TorBox URL carries the
**account API key** as `?token=`. `Remux failed`, `HLS session ... exited`,
and the mint warning all logged it in the clear; all three are redacted now.
See [[debrid-token-log-leakage]] in memory.

Tests: `src/tests/test_upstream_guard.py` (stdlib-only).

## Keep on disk
- `POST /api/v1/keep/{id}` copies a title's active file to
  `filesystem.local_download_path` (bound to `./downloads` on the server) and
  tracks it in a `LocalCopy` row: Queued / Syncing / OnDisk / Failed, with
  bytes so far. `DELETE` stops it and removes the file. An empty path disables
  the feature and the frontend hides the button.
- The copy reads through the **VFS mount**, not the provider: the VFS already
  re-mints spent links, honours VPN routing and shares its chunk cache with
  playback. A second download path would reimplement all three and drift.
- Resumable via a `.part` file; a restart re-queues anything left Syncing.
- TRAP: a new optional setting must NOT default to `None`. `save()` writes with
  `exclude_none=True` and `check_environment` only walks keys already in the
  file, so a None default is never serialized and its `RIVEN_*` variable is
  read by nothing. Use `str` with `""`. Even then a brand-new setting needs
  **two restarts** to take from the environment: the first writes the key.

## Direct debrid playback
- `GET /api/v1/stream/direct/{id}` hands the player the provider's CDN URL so
  video does not cross this server twice. Verified against TorBox: not
  IP-bound, CORS reflected, range requests honoured (a seek 2 GB in works).
- **Off by default** (`stream.direct_debrid_handoff`). TorBox embeds the
  ACCOUNT API KEY in that URL as `?token=`, and has no scoped or ephemeral
  alternative — `user/refreshtoken` rotates the real key. Enabling it hands
  the key to every device that plays.
- TRAP: probe the TorBox API from **inside the container**. Cloudflare answers
  plain `urllib` with 403 "error code: 1010" on browser fingerprint, which
  reads exactly like a revoked key.

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
- **Only movie categories reach the page.** Almost every AVN category names a
  work somewhere -- "Best Actor" is awarded *for* a film, so parsing one yields
  a real title -- which is why `is_media` being `bool(title)` let several
  hundred person awards onto the page. Two gates now run against the *category*,
  in order: `PERSON_AWARD` rejects person and craft awards outright, then
  `WORK_CATEGORY` requires a format noun (movie/film/video/feature/release/
  series/scene/tape/...). The order matters: "Movie of the Year" and
  "Best Sex Scene, Film (Couple)" are real movie categories, so "of the year"
  and "couple" are in neither gate and are handled by the work-noun requirement
  instead. Measured live: 544 of 855 categories and 1,776 of 2,792 winners
  survive.
- `sync_corpus` only ever *adds*, so tightening the gates needed
  `_prune_person_awards` as well -- a library that synced before the change
  would otherwise show Best Actor forever. Unlike the nominee prune it does
  *not* spare requested entries: deleting a collection entry never touches its
  MediaItem, so the film stays in the library and only its awards-page listing
  goes -- and sparing them would defeat the prune on exactly the ceremonies that
  have been synced longest, which are full of auto-requested Best Actor winners. `sync_corpus` returns early on an empty corpus so a
  Wikipedia outage cannot delete anything.
- `POST /collections/avn/enable` does two things and needs both: it saves the
  setting (so the switch survives a restart and matches Settings → Content →
  Awards) **and** calls `ProgramScheduler.refresh_content_jobs()`. Saving alone
  leaves a switch that reads "on" while nothing runs until the next restart.
- `refresh_content_jobs()` deliberately does *not* re-run `_schedule_functions`:
  every job that registers carries `next_run_time=now`, so a settings change
  would immediately fire the vacuum and the library retry as a side effect. It
  touches only the four awards/brochure jobs, adds or removes them, and drops
  the cached service instances (they read their settings at construction).

## Enabling content jobs from their own page
`/avn` and `/brochure` each have an enable button that posts to
`/collections/{avn,brochure}/enable`. Both go through `_toggle_content_job`,
which saves the setting **and** calls `refresh_content_jobs()`. AVN 409s without
a TPDB token because its titles are resolved against TPDB; the brochure does not,
because Adult Empire supplies studio, year and cast on its own.

`/collections/brochure/status` exists because empty shelves are ambiguous:
switched off and switched-on-but-not-yet-synced need different things said.

## Settings tabs
`content` is its own **Content** tab in the frontend settings page, not a
sub-section of TPDB. It was under TPDB before, which made every "Settings ->
Content -> ..." pointer in the UI a dead end. Tabs are a presentation layer over
one form -- inactive panels stay mounted and hidden with CSS, because a field
that is not rendered is dropped from the submitted payload.

## Silent traps in the adult scrape path
- **`MediaItem.__init__` must read every id column it declares.** `adultempire_id`
  was a column, and `is_adult` read it, but the constructor never assigned it
  from the payload -- so `Movie({"adultempire_id": ...})` looked completely
  normal and carried no id. `is_adult` then returned False, which sent brochure
  titles to the *mainstream* indexer categories and skipped the adult relevance
  filter: a manual scrape for "Pirates" returned five Pirates of the Caribbean
  films and no adult release at all. `src/tests/test_mediaitem_ids.py` guards
  the whole class of bug by reading item.py with `ast`.
- **An adult item searches XXX *instead of* the indexer's Movies categories**,
  not in addition. A one-word adult title collides with mainstream cinema
  constantly and the mainstream categories are far larger, so searching both
  buries the real matches. `select_category_ids` falls back to the type
  categories for indexers that expose no XXX category at all, or an adult-only
  tracker that Prowlarr mapped to "movie" would search nothing.
- **A brochure title has one row per shelf, and they are not interchangeable.**
  Only shelves that have been through the detail-enrichment pass carry studio,
  cast and release date; a row first seen in an unenriched shelf holds nothing
  but a title. Picking whichever row came back first handed the scraper a Movie
  with no site, no cast and no year, so the relevance filter had no evidence and
  rejected everything -- the manual scrape for "Pirates" came back with *zero*
  results, which looks like a broken scraper rather than a bad row. Always go
  through `adultempire_indexer.best_entry`, which orders by how much metadata a
  row actually has. Duplicates are normal and permanent: "Pirates" exists in
  four shelves and only two are enriched.
- **`item_exists_by_any_id` must accept every id `EventManager.add_item`
  passes.** They live in different modules and drifted: `add_item` handed over
  five ids, none of which a brochure title has, so the duplicate check raised
  `ValueError("At least one ID must be provided")` and the Request button
  returned a 500. `test_mediaitem_ids.py` now diffs the two lists.
- The manual scrape shares one path with the TPDB one -- `resolve_media_item`
  then `scraper.scrape(item, manual=True)`. Only item *resolution* differs; the
  filtering and ranking are the same code. `manual=True` skips the mainstream
  season/year/country filters but **not** `_filter_adult_torrents`.

## One download path: TPDB first, storefront as fallback
- A storefront title is resolved against TPDB **at the boundary** -- when it is
  requested, or when it is manually scraped -- not later by a background
  enricher. A match makes it an ordinary TPDB item, so indexing, scraping and
  the detail page are all the same code the rest of the fork uses, with nothing
  storefront-specific left downstream.
- `tpdb_lookup.enrich_entry` is the single implementation, used by the request
  endpoint, `resolve_media_item`, and the user-collections service. It used to
  be duplicated in `collections/service.py`; do not copy it again.
- The resolved id is written back to the `CollectionEntry`, so the lookup costs
  one TPDB round trip per title ever, not one per request.
- **The fallback is load-bearing, not dead code.** Measured against the
  all-time bestsellers, TPDB confidently matches about four titles in five. The
  fifth is usually a bare one-word title ("Nurses") or a pre-1980 release,
  where the matcher correctly refuses to guess. Those titles still download
  from the storefront's own metadata -- studio, year and cast is exactly what
  the scrapers match on -- which is why `AdultEmpireIndexer` and `build_movie`
  remain. Removing them would make one bestseller in five undownloadable.
- Ordering is the thing to protect: read `entry.tpdb_id` *after* enriching, or
  every unresolved title silently takes the storefront branch.
  `test_brochure_tpdb_first.py` asserts the ordering in both routers.
- The brochure card links to the TPDB detail page once an entry has a
  `tpdb_id`, and to its brochure page otherwise (`entryHref` in
  `lib/collections.ts`). Requesting or scraping from a brochure page navigates
  to the library page once resolution succeeds.

## Picking a release that is not cached
- `start_session` exists to let the user choose files *out of* a torrent, so it
  needs the provider to already hold it. An uncached torrent has no file list --
  TorBox has not fetched its metadata and reports it as queued -- so the pick
  was refused outright. For adult content that is the common case, not the edge
  one: `_request_uncached` exists precisely because these releases are rarely in
  anyone's cache.
- `start_session` now answers **409** (not 400) when the release is simply not
  cached; nothing about the request was malformed. The UI falls back to
  `POST /scrape/queue_release`, which pins `preferred_stream_hash` and hands the
  item to the pipeline -- the same mechanism as the "switch to this release"
  button, so there is no second downloader path.
- `queue_release` rebuilds the Stream from `_manual_streams`, a bounded cache
  the scrape endpoints populate. The browser sends only an infohash; it never
  describes a release back to the backend. A pick made against a stale
  candidate list 409s and asks for a fresh scrape rather than inventing a row.
- A brochure title picked this way has never been requested, so it exists only
  as a transient Movie built from the cached entry. `queue_release` persists it
  before attaching the stream, since a stream cannot point at a row that does
  not exist.

### Replacing a release that is already downloaded -- THREE traps, all silent
Picking a different release for something already in the library reported
"Queued" and did nothing at all. Three independent faults, each sufficient on
its own, and none of which produced an error anywhere:

1. **`resolve_media_item` returns a DETACHED item.** It goes through
   `db_functions.get_item_by_id`, which calls `session.expunge(item)` -- its
   own docstring says so. Appending a stream, pinning `preferred_stream_hash`
   and unblacklisting all mutated an object the session had never heard of, so
   `session.commit()` wrote **nothing**. Re-query by id inside the session
   before touching a resolved item. Verified live: the endpoint answered 200
   while `preferred_stream_hash` stayed NULL and no stream was attached.
2. **`em.add_item()` is a no-op for anything already in the library.** It only
   emits when `item_exists_by_any_id` is false, because it exists to admit NEW
   content. For a library item it returned `False` and queued nothing, while
   the endpoint reported success regardless. An existing item must be handed to
   the Downloader directly with `add_event(Event("Downloader", id))`.
3. **`state_transition.py` overrode that event anyway.** The `States.Completed`
   branch routed EVERY completed item to post-processing, ignoring the service
   the event named -- so `Event("Downloader", id)` became a post-processing run
   every time, and the log read "Post-processing complete" while nothing
   downloaded. It now checks `downloading_stream_hash` first. Confirmed against
   a healthy 11-seeder torrent, so this was **not** a dead-torrent case.

Consequences worth remembering:
- `downloading_stream_hash` is what puts the downloader in **candidate mode**:
  it fetches the new release alongside the current one and swaps only on
  success, so a failed fetch never costs a working file. `preferred_stream_hash`
  alone does not trigger it.
- The downloads view filters on state, and an item being replaced stays
  `Completed`, so it was invisible there. `/items/downloads` now also matches
  `downloading_stream_hash IS NOT NULL` when no explicit state filter is given.
- A pending candidate fetch does **not** survive a backend restart: the event
  queue is in memory, and nothing re-emits it from the persisted hash on
  startup. The pin remains, so re-selecting the release resumes it.
- `FilesystemEntry` has **no `stream_infohash` column**, so `select_stream`'s
  "switch back to an already-downloaded release without re-downloading" branch
  can never match and will always re-download. Still open.
- Before blaming the code, check seeders. `_request_uncached` logs
  `(0% done, N seeders)`; a 0-seeder release is dead and no fix will make it
  download.

## Auto-requesting award winners
- `content.awards.auto_request_winners` defaults to **off**. A synced corpus is
  ~1,800 winners, so leaving it on made "enable AVN" mean "download a library's
  worth of titles", which is not what the button says.
- Turning it off is not just a scheduling change: `AwardsService.
  cancel_auto_requests()` cancels the jobs *and deletes* the unfinished
  MediaItems, because cancelling alone only pauses them and the next library
  retry picks the same items straight back up. It fires from the settings save
  (on the True -> False transition), from disabling the AVN section, and from
  `POST /collections/avn/cancel-downloads` for a backlog queued earlier.
- The sweep goes by `requested_by` **on the MediaItem**, never by walking
  `CollectionEntry.media_item_id`. A freshly auto-requested winner has no link
  to walk: `request_matched_winners` hands a transient MediaItem to the event
  manager and the row is persisted later by the pipeline, so the entry link is
  still null while the download is in flight. The first version walked entries
  and left 35 titles downloading after the source was switched off.
- It only touches items stamped `requested_by == "awards"` and only those not
  yet Completed/Symlinked, and skips anything with a `filesystem_entry` (that
  is already mounted in the VFS; `remove_item` is what tears those down). A title the user clicked Request on is stamped
  `"collections"` and survives; so does anything already downloaded, which is
  media they now own. The `CollectionEntry` always survives -- it is a catalogue
  row, so the title stays browsable and re-requestable.

## Direct-scrape matching and scraper plugins: moved

`ranking.py` (why a bare performer-name match is deliberately not enough, and
the containment/bloat gate that fixed 80% of falsely-confident matches) and
`plugins.py` (per-file error isolation, key collisions) went to
riven-addon-tubescraper with the rest. Both sections, including the measured
50-title study they came from, are in that repo's `AGENTS.md` and `README.md`.

Two findings from them that are about THIS repo and so stay here:

- **The stream proxy must send a browser User-Agent.** These sites gate their
  MEDIA handler the same way they gate their markup. Measured on x-x-x.tube:
  the identical resolved URL answers **500 to httpx's default agent and 206 to
  a browser's**. Maximally confusing because resolving succeeds -- it goes over
  the scraper's own session -- so the site looks reachable and only playback
  fails. The same applies to the OnlyFans add-on's `/stream` and `/image`.
- **`settings.direct_scraping` is gone from `AppModel`.** Extracting a feature
  moves its subtree to `settings.addons.<key>`, and the old one is dropped by
  validation on the first save. Anything that was not a default has to be
  re-set -- see the add-ons trap about `onlyfans.enabled` coming back false.

## TPDB search ordering
TPDB's `q` search returns matches in no useful order, ignores every ordering
parameter it accepts, and its page size is fixed at 20 whatever `per_page` says.
"pirates" put Digital Playground's Pirates -- an exact title match -- on page 2,
so a single-page UI never saw it. `/tpdb/search` now pools
`RELEVANCE_POOL_PAGES` pages and ranks them with `utils/search_ranking.py`:
exact title, then prefix, then contains, with token overlap only breaking ties
inside a tier ("Pirates" and "Butthole Pirates #4" both contain every query
token, so overlap alone cannot separate them).

## Shared TPDB lookup
`services/recommendations/tpdb_lookup.resolve_movie()` is the single two-pass
search-then-detail matcher. Both the brochure enricher and the collections
service call it. Do not re-implement it: scoring the flat `/movies?q=` records
directly leaves studio and cast unset, the score never clears `ACCEPT_SCORE`,
and nothing ever matches -- silently.

## Media-server masquerade (Jellyfin/Emby/Plex) -- analysis, not yet built
Full writeup in `docs/media-server-masquerade.md` (gitignored; regenerate from
this section if absent). Decisions so far, so they are not relitigated:
- The goal is television playback without writing six native clients: speak a
  protocol whose clients already exist, so riven-tpdb IS the server they
  connect to.
- TRAP, and it is the intuitive-but-wrong architecture: do NOT point a real
  Jellyfin/Plex at the VFS mount (which is what upstream's
  `services/updaters/` assumes). A library scan runs ffprobe over every file
  and extracts chapter images by seeking; `vfs/rivenvfs.py` `read()` serves
  those bytes by pulling 32MB chunks from the debrid provider. A scheduled
  scan is therefore a self-inflicted DoS on your own debrid account. It also
  discards the TPDB metadata (`performers`, `site_name`, `network`,
  `tpdb_id`) in favour of a TMDB/TVDB scraper guess against scene filenames --
  the same failure as the RTN title-check trap above.
- Jellyfin is the only viable target: open-source clients (so the required
  endpoint set can be MEASURED, not guessed), published OpenAPI, no cloud
  dependency, and codec negotiation where the client POSTs its own
  `DeviceProfile` to `/Items/{id}/PlaybackInfo`.
- As the server, be permissive on auth headers -- accept the modern
  `Authorization: MediaBrowser Token="...", Client=...` form AND the legacy
  `X-Emby-Authorization` / `X-Emby-Token` / `X-MediaBrowser-Token` headers and
  the `ApiKey` query param. We do not control which client build connects and
  TV apps update slowly; the 10.11 deprecation is a client-side concern.
- Plex is rejected on architecture, NOT content policy: server identity is
  anchored to plex.tv via a short-lived claim token with per-machine
  `*.plex.direct` certificates that cannot be minted, and Plex telemetry
  reports library data including adult flags. Plex's ToS does NOT ban adult
  content (it bans infringement and sharing/selling access) -- do not repeat
  that claim, it is wrong and checkable.
- Emby: unpromised side effect of the Jellyfin work (Jellyfin forked from Emby
  3.5.2, hence the `X-Emby-*` names). Closed source since 3.6, plus Connect and
  Premiere checks. Spend no effort.
- The one real refactor this needs: `streaming/transcode.py` `decide()` hardcodes
  the BROWSER's codec sets as module constants, so the playback decision cannot
  express "this Roku". It must take capabilities as a parameter, with a
  `from_device_profile()` adapter. This is the SAME bug as the "HLS probe is
  backwards" trap -- the decision needs both halves, what the file contains and
  what this client accepts, and neither may be hardcoded.
- Start by measuring, not by implementing the documented API: stub
  `/System/Info/Public` plus auth, log every inbound request, point a real
  client at it and let it state its requirements.

## Jellyfin masquerade -- moved to the frontend repo
Built here first (this session, 2026-08-28), then relocated whole: implementing
it as a reverse proxy in front of a SEPARATE frontend app meant two different
processes each thought they owned "the origin" (SvelteKit's CSRF check pins a
fixed `ORIGIN` env var; the login response's THREE `Set-Cookie` headers
collapsed into one malformed value going through a hand-rolled proxy's
`dict`-based header handling) -- a new integration bug for every layer bridged.
Moving the whole client protocol into `riven-tpdb-frontend` (which already
calls this backend's plain API as a BFF) removes the bridge entirely: one
process, one origin, nothing to keep in sync.

This backend now speaks NO Jellyfin protocol. `routers/secure/stream.py`
(`/api/v1/stream/file/{id}`, `/playback_info/{id}`, `/hls/{id}/...`) is the
ONLY piece Jellyfin playback still depends on here, and it needed zero changes
-- it already existed for the browser player, and the frontend's Jellyfin
routes call it exactly the way the browser player does.

All the hard-won client-protocol facts (case-insensitive routing, the
WebView-shell-vs-native client split, the `main.*.bundle.js` connection
trigger, auth header permissiveness, `PlaybackInfo` probing rules) now live in
`riven-tpdb-frontend`'s `AGENTS.md` alongside the implementation. Read there
before touching Jellyfin behavior; this repo has nothing left to find on it.

Discovery (UDP 7359) was NOT ported. It never worked on this deployment
regardless of which process ran it: the container is bridge-networked, so LAN
broadcast cannot reach it either way, and `network_mode: host` would collide
with the real Jellyfin already on 7359/8096. Revisit only if that networking
constraint changes.


## Seeking is a rate problem, not a concurrency one

The per-file connection cap shipped in September and the same file was
throttled again on 2026-09-19 (item 869, "Drive", in MX Player) with the cap
in force the whole time. The cap was never the thing being exceeded.

An external player seeks by opening a new range request and abandoning the
old one. Eight seeks in ten seconds are eight requests but never more than
one or two live at once, so a concurrency cap sees nothing wrong while the
CDN sees a burst for one file and starts refusing it. `RateLimiter` in
`upstream_guard.py` is the missing half: a token bucket per file, BURST
requests free and then one every two seconds.

It waits rather than refusing. A seek that arrives half a second late is
still a seek; a seek that gets a 503 is a stopped video, and a player that
gets one retries immediately -- which is how the file was throttled in the
first place.

Normal playback is untouched: a file watched end to end is ONE upstream
request that streams for an hour and spends a single token.

**Diagnosing a repeat:** the cooldown clears itself, so by the time it is
reported the file usually plays again. Probe `/api/v1/stream/file/{id}` with
three ranges (head, middle, tail) from inside the container before assuming
anything is still broken, and read the backend log for "not asking again" to
find when the last refusal actually was.

## The tunnel has a second consumer now

`stremio-tv` routes its **live TV** through the same tailscaled, and depends
on two things in this repo staying as they are:

* **`/api/v1/vpn/status` and `/api/v1/vpn/exit-node`**, read and written with
  the server's own API key. Changing either shape breaks live TV silently —
  that surface treats an unreadable status as "no VPN configured" and draws
  no panel, which looks like a deployment choice rather than a fault.
* **`tailscale:1055` serving an HTTP proxy as well as SOCKS5.** Both
  `TS_SOCKS5_SERVER` and `TS_OUTBOUND_HTTP_PROXY_LISTEN` point at it and
  tailscaled multiplexes the two. This repo uses the SOCKS side; stremio-tv
  uses the HTTP side, because it carries no `node_modules` and SOCKS would
  need a package. **Do not drop `TS_OUTBOUND_HTTP_PROXY_LISTEN`** because
  nothing here uses it.

What it does **not** touch is `vpn.route_scraping` / `vpn.route_streaming`.
Those stay scoped to this repo's add-ons and remain the owner's settings;
live TV has its own switch, kept over there. A new consumer of the tunnel
should do the same rather than widening one of these.
