"""Shared plumbing for the direct-site scrapers."""

import re
from abc import ABC, abstractmethod

import requests

from program.services.directscrapers.models import DirectSource, DirectVideo


# These sites serve different markup to anything that looks automated, so the
# UA is a real browser's rather than a courtesy string.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class DirectScraper(ABC):
    """A site that can be searched for videos and resolved to media URLs."""

    key: str
    name: str
    base_url: str

    #: Requests per second. These are small sites being scraped, not APIs with
    #: a published quota; the limit is politeness, not a rule they enforce.
    rate_limit: float = 1.0

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.initialized = True

    @abstractmethod
    def search(self, query: str, limit: int = 20) -> list[DirectVideo]:
        """Return videos matching `query`, best match first."""

    @abstractmethod
    def resolve(self, video_id: str) -> list[DirectSource]:
        """Return playable renditions for one video, highest quality first."""

    def _get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 20)
        response = self.session.get(url, **kwargs)
        response.raise_for_status()
        return response


_DURATION_UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text: str | None) -> int | None:
    """Turn any of ``30:30``, ``1:02:03``, ``37m``, ``12 min`` into seconds.

    The three sites each use a different format and two of them use more than
    one, so this is deliberately permissive. Anything unrecognised returns None
    rather than zero -- a video shown as "0:00" reads as broken.
    """

    if not text:
        return None
    text = text.strip().lower()

    if ":" in text:
        try:
            parts = [int(p) for p in text.split(":")]
        except ValueError:
            return None
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds or None

    total = 0
    for value, unit in re.findall(r"(\d+)\s*(h|m|s)", text):
        total += int(value) * _DURATION_UNITS[unit]
    return total or None


def parse_count(text: str | None) -> int | None:
    """``1.1K`` / ``43K`` / ``115 000`` -> an int."""

    if not text:
        return None
    match = re.search(r"([\d.,\s]+)\s*([kmKM])?", text.strip())
    if not match:
        return None
    number = match.group(1).replace(",", "").replace(" ", "")
    if not number or number == ".":
        return None
    try:
        value = float(number)
    except ValueError:
        return None
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def resolution_from_height(height: int | None) -> str | None:
    """Map a pixel height onto the label the rest of the app uses."""

    if not height:
        return None
    for threshold, label in (
        (2000, "2160p"),
        (1400, "1440p"),
        (1000, "1080p"),
        (700, "720p"),
        (560, "576p"),
        (460, "480p"),
        (340, "360p"),
    ):
        if height >= threshold:
            return label
    return f"{height}p"


def resolution_from_dimensions(dimensions: str | None) -> str | None:
    """``1280x720`` -> ``720p``."""

    if not dimensions:
        return None
    match = re.match(r"\s*(\d+)\s*[x×]\s*(\d+)", dimensions)
    if not match:
        return None
    return resolution_from_height(int(match.group(2)))


__all__ = [
    "BROWSER_HEADERS",
    "DirectScraper",
    "DirectSource",
    "DirectVideo",
    "parse_count",
    "parse_duration",
    "resolution_from_dimensions",
    "resolution_from_height",
]
