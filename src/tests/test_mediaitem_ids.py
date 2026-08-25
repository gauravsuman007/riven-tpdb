"""Every identifier column MediaItem declares must be read by its constructor.

This is a generic guard for a bug that is invisible at every other layer.
``adultempire_id`` was declared as a column and read by ``is_adult``, but
``MediaItem.__init__`` never assigned it from the payload -- so
``Movie({"adultempire_id": "700215"})`` produced an object that looked
completely normal and carried no Adult Empire id at all.

Nothing failed. ``is_adult`` returned False, which sent brochure titles to the
mainstream indexer categories and skipped the adult relevance filter, so a
manual scrape for "Pirates" came back with five Pirates of the Caribbean films
and no adult release whatsoever.

Read with ``ast`` rather than by importing: item.py pulls in RTN, SQLAlchemy and
the FUSE-backed filesystem models, none of which this needs.
"""

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ITEM = SRC / "program" / "media" / "item.py"

tree = ast.parse(ITEM.read_text())

media_item = next(
    node
    for node in ast.walk(tree)
    if isinstance(node, ast.ClassDef) and node.name == "MediaItem"
)

# Columns whose name ends in `_id` and that are plain scalars: the identifiers a
# caller passes in a payload dict. Relationship and foreign-key columns are not
# set this way and are excluded by the Mapped[...] shape below.
declared: set[str] = set()

for node in media_item.body:
    if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
        continue

    name = node.target.id

    if not name.endswith("_id") or name == "id":
        continue

    annotation = ast.unparse(node.annotation)

    # Only the simple `Mapped[str | None]` / `Mapped[int | None]` scalars.
    if annotation.startswith("Mapped[") and ("str" in annotation or "int" in annotation):
        declared.add(name)

init = next(
    node
    for node in media_item.body
    if isinstance(node, ast.FunctionDef) and node.name == "__init__"
)

assigned = {
    target.attr
    for node in ast.walk(init)
    if isinstance(node, ast.Assign)
    for target in node.targets
    if isinstance(target, ast.Attribute)
}

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
    except Exception as exc:
        FAILED.append((name, exc))


def test_the_scan_found_something():
    """Guard the guard: a silent parse failure would make this vacuously pass."""

    assert "tpdb_id" in declared, declared
    assert "adultempire_id" in declared, declared


def test_every_declared_id_is_read_from_the_payload():
    missing = sorted(declared - assigned)

    assert not missing, (
        f"MediaItem declares {missing} but __init__ never reads them from the "
        "payload, so they are silently dropped when an item is constructed"
    )


def _id_kwargs_accepted():
    functions = (SRC / "program/db/db_functions.py").read_text()
    start = functions.index("def item_exists_by_any_id(")
    signature = functions[start:functions.index(") -> bool:", start)]

    return {
        line.split(":")[0].strip()
        for line in signature.split("\n")[1:]
        if ":" in line
    }


def _call_sites():
    """Every call to item_exists_by_any_id in the tree, with its kwargs."""

    sites = []

    for path in sorted((SRC / "program").rglob("*.py")) + sorted(
        (SRC / "routers").rglob("*.py")
    ):
        text = path.read_text()
        needle = "item_exists_by_any_id("
        offset = 0

        while (found := text.find(needle, offset)) != -1:
            offset = found + len(needle)

            # Skip the definition itself.
            if text[max(0, found - 4):found].endswith("def "):
                continue

            depth = 1
            i = offset

            while i < len(text) and depth:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                i += 1

            call = text[offset:i - 1]
            kwargs = {
                part.split("=")[0].strip()
                for part in call.split(",")
                if "=" in part and part.split("=")[0].strip().isidentifier()
            }

            sites.append((path.relative_to(SRC), kwargs))

    return sites


def test_the_call_site_scan_found_something():
    """Guard the guard: a parse change must not make this vacuously pass."""

    sites = _call_sites()

    assert len(sites) >= 2, f"expected several call sites, found {sites}"


def test_every_call_site_passes_only_ids_the_check_accepts():
    accepted = _id_kwargs_accepted()

    for path, kwargs in _call_sites():
        unknown = sorted(kwargs - accepted)

        assert not unknown, (
            f"{path} passes {unknown} to item_exists_by_any_id, which does "
            "not accept them"
        )


def test_every_call_site_passing_a_bare_item_passes_adultempire_id():
    """A brochure title has no other id, so omitting it raises at runtime.

    Both known call sites drifted this way: `EventManager.add_item` and the
    idempotent insert in `run_thread_with_db_item`. Each one turned a request
    for an Adult Empire title into "At least one ID must be provided" -- a 500
    on the button in the first case, and an item that silently never reached
    the pipeline in the second.
    """

    # Only the "check every id this item has" pattern is in scope. A call that
    # deliberately passes one known id -- tpdb_content asking whether a TPDB id
    # is already known -- is not drift and must not be flagged.
    mainstream = {"imdb_id", "tmdb_id", "tvdb_id"}

    offenders = [
        str(path)
        for path, kwargs in _call_sites()
        if kwargs & mainstream and "adultempire_id" not in kwargs
    ]

    assert not offenders, (
        f"{offenders} pass tpdb_id but not adultempire_id; an Adult Empire "
        "title with no other id trips the 'At least one ID' guard"
    )


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
