# Recommendation engine + generic list import — strategy

Status: **design, not built.** Researched and verified 2026-09-10. Every
access claim below was checked against the live source on that date; re-check
robots.txt before acting on any of it.

---

## 1. The constraint that shapes everything: Reddit cannot be harvested

Two independent walls, both verified:

1. **`https://www.reddit.com/robots.txt` is `User-agent: * / Disallow: /`.**
   Every path, every crawler. `old.reddit.com` is the same.
2. **NSFW content has been unavailable through the Data API since 5 July
   2023.** Reddit restricted mature content to its own first-party app; this
   is what killed Apollo et al. The free tier (100 QPM with OAuth,
   personal/non-commercial — which this would qualify as) is real and usable,
   and returns nothing for adult subreddits.

Pushshift was shut down in 2024 under CFAA pressure. Arctic Shift's academic
dumps stop at 2023 and are framed for research use. Neither is a foundation.

**Therefore: Reddit is a paste-in source, not a crawl source.** The import
pipeline *is* the Reddit integration. This is also the more durable design —
it works identically for a forum post, a Discord message or a text file, and
does not rot when a platform changes terms.

## 2. Source access, as verified

| Source | Status | Value |
|---|---|---|
| StashDB | GraphQL API, key configured | **2,941 tags, categorised**; server-side faceting |
| TPDB | REST API | ~2.6k *flat* tags; `list_movies` filters by site only |
| Adult Empire | sitemap OK, `/Search` disallowed | per-title rating + bestseller/trending rank |
| Excalibur Films | permissive robots + sitemap | 2nd storefront, title-in-slug (same shape as AE) |
| XBIZ | permissive robots + sitemap | awards + editorial |
| Wikipedia | integrated | AVN (done), **XBIZ**, **XRCO** award corpora |
| IAFD | `Allow: /`, `use=reference` | cast/credit depth — see note |
| Data18 | `Disallow: /` except Googlebot/Bingbot | **out** |
| AdultDVDTalk | Cloudflare managed challenge | **out** — do not work around bot detection |

**IAFD note.** Its robots.txt has a wildcard `Allow: /` with
`Content-Signal: search=yes,ai-train=no,use=reference`, but an explicit
`User-agent: ClaudeBot -> Disallow: /`. That rule binds Claude, not this
software. Riven-TPDB may crawl IAFD under the wildcard; a Claude session
cannot fetch it. If an IAFD provider is wanted, the operator runs the fetches
or supplies saved pages.

## 3. The finding that makes this buildable

StashDB's tags are a curated graph, grouped `SCENE` / `PEOPLE` / `ACTION`:

```
SCENE / Themes: 304      SCENE / Locations: 80     SCENE / Moods: 38
SCENE / Roles: 173       SCENE / Relations: 78     SCENE / Motivations: 14
SCENE / Group Makeup: 79 SCENE / Orientation: 8    ACTION / Acts: 736
```

- `Locations` (80): Outdoors, Nature, Beach, Forest, Park, Backyard, Camping,
  Boat, Poolside, Garden, Balcony, Alley, Parking Lot, Barn, Cave, Farm...
- `Moods` (38): Romance, Passion, Artistic, Softcore, Playful, Relaxed,
  Sultry, Night, Sunlit ... Brutal, Aggressive, Hardcore, Reluctance
- `Themes` (304): 3rd Person Narrative, Parody, Amateur, Casting, Cheating,
  1970s...1990s, Apocalyptic, Ancient History...

And `queryScenes` **facets server-side**: `tags` with an `INCLUDES_ALL`
modifier, plus `studios`, `performers`, `date`, `sort`, `direction`, and a
`favorites` filter. Verified live:

```
Outdoors AND Romance -> 233 scenes, one call
  2026-09-01 | Lustery         | Quick(ie) On The Draw
  2026-08-05 | Private Society | Cream Me by the Firelight
```

Every hit was modern amateur/gonzo — StashDB is scene-centric. That is the
evidence for **separate scene and movie engines**: it is the right corpus for
scenes and the wrong one for "something with a real plot".

TPDB cannot filter movies by tag at all (site only), so the movie engine needs
a **local index** — the pattern already proven by the 130,547-title Adult
Empire sitemap index.

## 4. Architecture: three layers

### Layer 1 — Facets: what a title *is*

Adopt StashDB's category graph as the canonical vocabulary; normalise every
provider into it. `MediaItem.genres` is today one flat list mixing kinds — the
live library has `blowjob`, `narrative` and `brown hair` in the same bag.
`narrative` and `character` are literally the "real plot" signal, unusable
because nothing knows they are a different *kind* of fact from `brown hair`.

Add `Facet(kind, category, value)` with per-provider aliases. This is the
prerequisite for everything else.

### Layer 2 — Intents: what a person *asks for*

"Outdoor", "real plot", "believable" are not tags. They are named, versioned,
hand-authored facet expressions — shipped as defaults, user-editable:

