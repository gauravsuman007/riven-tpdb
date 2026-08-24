"""Tests for the TorBox response models.

Every payload here is shaped like something TorBox actually returned against
this account, including the in-progress torrent whose `files` is null -- which
made every progress read raise a validation error.
"""

from program.services.downloaders.torbox import TorBoxTorrent

PASS = FAIL = 0


def check(name, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


print("\n-- files is null while a torrent is still being fetched --")
in_progress = TorBoxTorrent.model_validate(
    {
        "id": 82589218,
        "name": "Deny It All You Want [2022] 1080p WEB-DL",
        "hash": "4eb97fdd7a5118addbbc1ce08f79c1bcfd43",
        "size": 2147483648,
        "cached": False,
        "progress": 0.0,
        "download_state": "downloading",
        "files": None,
    }
)
check(
    "a null file list validates instead of raising",
    in_progress.files == [],
    "this raised `Input should be a valid list`, so progress could never be read",
)
check("progress still comes through", in_progress.progress == 0.0)
check("download_state still comes through", in_progress.download_state == "downloading")
check("cached still comes through", in_progress.cached is False)

print("\n-- files absent entirely --")
missing = TorBoxTorrent.model_validate({"id": 1, "name": "x"})
check("an omitted file list is empty, not None", missing.files == [])

print("\n-- a finished torrent --")
done = TorBoxTorrent.model_validate(
    {
        "id": 82589219,
        "name": "Alpha Male",
        "cached": True,
        "progress": 1.0,
        "download_state": "completed",
        "files": [
            {"id": 0, "name": "Scene_02.mp4", "size": 1073741824},
            {"id": 1, "name": "sample.mp4", "size": 1048576},
        ],
    }
)
check("both files are parsed", len(done.files) == 2)
check("file ids survive", [f.id for f in done.files] == [0, 1])
check("file names survive", done.files[0].name == "Scene_02.mp4")
check("cached is reported", done.cached is True)

print("\n-- an empty list is not confused with null --")
empty = TorBoxTorrent.model_validate({"id": 2, "files": []})
check("an explicitly empty list stays empty", empty.files == [])

print("\n-- seeder count, used to tell a dead torrent from a slow one --")
dead = TorBoxTorrent.model_validate(
    {
        "id": 82589417,
        "name": "0326f338ab97ea05dbd6e0840d88407b99050cc4",
        "download_state": "checking",
        "progress": 0.0,
        "seeds": 0,
        "files": None,
    }
)
check("zero seeders is parsed as 0, not dropped", dead.seeds == 0)
check(
    "a stalled torrent is distinguishable from a starting one",
    dead.seeds == 0 and not dead.progress,
    "this exact shape sat for hours: checking, no peers, no progress",
)
check(
    "a torrent still named after its infohash has no metadata yet",
    dead.name == dead.name.lower() and len(dead.name) == 40,
)

alive = TorBoxTorrent.model_validate(
    {"id": 1, "progress": 0.0, "seeds": 12, "files": None}
)
check("a seeded torrent at zero progress is not stalled", alive.seeds == 12)

unknown = TorBoxTorrent.model_validate({"id": 1, "progress": 0.0, "files": None})
check(
    "an absent seed count is None, not 0",
    unknown.seeds is None,
    "conflating 'unknown' with 'none' would abandon healthy torrents",
)

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
