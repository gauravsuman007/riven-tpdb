# Multi-file releases — part ordering and VFS registration

Status: **part 1 done, part 2 outstanding**, as of 2026-09-19. The bug that
motivated it is fixed and deployed (`entry_selection.stale_entries`, commit
9d9041aa). **Part 1 (ordering) shipped** as `program/media/part_ordering.py`
and **item 872 is repaired**; part 2 (VFS registration) is still design.

Prerequisite reading: "Only one file of a multi-file release reached the
library" and "Multi-file releases (playlists)" in `AGENTS.md`.

## Why this exists

Until 2026-09-19 a multi-file release reached the library as ONE file --
`_update_attributes` cleared `filesystem_entries` once per file, so each file
deleted the one before it. Nine titles were damaged; eight are repaired. With
every file now kept, two things that were unreachable become visible:

1. `media_parts` orders by filename, and `parts[0]` is what plays first and
   what the VFS mounts. For numbered scenes filename order is right. For a
   release carrying extras it is not: **Island Fever 3** would open on
   `BTS.mkv` rather than the 3.1 GB feature.
2. Only `parts[0]` is registered in the VFS at all.

Neither affects playback. The stream endpoints resolve a part straight from
the debrid provider (`playback_url.resolve(..., part=)`) and never consult the
VFS, so the web player, `/stream/parts`, the parts panel and the `.m3u`
hand-off already serve every part.

## Upstream status

**`upstream/main` has both bugs, and is worse.** It carries the identical
`item.filesystem_entries.clear()` and the identical single-entry
`RivenVFS.add()`, and has no `media_parts` concept at all -- its `media_entry`
is literally "the first MediaEntry". So upstream silently drops every file but
one from a multi-file movie, and where a sample sorts last it keeps the
sample.

It goes unnoticed there because TV is unaffected (each episode is its own
MediaItem, so the clear applies per episode) and mainstream movie torrents are
feature-plus-sample. This fork hits it constantly because split-scene releases
are the norm for adult content. Do not expect an upstream fix to arrive; do
expect a merge conflict here at both sites.

## Part 1 — ordering (DONE)

Shipped as `program/media/part_ordering.py`, with `src/tests/test_part_ordering.py`
covering every case below. It lives under `program/media` rather than beside
`entry_selection` because importing the downloaders package pulls in
`MediaItem` itself.

Measured over all 251 torrents in the TorBox account (2026-09-19). 76 hold
more than one video file; **30 are movie-shaped** (<= 12 video files, which
excludes the TV box sets that go through the episode path anyway). Those 30
are the sample the rules below were chosen against.

Two independent steps. Neither ever has to answer "which file is the
feature" -- that falls out of putting extras last.

### Step 1: extras go to the end

A file is an extra when it BOTH

* matches `bts | behind the scenes | trailer | teaser | sample | preview |
  outtake | blooper | photoshoot | promo`, and
* is under **50%** of the largest video file;

or, regardless of name, is under **10%** of the largest.

Both conditions are load-bearing, and this is the measurement that decided it.
Keyword-only misclassified the FEATURE in two of thirty:

* `The.Amazing.Spider-Man.2012.1080p.BluRay.H264.AAC-RARBG.mp4` -- the release
  group matched `rarbg` when that token was in the list. It was removed; the
  10% rule catches the real junk file (`RARBG.com.mp4`, 0.00 GB) without it.
* `Pirates 2005 and Pirates 2 2008 **Bonus** Edition ...` -- both halves of a
  double feature matched `bonus`.

Guard: if every file classifies as an extra, none do. That is what kept the
Pirates double feature orderable once `bonus` still matched both.

Result on the sample: 5 of 30 torrents have extras, zero features
misclassified.

### Step 2: order the remainder by the first signal that gives a total order

1. **A varying digit-run at a shared position.** Find a digit run that differs
   across every filename while the text BEFORE it is identical. Sort
   numerically. This is the workhorse -- **19 of 30**:
   `DEEPER_1014{26..29}`, `TV15_s{01..05}_...`, `Scene{01..04}.mp4`,
   `{01..04}.mp4`, `mommys-boys-scene-{1..3}`, `...720p.P{1..5}`,
   `{1..5} scene_...`.
   The identical-prefix requirement is what stops a resolution or a year being
   mistaken for an index.
