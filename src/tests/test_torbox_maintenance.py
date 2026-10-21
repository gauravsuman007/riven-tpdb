"""When a TorBox torrent is deleted, stopped, paused for room, or left alone."""

from datetime import datetime, timedelta, timezone

from program.services.downloaders import torbox_maintenance as m

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def torrent(h, **kw):
    base = dict(
        id=int(h, 16), hash=h, name="Some Release", owner="me", auth_id="me",
        active=True, download_finished=False, progress=0.4,
        download_state="downloading",
        created_at=(NOW - timedelta(days=3)).isoformat(),
    )
    base.update(kw)
    return base


def stalled(h, hours, **kw):
    t = torrent(h, download_state="stalled (no seeds)", **kw)
    return t, hours


def state_for(*pairs):
    st = m.State()
    for t, hours in pairs:
        st.stalled_since[t["hash"]] = (NOW - timedelta(hours=hours)).isoformat()
    return st


def test_unresolved_after_five_minutes_is_deleted():
    old = torrent("a1", name="a1", progress=0, download_state="checking",
                  created_at=(NOW - timedelta(minutes=6)).isoformat())
    assert m.plan([old], m.State(), NOW).delete == [old]


def test_unresolved_inside_five_minutes_is_left():
    new = torrent("a2", name="a2", progress=0, download_state="checking",
                  created_at=(NOW - timedelta(minutes=4)).isoformat())
    assert m.plan([new], m.State(), NOW).delete == []


def test_anything_with_progress_or_a_name_is_never_deleted():
    named = torrent("a3", progress=0, download_state="stalled (no seeds)")
    moving = torrent("a4", name="a4", progress=0.2, download_state="checking")
    assert m.plan([named, moving], m.State(), NOW).delete == []


def test_someone_elses_torrent_is_never_deleted():
    theirs = torrent("a5", name="a5", progress=0, download_state="checking",
                     owner="other")
    assert m.plan([theirs], m.State(), NOW).delete == []


def test_stalled_48h_is_stopped_not_deleted():
    t, h = stalled("b1", 49)
    p = m.plan([t], state_for((t, h)), NOW)
    assert p.stop == [t] and p.delete == []


def test_stalled_47h_is_left_alone():
    t, h = stalled("b2", 47)
    assert m.plan([t], state_for((t, h)), NOW).stop == []


def test_pause_candidates_need_five_hours_and_longest_goes_first():
    a, b, c = stalled("c1", 6)[0], stalled("c2", 20)[0], stalled("c3", 2)[0]
    st = state_for((a, 6), (b, 20), (c, 2))
    assert m.pause_candidates([a, b, c], st, NOW) == [b, a]


def test_first_sighting_starts_the_clock():
    t = stalled("d1", 0)[0]
    st = m.State()
    st.observe([t], NOW)
    assert st.stalled_for(t, NOW + timedelta(hours=3)) == timedelta(hours=3)
    t["download_state"] = "downloading"
    st.observe([t], NOW + timedelta(hours=4))
    assert st.stalled_since == {}


def test_in_flight_blocks_resume():
    assert m.in_flight([torrent("e1")]) is True
    assert m.in_flight([stalled("e2", 9)[0]]) is False
