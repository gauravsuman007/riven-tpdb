"""Builds AVN award collections and resolves their entries against TPDB.

The work splits into two phases that run on different clocks:

    * :meth:`AwardsService.sync_corpus` fetches the Wikipedia articles and
      writes one :class:`Collection` per ceremony year. Cheap, weekly.
    * :meth:`AwardsService.resolve_batch` takes a bounded slice of entries still
      marked ``pending`` and looks each one up on TPDB. TPDB allows two requests
      a second, so resolving ~11,000 entries is roughly an hour of wall clock.

Resumability is a property of the data, not of a checkpoint file: an entry's
``match_state`` *is* the progress marker. Every batch commits, so a restart
mid-run resumes at the first still-pending entry and repeats no lookups.

Only winners are auto-requested, and only after they resolve. Nominees stay as
catalogue rows so the collection reads as a full ballot without pulling
thousands of titles into the library.
"""

from datetime import datetime

from kink import di
from loguru import logger
from sqlalchemy import func, select

from program.apis.tpdb_api import TpdbApi, TpdbApiError
from program.db.db import db_session
from program.media.collection import (
    MATCH_MATCHED,
    MATCH_PENDING,
    MATCH_UNMATCHED,
    Collection,
    CollectionEntry,
)
from program.media.item import MediaItem
from program.services.awards import avn
from program.services.awards.matching import (
    MIN_TITLE_RATIO,
    best_match,
    evaluate_candidate,
    title_ratio,
)
from program.settings import settings_manager

SOURCE = "avn"


def collection_key(year: int) -> str:
    return f"{SOURCE}-{year}"


