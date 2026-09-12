"""The performer index: handle identity and cross-site deduplication.

What is protected here is the judgement, not the parsing. The scrapers are
covered by running them against the sites; what matters in this file is the
rule that decides two sites are describing the same person -- and, just as
importantly, the rule that decides they are not.

Stdlib plus sqlalchemy. The service's heavy imports are stubbed rather than
installed; the rules under test are pure.
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
    ("program.utils", "program/utils"),
):
    module = types.ModuleType(pkg)
    module.__path__ = [str(SRC / rel)]
    sys.modules[pkg] = module

_bm = types.ModuleType("program.db.base_model")
_bm.Base = Base
sys.modules["program.db.base_model"] = _bm

from program.media.onlyfans import (  # noqa: E402
    OnlyFansAccount,
    OnlyFansAccountSource,
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# service.py reaches for the database, the settings and the scraper registry at
# import time; none of that is needed to exercise the identity rule.
for name in ("program.db.db", "program.settings", "program.services"):
    sys.modules.setdefault(name, types.ModuleType(name))

sys.modules["program.db.db"].db_session = lambda: None
sys.modules["program.settings"].settings_manager = types.SimpleNamespace(
    settings=types.SimpleNamespace(
        content=types.SimpleNamespace(
            onlyfans=types.SimpleNamespace(enabled=False)
        )
    )
)

service = _load(
    "program.services.onlyfans.service",
    SRC / "program/services/onlyfans/service.py",
)
normalise_handle = service.normalise_handle


failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


# --- The identity rule ------------------------------------------------------
#
# The three spellings below are the ones actually observed across the five
# sites for a single performer. If these stop collapsing to one value the
# index silently grows duplicate accounts, each carrying a subset of the
# sites -- which looks like a working index, not a bug.

check("hyphenated", normalise_handle("sophie-rain"), "sophierain")
check("spaced", normalise_handle("Sophie Rain"), "sophierain")
check("underscored", normalise_handle("sophie_rain"), "sophierain")
check("already collapsed", normalise_handle("sophierain"), "sophierain")
check("mixed case", normalise_handle("SoPhIeRaIn"), "sophierain")
check("dotted", normalise_handle("sophie.rain"), "sophierain")
check("digits kept", normalise_handle("bunny99"), "bunny99")
check("empty", normalise_handle(""), "")
check("none-ish", normalise_handle(None), "")

# Distinct people must stay distinct. The collapse is aggressive, so this is
# the half worth guarding: it strips separators but must never strip content.
if normalise_handle("sophie-rain") == normalise_handle("sophie-raine"):
    failures.append("distinct handles collapsed into one account")
if normalise_handle("bunny99") == normalise_handle("bunny98"):
    failures.append("digits ignored, distinct accounts collapsed")


# --- Deduplication across sites ---------------------------------------------

engine = create_engine("sqlite://")
Base.metadata.create_all(engine)

with Session(engine) as session:
    account = OnlyFansAccount(
        handle=normalise_handle("Sophie Rain"),
        display_name="Sophie Rain",
        avatar_url=None,
    )
    session.add(account)
    session.flush()

    # Three sites, three spellings, one person.
    for site, spelling in (
        ("ultrathots", "sophie-rain"),
        ("notfans", "sophierain"),
        ("porntn", "sophie_rain"),
    ):
        session.add(
            OnlyFansAccountSource(
                account_id=account.id,
                site=site,
                site_handle=spelling,
                page_url=f"https://{site}/models/{spelling}/",
            )
        )
    session.commit()

    accounts = session.execute(select(OnlyFansAccount)).scalars().all()
    check("one account for three sites", len(accounts), 1)
    check("three sources recorded", len(accounts[0].sources), 3)

    # The site's own spelling has to survive. It is what that site's URLs are
    # built from and cannot be recovered from the collapsed handle, so losing
    # it would make every content request for that site 404.
    spellings = {source.site: source.site_handle for source in accounts[0].sources}
    check("ultrathots spelling kept", spellings["ultrathots"], "sophie-rain")
    check("notfans spelling kept", spellings["notfans"], "sophierain")
    check("porntn spelling kept", spellings["porntn"], "sophie_rain")

    # One row per account per site. Without the constraint a second sync run
    # inserts duplicates and source_count becomes a count of sync runs.
    session.add(
        OnlyFansAccountSource(
            account_id=account.id,
            site="ultrathots",
            site_handle="sophie-rain",
            page_url="https://ultrathots.com/models/sophie-rain/",
        )
    )
    try:
        session.commit()
        failures.append("a duplicate (account, site) source was accepted")
    except sqlalchemy.exc.IntegrityError:
        session.rollback()

# A different person gets their own account, even with a near-identical handle.
with Session(engine) as session:
    session.add(
        OnlyFansAccount(
            handle=normalise_handle("sophie-raine"), display_name="Sophie Raine"
        )
    )
    session.commit()
    check(
        "near-identical handle kept separate",
        len(session.execute(select(OnlyFansAccount)).scalars().all()),
        2,
    )


if failures:
    print("FAILURES:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)

print("test_onlyfans: all checks passed")
