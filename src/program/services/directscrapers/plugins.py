"""Discovering `DirectScraper` subclasses dropped into a folder.

The built-in sites (tnaflix.py, eporner.py, ...) and a user's own scraper file
are the same shape by construction: both are plain modules defining a
`DirectScraper` subclass, and `DirectScraperService` treats a discovered
plugin no differently from a built-in once it is loaded. There is one
mechanism, not a built-in path and a separate plugin path. See the README for
the interface a plugin file must implement.

A broken plugin must never take the service down with it. A syntax error, a
missing dependency, or a file that defines no scraper at all is recorded as a
per-file error and skipped -- the same "degrade, never raise" contract already
used for VPN providers.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from program.services.directscrapers.base import DirectScraper


@dataclass(slots=True)
class LoadedPlugin:
    """One `DirectScraper` subclass found in `plugin_dir`, and where from."""

    scraper: DirectScraper
    source_file: str


@dataclass(slots=True)
class DiscoveryResult:
    plugins: dict[str, LoadedPlugin]
    #: Filename -> what went wrong. Kept separate from the plugin dict so a
    #: broken file is still visible in the Plugins tab rather than silently
    #: absent, which reads as "the folder is empty" rather than "this file
    #: is broken."
    errors: dict[str, str]


def discover_plugins(plugin_dir: str) -> DiscoveryResult:
    """Import every `*.py` file in `plugin_dir` and collect its scrapers.

    Each file gets its own module namespace, keyed by path rather than
    filename, so two plugins that happen to share a filename in different
    checkouts (unlikely, but free to guard against) cannot collide in
    `sys.modules`.
    """

    directory = Path(plugin_dir)
    plugins: dict[str, LoadedPlugin] = {}
    errors: dict[str, str] = {}

    if not directory.is_dir():
        # Not configured, or the compose volume was never mounted. Neither is
        # an error -- the service works fine with zero plugins.
        return DiscoveryResult(plugins, errors)

    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue

        module_name = f"riven_direct_scrape_plugin__{path.stem}"

        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"could not build an import spec for {path.name}")

            module = importlib.util.module_from_spec(spec)
            # Registered before exec so a plugin importing itself, or dataclasses
            # resolving forward references against the module, behave the same
            # as an ordinarily imported module.
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - a plugin's own bug must not propagate
            errors[path.name] = f"{type(exc).__name__}: {exc}"
            sys.modules.pop(module_name, None)
            logger.warning(f"Direct-scrape plugin {path.name} failed to load: {exc}")
            continue

        found_any = False
        for _, obj in inspect.getmembers(module, inspect.isclass):
            # Only classes this module itself defines -- `inspect.getmembers`
            # also surfaces whatever the plugin imported (DirectScraper
            # itself, DirectVideo, requests.Session, ...), and instantiating
            # an imported base class would either crash on missing abstract
            # methods or silently register something that is not this file's
            # own scraper.
            if obj.__module__ != module_name:
                continue
            if not issubclass(obj, DirectScraper) or obj is DirectScraper:
                continue

            try:
                instance = obj()
            except Exception as exc:  # noqa: BLE001
                errors[path.name] = (
                    f"{obj.__name__} failed to construct: {type(exc).__name__}: {exc}"
                )
                logger.warning(
                    f"Direct-scrape plugin {path.name}: {obj.__name__} "
                    f"failed to construct: {exc}"
                )
                continue

            if not getattr(instance, "key", None):
                errors[path.name] = f"{obj.__name__} has no `key` set"
                continue

            if instance.key in plugins:
                errors[path.name] = (
                    f"duplicate scraper key {instance.key!r} "
                    f"(already loaded from {plugins[instance.key].source_file})"
                )
                continue

            plugins[instance.key] = LoadedPlugin(scraper=instance, source_file=path.name)
            found_any = True

        if not found_any and path.name not in errors:
            errors[path.name] = "defines no DirectScraper subclass"

    return DiscoveryResult(plugins, errors)


__all__ = ["DiscoveryResult", "LoadedPlugin", "discover_plugins"]
