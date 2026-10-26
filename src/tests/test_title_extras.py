"""TMDB matching for billing order and wide art, and the IMDb ratings scan."""

import gzip
import os

import pytest

from program.apis import imdb_ratings, title_extras
from program.apis.tpdb_api import TpdbApi


def fake_tmdb(searches, movies):
    """`searches`: {(query, year): [results]}; `movies`: {id: details}."""

    calls = []

    def call(path, **params):
        calls.append((path, params))

        if path == "search/movie":
            return {"results": searches.get((params["query"], params["year"]), [])}

        return movies[int(path.split("/")[1])]

    return call, calls


def details(movie_id, cast, backdrops=(), imdb=None, backdrop_path=None):
    return {
        "id": movie_id,
        "credits": {"cast": [{"name": n, "order": i} for i, n in enumerate(cast)]},
        "external_ids": {"imdb_id": imdb},
        "images": {"backdrops": list(backdrops)},
        "backdrop_path": backdrop_path,
    }


def test_the_year_is_a_search_filter(monkeypatch):
    """"Drive" (2019) is buried under mainstream films without it."""

    call, calls = fake_tmdb(
        {("Drive", 2019): [{"id": 1, "title": "Drive", "adult": True}]},
        {1: details(1, ["Maitland Ward"])},
    )
    monkeypatch.setattr(title_extras, "_tmdb", call)

    assert title_extras.match_tmdb("Drive", 2019, ["Maitland Ward"])["id"] == 1
    assert calls[0] == ("search/movie", {"query": "Drive", "include_adult": "true", "year": 2019})


def test_only_an_exact_adult_title_matches(monkeypatch):
    call, _ = fake_tmdb(
        {
            ("Pirates", 2005): [
                {"id": 1, "title": "Pirates", "adult": False},
                {"id": 2, "title": "Butthole Pirates", "adult": True},
            ]
        },
        {},
    )
    monkeypatch.setattr(title_extras, "_tmdb", call)

    assert title_extras.match_tmdb("Pirates", 2005, []) is None


def test_a_cast_sharing_nobody_is_another_film(monkeypatch):
    call, _ = fake_tmdb(
        {("Grinders", 2022): [{"id": 1, "title": "Grinders", "adult": True}]},
        {1: details(1, ["Somebody Else"])},
    )
    monkeypatch.setattr(title_extras, "_tmdb", call)

    assert title_extras.match_tmdb("Grinders", 2022, ["Maya Woulfe"]) is None


def test_a_neighbouring_year_is_tried(monkeypatch):
    call, _ = fake_tmdb(
        {("After Dark", 2018): [{"id": 7, "title": "After Dark", "adult": True}]},
        {7: details(7, ["Tori Black"])},
    )
    monkeypatch.setattr(title_extras, "_tmdb", call)

    assert title_extras.match_tmdb("After Dark", 2019, ["Tori Black"])["id"] == 7


def test_a_textless_backdrop_is_preferred():
    chosen = title_extras._best_backdrop(
        {
            "images": {
                "backdrops": [
                    {"file_path": "/titled.jpg", "iso_639_1": "en", "vote_average": 9},
                    {"file_path": "/clean.jpg", "iso_639_1": None, "vote_average": 5},
                ]
            }
        }
    )
    assert chosen == "/clean.jpg"


class Record:
    def __init__(self, data):
        self.data = data

    def model_dump(self):
        return self.data


def test_a_scene_uses_its_own_still(tmp_path):
    api = TpdbApi(cache_dir=tmp_path)
    api.get_movie = lambda uuid: None
    api.get_scene = lambda uuid: Record(
        {"title": "x", "background": {"full": "https://cdn.theporndb.net/s.jpg"}}
    )

    result = title_extras.compute(api, "u")
    assert result["kind"] == "scene" and result["wide"] == "https://cdn.theporndb.net/s.jpg"


def test_the_records_type_beats_the_endpoint_that_answered(tmp_path, monkeypatch):
    """A record typed as a scene is never matched on TMDB as a film."""

    api = TpdbApi(cache_dir=tmp_path)
    api.get_movie = lambda uuid: Record(
        {"type": "Scene", "title": "Abella Danger", "background": {"full": "https://cdn.theporndb.net/s.jpg"}}
    )
    monkeypatch.setattr(title_extras, "_tmdb", lambda *a, **k: pytest.fail("asked TMDB about a scene"))

    result = title_extras.compute(api, "u")
    assert result["kind"] == "scene" and result["wide"] == "https://cdn.theporndb.net/s.jpg"


def test_a_movie_gets_billing_order_backdrop_and_imdb(tmp_path, monkeypatch):
    api = TpdbApi(cache_dir=tmp_path)
    api.get_movie = lambda uuid: Record(
        {
            "title": "Drive",
            "date": "2019-09-30",
            "performers": [{"name": "Angela White"}, {"name": "Maitland Ward"}],
        }
    )
    call, _ = fake_tmdb(
        {("Drive", 2019): [{"id": 1, "title": "Drive", "adult": True}]},
        {1: details(1, ["Maitland Ward", "Angela White"], imdb="tt1", backdrop_path="/b.jpg")},
    )
    monkeypatch.setattr(title_extras, "_tmdb", call)

    result = title_extras.compute(api, "u")
    assert result["cast_order"] == ["Maitland Ward", "Angela White"]
    assert result["imdb_id"] == "tt1"
    assert result["wide"] == "https://image.tmdb.org/t/p/original/b.jpg"


def test_a_miss_is_cached_too(tmp_path, monkeypatch):
    api = TpdbApi(cache_dir=tmp_path)
    looked = []
    api.get_movie = lambda uuid: looked.append(uuid) or None
    api.get_scene = lambda uuid: None

    title_extras.extras(api, "0c4558fd-3478-4ddd-834e-ead9a9f4e480")
    title_extras.extras(api, "0c4558fd-3478-4ddd-834e-ead9a9f4e480")
    assert len(looked) == 1


def test_imdb_ratings_keep_only_wanted_ids(tmp_path, monkeypatch):
    api = TpdbApi(cache_dir=tmp_path)
    directory = tmp_path / "imdb"
    directory.mkdir()
    with gzip.open(directory / "title.ratings.tsv.gz", "wt") as handle:
        handle.write("tconst\taverageRating\tnumVotes\n")
        handle.write("tt1\t6.8\t1200\n")
        handle.write("tt2\t5.0\t10\n")
    monkeypatch.setattr(
        imdb_ratings.requests, "get", lambda *a, **k: pytest.fail("downloaded a fresh file")
    )

    imdb_ratings.want(api, "tt1")
    assert imdb_ratings.refresh(api) == 1
    assert imdb_ratings.get(api, "tt1") == (6.8, 1200)
    assert imdb_ratings.get(api, "tt2") is None


def test_imdb_files_count_towards_the_cache_budget(tmp_path):
    api = TpdbApi(cache_dir=tmp_path, cache_max_size_mb=1)
    (tmp_path / "imdb").mkdir()
    old = tmp_path / "imdb" / "title.ratings.tsv.gz"
    old.write_bytes(b"x" * 600_000)
    os.utime(old, (1, 1))
    (tmp_path / "answer.json").write_bytes(b"x" * 600_000)

    api._enforce_size_limit()

    assert not old.exists()
