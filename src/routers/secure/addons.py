"""Managing add-ons: listing, installing, updating, disabling, removing.

This router is also what makes an add-on's *settings tab* and *navigation
entry* free. The frontend reads this list and renders a tab per add-on
straight from the JSON Schema below, so an add-on contributes both without
shipping a line of frontend code.

The destructive operations are deliberately two different things. Removing an
add-on deletes its folder and leaves its schema standing, so reinstalling it
finds its data where it left it. Purging drops the schema -- one statement,
complete -- and is refused unless the caller names the add-on back, because
there is nothing to undo it with.
"""

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from program.addons import registry
from program.addons import database as addon_db
from program.addons.installer import InstallError, install, uninstall, update
from program.settings import settings_manager


router = APIRouter(prefix="/addons", tags=["addons"])


class AddonResponse(BaseModel):
    key: str
    name: str
    description: str = ""
    version: str = "0.0.0"
    #: "ok", "disabled" or "failed". A failed add-on is still listed, with its
    #: reason: a folder on disk that silently does not appear is the one
    #: outcome the user cannot diagnose.
    state: str
    error: str | None = None
    source: str | None = None
    revision: str | None = None
    nav: dict[str, Any] | None = None
    #: The add-on's settings as JSON Schema, which is all the settings page
    #: needs to render its tab.
    settings_schema: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    #: What a purge would destroy, so "remove" and "remove with its 6,402
    #: accounts" are visibly different decisions.
    tables: int = 0
    bytes: int = 0
    status: dict[str, Any] = {}


class AddonsResponse(BaseModel):
    addons: list[AddonResponse]
    directory: str


def _describe(key: str) -> AddonResponse:
    record = registry().get(key)

    if record is None:
        raise HTTPException(status_code=404, detail=f"No add-on named {key}")

    size = addon_db.schema_size(key)
    status: dict[str, Any] = {}

    if record.state == "ok" and record.addon is not None:
        try:
            status = record.addon.status()
        except Exception as exc:
            # A broken status() must not be able to hide the add-on from the
            # page that would let you remove it.
            logger.debug(f"Addon {key}: status() raised: {exc}")

    return AddonResponse(
        key=record.key,
        name=record.name or record.key,
        description=record.description,
        version=record.version,
        state=record.state,
        error=record.error,
        source=record.source,
        revision=record.revision,
        nav=record.nav,
        settings_schema=record.settings_schema,
        settings=settings_manager.settings.addons.get(key),
        tables=size["tables"],
        bytes=size["bytes"],
        status=status,
    )


def _all() -> AddonsResponse:
    return AddonsResponse(
        addons=[_describe(key) for key in sorted(registry().addons)],
        directory=str(registry().directory),
    )


@router.get("", operation_id="list_addons")
def list_addons() -> AddonsResponse:
    return _all()


@router.post("/rescan", operation_id="rescan_addons")
def rescan() -> AddonsResponse:
    """Re-read the folder: stop everything, load it all again.

    Also the way an updated add-on takes effect, since the loader drops its
    cached modules on the way out.
    """

    registry().discover()
    _reconcile()
    return _all()


class InstallRequest(BaseModel):
    url: str
    #: Branch or tag. Omitted takes the repository's default branch.
    ref: str | None = None
    #: For a private repository. Falls back to `settings.addons_git_token`, so
    #: the usual case is to set it once rather than paste it per install. Never
    #: stored here and never returned by any endpoint in this router.
    token: str | None = None


