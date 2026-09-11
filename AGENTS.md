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

## The noodlemagazine plugin
The direct-scraper plugins are their own repo,
`gauravsuman007/riven-tpdb-scrapers` (`../riven-tpdb-scrapers`, files under
`scrapers/`). `plugins/` in THIS repo holds only `.gitkeep`; the deployment
copies live at `/home/hellonfire/Server/riven-tpdb/plugins` on the server,
bind-mounted read-only. There is deliberately no CI and no build: a plugin
only imports successfully inside a running container. So a change is three
steps -- commit to the scrapers repo, `scp` the file to that server path, then
`POST /api/v1/direct/plugins/rescan` (Settings -> Plugins -> Rescan folder).
No rebuild, no restart. Check all three copies agree before assuming a fix is
live; the server copy is the one that runs.

- The duration/sort/HD filters are NOT honoured on a GET. `?len=long` on the
  search URL renders the UNFILTERED page, the same silent-ignore failure as
  `/home?story=`. The site's own control writes the filters into
  `location.search` and then POSTs them back to the URL it just wrote, so a
  filtered search is: GET once for the CSRF token and cookies, then POST
  `len`/`p` to `<search url>?len=long&p=N`, which answers with an HTML
  fragment of the same `<div class="item">` markup. Measured: shortest result
  went from 2:41 to 10:18 and nothing under ten minutes survived.
- Pagination is that same POST with `p=0,1,2...`, 24 items a page, which is
  the only way to satisfy a `limit` above 24.
- Attribute values are HTML-ESCAPED. About one thumbnail in five is on
  `img.pvvstream.pro` and carries a query string, so an un-unescaped `data-src`
  requests `&amp;idx=14` and the CDN answers 403 -- the whole of the "some
  thumbnails never load" bug. Nothing was missing from the markup and nothing
  failed to parse; the URLs were simply wrong. `cdn2.pvvstream.pro` has no
  query string, which is why most were fine and the breakage looked random.
  Verified after the fix: 72 of 72 thumbnails across three queries return 200.

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

## Direct-scrape matching (tube sites)
- `services/directscrapers/ranking.py` scores results from the streaming-site
  scrapers, not just titles: `MatchTarget` carries performers/studio/series,
  and a bare performer-name match with no title agreement is deliberately
  NOT enough (these actresses appear in hundreds of unrelated scenes).
- TRAP, the SAME bug as the "Pirates" containment bug in
  `services/awards/matching.py`, reapplied here: a query's tokens being
  *contained* in a result said nothing about how much of the result was
  unaccounted for. A one- or two-word title ("Unfolding", "Disciplinary
  Action") cleared a perfect score against any sentence that happened to
  contain the word, because the +0.25 "whole title as one run" bonus never
  checked how much surrounding, unrelated text that run sat inside. Measured
  live against a 50-title/6,700-result study: 80% of "confident" (score 1.0)
  matches carried no performer/studio corroboration, and reading a sample of
  those showed most were wrong. (`docs/` is gitignored, so the full study
  writeup with the sample data lives only on whichever machine produced it --
  the summary here is the durable copy; re-run the study rather than looking
  for that file elsewhere.)
- Fixed two ways: `_COMMON` was missing this vertical's own genre/tag
  vocabulary ("anal", "milf", "feels", "good", "affair", ...) -- these were
  scored as fully distinctive, which is why even 3+ word titles built from
  generic descriptors collided constantly. Adding them alone fixed most
  cases via the existing "nothing distinctive matched -> 0" rule. What is
  left (a title with 0-1 distinctive tokens even after that) gets a
  containment/bloat gate: if the video title carries 2+ distinctive tokens
  that neither the target's words nor its known performers/studio explain,
  the score is capped well under `MIN_RELEVANCE`.
- TRAP: the bloat gate only fires when `target.performers or target.studio`
  is non-empty. A bare custom search (the "Watch from a site" free-text
  field) has no performer/studio data to tell an appended name apart from an
  unrelated sentence, so it is left ungated -- a real limit of that path
  (there is nothing to corroborate against), not an inconsistency.
- Duration was the presumed strong corroborator going in and measured out
  weak: only 5% of even the most confident matches had a duration close to
  the target's runtime, because these sites overwhelmingly host individual
  scenes cut from a feature, not the feature itself. Kept as a minor ordering
  signal only (`sort_key`), not load-bearing.
- Conclusion, and why this is rules rather than a model: 50 titles is a
  validation set, nowhere near enough to train anything, and a wrong match
  that looks confident is the worst failure mode in this codebase. Rules fail
  with a traceable reason; a model fails silently at high confidence.

## Direct-scrape scraper plugins
- `services/directscrapers/plugins.py` discovers `DirectScraper` subclasses
  dropped into `settings.direct_scraping.plugin_dir` (default
  `/riven/plugins`, mapped from a `./plugins` host folder in
  `docker-compose.yml`, `:ro` -- the container only ever reads plugin files).
  There is ONE registry, not a built-in path and a separate plugin path: the
  eight bundled scrapers (`BUILTIN` on `DirectScraperService`) and anything
  discovered from disk both end up as ordinary `DirectScraper` instances in
  `service.services`, and `search`/`resolve` never ask which kind a scraper
  is. README.md documents the plugin interface for anyone adding a site.
- TRAP averted, not hit: a plugin key CANNOT shadow a built-in
  (`tnaflix`/`eporner`/etc are reserved). Without that check, a plugin file
  claiming an existing key would silently replace a tested, maintained
  scraper with an unreviewed one on the next rescan -- reported as a load
  error instead, visible in Settings -> Plugins.
- A broken plugin (syntax error, missing class, a constructor that raises)
  degrades to a per-file error the same way a VPN provider degrades to
  "unavailable" -- never raises into the service, never takes the other
  scrapers down with it. `discover_plugins` is the one place that boundary
  lives; do not let a plugin's exception propagate past it.
- Settings: `direct_scraping.disabled` (list of scraper keys, builtin or
  plugin) is the single source of truth for on/off, written ONLY via
  `POST /direct/plugins/{key}/enabled`, never through the generic settings
  form -- `disabled` is in `HIDDEN_SECTIONS` for exactly the reason
  `tailscale.auth_key` was: two write paths to the same value is how the
  "two auth key fields" bug happened the first time.
- `DirectScraperService` (and thus `describe_scrapers()`, which the Plugins
  tab polls) reads settings AND re-scans the plugin folder on every call --
  deliberately no caching of the file list itself, since "did my dropped-in
  file show up" needs to be true within one click, not after some unrelated
  settings change invalidates a cache. The registry actually used by
  `/direct/search` IS cached (`services/directscrapers` module-level
  `service()`/`reset()`, same singleton-plus-observer pattern as VPN) and is
  invalidated by the enable/disable endpoint and by `/direct/plugins/rescan`.
- `program/services/directscrapers/__init__.py` imports `settings_manager`
  lazily, inside the methods that need it, not at module scope. Importing it
  at the top would pull RTN and the DB models into `test_direct_scrapers.py`,
  which is otherwise self-contained and runs without a real settings module.

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
