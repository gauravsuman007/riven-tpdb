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


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