```yaml
believable:
  any:  [Moods:Passion, Moods:Romance, Moods:Relaxed, Moods:Playful]
  none: [Moods:Brutal, Moods:Aggressive, Themes:Blackmail, Themes:Casting]
real-plot:
  any:  [Themes:"3rd Person Narrative", Themes:Parody, genre:narrative, genre:character]
  none: [Themes:Amateur, Themes:Casting]
  min_runtime: 80
outdoor:
  any:  [Locations:Outdoors, Locations:Nature, Locations:Beach, Locations:Forest,
         Locations:Park, Locations:Backyard, Locations:Camping, Locations:Boat,
         Locations:Poolside, Locations:Garden]
```

This is the honest version of semantic search: inspectable and correctable.

**Do not build vector/embedding search over titles.** It produces confident
wrong answers with no provenance — the opposite of this codebase's stance
(`ACCEPT_SCORE = 6.0`, the title-symmetry gate, `assign_provider_id` refusing
rather than guessing). Use an LLM offline to *draft* intent expressions and to
pre-segment pasted text; let the deterministic matcher decide.

### Layer 3 — Ranking

Weighted per-signal, provenance retained so a result can explain itself:

- award density (winner > nominee; **XRCO is critics, AVN is industry, XBIZ is
  business — weight separately, they disagree usefully**)
- Adult Empire rating (audience) and bestseller/trending rank (demand)
- StashDB tag richness + fingerprint-submission count (curation effort proxy)
- recency decay, runtime, cast overlap with the library
- **own-library signals**: what was kept (`routers/secure/keep.py`), watched,
  replaced

## 5. Two engines, deliberately

- **Scene engine** — StashDB-native, server-side facet query, one call.
- **Movie engine** — Adult Empire + TPDB + awards, local facet index. Movies
  are where "plot" lives.

Different corpora, different ranking, different acceptance bars. Sharing one
would make both worse.

## 6. Studio engine

Two distinct questions:

**(a) Which studio should I explore?** Rank studios by award density *per
release*, median rating, catalogue depth, era. Not by size — a 40-title studio
with six XRCO nods beats a 4,000-title gonzo mill.

**(b) Best of studio X, beyond bestselling.** Already half-answered by an
existing code comment — `services/recommendations/adultempire.py` notes the
site "carries a rating per title but will not order by it", which is why
`STUDIO_SORTS = ("bestseller", "trending")`. So ingest the studio's full
catalogue once and rank **locally**:

- rating with a vote-count prior (Wilson / Bayesian — else one 5.0 vote beats
  400 votes at 4.8)
- award and critic hits
- **deviation from the studio's own baseline** — what is unusual *for them*
- and the split that matters: high-critic/low-sales = deep cuts;
  high-sales/low-critic = popular. Two rows, honestly labelled.

## 7. Generic list/collection import

One endpoint, tiered parsing. Everything lands as `Collection(source="import")`
+ `CollectionEntry` — the model already exists, `external_source` carries
provenance, `match_state` gives the resolve loop for free.

1. **Structured** — CSV/JSON with declared columns; Letterboxd/Trakt-shaped
   exports.
2. **Semi-structured** — markdown/bullet lists. `services/awards/avn.py`
   already does exactly this and was just proven on the nastiest real input
   available (`Cast, Cast; ''Title''`, studio hidden in emphasis). Reuse that
   lineage.
3. **Free text** — a pasted Reddit thread. Candidates from quoted/emphasised
   spans, years in parens, a studio gazetteer built from the studios table,
   and n-gram matching against the 130,547-title Adult Empire index. That last
   signal is the strong one and already exists.

Two rules to hold firmly:

- **Never auto-accept a free-text parse.** Show `line -> candidate -> score`
  and require operator confirmation: the 6.0 bar plus a human pass.
- **Keep the raw pasted text on the Collection.** A parser improvement must be
  re-runnable without re-pasting. The 2026-09-10 AVN repair had to re-fetch 43
  Wikipedia articles purely because rows had not kept their source line.

## 8. Build order

1. Facet schema + StashDB tag ingest — unlocks all three engines, no new source
2. Intent library + **scene engine** (StashDB does the query work)
3. Import v1: structured + semi-structured (reuses AVN parsing)
4. Studio "beyond bestselling" local re-rank (pure local compute, data already fetched)
5. Movie facet index (extend the Adult Empire index)
6. Import v2: free text + review queue
7. XBIZ / XRCO award corpora (more ground truth for ranking)
8. Excalibur as a second storefront (cross-source rating agreement)

## Sources

- Reddit Public Content Policy — https://support.reddithelp.com/hc/en-us/articles/26410290525844-Public-Content-Policy
- Reddit API controversy (NSFW cutoff) — https://en.wikipedia.org/wiki/Reddit_API_controversy
- Vice, "You Can't Look at Porn on Any Reddit Third-Party App Now"
- StashDB tag guidelines — https://guidelines.stashdb.org/docs/scenes/edit/scene-tags/
- XRCO Awards — https://en.wikipedia.org/wiki/XRCO_Awards
- XBIZ Awards — https://en.wikipedia.org/wiki/XBIZ_Awards