@router.post("/install", operation_id="install_addon")
def install_addon(body: Annotated[InstallRequest, Body()]) -> AddonsResponse:
    """Clone an add-on from git and load it.

    Understand what this does: an add-on runs in this process with this
    database and these credentials. Installing one is running someone else's
    code as Riven. The URL is checked, the clone is validated before it is
    kept, and that is the extent of the protection -- there is no sandbox.
    """

    try:
        key = install(
            body.url,
            registry().directory,
            ref=body.ref,
            token=body.token or settings_manager.settings.addons_git_token or None,
        )
    except InstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"Addons: install of {body.url} failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Install failed: {exc}")

    # A newly installed add-on is never silently disabled by a stale entry
    # left over from a previous removal.
    disabled = settings_manager.settings.addons_disabled

    if key in disabled:
        disabled.remove(key)
        settings_manager.save()

    registry().discover()
    _reconcile()
    return _all()


@router.post("/{key}/update", operation_id="update_addon")
def update_addon(key: str) -> AddonsResponse:
    record = registry().get(key)

    if record is None:
        raise HTTPException(status_code=404, detail=f"No add-on named {key}")

    try:
        update(
            record.path,
            token=settings_manager.settings.addons_git_token or None,
        )
    except InstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    registry().discover()
    _reconcile()
    return _all()


@router.post("/{key}/enabled", operation_id="set_addon_enabled")
def set_enabled(key: str, enabled: Annotated[bool, Query()]) -> AddonsResponse:
    """Turn an add-on off without touching anything it owns.

    Disabling keeps the folder, the schema and the settings, so turning it
    back on restores the configuration rather than handing back a blank form.
    Deleting data is `remove`'s job, and only when asked.
    """

    if registry().get(key) is None:
        raise HTTPException(status_code=404, detail=f"No add-on named {key}")

    disabled = settings_manager.settings.addons_disabled

    if enabled and key in disabled:
        disabled.remove(key)
    elif not enabled and key not in disabled:
        disabled.append(key)

    settings_manager.save()
    registry().discover()
    _reconcile()
    return _all()


@router.delete("/{key}", operation_id="remove_addon")
def remove_addon(
    key: str,
    purge: Annotated[bool, Query()] = False,
    confirm: Annotated[str | None, Query()] = None,
) -> AddonsResponse:
    """Remove an add-on. With `purge`, remove its data too.

    Without `purge` this is reversible: reinstall and the schema is still
    there with everything in it. With `purge` it is not, which is why it
    requires `confirm` to equal the key -- a destructive default that can be
    reached by a mistyped URL is not a safe API.
    """

    record = registry().get(key)

    if record is None:
        raise HTTPException(status_code=404, detail=f"No add-on named {key}")

    if purge and confirm != key:
        raise HTTPException(
            status_code=400,
            detail=f"Purging {key} destroys its data; pass confirm={key} to proceed",
        )

    path = record.path

    if record.addon is not None and record.state == "ok":
        try:
            record.addon.stop()
        except Exception as exc:
            logger.warning(f"Addon {key}: stop() raised during removal: {exc}")

    uninstall(path)

    if purge:
        addon_db.purge(key)
        settings_manager.settings.addons.pop(key, None)

    if key in settings_manager.settings.addons_disabled:
        settings_manager.settings.addons_disabled.remove(key)

    settings_manager.save()
    registry().discover()
    _reconcile()
    return _all()


@router.get("/{key}", operation_id="get_addon")
def get_addon(key: str) -> AddonResponse:
    return _describe(key)


def _reconcile() -> None:
    """Re-wire the running host to whatever is loaded now.

    Routes, scheduled jobs and scraper folders, in that order. All three are
    live: an add-on installed through this router answers requests, runs its
    jobs and contributes its scrapers without a restart.
    """

    try:
        from main import app

        from program.addons import mounting

        mounting.remount(app)
    except Exception as exc:
        logger.error(f"Addons: could not remount routes: {exc}")

    try:
        from kink import di

        from program.program import Program

        di[Program].scheduler_manager.refresh_content_jobs()
    except Exception as exc:
        logger.debug(f"Addons: could not refresh scheduled jobs: {exc}")

    try:
        from program.services.directscrapers import reset as reset_direct

        reset_direct()
    except Exception as exc:
        logger.debug(f"Addons: could not reset the scraper registry: {exc}")
