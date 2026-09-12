"""Finding add-ons, loading them, and keeping the failures visible.

An add-on is a folder containing ``riven_addon.py`` with a module-level
``ADDON``. Loading one means importing that file, validating what it claims,
migrating its schema and starting it -- and every one of those steps can fail
for reasons that are the add-on's fault and not the host's.

So the registry keeps failures as first-class rows rather than dropping them.
An add-on that fails to import must appear on the management page saying why;
silently absent is the worst outcome, because the folder is on disk and the
user has no way to tell "not installed" from "installed and broken".

The host is never brought down by an add-on. Import errors, migration
failures and exceptions from `start()` are all caught and recorded.
"""

import importlib.util
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from program.addons import database
from program.addons.contract import HOST_API_VERSION, Addon
from program.settings import settings_manager


@dataclass
class LoadedAddon:
    """One add-on and everything the host learned while loading it."""

    key: str
    path: Path
    addon: Addon | None = None
    #: "ok", "disabled", or "failed". A disabled add-on is imported far enough
    #: to know its name and settings and then deliberately not started, so the
    #: management page can describe something it is not running.
    state: str = "ok"
    error: str | None = None
    version: str = "0.0.0"
    name: str = ""
    description: str = ""
    #: The git remote it came from, when it was installed from one. Kept so
    #: the page can offer "update" on exactly the add-ons that can be updated.
    source: str | None = None
    revision: str | None = None
    settings_schema: dict[str, Any] | None = None
    nav: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class AddonRegistry:
    """Every add-on the host knows about, loaded or otherwise."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.addons: dict[str, LoadedAddon] = {}

    # --- Discovery ----------------------------------------------------------

    @property
    def directory(self) -> Path:
        return Path(settings_manager.settings.addons_dir)

    def _disabled(self) -> set[str]:
        return set(settings_manager.settings.addons_disabled)

    def discover(self) -> None:
        """(Re)load every add-on in the folder.

        Existing add-ons are stopped first. Reloading rather than merging
        because a folder that changed on disk can have changed in any way --
        renamed, replaced, downgraded -- and reconciling that is more code
        than starting from what is actually there.
        """

        with self._lock:
            self.unload_all()

            directory = self.directory

            if not directory.exists():
                logger.debug(f"Addons: {directory} does not exist, nothing to load")
                return

            for path in sorted(directory.iterdir()):
                if not path.is_dir() or path.name.startswith((".", "_")):
                    continue

                if not (path / "riven_addon.py").exists():
                    logger.debug(f"Addons: {path.name} has no riven_addon.py, skipping")
                    continue

                self.addons[path.name] = self._load_one(path)

            loaded = sum(1 for a in self.addons.values() if a.state == "ok")
            failed = [a.key for a in self.addons.values() if a.state == "failed"]

            logger.info(
                f"Addons: {loaded} loaded"
                + (f", {len(failed)} failed ({', '.join(failed)})" if failed else "")
            )

    def _load_one(self, path: Path) -> LoadedAddon:
        key = path.name
        record = LoadedAddon(key=key, path=path)
        record.source, record.revision = _git_origin(path)

        try:
            addon = _import_addon(path)
        except Exception as exc:
            logger.error(f"Addon {key}: failed to import: {exc}")
            logger.debug(traceback.format_exc())
            record.state = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            return record

        manifest = addon.manifest
        record.addon = addon
        record.name = manifest.name
        record.description = manifest.description
        record.version = manifest.version

        # Checked before anything is imported FROM the add-on, so a bad
        # contract version produces one clear line naming both numbers rather
        # than an AttributeError from inside a request three hours later.
        if manifest.host_api != HOST_API_VERSION:
            record.state = "failed"
            record.error = (
                f"Built for host API {manifest.host_api}, this host is "
                f"{HOST_API_VERSION}"
            )
            logger.error(f"Addon {key}: {record.error}")
            return record

        # The folder name is the identity: it is the schema name, the route
        # prefix and the settings key, so a manifest that disagrees with it
        # would put the add-on's data somewhere its own uninstall would not
        # look.
        if manifest.key != key:
            record.state = "failed"
            record.error = f"Manifest key {manifest.key!r} does not match folder {key!r}"
            logger.error(f"Addon {key}: {record.error}")
            return record

        if not key.isidentifier() or key in ("public", "information_schema"):
            record.state = "failed"
            record.error = f"{key!r} is not usable as a schema name"
            logger.error(f"Addon {key}: {record.error}")
            return record

        if manifest.nav is not None:
            record.nav = {
                "label": manifest.nav.label,
                "icon": manifest.nav.icon,
                "href": f"/x/{key}",
                "tv": manifest.nav.tv,
            }

        try:
            self._apply_settings(record, addon)
        except Exception as exc:
            record.state = "failed"
            record.error = f"Settings rejected: {exc}"
            logger.error(f"Addon {key}: {record.error}")
            return record

        if key in self._disabled():
            record.state = "disabled"
            logger.info(f"Addon {key}: installed but disabled")
            return record

        try:
            self._prepare_database(record, addon)
        except Exception as exc:
            logger.error(f"Addon {key}: migrations failed: {exc}")
            logger.debug(traceback.format_exc())
            record.state = "failed"
            record.error = f"Migration failed: {exc}"
            return record

        try:
            addon.start()
        except Exception as exc:
            logger.error(f"Addon {key}: start() failed: {exc}")
            logger.debug(traceback.format_exc())
            record.state = "failed"
            record.error = f"Start failed: {exc}"
            return record

        logger.success(f"Addon {key} ({manifest.name} {manifest.version}) loaded!")
        return record

    # --- The pieces of loading ---------------------------------------------

    def _apply_settings(self, record: LoadedAddon, addon: Addon) -> None:
        """Validate the stored config against the add-on's model.

        Writing the result back is what materialises defaults, so a freshly
        installed add-on has a complete, editable settings form rather than an
        empty object the form cannot render.
        """

        model = addon.settings_model()

        if model is None:
            return

        stored = settings_manager.settings.addons.get(record.key, {})
        validated = model.model_validate(stored or {})
        settings_manager.settings.addons[record.key] = validated.model_dump()
        settings_manager.save()
        record.settings_schema = model.model_json_schema()

    def _prepare_database(self, record: LoadedAddon, addon: Addon) -> None:
        metadata = addon.metadata()

        if metadata is None:
            return

        # Refused rather than corrected. Tables in `public` would survive the
        # add-on's own uninstall, and an add-on whose data cannot be removed
        # is precisely what this design exists to prevent.
        if metadata.schema != record.key:
            raise ValueError(
                f"metadata schema is {metadata.schema!r}, must be {record.key!r}"
            )

        versions = addon.migrations_dir()

        if versions is not None and versions.exists():
            database.upgrade(record.key, versions, metadata)
        else:
            database.create_all(record.key, metadata)

    # --- Unloading ----------------------------------------------------------

    def unload_all(self) -> None:
        for record in self.addons.values():
            if record.addon is None or record.state != "ok":
                continue

            try:
                record.addon.stop()
            except Exception as exc:
                logger.warning(f"Addon {record.key}: stop() raised: {exc}")

            _forget_modules(record.key)

        self.addons = {}

    # --- Queries the rest of the host asks ---------------------------------

    def active(self) -> list[LoadedAddon]:
        return [record for record in self.addons.values() if record.state == "ok"]

    def get(self, key: str) -> LoadedAddon | None:
        return self.addons.get(key)

    def jobs(self) -> dict:
        collected: dict = {}

        for record in self.active():
            if record.addon is None:
                continue

            try:
                collected.update(record.addon.jobs())
            except Exception as exc:
                logger.warning(f"Addon {record.key}: jobs() raised: {exc}")

        return collected


def _import_addon(path: Path) -> Addon:
    """Import ``riven_addon.py`` with the add-on's folder on the path.

    The folder goes on `sys.path` so the add-on can lay itself out as a normal
    package (``from backend.service import ...``) instead of contorting
    everything into one file. It is removed again immediately: leaving it
    would let one add-on's ``backend`` package shadow another's, and the
    symptom would be an add-on running someone else's code.
    """

    entry = path / "riven_addon.py"
    module_name = f"riven_addon_{path.name}"

    sys.path.insert(0, str(path))
    try:
        spec = importlib.util.spec_from_file_location(module_name, entry)

        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load {entry}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(str(path))
        except ValueError:
            pass

    addon = getattr(module, "ADDON", None)

    if addon is None:
        raise AttributeError("riven_addon.py defines no ADDON")

    if not isinstance(addon, Addon):
        raise TypeError("ADDON is not a riven Addon")

    return addon


def _forget_modules(key: str) -> None:
    """Drop the add-on's modules so a reload re-reads them from disk.

    Without this, "rescan" after an update would re-run the old code: Python
    caches by module name and the name has not changed.
    """

    prefix = f"riven_addon_{key}"

    for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
        sys.modules.pop(name, None)


def _git_origin(path: Path) -> tuple[str | None, str | None]:
    """The remote and revision of an add-on that came from git, if it did."""

    import subprocess

    if not (path / ".git").exists():
        return None, None

    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() or None if result.returncode == 0 else None
        except Exception:
            return None

    return run("remote", "get-url", "origin"), run("rev-parse", "--short", "HEAD")


_registry = AddonRegistry()


def registry() -> AddonRegistry:
    return _registry
