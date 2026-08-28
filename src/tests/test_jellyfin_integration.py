"""End-to-end tests for the Jellyfin surface, through the real router.

Complements `test_jellyfin_server.py`, which tests the pieces in isolation.
This drives the actual FastAPI routes with a real database behind them, which
is the only way to catch the failures that matter most here: a route that
works in isolation but 404s because of decorator ordering, an auth dependency
that lets an anonymous request through, or a payload that is valid JSON and
still unusable to a client.

Needs the full dependency set and a database, so it is container-only and
skips cleanly elsewhere. The one thing it cannot cover is real byte delivery,
which needs a working debrid link.

The transcoding assertions seed the probe cache instead of probing for real.
That is deliberate and not a shortcut around a broken thing: what is under
test is the negotiation -- given these codecs and this client, what is
decided -- and feeding it known codecs is the only way to assert both
outcomes for one file.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, condition, extra=""):
    global PASS, FAIL

    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  -- {extra}" if extra else ""))


try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from program.db.db import db_session
    from program.media.item import MediaItem
    from program.services.streaming import transcode
    from program.settings import settings_manager
    from routers.jellyfin import router
except ModuleNotFoundError as exc:
    print(f"SKIP: {exc} (run inside the container)")
    sys.exit(0)

from program.services.jellyfin_server import ids  # noqa: E402

API_KEY = settings_manager.settings.api_key
SETTINGS = settings_manager.settings.jellyfin_server

# Force the feature on for the duration of the run, whatever the deployment
# has configured, and put it back afterwards.
_was_enabled = SETTINGS.enabled
SETTINGS.enabled = True

app = FastAPI()
app.include_router(router)
client = TestClient(app)

AUTH = {
    "Authorization": (
        f'MediaBrowser Token="{API_KEY}", Client="Test", Device="pytest", '
        f'DeviceId="t1", Version="1.0"'
    )
}

try:
    # ------------------------------------------------------------- handshake
    print("unauthenticated surface")
    r = client.get("/System/Info/Public")
    check("public info is reachable without a token", r.status_code == 200)
    check("it names a server", bool(r.json().get("ServerName")))
    check("it reports a version clients can gate on", bool(r.json().get("Version")))

    check("Items requires auth", client.get("/Items").status_code == 401)
    check("Views requires auth", client.get("/Users/me/Views").status_code == 401)
    check(
        "a wrong token is refused",
        client.get("/Items", headers={"X-Emby-Token": "nope"}).status_code == 401,
    )

    # Real clients send whatever casing they like, because Jellyfin's ASP.NET
    # routing is case-insensitive. A client library probing the lowercase
    # spelling was 404ing here until the router was made to match the same
    # way -- caught only by driving an actual client.
    print("\npaths match regardless of case")
    check("lowercase is accepted", client.get("/system/info/public").status_code == 200)
    check("mixed case is accepted", client.get("/SyStEm/InFo/pUbLiC").status_code == 200)
    check(
        "case-insensitivity does not bypass auth",
        client.get("/items").status_code == 401,
    )
    check(
        "authenticated lowercase browsing works",
        client.get("/items?limit=1", headers=AUTH).status_code == 200,
    )

    print("\nlogin")
    r = client.post(
        "/Users/AuthenticateByName",
        json={"Username": SETTINGS.username, "Pw": API_KEY},
        headers=AUTH,
    )
    check("valid credentials authenticate", r.status_code == 200, r.text[:120])
    body = r.json()
    check("an access token comes back", body.get("AccessToken") == API_KEY)
    check("the user object is populated", body["User"]["Name"] == SETTINGS.username)
    check("a session is described", body["SessionInfo"]["Client"] == "Test")

    check(
        "a bad password is refused",
        client.post(
            "/Users/AuthenticateByName",
            json={"Username": SETTINGS.username, "Pw": "wrong"},
            headers=AUTH,
        ).status_code
        == 401,
    )
    check(
        "a malformed body is a 400, not a 500",
        client.post(
            "/Users/AuthenticateByName", content=b"not json", headers=AUTH
        ).status_code
        == 400,
    )

    # --------------------------------------------------------------- browsing
    check(
        "lowercase login reaches the login route, not /Users/{user_id}",
        client.post(
            "/users/authenticatebyname",
            json={"Username": SETTINGS.username, "Pw": API_KEY},
            headers=AUTH,
        ).status_code
        == 200,
    )

    print("\nlibrary structure")
    views = client.get("/Users/me/Views", headers=AUTH).json()
    check("exactly one view is offered", views["TotalRecordCount"] == 1)
    check("it is a movie library", views["Items"][0]["CollectionType"] == "movies")
    check("the view is a folder", views["Items"][0]["IsFolder"] is True)

    print("\nitem listing")
    listing = client.get("/Items?limit=5", headers=AUTH).json()
    check("items come back", listing["TotalRecordCount"] > 0, listing)
    check("the page respects the limit", len(listing["Items"]) <= 5)

    sample = listing["Items"][0]
    check("every item has an id", bool(sample["Id"]))
    check("ids are 32 hex characters", len(sample["Id"]) == 32)
    check("items are typed as Movie", sample["Type"] == "Movie")
    check("items are not folders", sample["IsFolder"] is False)
    check("items carry UserData", "UserData" in sample)

    # Paging must not repeat rows; a client appending pages would show
    # duplicates and never reach the end.
    first = client.get("/Items?limit=3&startIndex=0", headers=AUTH).json()
    second = client.get("/Items?limit=3&startIndex=3", headers=AUTH).json()
    overlap = {i["Id"] for i in first["Items"]} & {i["Id"] for i in second["Items"]}
    check("paging does not repeat items", not overlap, overlap)

    names = [i["Name"] for i in client.get("/Items?limit=10", headers=AUTH).json()["Items"]]
    check("the default sort is by name", names == sorted(names), names[:3])
    desc = [
        i["Name"]
        for i in client.get(
            "/Items?limit=10&sortBy=SortName&sortOrder=Descending", headers=AUTH
        ).json()["Items"]
    ]
    check("descending sort is honoured", desc == sorted(desc, reverse=True))

    check(
        "an unknown sort key does not error",
        client.get("/Items?sortBy=Nonsense&limit=2", headers=AUTH).status_code == 200,
    )
    check(
        "an unknown filter parameter is ignored, not rejected",
        client.get("/Items?limit=2&IncludeItemTypes=Movie&Recursive=true&Fields=Overview",
                   headers=AUTH).status_code == 200,
    )

    print("\nlookup by id")
    item_id = sample["Id"]
    detail = client.get(f"/Items/{item_id}", headers=AUTH)
    check("an item can be fetched by id", detail.status_code == 200)
    detail = detail.json()
    check("the detail view includes MediaSources", len(detail.get("MediaSources", [])) == 1)
    check("the id round-trips", detail["Id"] == item_id)

    by_ids = client.get(f"/Items?Ids={item_id}", headers=AUTH).json()
    check("Ids= resolves a specific item", by_ids["TotalRecordCount"] == 1)
    check(
        "an unknown id yields nothing rather than everything",
        client.get(f"/Items?Ids={'0' * 31}1", headers=AUTH).json()["TotalRecordCount"] == 0,
    )
    check(
        "a malformed id is a 404",
        client.get("/Items/not-a-guid", headers=AUTH).status_code == 404,
    )
    check(
        "an unknown item is a 404",
        client.get(f"/Items/{ids.to_guid(999999999)}", headers=AUTH).status_code == 404,
    )

    print("\nsearch")
    term = sample["Name"].split()[0]
    found = client.get(f"/Items?searchTerm={term}&limit=20", headers=AUTH).json()
    check(f"searching {term!r} finds something", found["TotalRecordCount"] > 0)
    check(
        "a nonsense search finds nothing",
        client.get("/Items?searchTerm=zzzznotathing", headers=AUTH).json()[
            "TotalRecordCount"
        ]
        == 0,
    )

    print("\nimages")
    with db_session() as session:
        with_poster = session.execute(
            select(MediaItem).where(MediaItem.poster_path.isnot(None)).limit(1)
        ).scalar_one_or_none()

    if with_poster:
        r = client.get(
            f"/Items/{ids.to_guid(with_poster.id)}/Images/Primary", follow_redirects=False
        )
        check("a poster redirects to the CDN", r.status_code == 302, r.status_code)
        check("images need no token", "location" in r.headers)

    # ------------------------------------------------------------- playback
    print("\nplayback negotiation")

    BROWSERISH = {
        "DeviceProfile": {
            "DirectPlayProfiles": [
                {"Container": "mp4", "Type": "Video", "VideoCodec": "h264", "AudioCodec": "aac"}
            ]
        }
    }
    MODERN_TV = {
        "DeviceProfile": {
            "DirectPlayProfiles": [
                {
                    "Container": "mp4,mkv",
                    "Type": "Video",
                    "VideoCodec": "h264,hevc",
                    "AudioCodec": "aac,ac3,truehd",
                }
            ]
        }
    }

    # Pick an item whose file we can name, and seed the probe cache for it so
    # both branches can be asserted against one known file.
    target = None

    with db_session() as session:
        for candidate in session.execute(select(MediaItem).limit(50)).scalars():
            entries = [
                e
                for e in (candidate.filesystem_entries or [])
                if getattr(e, "entry_type", None) != "subtitle"
            ]

            if entries and getattr(entries[0], "original_filename", None):
                target = (candidate.id, entries[0].original_filename)
                break

    if not target:
        print("  (no item with a filename; skipping negotiation assertions)")
    else:
        target_id, filename = target
        guid = ids.to_guid(target_id)

        transcode._probe_cache[filename] = transcode.MediaProbe(
            duration=1800.0,
            video_codec="hevc",
            audio_codec="truehd",
            container="mkv",
            width=3840,
            height=2160,
        )

        r = client.post(f"/Items/{guid}/PlaybackInfo", json=BROWSERISH, headers=AUTH)
        check("PlaybackInfo answers", r.status_code == 200, r.text[:200])
        source = r.json()["MediaSources"][0]
        check(
            "a browser is told to transcode HEVC/TrueHD/MKV",
            source["SupportsDirectStream"] is False,
            source,
        )
        check("and is given a transcoding url", bool(source.get("TranscodingUrl")))
        check("the transcoding url is HLS", source.get("TranscodingSubProtocol") == "hls")
        check(
            "the transcoding url carries a token, since players send no headers",
            "api_key=" in (source.get("TranscodingUrl") or ""),
        )

        r = client.post(f"/Items/{guid}/PlaybackInfo", json=MODERN_TV, headers=AUTH)
        source = r.json()["MediaSources"][0]
        check(
            "a modern TV is allowed to direct stream the same file",
            source["SupportsDirectStream"] is True,
            source,
        )
        check("and gets no transcoding url", not source.get("TranscodingUrl"))
        # The whole point: same file, same server, two answers.
        check("stored metadata backfills the container", source["Container"] == "mkv")
        check("stored metadata backfills the runtime", source["RunTimeTicks"] == 18_000_000_000)

        r = client.post(f"/Items/{guid}/PlaybackInfo", json={}, headers=AUTH)
        check(
            "a client sending no DeviceProfile still gets an answer",
            r.status_code == 200 and r.json()["MediaSources"],
        )

        check(
            "PlaybackInfo requires auth",
            client.post(f"/Items/{guid}/PlaybackInfo", json={}).status_code == 401,
        )

        transcode._probe_cache.pop(filename, None)

    print("\nsession reporting")
    check(
        "progress is accepted",
        client.post(
            "/Sessions/Playing/Progress", json={"ItemId": "x", "PositionTicks": 1}, headers=AUTH
        ).status_code
        == 204,
    )
    check(
        "playback start is accepted",
        client.post("/Sessions/Playing", json={}, headers=AUTH).status_code == 204,
    )
    check(
        "capabilities are accepted",
        client.post("/Sessions/Capabilities/Full", json={}, headers=AUTH).status_code == 204,
    )
    check(
        "display preferences are served",
        client.get("/DisplayPreferences/usersettings", headers=AUTH).status_code == 200,
    )

    print("\ndisabled server exposes nothing")
    SETTINGS.enabled = False
    check("public info 404s", client.get("/System/Info/Public").status_code == 404)
    check(
        "login 404s",
        client.post(
            "/Users/AuthenticateByName",
            json={"Username": SETTINGS.username, "Pw": API_KEY},
            headers=AUTH,
        ).status_code
        == 404,
    )
    check("browsing 404s even with a valid token", client.get("/Items", headers=AUTH).status_code == 404)

finally:
    SETTINGS.enabled = _was_enabled

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