2. **`part` / `pt` / `cd` / `disc` / `disk` markers.** 0 of 30 needed this;
   kept because `divxfactory-fsd2_part1/_part2` is one rename away from it.
3. **Alphabetical** -- today's behaviour. **11 of 30**, and for those it is
   the right answer, not a fallback: they are performer-named
   (`Ava Sinclaire.mp4`, `Riley Reid.mp4`) or description-named
   (`Secrets and Seductions_..._Angela White...`) scenes with no intrinsic
   order. Note `Half His Age` sorts to `Cherie DeVille, Jill Kassidy 1,
   Jill Kassidy 2, Kristen Scott 1, Kristen Scott 2` -- the trailing digits
   fall out of alphabetical for free, which is why rule 1 not firing there
   costs nothing.

So rule 1 corrects the 63% that have a real order and today sort correctly
only by luck, and rule 3 preserves current behaviour wherever nothing better
exists.

### Where it goes

`media_parts` in `program/media/item.py` is the only caller. Put the rule in
its own import-free module beside `entry_selection.py` for the same reason
that one exists -- `item.py` cannot be imported in a test without a database.
Prototype and the measurement script: this session's scratchpad, reproduce
with `torrents/mylist?bypass_cache=true`.

**Do not reorder `media_entry` independently of `parts[0]`.** They were
deliberately unified; splitting them means the file the VFS mounts and the
file the player opens can disagree.

## Part 2 — VFS registration (outstanding)

Two changes, both small.

* `RivenVFS.add()` and `.remove()` iterate `item.media_parts` instead of
  `item.media_entry`. One loop each.
* `naming.generate_clean_path()` takes the part index and the part count.

**Single-part items must keep byte-identical paths.** That is the important
half: ~90% of the library is single-part and must not churn. Multi-part items
get distinct siblings inside the same title folder:

```
/movies/Mistress Maitland (2020) {tpdb-...}/
    Mistress Maitland (2020) - pt01 - DEEPER 101426 1080lP.mp4
    Mistress Maitland (2020) - pt02 - DEEPER 101427 1080lP.mp4
    ...
```

Deliberately **not** Jellyfin's `- part1` / `- cd1` convention, which STACKS
files as segments of one film. That is wrong for a scene compilation, and this
fork does not point a media server at the VFS anyway (see "Media-server
masquerade" in `AGENTS.md`), so media-server semantics are a non-goal. `pt01`
sorts correctly, stays readable over Samba, and claims no relationship the
files do not have.

Ordering (part 1) should land first, or `pt01` names the wrong file.

## Repair: item 872, Island Fever 3 (DONE)

Repaired 2026-09-19. It held `Trailer.mkv` (105 MB) of a
4114 MB torrent whose video files are `Island.Fever.3.mkv` (3.1 GB),
`BTS.mkv` (0.8 GB) and `Trailer.mkv` -- so it plays the trailer.

Neither repair path reaches it:

* `queue_release` refuses -- its release is no longer returned by any scraper,
  and the endpoint reads `_manual_streams`, an in-memory cache the scrape
  endpoint fills. The torrent is still in TorBox regardless.
* the manual session (`start_session` + `update_attributes`) refuses -- for a
  movie, `file_data` is a single `DebridFile`. **That API predates playlists
  and cannot attach more than one file to a movie**, which is worth fixing on
  its own account: it is the only UI path for picking files out of a torrent.

The repair is to drop the stale entry and let the pipeline re-ingest the
pinned release:

```sql
update "MediaItem" set updated = false,
       preferred_stream_hash = (active_stream->>'infohash') where id = 872;
delete from "FilesystemEntry" where media_item_id = 872;
-- and this, or nothing happens: a Completed item is not "incomplete", so
-- `retry_library` only sees it through the pending-candidate clause.
update "MediaItem" set downloading_stream_hash = preferred_stream_hash
       where id = 872;
```

Then restart the backend, which runs `retry_library` at startup. The item came
back with all three files and `pin_satisfied` cleared the pin on its own.

## Build order

1. ~~Part ordering~~ -- done.
2. ~~Repair item 872~~ -- done, by the SQL above plus setting
   `downloading_stream_hash` to the pinned hash, which is the one thing that
   makes `retry_library` re-queue a Completed item. A restart then drove it.
3. `update_attributes` accepting several files for a movie.
4. VFS multi-part registration + naming.
