"""Performer accounts mirrored from the OnlyFans archive sites.

An account is a *person*, not a title, and that is the whole reason this is its
own model rather than a :class:`~program.media.collection.Collection`. A
collection entry answers "does this title exist and do we own it"; an account
answers "who is this, and which sites carry them". Nothing here is ever
promoted into the library: an account is a browsing surface, and the videos
under it are resolved live from the sites at request time.

The split into two tables is the point of the design.

``OnlyFansAccount`` is the deduplicated person. Five sites carry overlapping
rosters and spell the same handle three ways (``sophie-rain``, ``sophierain``,
``Sophie Rain``), so the identity is ``handle`` -- the collapsed, casefolded,
alphanumeric-only form. Deduplication is *exact on the collapsed form* and
deliberately not fuzzy: two different people with similar handles merging into
one account would be silently unrecoverable, and there is no way to tell
afterwards which site contributed what. This is the same refusal-rather-than-
guess stance as `studios.pick_site` and `assign_provider_id`.

``OnlyFansAccountSource`` is one site's copy of that person. It holds the
site's *own* slug, which is what its URLs are built from and therefore what
every content request needs -- the collapsed handle cannot be turned back into
``sophie-rain``. This table is also what the account detail page reads to know
which per-site buttons to offer.

Artwork is nullable and frequently absent, which is a property of the sources
rather than an oversight: three of the five sites render "no image" for every
model in their index. An account carried only by those three legitimately has
no avatar until enrichment finds one elsewhere.
"""

from datetime import datetime

import sqlalchemy
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from program.db.base_model import Base
from program.utils.time import utcnow


class OnlyFansAccount(Base):
    """One performer, deduplicated across every site that carries them."""

    __tablename__ = "OnlyFansAccount"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)

    # The identity: lowercase, alphanumeric only. Never shown to the user --
    # `display_name` is for that -- because it has thrown away the casing and
    # separators that make a handle readable.
    handle: Mapped[str] = mapped_column(sqlalchemy.String, unique=True, index=True)

    # As the site spelled it ("BeriGalaxy"). The first non-empty one wins and
    # later syncs do not overwrite it: the sites disagree on casing and a
    # weekly sync that reshuffled display names would churn for no gain.
    display_name: Mapped[str] = mapped_column(sqlalchemy.String, index=True)

    avatar_url: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    bio: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)

    # WHERE THE PICTURE CAME FROM, and the only reason this column exists.
    # An archive site's own thumbnail -- or, for the three that publish no
    # avatar at all, a still from the performer's newest video there -- is a
    # good answer and a bad one to KEEP: once the performer's own profile is
    # found, its picture must replace the borrowed one, and an
    # `avatar_url or ...` can never do that because the column is full.
    # True means borrowed and replaceable; False means it is theirs.
    avatar_from_site: Mapped[bool] = mapped_column(
        sqlalchemy.Boolean, default=False, server_default="false"
    )

    # --- from the performer's own onlyfans.com profile ----------------------
    # All nullable and all independent: a profile that is found fills these,
    # one that is not leaves them alone, and nothing else in the index reads
    # them, so a site that stops answering costs detail rather than rows.
    of_user_id: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    # As OnlyFans spells it, which is NOT `handle`: that one has been stripped
    # to alphanumerics for deduplication and would 404 for anyone whose name
    # contains a dot or an underscore.
    of_username: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    header_url: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    website: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    location: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    is_verified: Mapped[bool] = mapped_column(
        sqlalchemy.Boolean, default=False, server_default="false"
    )
    posts_count: Mapped[int | None] = mapped_column(sqlalchemy.Integer, nullable=True)
    photos_count: Mapped[int | None] = mapped_column(sqlalchemy.Integer, nullable=True)
    videos_count: Mapped[int | None] = mapped_column(sqlalchemy.Integer, nullable=True)
    likes_count: Mapped[int | None] = mapped_column(sqlalchemy.Integer, nullable=True)
    subscribe_price: Mapped[float | None] = mapped_column(
        sqlalchemy.Float, nullable=True
    )

    # How many sites carry this account. Ordering signal: a performer three
    # sites independently indexed is more likely to be a real, findable
    # account than one that appears once.
    source_count: Mapped[int] = mapped_column(
        sqlalchemy.Integer, default=0, index=True, server_default="0"
    )

    # Set by the user, cleared by the user. Never touched by a sync, for the
    # same reason `Studio.saved` is not: a weekly refresh that dropped saved
    # accounts is indistinguishable from data loss.
    saved: Mapped[bool] = mapped_column(
        sqlalchemy.Boolean, default=False, index=True, server_default="false"
    )
    saved_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy.DateTime(timezone=True), default=utcnow
    )
    refreshed_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )
    # Separate from refreshed_at, and stamped on failure as well as success:
    # the public onlyfans.com fetch is expected to fail for most accounts, and
    # without its own stamp every weekly sync would retry every miss forever.
    of_checked_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )

    sources: Mapped[list["OnlyFansAccountSource"]] = relationship(
        "OnlyFansAccountSource",
        back_populates="account",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<OnlyFansAccount {self.display_name} ({self.handle})>"


class OnlyFansAccountSource(Base):
    """One site's copy of an account.

    ``site_handle`` is the site's own slug and is not derivable from the
    account's collapsed handle, which is exactly why it is stored: every
    content request for this account on this site is built from it.
    """

    __tablename__ = "OnlyFansAccountSource"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)

    account_id: Mapped[int] = mapped_column(
        sqlalchemy.Integer,
        sqlalchemy.ForeignKey("OnlyFansAccount.id", ondelete="CASCADE"),
        index=True,
    )

    # The scraper key, e.g. ``ultrathots``. Matches `DirectScraper.key`, which
    # is what routes a content request back to the right plugin.
    site: Mapped[str] = mapped_column(sqlalchemy.String, index=True)
    site_handle: Mapped[str] = mapped_column(sqlalchemy.String)
    page_url: Mapped[str] = mapped_column(sqlalchemy.String)

    # What the site's index claimed. Nullable rather than zero: "this site did
    # not say" and "this site holds nothing" are different answers, and only
    # one of them is a reason to hide the button.
    video_count: Mapped[int | None] = mapped_column(
        sqlalchemy.Integer, nullable=True
    )
    image_count: Mapped[int | None] = mapped_column(
        sqlalchemy.Integer, nullable=True
    )

    refreshed_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )

    account: Mapped["OnlyFansAccount"] = relationship(
        "OnlyFansAccount", back_populates="sources"
    )

    __table_args__ = (
        # One row per account per site. Without this a re-run of the sync
        # doubles every source instead of updating it, and `source_count`
        # becomes a count of how many times the job has run.
        UniqueConstraint("account_id", "site", name="ux_onlyfans_source_site"),
        Index("ix_onlyfans_source_site_handle", "site", "site_handle"),
    )

    def __repr__(self) -> str:
        return f"<OnlyFansAccountSource {self.site}:{self.site_handle}>"


