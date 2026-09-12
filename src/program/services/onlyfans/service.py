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
from program.media.onlyfans import OnlyFansAccount, OnlyFansAccountSource
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
        self.settings = settings_manager.settings.content.onlyfans
        self.initialized = False

        if not self.settings.enabled:
            return

        self.initialized = True
        logger.success("OnlyFans performer index initialized!")

    # --- Building the index -------------------------------------------------

    def sync(self) -> int:
        """Walk every configured site's model index. Returns accounts touched."""

        from program.services.onlyfans.registry import registry

        scrapers = registry().services
        touched = 0

        for key in self.settings.sites:
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

        logger.info(f"OnlyFans: indexed {touched} accounts")
        return touched

    def _sync_site(self, key: str, scraper) -> int:
        """One site's index, paged until it stops producing new accounts."""

        seen: set[str] = set()
        touched = 0

        for page in range(1, self.settings.max_pages_per_site + 1):
            accounts = scraper.list_accounts(page)

            if not accounts:
                break

            # A site that has run out of accounts serves the last page again
            # rather than an empty one, so "no new handles" is the real end of
            # the index. Paging to `max_pages_per_site` regardless would make
            # every sync do the maximum number of requests on every site.
            fresh = [a for a in accounts if a.handle not in seen]
            if not fresh:
                break
            seen.update(a.handle for a in fresh)

            for account in fresh:
                if self._store(key, account):
                    touched += 1

        logger.debug(f"OnlyFans: {key} contributed {touched} accounts")
        return touched

    def _store(self, key: str, account) -> bool:
        """Upsert one account and its source row.

        One session and one commit per account rather than per run: a sync
        that dies on its four-thousandth account keeps the first three
        thousand nine hundred and ninety-nine.
        """

        handle = normalise_handle(account.handle)

        if len(handle) < _MIN_HANDLE_LENGTH:
            logger.debug(f"OnlyFans: {key} yielded unusable handle {account.handle!r}")
            return False

        try:
            with db_session() as session:
                existing = session.execute(
                    select(OnlyFansAccount).where(OnlyFansAccount.handle == handle)
                ).scalar_one_or_none()

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
                return True
        except Exception as exc:
            logger.debug(f"OnlyFans: could not store {key}:{account.handle}: {exc}")
            return False

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

                if profile is None:
                    continue

                account.avatar_url = account.avatar_url or profile.avatar
                account.bio = account.bio or profile.bio

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


def _public_profile(handle: str) -> dict[str, str] | None:
    """Best-effort read of a public onlyfans.com profile.

    onlyfans.com has no public API, and its profile pages sit behind
    Cloudflare plus signed-request auth, so this is expected to fail for most
    accounts and returns None rather than raising when it does. It exists
    because when it *does* work it is the only source of the performer's own
    words, and the archive sites' bios are copies at best.

    The TLS fingerprint is the part worth trying -- as with
    `noodlemagazine`, the first obstacle is the handshake rather than
    JavaScript -- but unlike that site there is a real auth wall behind it, so
    a 200 here is the exception.
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

        # The public profile renders its name and description into Open Graph
        # tags, which survive when the rest of the page is a login wall.
        avatar = re.search(
            r'<meta property="og:image" content="([^"]+)"', response.text
        )
        bio = re.search(
            r'<meta property="og:description" content="([^"]+)"', response.text
        )

        found = {}
        if avatar:
            found["avatar"] = avatar.group(1)
        if bio:
            found["bio"] = bio.group(1)
        return found or None
    except Exception:
        # Deliberately silent at debug level only: this failing is the normal
        # case and logging it as a warning would fill the log with expected
        # outcomes once per account per week.
        logger.debug(f"OnlyFans: no public profile for {handle}")
        return None
