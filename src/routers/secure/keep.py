"""Keeping library titles on local disk.

Everything in the library is streamed from the debrid provider on demand and
stored nowhere. These routes let one title be copied onto this server, and
report how far that has got.
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy import select

from program.db.db import db_session
from program.media.item import MediaItem
from program.media.local_copy import LocalCopy, LocalCopyState
from program.services.localsync import local_sync

router = APIRouter(
    prefix="/keep",
    tags=["keep"],
    responses={404: {"description": "Not found"}},
)


class KeepStatus(BaseModel):
    """One title's local copy, as the Keep button needs to render it."""

    # Absent entirely when the title has never been kept, so the button can
    # distinguish "not kept" from "kept, zero bytes so far".
    enabled: bool
    state: str | None = None
    percent: float = 0.0
    bytes_done: int = 0
    bytes_total: int = 0
    path: str | None = None
    error: str | None = None


class KeepSettings(BaseModel):
    enabled: bool
    path: str | None = None
    reason: str | None = None


def _status(copy: LocalCopy | None, enabled: bool) -> KeepStatus:
    if not copy:
        return KeepStatus(enabled=enabled)

    return KeepStatus(
        enabled=enabled,
        state=copy.state,
        percent=copy.percent,
        bytes_done=copy.bytes_done,
        bytes_total=copy.bytes_total,
        path=copy.path,
        error=copy.error,
    )


@router.get("", summary="Whether keeping to disk is configured", operation_id="keep_settings")
def keep_settings() -> KeepSettings:
    """So the UI can hide the button entirely rather than offer a failing one."""

    service = local_sync()
    ok, reason = service.validate()

    return KeepSettings(
        enabled=ok,
        path=str(service.root) if service.root else None,
        reason=reason or None,
    )


@router.get(
    "/status",
    summary="Local copy status for several items",
    operation_id="keep_status_many",
)
def keep_status_many(
    ids: Annotated[str, Query(description="Comma-separated media item ids")],
) -> dict[str, KeepStatus]:
    """One call for a whole page of posters, rather than one call per card."""

    try:
        wanted = [int(value) for value in ids.split(",") if value.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="ids must be integers") from exc

    if not wanted:
        return {}

    enabled = local_sync().validate()[0]

    with db_session() as session:
        rows = (
            session.execute(
                select(LocalCopy).where(LocalCopy.media_item_id.in_(wanted))
            )
            .scalars()
            .all()
        )

        found = {row.media_item_id: row for row in rows}

        return {
            str(item_id): _status(found.get(item_id), enabled) for item_id in wanted
        }


@router.get(
    "/{id}", summary="Local copy status for one item", operation_id="keep_status"
)
def keep_status(id: Annotated[int, Path()]) -> KeepStatus:
    enabled = local_sync().validate()[0]

    with db_session() as session:
        copy = (
            session.execute(
                select(LocalCopy).where(LocalCopy.media_item_id == id)
            )
            .scalars()
            .one_or_none()
        )

        return _status(copy, enabled)


@router.post("/{id}", summary="Keep an item on local disk", operation_id="keep_item")
def keep_item(id: Annotated[int, Path()]) -> KeepStatus:
    with db_session() as session:
        item = (
            session.execute(select(MediaItem).where(MediaItem.id == id))
            .unique()
            .scalar_one_or_none()
        )

        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        if not item.media_entry:
            raise HTTPException(
                status_code=409,
                detail="This title has not been downloaded yet, so there is nothing to copy",
            )

    try:
        copy = local_sync().request(id)
    except ValueError as exc:
        # A misconfigured path is the caller's problem to fix in settings, not
        # a server fault -- 409 rather than 500.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return _status(copy, True)


@router.delete(
    "/{id}", summary="Stop keeping an item on local disk", operation_id="unkeep_item"
)
def unkeep_item(
    id: Annotated[int, Path()],
    delete_file: Annotated[
        bool,
        Query(description="Also remove the copy from disk (default true)"),
    ] = True,
) -> dict[str, Any]:
    local_sync().cancel(id, delete_file=delete_file)

    return {"success": True, "deleted": delete_file}