class AwardsService:
    """Owns the AVN corpus and its resolution against TPDB."""

    # Detail lookups are the expensive half of resolution, so only the most
    # plausible candidates earn one. Three is enough to cover a title that TPDB
    # lists under several editions.
    DETAIL_CANDIDATES = 3

    def __init__(self) -> None:
        self.settings = settings_manager.settings.content.awards
        self.initialized = False

        if not self.settings.enabled:
            return

        if not settings_manager.settings.tpdb.api_token:
            logger.error("Awards collections need a TPDB API token; disabling.")
            return

        self.api = di[TpdbApi]
        self.initialized = True
        logger.success("AVN award collections initialized!")

    # ------------------------------------------------------------------ corpus

    def sync_corpus(self) -> int:
        """Fetch the award corpus and upsert one collection per ceremony year.

        Returns the number of entries newly added. Existing entries are left
        alone so that resolution results and requests survive a refresh.
        """

        try:
            corpus = avn.build_corpus()
        except Exception as exc:
            logger.error(f"Failed to build AVN corpus: {exc}")
            return 0

        if not corpus:
            logger.warning("AVN corpus came back empty; leaving collections untouched")
            return 0

        keep_nominees = self.settings.include_nominees
        by_year: dict[int, list[avn.AwardEntry]] = {}

        for entry in corpus:
            if entry.year < self.settings.first_year or not entry.is_media:
                continue

            if not entry.winner and not keep_nominees:
                continue

            by_year.setdefault(entry.year, []).append(entry)

        added = 0

        with db_session() as session:
            for year, entries in sorted(by_year.items()):
                collection = session.execute(
                    select(Collection).where(Collection.key == collection_key(year))
                ).scalar_one_or_none()

                if collection is None:
                    collection = Collection(
                        key=collection_key(year),
                        source=SOURCE,
                        name=f"AVN Awards {year}",
                        description=(
                            f"Winners and nominees from the {avn.ordinal(year - avn.CEREMONY_YEAR_OFFSET)} "
                            f"AVN Awards, held in {year}."
                        ),
                        year=year,
                    )
                    session.add(collection)
                    session.flush()

                # One query per collection rather than per entry: the corpus is
                # ~11k rows and a per-entry existence check made this the
                # slowest part of a refresh.
                existing = {
                    (title, category)
                    for title, category in session.execute(
                        select(CollectionEntry.title, CollectionEntry.category).where(
                            CollectionEntry.collection_id == collection.id
                        )
                    ).all()
                }

                for entry in entries:
                    assert entry.title is not None
                    slot = (entry.title, entry.category)

                    if slot in existing:
                        continue

                    existing.add(slot)
                    session.add(
                        CollectionEntry(
                            collection_id=collection.id,
                            title=entry.title,
                            studio=entry.studio,
                            performers=entry.performers or None,
                            category=entry.category,
                            year=entry.year,
                            winner=entry.winner,
                            match_state=MATCH_PENDING,
                        )
                    )
                    added += 1

                collection.refreshed_at = datetime.now()

            removed = self._prune_nominees(session) if not keep_nominees else 0
            demoted = self._prune_person_awards(session)
            session.commit()

        scope = "winners and nominees" if keep_nominees else "winners only"
        logger.info(
            f"AVN corpus synced ({scope}): {len(by_year)} collections, "
            f"{added} new entries"
            + (f", {removed} nominee(s) removed" if removed else "")
            + (f", {demoted} person award(s) removed" if demoted else "")
        )

        return added

    @staticmethod
    def _prune_nominees(session) -> int:
        """Delete stored nominees after the setting is turned off.

        An entry that was already requested is left alone: it is in the library
        now, and silently unlinking it from its collection would make the title
        look like it arrived from nowhere.
        """

        nominees = (
            session.execute(
                select(CollectionEntry)
                .join(Collection)
                .where(
                    Collection.source == SOURCE,
                    CollectionEntry.winner.is_(False),
                    CollectionEntry.media_item_id.is_(None),
                )
            )
            .scalars()
            .all()
        )

        for entry in nominees:
            session.delete(entry)

        return len(nominees)

    @staticmethod
    def _prune_person_awards(session) -> int:
        """Delete stored entries whose category awards a person, not a film.

        Needed because the category gates were tightened after the corpus had
        already been synced, and ``sync_corpus`` only ever adds. Without this, a
        library that synced before the change would keep showing Best Actor and
        Best Male Newcomer forever -- the rows are already there and nothing
        would revisit them.

        Unlike the nominee prune this does **not** spare entries that were
        already requested, and the difference is deliberate. Deleting a
        collection entry does not touch its MediaItem: the film stays in the
        library exactly as it was, and only its listing on the awards page
        goes. A nominee is spared because its entry is the only record of why
        that title was ever fetched; a person award's entry is a row that
        should never have existed, so keeping it would defeat the prune on
        precisely the ceremonies that have been synced longest.
        """

        stale = [
            entry
            for entry in session.execute(
                select(CollectionEntry)
                .join(Collection)
                .where(Collection.source == SOURCE)
            )
            .scalars()
            .all()
            if not avn.awards_a_work(entry.category)
        ]

        for entry in stale:
            session.delete(entry)

        return len(stale)

    # -------------------------------------------------------------- resolution

    def resolve_batch(self, limit: int | None = None) -> tuple[int, int]:
        """Resolve up to ``limit`` pending entries. Returns (matched, unmatched).

        Winners are resolved before nominees so the auto-request path reaches
        the titles that matter first; within that, oldest collections first so
        progress is visible year by year rather than scattered.
        """

        limit = limit or self.settings.resolve_batch_size

        with db_session() as session:
            pending = (
                session.execute(
                    select(CollectionEntry)
                    .join(Collection)
                    # Scoped to this source on purpose. Other catalogues are
                    # self-sourced and owe TPDB nothing; without this filter a
                    # future source that used "pending" would silently have its
                    # entries resolved here, spending the rate limit on lookups
                    # nobody asked for.
                    .where(
                        Collection.source == SOURCE,
                        CollectionEntry.match_state == MATCH_PENDING,
                    )
                    .order_by(
                        CollectionEntry.winner.desc(),
                        CollectionEntry.year.desc(),
                        CollectionEntry.id,
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            if not pending:
                return 0, 0

            matched = unmatched = 0

            for entry in pending:
                try:
                    match = self._resolve_one(entry)
                except TpdbApiError as exc:
                    # A TPDB outage must not burn through the queue marking
                    # everything unmatched; stop and retry on the next run.
                    logger.warning(f"TPDB unavailable, pausing resolution: {exc}")
                    break
                except Exception as exc:
                    logger.error(f"Failed to resolve {entry.title!r}: {exc}")
                    match = None

                entry.matched_at = datetime.now()

                if match is None:
                    entry.match_state = MATCH_UNMATCHED
                    unmatched += 1
                else:
                    entry.tpdb_id = match.tpdb_id
                    entry.tpdb_kind = match.kind
                    entry.match_score = match.score
                    entry.poster_path = match.poster
                    entry.match_state = MATCH_MATCHED
                    matched += 1

                # Per entry, not per batch. A batch is minutes of rate-limited
                # HTTP, and holding one transaction open across all of it would
                # both block other writers and throw away the whole batch's
                # progress if the process died partway through.
                session.commit()

        logger.debug(f"Resolved {matched} matched, {unmatched} unmatched")

        return matched, unmatched

    def _resolve_one(self, entry: CollectionEntry):
        """Search TPDB for one entry and return the best acceptable match.

        Two passes, because TPDB's search and detail endpoints return different
        shapes. ``/movies?q=`` gives a *flat* record: no nested ``site`` and no
        ``performers``, only a top-level ``site_id``. Scoring straight off that
        would leave studio and cast permanently unset -- the two strongest
        signals -- and nothing would ever clear the acceptance bar.

        So: shortlist on title similarity alone, then fetch the detail record
        for the few plausible ones and score those properly. Movies are tried
        before scenes because award categories overwhelmingly name feature
        releases, and a scene search on a feature title returns that feature's
        individual scenes, any of which would be a wrong match.
        """

        for kind, search, fetch in (
            ("movie", self.api.search_movies_text, self.api.get_movie),
            ("scene", self.api.search_scenes_text, self.api.get_scene),
        ):
            try:
                results = search(entry.title, per_page=20) or []
            except TpdbApiError:
                raise
            except Exception as exc:
                logger.debug(f"TPDB {kind} search failed for {entry.title!r}: {exc}")
                continue

            shortlist = self._shortlist(entry, results)
            candidates = []

            for result in shortlist:
                detail = self._detail(fetch, result)

                if detail is None:
                    continue

                site = detail.site.name if detail.site else None
                poster = detail.poster or (
                    detail.posters.large if detail.posters else None
                )

                candidates.append(
                    evaluate_candidate(
                        entry_title=entry.title,
                        entry_studio=entry.studio,
                        entry_year=entry.year,
                        entry_performers=entry.performers,
                        tpdb_id=detail.id or result.id,
                        tpdb_kind=kind,
                        tpdb_title=detail.title,
                        tpdb_site=site,
                        tpdb_date=detail.date,
                        tpdb_performers=[p.name for p in detail.performers if p.name],
                        tpdb_poster=poster,
                    )
                )

            match = best_match(candidates)

            if match is not None:
                return match

        return None

    def _shortlist(self, entry: CollectionEntry, results: list):
        """The few search hits worth spending a detail request on.

        Ranked by title similarity, which is the only signal a flat search
        result carries.
        """

        scored = []

        for result in results:
            if not result.id:
                continue

            ratio = title_ratio(entry.title, result.title or "")

            if ratio < MIN_TITLE_RATIO:
                continue

            scored.append((ratio, result))

        scored.sort(key=lambda pair: pair[0], reverse=True)

        return [result for _, result in scored[: self.DETAIL_CANDIDATES]]

    @staticmethod
    def _detail(fetch, result):
        """Fetch the full record for a search hit.

        A detail lookup that fails is skipped rather than scored off the flat
        record: a flat record cannot supply site or cast, so scoring it would
        just produce a confident-looking title-only match.
        """

        try:
            return fetch(result.id)
        except TpdbApiError:
            raise
        except Exception as exc:
            logger.debug(f"TPDB detail lookup failed for {result.id}: {exc}")
            return None

    # ----------------------------------------------------------- auto-requests

    def request_matched_winners(self, limit: int = 50) -> int:
        """Queue matched winners that have not been requested yet.

        Deliberately bounded: the first run after a full corpus sync has
        thousands of eligible winners, and handing them to the event manager all
        at once would swamp the scrapers.
        """

        if not self.settings.auto_request_winners:
            return 0

        from program.program import Program

        queued = 0

        with db_session() as session:
            eligible = (
                session.execute(
                    select(CollectionEntry)
                    .join(Collection)
                    .where(
                        Collection.source == SOURCE,
                        CollectionEntry.winner.is_(True),
                        CollectionEntry.match_state == MATCH_MATCHED,
                        CollectionEntry.media_item_id.is_(None),
                        CollectionEntry.tpdb_id.is_not(None),
                    )
                    .order_by(CollectionEntry.year.desc(), CollectionEntry.id)
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            for entry in eligible:
                existing = session.execute(
                    select(MediaItem).where(MediaItem.tpdb_id == entry.tpdb_id)
                ).scalar_one_or_none()

                if existing is not None:
                    # Already in the library for another reason; adopt it rather
                    # than requesting a duplicate.
                    entry.media_item_id = existing.id
                    continue

                item = MediaItem(
                    {
                        "tpdb_id": entry.tpdb_id,
                        "requested_by": "awards",
                        "requested_at": datetime.now(),
                    }
                )

                if di[Program].em.add_item(item):
                    queued += 1
                else:
                    logger.debug(f"{entry.title!r} was not queued (already present)")

            session.commit()

        if queued:
            logger.info(f"Queued {queued} AVN award winner(s)")

        return queued

    # ---------------------------------------------------------------- progress

    @staticmethod
    def progress() -> dict[str, int]:
        """Counts per match state, for the API and for logging."""

        with db_session() as session:
            rows = session.execute(
                select(CollectionEntry.match_state, func.count(CollectionEntry.id))
                .join(Collection)
                .where(Collection.source == SOURCE)
                .group_by(CollectionEntry.match_state)
            ).all()

        return {state: count for state, count in rows}
