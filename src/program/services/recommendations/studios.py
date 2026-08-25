"""The Adult Empire studio directory, kept locally and enriched from TPDB.

Split in two because the two halves have completely different costs and
completely different failure modes:

    * The *directory* -- which studios exist, what they are called, how many
      titles each has -- is slow to obtain (three sitemaps plus one page per
      studio at the storefront's one-request-a-second courtesy delay) and
      changes about never. It is synced weekly, overnight.
    * A studio's *titles* are never stored at all. They are read live in
      whichever ranked order the page asked for. Mirroring two orders for a
      hundred studios would rebuild twenty thousand rows a week to serve pages
      that are mostly never opened, and the rank would still be stale by the
      time anyone looked.

Artwork and descriptions come from TPDB rather than Adult Empire. This is not
a preference: the storefront's studio pages carry a name and a result count and
nothing else -- no description, no logo, no cover image. TPDB's ``/sites`` has
a logo and a poster for most of the studios that matter, and a short
description for some of them.
"""

from datetime import datetime

from loguru import logger
from sqlalchemy import select

from program.db.db import db_session
from program.media.studio import Studio
from program.services.recommendations.adultempire import (
    STUDIO_SORTS,
    AdultEmpireClient,
    AdultEmpireError,
    RankedTitle,
    StudioRef,
)
from program.services.recommendations.tpdb_lookup import client as tpdb_client
from program.settings import settings_manager

# A studio the storefront lists but with almost nothing behind it is noise in
# the directory. Measured: the real studios sit in the hundreds or thousands.
MIN_TITLES = 5


class StudioService:
    """Mirrors the studio directory; reads studio listings live."""

    def __init__(self) -> None:
        self.settings = settings_manager.settings.content.brochure
        self.initialized = False

        if not self.settings.enabled or not self.settings.studios_enabled:
            return

        self.client = AdultEmpireClient()
        self.initialized = True
        logger.success("Adult Empire studios initialized!")

    # ------------------------------------------------------------ directory

    def sync(self) -> int:
        """Refresh the studio directory. Returns the number of studios stored.

        Resumable and non-destructive. A studio that has dropped off the
        sitemaps is left in place rather than deleted: the user may have saved
        it, and a weekly job that silently removed someone's saved studios
        would be indistinguishable from data loss.
        """

        try:
            refs = self.client.studio_refs()
        except AdultEmpireError as exc:
            logger.warning(f"Adult Empire studio directory failed: {exc}")
            return 0

        if not refs:
            logger.warning("Adult Empire studio directory came back empty")
            return 0

        logger.debug(f"Adult Empire lists {len(refs)} studios")

        stored = 0

        for ref in refs:
            try:
                detail = self.client.studio_detail(ref)
            except AdultEmpireError as exc:
                logger.warning(f"Adult Empire unavailable, pausing sync: {exc}")
                break

            if detail.name is None:
                # No headline on the page means it is not a studio listing --
                # a redirect, or a slug that no longer resolves.
                continue

            if (detail.title_count or 0) < MIN_TITLES:
                continue

            if self._store(detail):
                stored += 1

        logger.info(f"Adult Empire studios synced: {stored} studios")

        return stored

    def _store(self, ref: StudioRef) -> bool:
        with db_session() as session:
            studio = session.execute(
                select(Studio).where(Studio.ae_id == ref.ae_id)
            ).scalar_one_or_none()

            if studio is None:
                studio = Studio(ae_id=ref.ae_id, name=ref.name or ref.slug)
                session.add(studio)

            studio.name = ref.name or studio.name
            studio.slug = ref.slug
            studio.title_count = ref.title_count
            studio.refreshed_at = datetime.now()

            # `saved` is deliberately never written here. It is the user's.
            session.commit()

        return True

    # -------------------------------------------------------------- artwork

    def enrich_batch(self, limit: int | None = None) -> int:
        """Attach TPDB artwork and description to studios that lack it.

        Bounded and resumable for the same reason every TPDB pass here is:
        the API allows two requests a second and the directory is a hundred
        studios deep.

        ``tpdb_checked_at`` records the attempt, not the outcome. Plenty of
        storefront studios have no TPDB site at all, and keying off
        ``tpdb_site_id`` alone would re-ask about those every single run.
        """

        api = tpdb_client()

        if api is None:
            return 0

        limit = limit or self.settings.studio_enrich_batch_size
        done = 0

        with db_session() as session:
            pending = (
                session.execute(
                    select(Studio)
                    .where(Studio.tpdb_checked_at.is_(None))
                    # Saved studios first: those are the ones actually on
                    # screen, and artwork is the whole point of this pass.
                    .order_by(
                        Studio.saved.desc(),
                        Studio.title_count.desc().nullslast(),
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            for studio in pending:
                site = self._match(api, studio.name)

                studio.tpdb_checked_at = datetime.now()

                if site is not None:
                    studio.tpdb_site_id = str(site.id) if site.id else None
                    studio.logo_path = site.logo or studio.logo_path
                    studio.poster_path = site.poster or studio.poster_path
                    # Blank for plenty of sites, so `or` rather than a
                    # straight assign: an empty string would overwrite a
                    # description a previous run had found.
                    studio.description = site.description or studio.description
                    done += 1

                session.commit()

        if done:
            logger.debug(f"Matched {done} studios to TPDB sites")

        return done

    @staticmethod
    def _match(api, name: str):
        """The TPDB site for this studio, or None."""

        try:
            results = api.search_sites(name) or []
        except Exception as exc:
            logger.debug(f"TPDB site lookup failed for {name!r}: {exc}")
            return None

        return pick_site(results, name)

    # ------------------------------------------------------------- listings

    def listing(
        self, studio: Studio, sort: str, pages: int = 1
    ) -> list[RankedTitle]:
        """A studio's titles in one of its ranked orders, read live."""

        if sort not in STUDIO_SORTS:
            raise AdultEmpireError(f"Unknown studio sort {sort!r}")

        if not studio.slug:
            return []

        ref = StudioRef(
            ae_id=studio.ae_id,
            slug=studio.slug,
            path=f"/{studio.ae_id}/studio/{studio.slug}.html",
        )

        return self.client.studio_listing(ref, sort, pages=pages)


def pick_site(results, name: str):
    """The TPDB site whose name is exactly this studio's, or None.

    Exact-on-normalised only, with no fuzzy fallback, and that restraint is
    the point. TPDB's site catalogue is full of near-namesakes -- a search for
    "Evil Angel" returns twenty-two sites, among them "Mylf X Evil Angel" --
    and the search endpoint returns them in its own order, so "best result"
    would frequently be a different studio's. Hanging the wrong network's logo
    on a studio is a mistake nobody can see to report.

    Normalising drops case and punctuation only, so "Rocco's" matches "Roccos"
    without letting "Evil Angel 2" through.
    """

    wanted = _normalise(name)

    for site in results:
        if site.name and _normalise(site.name) == wanted:
            return site

    return None


def _normalise(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())
