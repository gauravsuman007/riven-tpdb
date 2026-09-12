"""Attaching and detaching add-on routes on a running app.

FastAPI builds no static route table -- matching walks ``app.router.routes``
on every request -- so routes really can be added and removed while the server
is up. What does NOT update itself is the cached OpenAPI document, which is
why it is cleared here; without that, an add-on installed at runtime works
perfectly and is invisible to the docs and to any client generating from them.

Detaching is by path prefix rather than by remembering objects. The host owns
``/api/v1/addons/<key>/``, so the prefix is an exact statement of what belongs
to an add-on, and it stays correct across a reload that produced entirely new
router objects -- which is precisely the case a remembered list gets wrong.
"""

from typing import Any

from loguru import logger

from program.addons.loader import registry


#: The host owns this prefix. An add-on declares paths relative to it and
#: cannot pick its own, so two add-ons cannot collide and none can shadow a
#: host route by choosing a path that already exists.
#:
#: Deliberately NOT `/api/v1/addons`, which is the management router's own
#: prefix: `/api/v1/addons/{key}/update` would sit inside the space this
#: module clears out, and detaching an add-on would quietly delete the
#: endpoints that manage add-ons. It mirrors the frontend's `/x/<key>` page
#: path instead, so one segment means the same thing on both sides.
PREFIX = "/api/v1/x"


def detach_all(app: Any) -> int:
    """Strip every add-on route off the app.

    By prefix, not by a remembered list of route objects. A reload produces
    entirely new objects, and an add-on that has just been uninstalled is not
    in the registry to be looked up -- so anything keyed on what is currently
    loaded would leave exactly the routes that most need removing: those of
    the add-on that is gone.
    """

    removed = [
        route
        for route in app.router.routes
        if getattr(route, "path", "").startswith(PREFIX + "/")
    ]

    for route in removed:
        app.router.routes.remove(route)

    return len(removed)


def attach(app: Any, key: str) -> int:
    """Mount one add-on's router. Returns how many routes it contributed."""

    record = registry().get(key)

    if record is None or record.addon is None or record.state != "ok":
        return 0

    try:
        router = record.addon.router()
    except Exception as exc:
        logger.error(f"Addon {key}: router() raised: {exc}")
        return 0

    if router is None:
        return 0

    from auth import resolve_api_key
    from fastapi import Depends

    before = len(app.router.routes)
    # Behind the same API key as everything else, applied HERE rather than
    # trusted to the add-on: an add-on that forgot would otherwise publish an
    # unauthenticated surface on the host's port.
    app.include_router(
        router,
        prefix=f"{PREFIX}/{key}",
        dependencies=[Depends(resolve_api_key)],
    )
    _attach_ui(app, key, record.path)
    return len(app.router.routes) - before


#: An add-on's compiled page, if it ships one. Served from the host because
#: the frontend runs in a different container and cannot see the add-ons
#: volume -- so the browser fetches the bundle through the same API it uses
#: for everything else.
UI_DIRNAME = "ui"

_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
}


def _attach_ui(app: Any, key: str, root: Any) -> None:
    """Serve ``<addon>/ui/`` under the add-on's own API prefix.

    Under the add-on prefix rather than the management router's, so that one
    passthrough rule on the frontend covers an add-on's assets, its images and
    its video streams -- all three are bytes, none of them are JSON, and the
    generic JSON proxy mangles all of them identically.
    """

    from pathlib import Path

    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    ui_root = Path(root) / UI_DIRNAME

    if not ui_root.is_dir():
        return

    from auth import resolve_api_key
    from fastapi import Depends

    @app.get(
        f"{PREFIX}/{key}/{UI_DIRNAME}/{{path:path}}",
        include_in_schema=False,
        dependencies=[Depends(resolve_api_key)],
    )
    def _serve(path: str) -> FileResponse:
        target = (ui_root / path).resolve()

        # Resolved and then checked against the root, so that neither `..`
        # nor a symlink inside the add-on can reach a file outside its own
        # ui folder -- the add-on directory is writable by whoever installs
        # add-ons, and this endpoint is the one thing in it that serves
        # arbitrary paths.
        if not target.is_file() or ui_root.resolve() not in target.parents:
            raise HTTPException(status_code=404, detail="No such file")

        return FileResponse(
            target,
            media_type=_CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
        )


def remount(app: Any) -> None:
    """Make the running app's routes match what is loaded right now."""

    detach_all(app)

    total = 0

    for record in registry().active():
        total += attach(app, record.key)

    # Without this the docs describe the add-ons that were loaded at startup,
    # forever.
    app.openapi_schema = None

    logger.debug(f"Addons: {total} routes mounted")
