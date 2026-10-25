"""Performer pictures: TPDB-only fetching, plain downscaling, one cache budget."""

import os
import shutil
import subprocess

import pytest

from program.apis import tpdb_images
from program.apis.tpdb_api import TpdbApi

POSTER = "https://cdn.theporndb.net/performer/d4/6e/2c/x/poster/someone.jpg"


def jpeg(width, height):
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc=size={width}x{height}", "-frames:v", "1",
         "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
        capture_output=True, check=True,
    ).stdout


def size_of(webp):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-f", "webp_pipe", "-i", "pipe:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0"],
        input=webp, capture_output=True, check=True,
    ).stdout.decode().strip()
    return tuple(int(n) for n in out.split(","))


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="no ffmpeg")


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.theporndb.net/a.jpg",  # not https
        "https://192.168.2.100/a.jpg",  # the home network
        "https://cdn.theporndb.net.evil.example/a.jpg",
        "https://example.com/a.jpg",
        "not a url",
    ],
)
def test_only_tpdb_image_hosts_are_fetched(url, tmp_path, monkeypatch):
    monkeypatch.setattr(tpdb_images, "_fetch", lambda _: pytest.fail("fetched"))

    assert not tpdb_images.is_allowed(url)
    assert tpdb_images.performer_image(TpdbApi(cache_dir=tmp_path), url) is None


@needs_ffmpeg
def test_a_poster_is_shrunk_to_200_wide_keeping_its_shape():
    assert size_of(tpdb_images.downscale(jpeg(1320, 1980))) == (200, 300)


@needs_ffmpeg
def test_a_small_poster_is_never_enlarged():
    assert size_of(tpdb_images.downscale(jpeg(150, 226))) == (150, 226)


@needs_ffmpeg
def test_a_picture_is_fetched_once_then_served_from_disk(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tpdb_images, "_fetch", lambda url: calls.append(url) or jpeg(600, 900))
    api = TpdbApi(cache_dir=tmp_path)

    first = tpdb_images.performer_image(api, POSTER)
    second = tpdb_images.performer_image(api, POSTER)

    assert first == second and calls == [POSTER]
    assert len(list((tmp_path / "images").glob("*.webp"))) == 1


def test_a_failure_is_remembered(tmp_path, monkeypatch):
    calls = []

    def broken(url):
        calls.append(url)
        raise RuntimeError("404")

    monkeypatch.setattr(tpdb_images, "_fetch", broken)
    api = TpdbApi(cache_dir=tmp_path)

    assert tpdb_images.performer_image(api, POSTER) is None
    assert tpdb_images.performer_image(api, POSTER) is None
    assert calls == [POSTER]


def test_pictures_count_towards_the_tpdb_cache_budget(tmp_path):
    # 1 MB budget: an old picture and a newer JSON answer of 600 KB each.
    api = TpdbApi(cache_dir=tmp_path, cache_max_size_mb=1)
    images = tmp_path / "images"
    images.mkdir()
    old = images / "old.webp"
    old.write_bytes(b"x" * 600_000)
    os.utime(old, (1, 1))
    newer = tmp_path / "answer.json"
    newer.write_bytes(b"x" * 600_000)

    api._enforce_size_limit()

    assert not old.exists() and newer.exists()
