"""What an add-on is, from the host's side.

An add-on is a folder under ``settings.addons_dir`` containing a
``riven_addon.py`` that defines a module-level ``ADDON``. That object is the
entire contract: everything the host does to an add-on, it does through the
methods below, and everything an add-on contributes to the host, it
contributes by returning it from one of them.

THE ONE RULE THAT MAKES UNINSTALL WORK
--------------------------------------

Every table an add-on owns lives in a Postgres schema named after it, and so
does its ``alembic_version``. That is what makes removal a single statement:

    DROP SCHEMA <key> CASCADE

Complete by construction -- no enumerating tables, no orphaned sequences, no
version rows left behind to crash the next startup with a revision nothing
can resolve. It also means each add-on runs its own independent migration
environment rather than sharing the host's chain, so a broken add-on
migration cannot stop the host from starting.

The corollary is a rule the host must keep: **an add-on may reference host
tables, but the host must never reference an add-on's.** A host foreign key
pointing into the add-on's schema would be dropped by that CASCADE, which
turns a clean uninstall into a silent mutation of the host's own schema.

WHAT AN ADD-ON MAY ASSUME
-------------------------

It runs in the host's process, against the host's database session, with the
host's settings and VPN policy available by import. That is deliberate: these
add-ons exist to isolate *code ownership*, not failure or language, and a
package boundary buys that at no runtime cost. An add-on that genuinely needs
to fail alone belongs in its own container behind an HTTP boundary, and the
manifest below is deliberately shaped so that such a thing could be described
later without changing what already exists.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from fastapi import APIRouter
    from pydantic import BaseModel
    from sqlalchemy import MetaData


#: Bumped when the host changes what it passes to or expects from an add-on in
#: a way older add-ons cannot survive. An add-on declares the version it was
#: built against and the host refuses to load a mismatch -- which turns what
#: would be a mid-request AttributeError into one clear line at startup
#: naming the add-on and both versions.
HOST_API_VERSION = 1


@dataclass(slots=True)
class AddonNav:
    """Where the add-on appears in the host's navigation.

    Consumed by the frontend from ``/api/v1/addons``, so adding an entry costs
    no frontend build. ``href`` is always ``/x/<key>`` -- the host owns the
    URL space and an add-on that could pick its own path could collide with a
    host route and shadow it.
    """

    label: str
    #: A lucide icon name, e.g. "users". Resolved by the frontend against the
    #: icon set it already ships; an unknown name falls back rather than
    #: breaking the sidebar.
    icon: str = "puzzle"
    #: False hides it from the TV surface, which cannot drive every page.
    tv: bool = False


@dataclass(slots=True)
class AddonTv:
    """What this add-on offers the TELEVISION, as data rather than as a page.

    `riven-tv` is a second, JavaScript-free renderer for televisions running
    engines from about 2016 -- it cannot run an add-on's ``ui/addon.js`` any
    more than it can run the frontend's own bundle. So an add-on reaches that
    surface the only way anything does there: by answering plain JSON that a
    generic renderer draws.

    Each flag is a promise that this add-on's router answers the matching
    endpoint, relative to its own mount (``/api/v1/x/<key>/``). They are
    separate because they are genuinely separate features -- the tube scraper
    has nothing to browse but belongs on a title's page; an add-on that owns a
    catalogue is the other way round -- and an add-on may of course offer both
    or neither.

    ``tv/browse``  -> a screen of its own, reached from the television's nav
    ``tv/detail``  -> what one card on that screen opens
    ``tv/play``    -> where the bytes for one video are
    ``tv/title``   -> a section inside the television's own title page

    THE SHAPES ARE DELIBERATELY SMALL, and documented in ``docs/tv.md`` in each
    add-on repo. A card is an id, a title, a picture and what it does. That is
    everything a panel across a room can show and everything a directional pad
    can operate, and holding the contract to it is what lets ONE renderer draw
    an add-on nobody had written when it was built.

    A FLAG IS NOT A GUARANTEE THE ENDPOINT WORKS. The television treats a
    missing or malformed answer as "that section does not appear", never as an
    error page -- the same way the host treats a slot an add-on names but does
    not fill. An add-on is free to be newer than the television.
    """

    #: Its own screen: ``tv/browse``, ``tv/detail`` and ``tv/play``. Implies a
    #: nav entry, so ``AddonNav.tv`` is ignored unless this is set -- one flag
    #: meaning "appears on the TV" is one fewer way to be half-configured.
    browse: bool = False
    #: A section on the television's title page: ``tv/title``.
    title: bool = False


@dataclass(slots=True)
class AddonManifest:
    """The add-on's own description of itself."""

    key: str
    name: str
    description: str = ""
    version: str = "0.0.0"
    host_api: int = HOST_API_VERSION
    nav: AddonNav | None = None
    #: What the television surface can draw for this add-on. ``None`` means it
    #: has no presence there at all, which is the correct default: a TV screen
    #: is a second renderer to keep working, and most add-ons do not want one.
    tv: AddonTv | None = None
    #: Named places in the HOST's own pages this add-on contributes a section
    #: to, e.g. ``("details",)``. A page is the right shape for a feature that
    #: owns its own screen; a slot is the right shape for one that belongs
    #: *inside* something the host already renders -- a panel on a title's
    #: page cannot be a route without tearing the page in half.
    #:
    #: The VOCABULARY IS THE HOST'S, not the add-on's. An add-on naming a slot
    #: the host does not offer contributes nothing and is not an error: the
    #: host is free to retire a slot, and an add-on built against an older
    #: host must degrade to "that section does not appear" rather than to a
    #: broken page.
    #:
    #: Filled the same way a page is -- from the add-on's ``ui/addon.js`` --
    #: except that the bundle exports a ``slots`` object keyed by slot name
    #: rather than a default mount function. Nothing is fetched unless a page
    #: with that slot is actually rendered.
    slots: tuple[str, ...] = ()


