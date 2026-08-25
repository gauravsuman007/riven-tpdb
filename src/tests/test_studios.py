"""The studio directory: site matching, sync rules and promotion.

The parsing half is covered in test_adultempire.py. What is protected here is
the judgement: which TPDB site a studio is allowed to claim, what the weekly
sync is allowed to overwrite, and the fact that opening a title twice does not
produce two of it.

Stdlib plus sqlalchemy. The service's heavy imports are stubbed rather than
installed -- the rules under test are pure.
"""

import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    import sqlalchemy
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import DeclarativeBase, Session
except ImportError:  # pragma: no cover
    print("SKIP: sqlalchemy not installed")
    sys.exit(0)


class _Logger:
    def __getattr__(self, _):
        return lambda *args, **kwargs: None


sys.modules.setdefault("loguru", types.ModuleType("loguru"))
sys.modules["loguru"].logger = _Logger()


class Base(DeclarativeBase):
    pass


for pkg, rel in (
    ("program", "program"),
    ("program.db", "program/db"),
    ("program.media", "program/media"),
):
    module = types.ModuleType(pkg)
    module.__path__ = [str(SRC / rel)]
    sys.modules[pkg] = module

_bm = types.ModuleType("program.db.base_model")
_bm.Base = Base
sys.modules["program.db.base_model"] = _bm

from program.media.studio import Studio  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# studios.py reaches for the database, the settings and the storefront client
# at import time; none of that is needed to exercise the matching rule.
for name in (
    "program.db.db",
    "program.settings",
    "program.services",
    "program.services.recommendations",
    "program.services.recommendations.adultempire",
    "program.services.recommendations.tpdb_lookup",
):
    stub = types.ModuleType(name)
    sys.modules.setdefault(name, stub)

sys.modules["program.db.db"].db_session = lambda: None
sys.modules["program.settings"].settings_manager = types.SimpleNamespace(
    settings=types.SimpleNamespace(
        content=types.SimpleNamespace(brochure=types.SimpleNamespace(enabled=False))
    )
)

for attr in ("STUDIO_SORTS", "AdultEmpireClient", "AdultEmpireError", "RankedTitle", "StudioRef"):
    setattr(sys.modules["program.services.recommendations.adultempire"], attr, object)

sys.modules["program.services.recommendations.tpdb_lookup"].client = lambda: None

studios = _load(
    "studios_under_test",
    SRC / "program" / "services" / "recommendations" / "studios.py",
)

PASSED, FAILED = [], []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


class _Site:
    def __init__(self, name, site_id=1):
        self.name = name
        self.id = site_id


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


# ----------------------------------------------------------- site matching


def test_an_exact_name_matches():
    site = studios.pick_site([_Site("Evil Angel")], "Evil Angel")

    assert site is not None and site.name == "Evil Angel"


def test_a_namesake_is_refused():
    """The real failure mode. TPDB returns twenty-two sites for "Evil Angel",
    in its own order, and taking the first would brand the studio with another
    network's logo."""

    results = [_Site("Mylf X Evil Angel"), _Site("Evil Angel Presents")]

    assert studios.pick_site(results, "Evil Angel") is None


def test_the_exact_name_wins_over_an_earlier_namesake():
    results = [_Site("Mylf X Evil Angel"), _Site("Evil Angel", 4611)]
    site = studios.pick_site(results, "Evil Angel")

    assert site is not None and site.id == 4611


def test_punctuation_and_case_do_not_block_a_match():
    """Adult Empire writes "Rocco's", TPDB writes "Roccos"; both name the same
    studio and neither spelling is wrong."""

    assert studios.pick_site([_Site("Roccos Siffredi")], "Rocco's Siffredi")
    assert studios.pick_site([_Site("EVIL ANGEL")], "Evil Angel")


def test_a_numbered_variant_is_still_a_different_studio():
    assert studios.pick_site([_Site("Evil Angel 2")], "Evil Angel") is None


def test_no_results_is_not_an_error():
    assert studios.pick_site([], "Nobody") is None


# ------------------------------------------------------------- sync rules


def test_a_saved_studio_survives_a_resync():
    """The weekly job rewrites names and counts. If it ever rewrote `saved`,
    a user's studios section would empty itself overnight and look exactly
    like data loss."""

    session = _session()
    session.add(Studio(ae_id="149", name="Evil Angel", saved=True))
    session.commit()

    studio = session.execute(select(Studio)).scalars().one()

    # What _store does on a resync.
    studio.name = "Evil Angel"
    studio.slug = "evil-angel-porn-movies"
    studio.title_count = 3978
    session.commit()

    assert session.execute(select(Studio)).scalars().one().saved is True


def test_the_sync_never_writes_saved():
    """Guard the shipped _store, since the test above can only mirror it."""

    text = (SRC / "program/services/recommendations/studios.py").read_text()
    body = text[text.index("def _store("):]
    body = body[: body.index("# ------")]

    assert "studio.saved" not in body, (
        "_store writes `saved`, so a weekly sync can clear a user's studios"
    )


def test_tiny_studios_are_kept_out_of_the_directory():
    assert studios.MIN_TITLES > 0


# --------------------------------------------------------------- promotion


def test_promotion_reuses_an_existing_entry():
    """A studio's top sellers overlap the brochure's shelves heavily. Two
    entries for one storefront id would be two detail pages disagreeing about
    whether the title had been requested."""

    text = (SRC / "routers/secure/studios.py").read_text()
    body = text[text.index("def promote_title("):]

    lookup = body.index("CollectionEntry.external_id == product_id")
    insert = body.index("session.add(entry)")

    assert lookup < insert, (
        "promote_title creates an entry before looking for an existing one"
    )


def test_promotion_looks_across_every_adult_empire_collection():
    """Not just its own. The overlap is with the brochure shelves."""

    text = (SRC / "routers/secure/studios.py").read_text()
    body = text[text.index("def promote_title("):]
    body = body[: body.index("session.add(entry)")]

    assert 'Collection.source == "adultempire"' in body


def test_promotion_resolves_tpdb_before_returning():
    """The page it redirects to picks its view from tpdb_id. Leaving that to
    the batch timer shows the storefront view once and the real one later."""

    text = (SRC / "routers/secure/studios.py").read_text()
    body = text[text.index("def promote_title("):]

    assert "enrich_entry(entry)" in body


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
