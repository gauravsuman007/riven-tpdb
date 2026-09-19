"""The feature plays first, and numbered scenes play in their own order.

Filename order was right for numbered scenes by luck and wrong everywhere
else: Island Fever 3 opened on its 105 MB trailer because `Trailer.mkv` sorts
before `Island.Fever.3.mkv`, and nothing in the player said the 3.1 GB feature
existed. The cases below are real releases out of the measured sample
(`design/MULTIFILE-RELEASES.md`), including the two that a keyword-only
extras test got wrong.

Stdlib only, like the other suites here.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "part_ordering", SRC / "program" / "media" / "part_ordering.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
order_parts = _module.order_parts

PASSED: list[str] = []
FAILED: list[tuple[str, Exception]] = []

GB = 1024**3


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the harness reports, it does not raise
        FAILED.append((name, exc))
        print(f"  FAIL {name}: {exc}")
    else:
        PASSED.append(name)
        print(f"  ok   {name}")


def _parts(*pairs):
    return [
        SimpleNamespace(original_filename=name, file_size=int(size * GB))
        for name, size in pairs
    ]


def _names(entries):
    return [entry.original_filename for entry in entries]


def test_the_feature_plays_before_its_bonus_material():
    """Island Fever 3, the release that prompted all of this."""

    ordered = order_parts(
        _parts(("BTS.mkv", 0.8), ("Trailer.mkv", 0.1), ("Island.Fever.3.mkv", 3.1))
    )

    assert _names(ordered) == ["Island.Fever.3.mkv", "BTS.mkv", "Trailer.mkv"]


def test_numbered_scenes_are_ordered_numerically():
    """Mistress Maitland (Deeper). Alphabetical happens to agree here."""

    ordered = order_parts(
        _parts(
            ("DEEPER_101428_1080lP.mp4", 4),
            ("DEEPER_101426_1080lP.mp4", 4),
            ("DEEPER_101429_1080lP.mp4", 4),
            ("DEEPER_101427_1080lP.mp4", 4),
        )
    )

    assert _names(ordered) == [
        "DEEPER_101426_1080lP.mp4",
        "DEEPER_101427_1080lP.mp4",
        "DEEPER_101428_1080lP.mp4",
        "DEEPER_101429_1080lP.mp4",
    ]


def test_a_double_digit_scene_does_not_sort_before_scene_two():
    """The whole point of reading the run as a number rather than as text."""

    ordered = order_parts(
        _parts(("Scene10.mp4", 4), ("Scene2.mp4", 4), ("Scene1.mp4", 4))
    )

    assert _names(ordered) == ["Scene1.mp4", "Scene2.mp4", "Scene10.mp4"]


def test_a_release_group_in_the_name_is_not_an_extra():
    """`rarbg` was once an extras keyword, and this file is the feature.

    Its actual junk file is caught on size instead, with no keyword at all.
    """

    ordered = order_parts(
        _parts(
            ("RARBG.com.mp4", 0.0001),
            ("The.Amazing.Spider-Man.2012.1080p.BluRay.H264.AAC-RARBG.mp4", 8),
        )
    )

    assert _names(ordered)[0].startswith("The.Amazing")


def test_a_big_file_named_bonus_is_still_a_feature():
    """Both halves of a double feature carry `Bonus` in the title.

    Keyword-only sent both to the end, which is why an extra must also be
    small.
    """

    ordered = order_parts(
        _parts(
            ("Pirates 2005 Bonus Edition.mkv", 7),
            ("Pirates 2 2008 Bonus Edition.mkv", 8),
            ("trailer.mkv", 0.1),
        )
    )

    assert _names(ordered)[-1] == "trailer.mkv"
    assert len(_names(ordered)) == 3


def test_when_everything_looks_like_an_extra_nothing_is():
    """Otherwise a release is reordered on no information at all."""

    ordered = order_parts(_parts(("sample 1.mp4", 1), ("sample 2.mp4", 1)))

    assert _names(ordered) == ["sample 1.mp4", "sample 2.mp4"]


def test_part_markers_order_when_the_prefix_differs():
    """One rename away from the digit-run rule, so the marker rule stays."""

    ordered = order_parts(
        _parts(("fsd2 part 2.avi", 1), ("another part 1.avi", 1))
    )

    assert _names(ordered) == ["another part 1.avi", "fsd2 part 2.avi"]


def test_performer_named_scenes_keep_alphabetical_order():
    """Not a fallback: these have no intrinsic order, and this is today's
    behaviour, which must not change for the 11 of 30 it already suits."""

    ordered = order_parts(
        _parts(("Riley Reid.mp4", 4), ("Ava Sinclaire.mp4", 4))
    )

    assert _names(ordered) == ["Ava Sinclaire.mp4", "Riley Reid.mp4"]


def test_a_year_is_not_mistaken_for_a_scene_index():
    """The identical-prefix requirement earning its keep: the varying run
    here is the resolution, not an index, and the text before it differs."""

    ordered = order_parts(
        _parts(("Alpha 1080p.mp4", 4), ("Beta 720p.mp4", 4))
    )

    assert _names(ordered) == ["Alpha 1080p.mp4", "Beta 720p.mp4"]


def test_a_single_file_is_returned_untouched():
    entries = _parts(("trailer.mkv", 0.1))

    assert order_parts(entries) == entries


def test_media_parts_orders_through_this_module():
    """`media_parts` must not grow a sort of its own again.

    It cannot be imported here (it needs a database), so the guarantee is
    made against its source.
    """

    body = (SRC / "program" / "media" / "item.py").read_text()

    assert "media_entries = order_parts(media_entries)" in body
    assert "media_entries.sort(" not in body


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

sys.exit(1 if FAILED else 0)
