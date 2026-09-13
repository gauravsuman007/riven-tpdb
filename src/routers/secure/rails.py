"""The arrangement of a page's rows.

THE HOST STORES THE ORDER; IT DOES NOT STORE THE ROWS
-----------------------------------------------------

A layout here is a list of keys and whether each is on. What those keys mean
-- the title, where the items come from, whether a television can draw it --
belongs to whoever draws the rail: the frontend for its own built-in rows,
an add-on for the rows it contributes (``Addon.rails()``), which travel to
every surface in the add-on listing rather than through here.

That is why this router is so small, and it is worth keeping small. The
moment it stored a title, a retitled row would keep its old name on every
deployment that had ever saved a layout, and the stale copy would be the one
on screen.

EMPTY IS NOT THE SAME AS NOTHING
--------------------------------

A page with no saved layout answers with an empty list, and the surfaces read
that as "use your own defaults" -- every row it knows about, in its own
order. That is deliberately distinguishable from a layout that exists and has
every row switched off, which is a decision somebody made and which must be
honoured. The two would be identical if this returned defaults itself, and a
user who turned everything off would watch the page put it all back.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Path
from pydantic import BaseModel

from program.db.db import db_session
from program.rails import layout_for, save_layout


router = APIRouter(prefix="/rails", tags=["rails"])


class RailPlacement(BaseModel):
    key: str
    enabled: bool = True


class RailLayoutResponse(BaseModel):
    page: str
    #: In display order. A key here for a rail nothing offers any more is
    #: kept rather than pruned -- see `program.rails` for why.
    rails: list[RailPlacement]


def _read(page: str) -> RailLayoutResponse:
    with db_session() as session:
        return RailLayoutResponse(
            page=page,
            rails=[
                RailPlacement(key=record.rail_key, enabled=record.enabled)
                for record in layout_for(session, page)
            ],
        )


@router.get("/{page:path}", operation_id="get_rail_layout")
def get_layout(page: Annotated[str, Path()]) -> RailLayoutResponse:
    """One page's saved arrangement, or an empty list if it has never been
    arranged.

    ``page`` is a path parameter matching a path so that an add-on's page
    ("x/onlyfans") is one segment of vocabulary rather than two of routing.
    """

    return _read(page)


@router.put("/{page:path}", operation_id="set_rail_layout")
def set_layout(
    page: Annotated[str, Path()],
    rails: Annotated[list[RailPlacement], Body()],
) -> RailLayoutResponse:
    """Replace one page's arrangement.

    The whole ordering at once, because the order IS the state: moving one
    row by PATCH leaves the client and the server each holding half of it,
    and they disagree as soon as two tabs are open.

    Rails not named are left alone, so a surface that cannot see every row --
    an older frontend, a television, a page rendered while an add-on was
    reloading -- can save an order without deleting the rows it never knew
    about.
    """

    with db_session() as session:
        save_layout(session, page, [(rail.key, rail.enabled) for rail in rails])
        session.commit()

    return _read(page)
