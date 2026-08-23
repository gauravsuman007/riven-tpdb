"""Tests for the settings visibility filter.

Upstream's provider settings models are kept intact so upstream changes to
them merge cleanly; the ones this fork cannot use are hidden from the settings
schema instead. These tests pin the two properties that matter:

* the hidden sections really are absent from what the UI renders, and
* pruning does not mutate the schema pydantic handed us (it may be cached).

The pruner is pure, so this needs no app boot and no database.
"""

import sys
from copy import deepcopy
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from program.settings.visibility import (  # noqa: E402
    HIDDEN_SECTIONS,
    prune_settings_schema,
)

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


def _ref_schema():
    """A schema shaped the way `/settings/schema` returns one."""

    return {
        "properties": {
            "content": {"$ref": "#/$defs/ContentModel"},
            "scraping": {"$ref": "#/$defs/ScraperModel"},
        },
        "$defs": {
            "ContentModel": {
                "properties": {"tpdb": {}, "overseerr": {}, "trakt": {}},
                "required": ["tpdb", "overseerr"],
            },
            "ScraperModel": {
                "properties": {"prowlarr": {}, "jackett": {}, "torrentio": {}},
            },
        },
    }


def _inline_schema():
    """The shape `/settings/schema/keys` returns: sub-models inlined."""

    return {
        "properties": {
            "content": {"properties": {"tpdb": {}, "mdblist": {}}},
        }
    }


def _test_hidden_removed_from_refs():
    out = prune_settings_schema(_ref_schema())

    content = out["$defs"]["ContentModel"]["properties"]
    scraping = out["$defs"]["ScraperModel"]["properties"]

    assert "tpdb" in content, "tpdb must survive"
    assert "overseerr" not in content, "overseerr must be hidden"
    assert "trakt" not in content, "trakt must be hidden"
    assert {"prowlarr", "jackett"} <= set(scraping), "working scrapers must survive"
    assert "torrentio" not in scraping, "torrentio must be hidden"


def _test_required_is_pruned_too():
    out = prune_settings_schema(_ref_schema())

    required = out["$defs"]["ContentModel"]["required"]

    assert required == ["tpdb"], f"stale required entry: {required}"


def _test_inlined_sub_model_is_pruned():
    out = prune_settings_schema(_inline_schema())

    props = out["properties"]["content"]["properties"]

    assert "tpdb" in props
    assert "mdblist" not in props, "inlined sub-models must be pruned too"


def _test_input_is_not_mutated():
    original = _ref_schema()
    snapshot = deepcopy(original)

    prune_settings_schema(original)

    assert original == snapshot, "pruning mutated the caller's schema"


def _test_unknown_shapes_are_left_alone():
    # A top-level key with neither $ref nor inline properties must not raise.
    schema = {"properties": {"content": {"type": "string"}}}

    assert prune_settings_schema(schema) == schema


def _test_hidden_sections_are_not_empty():
    # Guards against the filter silently becoming a no-op.
    assert HIDDEN_SECTIONS, "no sections configured"
    for key, hidden in HIDDEN_SECTIONS.items():
        assert hidden, f"{key} lists no hidden sections"


TESTS = [
    ("visibility: hidden sections removed from $defs", _test_hidden_removed_from_refs),
    ("visibility: required list pruned too", _test_required_is_pruned_too),
    ("visibility: inlined sub-models pruned", _test_inlined_sub_model_is_pruned),
    ("visibility: caller's schema not mutated", _test_input_is_not_mutated),
    ("visibility: unknown shapes left alone", _test_unknown_shapes_are_left_alone),
    ("visibility: filter is not a no-op", _test_hidden_sections_are_not_empty),
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
