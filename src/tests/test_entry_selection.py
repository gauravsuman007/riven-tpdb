"""A multi-file release keeps all of its files.

The rule under test decided how much of a torrent reached the library, and it
got it wrong in the quietest possible way: the download succeeded, the item
completed, the player worked, and three quarters of a four-scene release were
simply not there. Everything downstream -- `media_parts`, the parts panel, the
`.m3u` hand-off -- was correct and had nothing to show.

Stdlib only, like the other suites here.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "entry_selection",
    SRC / "program" / "services" / "downloaders" / "entry_selection.py",
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
stale_entries = _module.stale_entries

PASSED: list[str] = []
FAILED: list[tuple[str, Exception]] = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the harness reports, it does not raise
        FAILED.append((name, exc))
        print(f"  FAIL {name}: {exc}")
    else:
        PASSED.append(name)
        print(f"  ok   {name}")


RELEASE = "3c384dfc5df51652015fb191e6e535af4910a948"
OTHER = "ed3b742fe815af536f163aa29872f401f2fb46f9"


def _entry(filename, infohash=RELEASE):
    return SimpleNamespace(original_filename=filename, stream_infohash=infohash)


# The four scenes of "Mistress Maitland" (Deeper), in the order TorBox lists
# them. The library kept only the last one.
DEEPER = [
    "DEEPER_101426_1080lP.mp4",
    "DEEPER_101427_1080lP.mp4",
    "DEEPER_101428_1080lP.mp4",
    "DEEPER_101429_1080lP.mp4",
]


def _ingest(files, infohash=RELEASE, existing=None):
    """What the item holds after `update_item_attributes` walks `files`."""

    entries = list(existing or [])

    for filename in files:
        for entry in stale_entries(entries, infohash, filename):
            entries.remove(entry)

        entries.append(_entry(filename, infohash))

    return entries


def test_every_file_of_one_release_survives_the_walk():
    """The regression. Four files in, four files out -- it used to be one."""

    kept = _ingest(DEEPER)

    assert [e.original_filename for e in kept] == DEEPER, [
        e.original_filename for e in kept
    ]


def test_a_single_file_release_is_unchanged():
    kept = _ingest(["Mistress Maitland.mp4"])

    assert len(kept) == 1


def test_the_previous_release_is_still_cleared_away():
    """The reason the old code cleared at all, and it has to keep working.

    `media_parts` groups by infohash and falls back to `is_active`; leaving a
    swapped-out release's files behind is how item 862 once played a file from
    a release it no longer used.
    """

    old = [_entry("Kristen Scott 1.mp4", OTHER), _entry("Kristen Scott 2.mp4", OTHER)]
    kept = _ingest(DEEPER, existing=old)

    assert [e.original_filename for e in kept] == DEEPER
    assert all(e.stream_infohash == RELEASE for e in kept)


def test_re_processing_a_release_replaces_its_files_rather_than_doubling_them():
    kept = _ingest(DEEPER, existing=_ingest(DEEPER))

    assert [e.original_filename for e in kept] == DEEPER


def test_an_entry_with_no_infohash_is_stale():
    """It predates the grouping, so it cannot be shown to belong to this one.

    Keeping it would put a file of unknown provenance into the playlist.
    """

    orphan = _entry("something-older.mp4", None)
    kept = _ingest(["DEEPER_101426_1080lP.mp4"], existing=[orphan])

    assert [e.original_filename for e in kept] == ["DEEPER_101426_1080lP.mp4"]


def test_the_downloader_uses_this_rule_and_no_longer_clears():
    """The clear() is the bug; this is what stops it coming back."""

    body = (SRC / "program" / "services" / "downloaders" / "__init__.py").read_text()

    assert "stale_entries(" in body, "the downloader must use the shared rule"

    # Statements only -- `_update_attributes` names the old call in a comment
    # explaining why it is gone, and that mention is the point of the comment.
    statements = [
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    ]

    assert not any("filesystem_entries.clear()" in line for line in statements), (
        "clearing per file is what dropped three of four scenes"
    )


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

sys.exit(1 if FAILED else 0)
