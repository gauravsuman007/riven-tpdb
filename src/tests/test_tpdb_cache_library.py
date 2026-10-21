"""A library title's TPDB data is answered locally, never by a slow TPDB call."""

import json
import os
import time

import pytest

from program.apis.tpdb_api import TpdbApi, TpdbApiError

UUID = "0c4558fd-3478-4ddd-834e-ead9a9f4e480"


class Reply:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class Session:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        status, body = self.answers[url]
        return Reply(status, body)


def make(tmp_path, answers, ttl=3600.0, max_mb=250):
    api = TpdbApi(cache_dir=tmp_path, cache_ttl=ttl, cache_max_size_mb=max_mb)
    api.session = Session(answers)
    return api


def test_a_404_is_remembered(tmp_path):
    api = make(tmp_path, {f"scenes/{UUID}": (404, {})})

    assert api.get_scene(UUID) is None
    assert api.get_scene(UUID) is None
    assert api.session.calls == [f"scenes/{UUID}"]


def test_expired_answer_is_served_at_once_and_refreshed(tmp_path):
    body = {"data": {"id": UUID, "title": "Half His Age"}}
    api = make(tmp_path, {f"movies/{UUID}": (200, body)}, ttl=1)

    api.get_movie(UUID)
    api._memory.clear()

    for path in tmp_path.rglob("*.json"):
        entry = json.loads(path.read_text())
        entry["stored_at"] -= 3600
        path.write_text(json.dumps(entry))

    started = time.monotonic()
    assert api.get_movie(UUID).title == "Half His Age"
    assert time.monotonic() - started < 0.5

    api._refresher.shutdown(wait=True)
    assert api.session.calls == [f"movies/{UUID}", f"movies/{UUID}"]


def test_pinned_answers_survive_eviction(tmp_path):
    api = make(tmp_path, {f"movies/{UUID}": (200, {"data": {"id": UUID}})}, max_mb=0)
    api.pinned.add(UUID)
    api.cache_max_bytes = 1  # anything unpinned is over budget

    api.get_movie(UUID)
    api._enforce_size_limit()

    assert list((tmp_path / "pinned").glob("*.json"))


def test_a_collection_write_keeps_title_records(tmp_path):
    api = make(
        tmp_path,
        {
            f"movies/{UUID}": (200, {"data": {"id": UUID}}),
            "user/collection?scene_id=7": (200, {"value": False}),
        },
    )
    api.get_movie(UUID)
    api.is_collected(7)

    api.invalidate_cache()
    api._memory.clear()

    api.get_movie(UUID)
    assert api.session.calls.count(f"movies/{UUID}") == 1

    api.is_collected(7)
    assert api.session.calls.count("user/collection?scene_id=7") == 2


def test_a_real_outage_still_raises_without_a_copy(tmp_path):
    api = make(tmp_path, {f"movies/{UUID}": (500, {})})

    with pytest.raises(TpdbApiError):
        api.get_movie(UUID)
