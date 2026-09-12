"""Building and enriching the performer index.

Two passes, deliberately separate.

`sync` walks each site's model index and records who is there. It is wide and
cheap: one request per page, 12-25 accounts each, and no per-account requests
at all. `enrich_batch` is narrow and expensive: one request per account, for
the avatar and bio that only exist on the account's own page.

Splitting them is what keeps the weekly job bounded. Enriching inline would
turn a five-site sync into tens of thousands of requests in one run; instead
the index fills immediately and the artwork arrives over the following hours,
prioritised so that accounts a user saved are done first.

The failure model throughout is the one the rest of this codebase uses:
degrade, never raise. A site that is down costs its own accounts and nothing
else, and a run that dies halfway leaves everything it already committed.
"""

import re
from datetime import datetime

from loguru import logger
from sqlalchemy import func, select

from program.db.db import db_session
from program.media.onlyfans import (
    OnlyFansAccount,
    OnlyFansAccountSource,
    OnlyFansSyncRun,
)
from program.settings import settings_manager
from program.utils.time import utcnow


_COLLAPSE_RE = re.compile(r"[^a-z0-9]+")

#: Handles shorter than this are almost always a parsing accident -- a
#: pagination link read as a performer, an empty slug -- rather than a real
#: account. Cheap guard on the way in, because a junk row is far more work to
#: find and remove later than to refuse now.
_MIN_HANDLE_LENGTH = 2


def normalise_handle(value: str) -> str:
    """The identity a performer is deduplicated on.

    Casefolded and stripped to alphanumerics, so ``Sophie Rain``,
    ``sophie-rain`` and ``sophie_rain`` are one account. Deliberately exact
    once collapsed and never fuzzy: merging two people who happen to have
    similar handles is silent and unrecoverable, and afterwards there is no
    way to tell which site contributed what. Refusing to guess is the same
    stance `studios.pick_site` takes for the same reason.
    """

    return _COLLAPSE_RE.sub("", (value or "").casefold())