class Addon:
    """Base class for an add-on. Override what you contribute, ignore the rest.

    Every method is defaulted, and that is load-bearing rather than politeness:
    an add-on that only adds a settings tab should not have to know that
    migrations exist, and a required method would break every existing add-on
    the moment the host grew a new capability.
    """

    manifest: AddonManifest

    # --- Configuration ------------------------------------------------------

    def settings_model(self) -> "type[BaseModel] | None":
        """The add-on's settings, as a pydantic model.

        Stored under ``settings.addons[<key>]`` and rendered by the host's
        settings page straight from this model's JSON Schema -- so a settings
        tab costs an add-on no frontend code at all.
        """

        return None

    # --- Database -----------------------------------------------------------

    def metadata(self) -> "MetaData | None":
        """The SQLAlchemy metadata whose tables this add-on owns.

        Its ``schema`` must be the add-on's key. The host checks, and refuses
        to load an add-on whose tables would land in ``public`` -- where they
        could not be dropped without naming them one by one, which is the
        whole thing this design exists to avoid.
        """

        return None

    def migrations_dir(self) -> Path | None:
        """An alembic ``versions`` directory, run as its own chain.

        Independent of the host's: its own ``alembic_version`` table, inside
        the add-on's schema, upgraded separately. No branch labels, no shared
        heads, and nothing left over when the schema is dropped.
        """

        return None

    # --- Runtime ------------------------------------------------------------

    def router(self) -> "APIRouter | None":
        """Routes, mounted under ``/api/v1/x/<key>``.

        Declare paths relative to that; the host owns the prefix so two
        add-ons cannot collide and no add-on can shadow a host route. The
        frontend's existing catch-all proxy forwards these without changes,
        so an add-on's whole API is reachable the moment it loads.
        """

        return None

    def jobs(self) -> dict[Callable[[], Any], dict[str, Any]]:
        """Scheduled work, in the shape the host's scheduler already takes.

        ``{callable: {"interval": seconds}}`` or ``{"cron": ...}``. Re-read
        whenever settings are saved, so an add-on whose interval is
        configurable gets that for free.
        """

        return {}

    def start(self) -> None:
        """Called once after the add-on is loaded and its tables exist."""

    def stop(self) -> None:
        """Called before the add-on is unloaded. Must not raise."""

    # --- Health -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Anything the management page should show about this add-on.

        Free-form and entirely optional; rendered as a row of figures. Kept
        deliberately untyped because the host cannot know what matters to an
        add-on, and a schema here would mean revising the host every time one
        of them grew a new number worth showing.
        """

        return {}
