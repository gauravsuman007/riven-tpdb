"""Which rows a page shows, and in what order.

WHAT THIS OWNS, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------

It owns the ARRANGEMENT: for one page, an ordered list of rail keys and
whether each is on. That is all. It does not own what a rail *is* -- not its
title, not where its items come from, not whether it can be drawn on a
television. Those belong to whoever defines the rail: the frontend for its
own built-in rows, an add-on for the rows it contributes.

That split is the point. A rail's definition changes with the code that draws
it and travels with that code; an arrangement is the user's and must survive
every one of those releases. Storing a title here would mean a retitled row
keeps its old name on every deployment that ever saved a layout, and the
stale copy is the one on screen.

THE KEY IS THE ONLY THING THAT CROSSES
--------------------------------------

So a row that stops being offered -- an add-on disabled, a built-in retired --
simply does not render, and its saved position is left untouched rather than
tidied away. Re-enable the add-on and the row comes back where it was. A
cleanup job that deleted rows for unknown keys would turn "disabled for the
afternoon" into "rearrange all your pages again", which is the same reason
the host never deletes an add-on's schema on removal.

UNKNOWN PAGES ARE ALLOWED
-------------------------

``page`` is a free string -- "home", "explore", "x/onlyfans" -- and is never
validated against a list of known pages. An add-on installed next year has a
page nothing here has heard of, and the alternative to accepting it is a
release of the host every time somebody installs one.
"""

from sqlalchemy import Boolean, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from program.db.base_model import Base


class RailLayout(Base):
    """One rail's place on one page."""

    __tablename__ = "RailLayout"
    __table_args__ = (UniqueConstraint("page", "rail_key", name="uq_rail_page_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    #: "home", "explore", or "x/<addon key>". Free-form on purpose.
    page: Mapped[str] = mapped_column(String, nullable=False, index=True)

    #: The rail's stable identity, as its definer declares it.
    rail_key: Mapped[str] = mapped_column(String, nullable=False)

    #: Ascending. Gaps are fine and expected -- a saved order is rewritten
    #: whole, and a row for a rail nobody offers any more keeps its number.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Off is a decision, and a different one from absent. A rail that is
    #: merely off keeps its place in the order, so turning it back on does
    #: not drop it at the bottom of a page somebody arranged months ago.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


def layout_for(session: Session, page: str) -> list[RailLayout]:
    """The saved arrangement of one page, in order.

    Empty means "never arranged", which is NOT the same as "every row off".
    The caller renders its own defaults for an empty layout; see the note in
    the router about why that distinction is worth keeping.
    """

    return list(
        session.execute(
            select(RailLayout)
            .where(RailLayout.page == page)
            .order_by(RailLayout.position, RailLayout.id)
        )
        .scalars()
        .all()
    )


def save_layout(session: Session, page: str, rails: list[tuple[str, bool]]) -> None:
    """Replace one page's arrangement with this one.

    Whole-list rather than per-rail, because the order is the state: a PATCH
    of one row's position means the client and the server each hold half an
    ordering, and they disagree the moment two tabs are open.

    Rails NOT named here are left exactly as they were -- that is what lets a
    client which cannot see an add-on's rows (an older frontend, a
    television) save an order without silently deleting the rows it did not
    know about. Positions for the named rails start after nothing and count
    up; unnamed rows keep whatever number they had, so they sort where their
    number puts them.
    """

    existing = {record.rail_key: record for record in layout_for(session, page)}

    for position, (key, enabled) in enumerate(rails):
        record = existing.get(key)

        if record is None:
            session.add(
                RailLayout(
                    page=page, rail_key=key, position=position, enabled=enabled
                )
            )
        else:
            record.position = position
            record.enabled = enabled