class OnlyFansService:
    """The performer index: who exists, and which sites carry them."""

    def __init__(self) -> None:
        self.settings = settings_manager.settings.onlyfans
        self.initialized = False

        if not self.settings.enabled:
            return

        self.initialized = True
        logger.success("OnlyFans performer index initialized!")

    # --- Building the index -------------------------------------------------

    def sync(self, sites: list[str] | None = None) -> int:
        """Walk each configured site's model index. Returns accounts touched.

        `sites` narrows the run to a subset, which is what the per-site "run
        now" button in Settings uses. Unknown names are ignored rather than
        rejected: the list is configuration and a plugin can be removed from
        the folder between the page rendering and the button being pressed.
        """

        from program.services.onlyfans.registry import registry

        scrapers = registry().services
        wanted = [key for key in self.settings.sites if sites is None or key in sites]
        touched = 0

        for key in wanted:
            scraper = scrapers.get(key)

            if scraper is None:
                # Not an error: the sites list is configuration and a plugin
                # can be disabled from the Plugins tab at any time.
                logger.debug(f"OnlyFans: no scraper named {key}, skipping")
                continue

            if not getattr(scraper, "indexes_accounts", False):
                logger.debug(f"OnlyFans: {key} does not index accounts, skipping")
                continue

            # Each site gets its own guard. With five sources, letting one
            # failure end the run would mean a single dead site costs the
            # other four their weekly update -- and the sites in this family
            # go down and change domains often.
            try:
                touched += self._sync_site(key, scraper)
            except Exception as exc:
                logger.error(f"OnlyFans: {key} index failed: {exc}")
                _record(key, state="failed", error=str(exc)[:500], finished=True)

        logger.info(f"OnlyFans: indexed {touched} accounts")
        return touched

    def _sync_site(self, key: str, scraper) -> int:
        """One site's index, paged until the site says there is no more.

        These indexes are deeper than they look -- measured 2026-09-12:
        ultrathots 155 pages, hornyfap 246, porn4fans 66, porntn 8, notfans 2
        -- and each full walk costs about twenty seconds, so the page cap is a
        runaway guard rather than a budget. It was 3, which silently truncated
        the index to 223 accounts out of roughly eight thousand.
        """

        seen: set[str] = set()
        touched = 0
        created = 0
        page = 0

        _record(key, state="running", started=True, pages=0, seen=0, new=0, error=None)

        for page in range(1, self.settings.max_pages_per_site + 1):
            try:
                accounts = scraper.list_accounts(page)
            except Exception as exc:
                # THE END OF THE INDEX IS A 404, not an empty page. Four of
                # the five sites answer the page after the last one with a
                # 404, which reads as a site failure and used to lose the
                # whole site's count and log an error for an ordinary
                # outcome. Only a first-page failure is a real failure.
                if page > 1 and _is_end_of_index(exc):
                    logger.debug(f"OnlyFans: {key} index ends at page {page - 1}")
                    break
                raise

            if not accounts:
                break

            # A site that has run out of accounts may also serve the last page
            # again rather than 404ing, so "no new handles" is the other end
            # of the index.
            fresh = [a for a in accounts if a.handle not in seen]
            if not fresh:
                break
            seen.update(a.handle for a in fresh)

            for account in fresh:
                stored, is_new = self._store(key, account)
                touched += stored
                created += is_new

            # Per page, not per run: a walk of two hundred pages that reports
            # nothing until it finishes cannot answer "is this moving", which
            # is the only question anyone has while it runs.
            _record(key, pages=page, seen=len(seen), new=created)

        _record(
            key,
            state="ok",
            finished=True,
            pages=page,
            seen=len(seen),
            new=created,
            error=None,
        )
        logger.debug(
            f"OnlyFans: {key} walked {page} pages, {len(seen)} accounts, {created} new"
        )
        return touched

    def _store(self, key: str, account) -> tuple[bool, bool]:
        """Upsert one account and its source row. Returns (stored, created).

        The two are separate because a re-run must be legible: "3859 seen, 0
        new" says the walk worked and nothing had changed, which a single
        number cannot.

        One session and one commit per account rather than per run: a sync
        that dies on its four-thousandth account keeps the first three
        thousand nine hundred and ninety-nine.
        """

        handle = normalise_handle(account.handle)

        if len(handle) < _MIN_HANDLE_LENGTH:
            logger.debug(f"OnlyFans: {key} yielded unusable handle {account.handle!r}")
            return False, False

        try:
            with db_session() as session:
                existing = session.execute(
                    select(OnlyFansAccount).where(OnlyFansAccount.handle == handle)
                ).scalar_one_or_none()
                created = existing is None

                if existing is None:
                    existing = OnlyFansAccount(
                        handle=handle,
                        display_name=account.display_name or account.handle,
                        avatar_url=account.avatar,
                        bio=account.bio,
                    )
                    session.add(existing)
                    session.flush()
                else:
                    # `or` per field, so a site that carries no avatar cannot
                    # blank one already found on a sibling site. Three of the
                    # five render "no image" for every model, which makes this
                    # the normal case rather than an edge one.
                    existing.avatar_url = existing.avatar_url or account.avatar
                    existing.bio = existing.bio or account.bio

                existing.refreshed_at = utcnow()

                source = session.execute(
                    select(OnlyFansAccountSource).where(
                        OnlyFansAccountSource.account_id == existing.id,
                        OnlyFansAccountSource.site == key,
                    )
                ).scalar_one_or_none()

                if source is None:
                    source = OnlyFansAccountSource(
                        account_id=existing.id, site=key
                    )
                    session.add(source)

                # The site's own slug, not the collapsed handle: it is what
                # this site's URLs are built from and cannot be derived back.
                source.site_handle = account.handle
                source.page_url = account.page_url
                source.video_count = account.video_count
                source.image_count = account.image_count
                source.refreshed_at = utcnow()

                session.flush()
                # Recounted from the table rather than incremented: an
                # increment is only correct the first time a site contributes
                # an account, and a re-run of the sync would otherwise turn
                # this into a count of how many times the job has run.
                existing.source_count = (
                    session.execute(
                        select(func.count())
                        .select_from(OnlyFansAccountSource)
                        .where(OnlyFansAccountSource.account_id == existing.id)
                    ).scalar_one()
                )

                session.commit()
                return True, created
        except Exception as exc:
            logger.debug(f"OnlyFans: could not store {key}:{account.handle}: {exc}")
            return False, False

    # --- Enrichment ---------------------------------------------------------

    def enrich_batch(self, limit: int | None = None) -> int:
        """Fetch profiles for accounts that still have no artwork.

        Ordered so that the accounts someone actually follows are done first,
        then the ones the most sites agree exist. Returns accounts attempted,
        not accounts improved -- a miss is a normal outcome here.
        """

        from program.services.onlyfans.registry import registry

        limit = limit or self.settings.enrich_batch_size
        scrapers = registry().services

        with db_session() as session:
            pending = (
                session.execute(
                    select(OnlyFansAccount)
                    .where(OnlyFansAccount.avatar_url.is_(None))
                    .order_by(
                        OnlyFansAccount.saved.desc(),
                        OnlyFansAccount.source_count.desc(),
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            account_ids = [account.id for account in pending]

        attempted = 0
        for account_id in account_ids:
            try:
                if self._enrich_one(account_id, scrapers):
                    attempted += 1
            except Exception as exc:
                logger.debug(f"OnlyFans: enrichment failed for {account_id}: {exc}")

        return attempted

    def _enrich_one(self, account_id: int, scrapers: dict) -> bool:
        with db_session() as session:
            account = session.get(OnlyFansAccount, account_id)

            if account is None:
                return False

            for source in account.sources:
                scraper = scrapers.get(source.site)

                if scraper is None:
                    continue

                profile = scraper.account_profile(source.site_handle)

                if profile is not None:
                    account.avatar_url = account.avatar_url or profile.avatar
                    account.bio = account.bio or profile.bio

                if account.avatar_url:
                    break

                # THE NEWEST VIDEO'S THUMBNAIL, as the avatar of last resort.
                #
                # Three of the five sites render "no image" for every model in
                # their index AND on the model's own page, so an account
                # carried only by those had no picture at all and fell back to
                # its initials -- which was most of the index. Every one of
                # them does carry video thumbnails, and a still from the
                # performer's own content is a far better answer than two
                # letters.
                #
                # Stored in the same column deliberately: the card wants "a
                # picture of this person", and keeping a second column for
                # "but it came from a video" would have every reader choose
                # between them identically.
                try:
                    videos = scraper.account_videos(source.site_handle, 1)
                except Exception as exc:
                    logger.debug(f"OnlyFans: {source.site} videos failed: {exc}")
                    continue

                for video in videos:
                    if video.thumbnail:
                        account.avatar_url = video.thumbnail
                        break

                if account.avatar_url:
                    break

            if self.settings.onlyfans_enrich and account.of_checked_at is None:
                public = _public_profile(account.handle)
                # Stamped whether or not it found anything. Without this the
                # weekly pass would retry every miss forever, and misses are
                # the expected outcome -- see `_public_profile`.
                account.of_checked_at = utcnow()

                if public:
                    account.avatar_url = account.avatar_url or public.get("avatar")
                    account.bio = account.bio or public.get("bio")

            account.refreshed_at = utcnow()
            session.commit()
            return True


#: An unfinished run older than this is treated as abandoned rather than in
#: progress. Nothing can correct a `running` row once the process that wrote
#: it is gone, and a permanently spinning progress bar is worse than a stale
#: result: it is a claim that something is still happening.
STALE_AFTER = 3600


def _is_end_of_index(exc: Exception) -> bool:
    """Whether a page request failed because there are no more pages.

    Four of the five sites 404 the page AFTER the last one rather than
    serving an empty list, so this is the ordinary way a walk ends, not a
    fault. Both exception shapes are checked because the scrapers do not
    agree on an HTTP client -- two use `requests` (an HTTPError carrying a
    `response`) and two use `urllib` (an HTTPError that IS the response, with
    `code`). Matching on only one of them made half the sites log an error
    and lose their count at the end of every successful walk.
    """

    status = getattr(getattr(exc, "response", None), "status_code", None)

    if status is None:
        status = getattr(exc, "code", None)

    return status in (404, 410)


def _record(
    site: str,
    *,
    state: str | None = None,
    started: bool = False,
    finished: bool = False,
    pages: int | None = None,
    seen: int | None = None,
    new: int | None = None,
    error: str | None = None,
) -> None:
    """Write one site's progress. Never raises.

    Status is a readout, so a failure to write it must not be able to end the
    run it is describing -- that would turn "the progress bar broke" into "the
    sync died", which is exactly backwards.
    """

    try:
        with db_session() as session:
            run = session.get(OnlyFansSyncRun, site)

            if run is None:
                run = OnlyFansSyncRun(site=site)
                session.add(run)

            if state is not None:
                run.state = state
            if started:
                run.started_at = utcnow()
                run.finished_at = None
            if finished:
                run.finished_at = utcnow()
            if pages is not None:
                run.pages = pages
            if seen is not None:
                run.accounts_seen = seen
            if new is not None:
                run.accounts_new = new

            # Cleared explicitly on a good run rather than left behind: a
            # stale error next to a green state reads as a current problem.
            if error is not None or state in ("running", "ok"):
                run.error = error

            session.commit()
    except Exception as exc:
        logger.debug(f"OnlyFans: could not record status for {site}: {exc}")


def _public_profile(handle: str) -> dict[str, str] | None:
    """Best-effort read of a public onlyfans.com profile.

    MEASURED 2026-09-12, AND IT NEVER WORKS. The TLS impersonation clears the
    handshake and every handle answers 200 -- but with the SAME 17669-byte
    application shell, whose Open Graph tags are the OnlyFans logo and the
    site's own marketing copy. There is no per-account data in the response at
    all. Two real handles returned byte-identical pages.

    That makes this worse than useless if it appears to succeed: adopting
    those tags would set the same logo as the avatar and the same boilerplate
    as the bio on every account in the index. The only reason it never did is
    that the patterns below expect quoted `content="..."` and the shell emits
    it unquoted, so the match failed and nothing was written -- an accident,
    not a safeguard, which is why there is now an explicit one.

    Kept rather than deleted because the wall is theirs and may move: if a
    profile ever renders its own tags again, this will pick them up. The
    setting that enables it now defaults to off.
    """

    try:
        from curl_cffi import requests as curl_requests

        from program.services.vpn import SCRAPING, vpn

        response = curl_requests.get(
            f"https://onlyfans.com/{handle}",
            impersonate="chrome124",
            proxies=vpn().proxies_for(SCRAPING) or None,
            timeout=15,
        )

        if response.status_code != 200:
            return None

        # Open Graph tags, quoted or not -- the shell emits them bare.
        avatar = re.search(
            r'<meta property="?og:image"? content="?([^">]+)"?', response.text
        )
        bio = re.search(
            r'<meta property="?og:description"? content="([^"]+)"', response.text
        )

        found = {}
        if avatar:
            found["avatar"] = avatar.group(1)
        if bio:
            found["bio"] = bio.group(1)

        # THE GENERIC SHELL, REFUSED. Its og:image is the OnlyFans logo and
        # its og:description is the site's marketing blurb, so adopting
        # either would stamp the same avatar and the same bio on every
        # account. Matched on the logo path rather than the body length,
        # which would change the first time they rebuild the bundle.
        if "of-logo" in found.get("avatar", "") or found.get("bio", "").startswith(
            "OnlyFans is the social platform"
        ):
            return None

        return found or None
    except Exception:
        # Deliberately silent at debug level only: this failing is the normal
        # case and logging it as a warning would fill the log with expected
        # outcomes once per account per week.
        logger.debug(f"OnlyFans: no public profile for {handle}")
        return None
