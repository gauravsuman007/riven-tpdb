"""A split release's parts are named 1.mkv, 2.mkv; for a Movie they are parts."""

from types import SimpleNamespace

from program.media.item import Movie
from program.services.downloaders import Downloader
from program.services.downloaders.shared import parse_filename


def _match(item, filename):
    calls = []
    fake = SimpleNamespace(
        service=None, _update_attributes=lambda *a, **k: calls.append(a)
    )
    file = SimpleNamespace(filename=filename)
    found = Downloader.match_file_to_item(
        fake, item, parse_filename(filename), file, SimpleNamespace()
    )
    return found, calls


def test_bare_number_matches_a_movie():
    for name in ("1.mkv", "2.mkv", "3.mkv"):
        found, calls = _match(Movie({"title": "Half His Age"}), name)
        assert found and len(calls) == 1, name


def test_named_non_movie_file_still_rejected():
    found, _ = _match(Movie({"title": "Half His Age"}), "Show.Name.S01E02.mkv")
    assert not found