class OnlyFansSyncRun(Base):
    """What the index walk did on one site, last time it ran.

    One row per site, overwritten each run, and written AS the run goes rather
    than at the end. That is the whole point: a full walk of the largest site
    is several hundred pages, and a status that only appears once the job
    finishes cannot answer the question anyone actually has while waiting,
    which is "is this moving, and how far along is it".

    A row survives a restart, so "when did this site last succeed" is
    answerable after a crash -- which is exactly when it is worth asking. A
    run interrupted by a restart is left as `running` with a stale
    `started_at`; the reader treats an unfinished run older than
    `STALE_AFTER` as abandoned rather than in progress, because there is no
    process left to correct it.
    """

    __tablename__ = "OnlyFansSyncRun"

    site: Mapped[str] = mapped_column(sqlalchemy.String, primary_key=True)

    #: "running", "ok", or "failed". Not an enum: a new state is a migration
    #: for no benefit, and every reader treats anything it does not know as
    #: "not running".
    state: Mapped[str] = mapped_column(sqlalchemy.String, default="running")

    started_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )

    #: Pages fetched so far. The index has no page count to compare against --
    #: these sites stop by 404ing the page after the last one -- so progress
    #: is honestly a count, never a percentage.
    pages: Mapped[int] = mapped_column(sqlalchemy.Integer, default=0)

    #: Distinct handles this site offered, and how many of them were new to
    #: the index. The second number is what makes a re-run legible: 3859 seen
    #: and 0 new means the walk worked and nothing had changed.
    accounts_seen: Mapped[int] = mapped_column(sqlalchemy.Integer, default=0)
    accounts_new: Mapped[int] = mapped_column(sqlalchemy.Integer, default=0)

    #: Why it stopped, when it stopped badly. Kept verbatim; a site in this
    #: family goes down or changes domain often enough that the text is the
    #: useful part.
    error: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)

    def __repr__(self) -> str:
        return f"<OnlyFansSyncRun {self.site} {self.state} pages={self.pages}>"
