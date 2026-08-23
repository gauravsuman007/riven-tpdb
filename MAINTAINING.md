# Maintaining this fork

`riven-tpdb` is a fork of [Riven](https://github.com/rivenmedia/riven) that
replaces TMDB/TVDB metadata with [ThePornDB](https://theporndb.net) and drops
the providers that cannot serve adult content. It is a deliberate behavioural
divergence, not a plugin — so upstream updates are *reviewed and merged*, not
applied blindly.

The goal of this document is to make that review short and predictable.

## Pulling an upstream update

```bash
./scripts/upstream-report.sh
```

This adds the `upstream` remote if missing, fetches it, and prints the only
thing that actually matters: **which files this fork has modified that
upstream has also changed.** Everything else merges mechanically.

Then:

```bash
git fetch upstream
git merge upstream/main
```

Resolve using the guidance below, run the checks, and commit.

## Why conflicts are rare

The fork's divergence falls into three buckets, and only one of them can
conflict on content:

| Kind | Count | Conflict risk |
|---|---|---|
| Files we **modified** | 23 | Real — the review list |
| Files we **deleted** | ~713 | Mechanical (modify/delete) |
| Files we **added** | 14 | None |

The ~713 deletions are overwhelmingly `src/schemas/{trakt,overseerr,mdblist,listrr}` —
generated schemas for providers this fork removed. If git asks about one,
the answer is **stay deleted**. The same applies to the removed service
modules themselves (`services/content/*`, the Stremio-style scrapers,
the TMDB/TVDB indexers).

## Keeping the review list small

Two rules do most of the work:

1. **Never let formatting drift.** `db_functions.py` once showed **1144
   changed lines for 10 lines of real change**, purely because its line
   endings had flipped from CRLF to LF. Every one of those lines was a
   guaranteed conflict. `.gitattributes` now sets `* -text` so nothing
   renormalises; do not remove it, and do not reformat upstream files.

2. **Keep edits to upstream files additive and small.** Prefer adding a
   parameter, a branch, or a new module over rewriting a function. When a
   change is fork-specific, say so in a comment — a reviewer merging upstream
   needs to know at a glance whether a hunk is ours on purpose.

Check the real size of a change (ignoring whitespace) before committing:

```bash
git diff -w --numstat <upstream-base>..HEAD -- <file>
```

## What diverges, and how to resolve it

The modified files, grouped by why:

**TPDB replaces TMDB/TVDB as the identifier.** `media/item.py`,
`db/db_functions.py`, `managers/event_manager.py`, `routers/secure/items.py`,
`routers/secure/scrape.py`, `services/filesystem/vfs/naming.py`.
These add `tpdb_id` alongside the existing ids. On conflict, keep upstream's
change and re-add the `tpdb_id` arm — it is always parallel to `imdb_id`.

**Provider set is reduced.** `services/scrapers/__init__.py`,
`services/indexers/__init__.py`, `services/content/__init__.py`,
`apis/__init__.py`, `settings/models.py`. These trim registries and settings
to what can work here. On conflict, take upstream's structure and re-apply the
trim; do not reintroduce a provider without checking it can serve adult
content (only Prowlarr and Jackett can).

**Adult-content correctness.** `services/scrapers/shared.py` (volume matching),
`services/scrapers/base.py` (`supports_adult`), `settings/models.py` (RTN
defaults). These fix real misbehaviour — RTN's `remove_adult_content` and its
0.85 `title_similarity` both reject this fork's content outright. Keep them.

**Bug fixes that belong upstream.** `services/downloaders/__init__.py`
(add-torrent fallback and download-url backfill), `program.py` (optional
updater), `services/updaters/__init__.py`. If upstream fixes the same thing,
prefer upstream's version.

## Before committing

```bash
# backend
python -m pytest src/tests -q

# frontend (separate repo: riven-tpdb-frontend)
pnpm run check     # holds at the upstream baseline of 73 errors
pnpm run build
```

`pnpm run check` inherits 73 pre-existing errors from upstream's frontend.
That number is the baseline — it should not grow.

## The frontend is a separate repo

`riven-tpdb-frontend` forks `rivenmedia/riven-frontend` and carries the same
policy. It has its own `scripts/upstream-report.sh`.

Regenerate the API client whenever backend routes change:

```bash
npx openapi-typescript@7 "http://<backend>/openapi.json" -o src/lib/providers/riven.ts
```

Note that `openapi-fetch` infers types from **literal** path strings. Writing
`params: { ... } as never` to silence a type error defeats inference and the
response becomes `never`; branch on the literal path instead.
