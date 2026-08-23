"""Tests for scraper eligibility on adult (TPDB) items.

TPDB items carry no IMDb id, so the Stremio-style scrapers -- which address
content purely by IMDb id -- can never serve them. Rarbg is separately unable
to: its query hardcodes ``ncategory:XXX``, excluding adult results outright.

``Scraping._eligible_services`` is the guard. Importing it for real would boot
the whole program, so this exercises the same predicate against stub services,
alongside the real ``supports_adult``/``requires_imdb_id`` values declared by
each scraper module (read statically, so no imports are needed).
"""

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

SCRAPERS = SRC / "program" / "services" / "scrapers"

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


@dataclass
class StubService:
    key: str
    requires_imdb_id: bool = False
    supports_adult: bool = True


@dataclass
class StubItem:
    tpdb_id: str | None = None
    imdb_id: str | None = None

    @property
    def is_adult(self) -> bool:
        return bool(self.tpdb_id)


def eligible(services, item):
    """Mirror of Scraping._eligible_services."""

    out = []
    for service in services:
        if item.is_adult and not service.supports_adult:
            continue
        if service.requires_imdb_id and not item.imdb_id:
            continue
        out.append(service)
    return out


def _class_flag(module: str, class_name: str, flag: str, default):
    """Read a class-level boolean assignment without importing the module."""

    tree = ast.parse((SCRAPERS / f"{module}.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == flag for t in stmt.targets
                    )
                    and isinstance(stmt.value, ast.Constant)
                ):
                    return stmt.value.value
    return default


def _test_imdb_scrapers_skipped_for_adult():
    services = [
        StubService("torrentio", requires_imdb_id=True),
        StubService("prowlarr"),
        StubService("jackett"),
    ]
    keys = [s.key for s in eligible(services, StubItem(tpdb_id="uuid-1"))]
    assert keys == ["prowlarr", "jackett"], keys


def _test_imdb_scrapers_kept_when_imdb_id_present():
    services = [StubService("torrentio", requires_imdb_id=True)]
    keys = [s.key for s in eligible(services, StubItem(imdb_id="tt123"))]
    assert keys == ["torrentio"], keys


def _test_adult_hostile_scraper_skipped():
    services = [StubService("rarbg", supports_adult=False), StubService("prowlarr")]
    keys = [s.key for s in eligible(services, StubItem(tpdb_id="uuid-1"))]
    assert keys == ["prowlarr"], keys


def _test_adult_hostile_scraper_kept_for_mainstream():
    services = [StubService("rarbg", supports_adult=False)]
    keys = [s.key for s in eligible(services, StubItem(imdb_id="tt123"))]
    assert keys == ["rarbg"], keys


def _test_no_eligible_services_yields_empty():
    services = [StubService("torrentio", requires_imdb_id=True)]
    assert eligible(services, StubItem(tpdb_id="uuid-1")) == []


def _test_real_flags_declared():
    # The scrapers that address content by IMDb id must say so.
    for module, cls in [
        ("torrentio", "Torrentio"),
        ("comet", "Comet"),
        ("mediafusion", "Mediafusion"),
        ("aiostreams", "AIOStreams"),
        ("orionoid", "Orionoid"),
    ]:
        assert _class_flag(module, cls, "requires_imdb_id", False) is True, module

    # Title-based scrapers must not.
    for module, cls in [("prowlarr", "Prowlarr"), ("jackett", "Jackett")]:
        assert _class_flag(module, cls, "requires_imdb_id", False) is False, module

    # Rarbg excludes XXX by construction.
    assert _class_flag("rarbg", "Rarbg", "supports_adult", True) is False
    assert _class_flag("base", "ScraperService", "supports_adult", None) is True


TESTS = [
    ("eligibility: imdb-only scrapers skipped for adult", _test_imdb_scrapers_skipped_for_adult),
    ("eligibility: imdb scrapers kept with imdb id", _test_imdb_scrapers_kept_when_imdb_id_present),
    ("eligibility: adult-hostile scraper skipped", _test_adult_hostile_scraper_skipped),
    ("eligibility: adult-hostile scraper kept for mainstream", _test_adult_hostile_scraper_kept_for_mainstream),
    ("eligibility: no eligible services yields empty", _test_no_eligible_services_yields_empty),
    ("eligibility: real scraper flags declared", _test_real_flags_declared),
]


def main():
    for name, fn in TESTS:
        check(name, fn)

    print(f"\nPASSED: {len(PASSED)}")
    print(f"FAILED: {len(FAILED)}")
    for name in PASSED:
        print(f"  ✓ {name}")
    for name, err in FAILED:
        print(f"  ✗ {name}: {err}")

    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
