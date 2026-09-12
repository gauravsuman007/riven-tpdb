"""The add-on loader's module bookkeeping.

Regression cover for a bug that made "update" look like it worked: the loader
forgot only the entry point (`riven_addon_<key>`), so the freshly-pulled
`riven_addon.py` re-ran and imported the STALE cached package. Every line the
update changed stayed unchanged until the next restart, and nothing anywhere
said so.

The cases below work in units of "load, change on disk, load again", because
that is the sequence that was broken and the only one that demonstrates it.
"""

import sys
import tempfile
import textwrap
from pathlib import Path

try:
    from program.addons.loader import (
        LoadedAddon,
        _forget_modules,
        _import_addon,
        _introduced_modules,
    )
except Exception as exc:  # pragma: no cover - dependency floor, same as siblings
    print(f"SKIP: {exc}")
    sys.exit(0)

PASS = FAIL = 0


def check(name, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}{(' -- ' + extra) if extra else ''}")


def write_addon(root: Path, value: str, package: str = "sample_addon_pkg") -> Path:
    """An add-on laid out the way a real one is: entry point plus a package.

    The package name deliberately does NOT derive from the folder name. That
    mismatch is the whole bug: `onlyfans` ships `onlyfans_addon`.
    """

    (root / package).mkdir(parents=True, exist_ok=True)
    (root / package / "__init__.py").write_text(f'VALUE = "{value}"\n')
    (root / "riven_addon.py").write_text(
        textwrap.dedent(
            f"""
            from {package} import VALUE


            class Stub:
                pass


            ADDON = Stub()
            ADDON.value = VALUE
            """
        )
    )
    return root


def load(path: Path):
    """Import the add-on; hand back its record and the value it saw.

    `_import_addon` raises TypeError here because the stub is not a real
    `Addon` -- and it raises AFTER the module has been executed and cached, so
    this is exactly the half-finished import the bookkeeping has to survive.
    """

    record = LoadedAddon(key=path.name, path=path)

    try:
        _import_addon(path, record.modules)
    except TypeError:
        pass

    return record, sys.modules[f"riven_addon_{path.name}"].ADDON.value


print("add-on module bookkeeping")

with tempfile.TemporaryDirectory() as tmp:
    addon = write_addon(Path(tmp) / "sample", "version-ONE")
    record, first = load(addon)

    check("an add-on's own package is loaded on first import", first == "version-ONE")
    check(
        "the package it imported is recorded, not just the entry point",
        "sample_addon_pkg" in record.modules
        and f"riven_addon_{addon.name}" in record.modules,
        str(sorted(record.modules)),
    )

    # A DIFFERENT LENGTH on purpose. Python's bytecode cache keys on mtime and
    # size, so two same-size writes inside one second are indistinguishable to
    # it -- which makes a same-length value look like the bug reproducing when
    # it is really just a stale .pyc. That cost time once already.
    write_addon(addon, "version-TWO-and-longer")

    _, stale = load(addon)
    check(
        "without forgetting, a reload re-runs the OLD package -- the bug",
        stale == "version-ONE",
        stale,
    )

    _forget_modules(record)
    _, fresh = load(addon)
    check(
        "after forgetting, the reload sees what is on disk",
        fresh == "version-TWO-and-longer",
        fresh,
    )

    # Nothing outside the add-on's folder may be tracked: an add-on importing
    # one of the host's dependencies for the first time makes that a new module
    # too, and forgetting it would hand the next add-on a second, non-identical
    # copy of it.
    outside = [
        name
        for name in record.modules
        if (origin := getattr(sys.modules.get(name), "__file__", None))
        and not str(Path(origin).resolve()).startswith(str(addon.resolve()))
    ]
    check("no module outside the add-on's folder is tracked", outside == [], str(outside))

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "broken"
    (root / "sample_broken_pkg").mkdir(parents=True)
    (root / "sample_broken_pkg" / "__init__.py").write_text("VALUE = 1\n")
    (root / "riven_addon.py").write_text(
        "from sample_broken_pkg import VALUE\n\nraise RuntimeError('boom')\n"
    )

    record = LoadedAddon(key="broken", path=root)

    try:
        _import_addon(root, record.modules)
    except RuntimeError:
        pass

    # Otherwise fixing the add-on and pressing update re-runs the wreckage of
    # the attempt that failed, and it fails again for a reason no longer true.
    check(
        "a FAILED import still reports what it managed to cache",
        "sample_broken_pkg" in record.modules,
        str(sorted(record.modules)),
    )

    _forget_modules(record)
    check(
        "forgetting a failed add-on clears it out of sys.modules",
        "sample_broken_pkg" not in sys.modules,
    )

with tempfile.TemporaryDirectory() as tmp:
    check(
        "an add-on that imported nothing new introduces nothing",
        _introduced_modules(set(sys.modules), Path(tmp)) == [],
    )

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
