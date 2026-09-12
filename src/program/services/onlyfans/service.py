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

        # An account still needs work if it has no picture at all -- or if the
        # picture it has was borrowed from an archive site and its own profile
        # has not been looked for yet. The second half is conditional on the
        # setting for a reason: with the profile pass off, nothing ever stamps
        # `of_checked_at`, so an unconditional clause would re-select the same
        # accounts forever and the ones with no picture would never come up.
        wanted = OnlyFansAccount.avatar_url.is_(None)

        if self.settings.onlyfans_enrich:
            wanted = wanted | OnlyFansAccount.of_checked_at.is_(None)

        with db_session() as session:
            pending = (
                session.execute(
                    select(OnlyFansAccount)
                    .where(wanted)
                    .order_by(
                        OnlyFansAccount.saved.desc(),
                        # Nothing at all before merely-borrowed.
                        OnlyFansAccount.avatar_url.is_(None).desc(),
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

            # THE PERFORMER'S OWN PROFILE FIRST, because it is the only
            # source here that is actually about the person rather than about
            # one archive's copy of them: real picture, real bio, real counts.
            # Everything below is a substitute for it.
            if self.settings.onlyfans_enrich and account.of_checked_at is None:
                self._apply_onlyfans_profile(account)

            for source in account.sources:
                scraper = scrapers.get(source.site)

                if scraper is None:
                    continue

                profile = scraper.account_profile(source.site_handle)

                if profile is not None:
                    if profile.avatar and not account.avatar_url:
                        account.avatar_url = profile.avatar
                        # Borrowed: the performer's own profile picture
                        # replaces it if one is ever found.
                        account.avatar_from_site = True

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
                        account.avatar_from_site = True
                        break

                if account.avatar_url:
                    break

            account.refreshed_at = utcnow()
            session.commit()
            return True

    # --- The performer's own profile ----------------------------------------

    #: Candidates tried per account before giving up. Each is one request, and
    #: the index runs to thousands of accounts, so this is a budget rather
    #: than an exhaustive search: the first two forms cover almost everything
    #: and the tail is guesswork that costs the same as a hit.
    _MAX_CANDIDATES = 4

    def _of_candidates(self, account: OnlyFansAccount) -> list[str]:
        """The usernames this account might have on onlyfans.com.

        `handle` has been stripped to alphanumerics so that three sites'
        spellings collapse to one identity, which makes it exactly wrong as a
        URL for anyone whose real username contains a dot or an underscore.
        The sites' own slugs usually preserve the separator, so they go first;
        the collapsed form is the fallback, and the last two are the two
        separators OnlyFans actually allows, reinstated.
        """

        candidates: list[str] = []

        for source in account.sources:
            slug = (source.site_handle or "").strip().strip("/")

            # Archive slugs are hyphenated by convention; OnlyFans usernames
            # cannot contain a hyphen, so a hyphenated slug is the site's
            # spelling and not a username.
            if slug and "-" not in slug:
                candidates.append(slug)

        candidates.append(account.handle)
        candidates.append(account.handle.replace(" ", "_"))

        for source in account.sources:
            slug = (source.site_handle or "").strip().strip("/")

            if slug and "-" in slug:
                candidates.extend([slug.replace("-", "_"), slug.replace("-", ".")])

        seen: list[str] = []
        for candidate in candidates:
            if candidate and candidate.casefold() not in [
                value.casefold() for value in seen
            ]:
                seen.append(candidate)

        return seen[: self._MAX_CANDIDATES]

    def _apply_onlyfans_profile(self, account: OnlyFansAccount) -> None:
        """Fill the account from onlyfans.com. Never raises.

        The stamp is the subtle part. `of_checked_at` means "asked and
        answered", so it is written for a hit and for a definitive 404 -- but
        NOT when every candidate merely failed. A rate limit or a signing
        rotation looks like a miss from here, and stamping those would
        permanently write off every account the pass happened to reach during
        the outage.
        """

        from program.services.onlyfans import profile as of_profile

        found: dict | None = None
        definitive = False

        try:
            for candidate in self._of_candidates(account):
                outcome, data = of_profile.profile(candidate)

                if outcome == "ok" and data:
                    found = data
                    definitive = True
                    break

                if outcome == "missing":
                    # This candidate is not an account; the next one still
                    # might be. Only meaningful once they ALL say so.
                    definitive = True
                    continue

                # "error" -- we do not know. Stop, and stamp nothing.
                definitive = False
                break
        except Exception as exc:
            logger.debug(f"OnlyFans: profile lookup failed for {account.handle}: {exc}")
            return

        if definitive:
            account.of_checked_at = utcnow()

        if not found:
            return

        # The picture and the bio OVERWRITE what an archive site lent us --
        # that is the whole point of the pass -- but never overwrite a
        # previous profile hit, and never clear a field the profile left
        # empty. A performer with no bio on OnlyFans should not lose the one
        # an archive site wrote for them.
        if found.get("avatar") and (account.avatar_url is None or account.avatar_from_site):
            account.avatar_url = found["avatar"]
            account.avatar_from_site = False

        if found.get("bio"):
            account.bio = found["bio"]

        for column, key in (
            ("header_url", "header"),
            ("website", "website"),
            ("location", "location"),
            ("of_user_id", "of_user_id"),
            ("of_username", "of_username"),
            ("posts_count", "posts_count"),
            ("photos_count", "photos_count"),
            ("videos_count", "videos_count"),
            ("likes_count", "likes_count"),
            ("subscribe_price", "subscribe_price"),
        ):
            if found.get(key) is not None:
                setattr(account, column, found[key])

        account.is_verified = bool(found.get("is_verified"))

        logger.debug(
            f"OnlyFans: profile matched {account.handle} -> {found['of_username']}"
        )


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
