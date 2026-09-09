"""A submodule of `program.utils` must not shadow a name the package imports.

`program/utils/__init__.py` did `from time import time` while
`program/utils/time.py` exists beside it. Importing that submodule anywhere in
the app -- and a dozen modules do, for `utcnow` -- rebinds `time` in the
package namespace to the module, because that is exactly what the import
system does to a parent package.

Nothing failed at import time and nothing failed under a unit test. What
failed was `benchmark()`, at runtime, with "TypeError: 'module' object is not
callable" -- inside the VFS read path, wrapped by an exception handler that
reported it as `DebridServiceException: torbox: Unexpected error connecting to
stream`. Every uncached read raised it, so all playback was broken server-wide
and the error blamed the debrid provider.

This checks the real thing rather than the one instance: import the package,
import each of its submodules, and confirm no plain-`from X import Y` name in
`__init__.py` has turned into a module.
"""

import ast
import importlib
import pkgutil
import sys
from pathlib import Path
from types import ModuleType

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

PASSED: list[str] = []
FAILED: list[tuple[str, Exception]] = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
    except Exception as exc:
        FAILED.append((name, exc))


def _plain_imported_names(init: Path) -> set[str]:
    """Names `__init__.py` binds with `from <stdlib> import <name>`."""

    names: set[str] = set()

    for node in ast.walk(ast.parse(init.read_text())):
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue

        for alias in node.names:
            if alias.name != "*":
                names.add(alias.asname or alias.name)

    return names


def test_no_submodule_shadows_an_imported_name():
    package = importlib.import_module("program.utils")
    init = Path(package.__file__ or "")
    imported = _plain_imported_names(init)

    submodules = {
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if not info.name.startswith("_")
    }

    collisions = sorted(imported & submodules)

    assert not collisions, (
        f"program/utils/__init__.py binds {collisions} with a plain import while "
        f"a submodule of the same name exists. Importing that submodule anywhere "
        f"rebinds the name to the module. Alias the import (e.g. "
        f"`from time import perf_counter as _perf_counter`) or rename the submodule."
    )


def test_benchmark_still_works_once_the_submodule_is_imported():
    """The end-to-end shape of the failure, not just its cause."""

    importlib.import_module("program.utils.time")

    from program.utils import benchmark

    recorded: list[float] = []

    with benchmark(log=recorded.append):
        pass

    assert recorded, "benchmark() logged nothing"
    assert isinstance(recorded[0], float), f"expected a float, got {recorded[0]!r}"


def test_the_scan_found_something():
    """Guard the guard: an empty submodule list would pass vacuously."""

    package = importlib.import_module("program.utils")
    found = [info.name for info in pkgutil.iter_modules(package.__path__)]

    assert len(found) > 3, f"only found {found} in program.utils"
    assert isinstance(sys.modules.get("program.utils"), ModuleType)


try:
    importlib.import_module("program.utils")
except Exception as exc:  # pragma: no cover - dependency-shaped skip
    print(f"SKIP: program.utils is not importable here ({exc})")
    sys.exit(0)

for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
