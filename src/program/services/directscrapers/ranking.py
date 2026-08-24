"""Deciding which of a site's search results are actually the title asked for.

These sites match on any word, so a search for "Deny It All You Want" comes
back with "Oh i want this i want you" and "You don t want and i insist" ranked
alongside the real scene. Every one of those shares only filler words, which is
the whole shape of the problem: the tokens that make a title identifiable are
never the common ones.

So relevance is measured over *distinctive* tokens only -- what is left after
the release noise and the everyday English words are removed. A result that
matches "want" and "you" but not "deny" scores zero, which is correct: it has
matched nothing that identifies anything.

Ordering, once the junk is gone, is deliberately not "highest score first".
Relevance beyond a point is noise -- 0.83 against 0.79 says nothing real -- so
scores are bucketed and, within a bucket, the longer and higher-quality video
wins. That is what someone is actually choosing between when two results are
both plainly the right scene.
"""

from __future__ import annotations

import re

from program.services.directscrapers.models import DirectVideo
from program.utils.text_matching import extract_volume, normalise, tokenise


#: Everyday words that carry no identifying weight. Distinct from the release
#: noise list in adult_matching, which strips format and encoding terms: these
#: are ordinary English, and they are exactly what junk results match on.
_COMMON = frozenset({
    "i", "you", "your", "yours", "me", "my", "mine", "we", "us", "our",
    "he", "she", "it", "its", "him", "her", "his", "hers", "they", "them",
    "this", "that", "these", "those", "there", "here", "who", "what", "when",
    "is", "am", "are", "was", "were", "be", "been", "being", "do", "does",
    "did", "done", "have", "has", "had", "will", "would", "can", "could",
    "shall", "should", "may", "might", "must", "get", "gets", "got",
    "all", "any", "some", "no", "not", "so", "if", "then", "than", "as",
    "but", "or", "nor", "out", "up", "down", "off", "over", "under",
    "from", "by", "into", "about", "after", "before", "again", "just",
    "very", "too", "more", "most", "much", "many", "one", "two",
    "want", "wants", "wanted", "like", "likes", "love", "loves",
    "let", "lets", "make", "makes", "take", "takes", "go", "goes",
    "big", "hot", "sexy", "best", "free", "full", "video", "porn", "sex",
})

#: What a common word is worth next to a distinctive one. Not zero: dropping
#: filler entirely makes a one-distinctive-token query like "Deny It All You
#: Want" score any title containing "deny" at a perfect 1.0. Not one either,
#: which is the bug this whole module exists to fix.
_COMMON_WEIGHT = 0.25

#: Below this, a result is not the title that was searched for. A real match
#: reliably clears it -- "Deny It All You Want - Vanna Bardot" carries every
#: token -- while a title sharing only filler cannot get near it.
MIN_RELEVANCE = 0.6

_RESOLUTION_ORDER = {
    "2160p": 6,
    "1440p": 5,
    "1080p": 4,
    "720p": 3,
    "576p": 2,
    "480p": 1,
    "360p": 0,
}


def _weighted_tokens(text: str) -> dict[str, float]:
    """Tokens in `text` with how much each says about which title this is.

    Digits survive at full weight: the instalment number is often the only
    thing separating "Daddy Issues 8" from "Daddy Issues". A bare year is
    dropped because it identifies a release rather than a work.
    """

    weights: dict[str, float] = {}
    for token in tokenise(text):
        if re.fullmatch(r"(19|20)\d{2}", token):
            continue
        weights[token] = _COMMON_WEIGHT if token in _COMMON else 1.0
    return weights


def distinctive_tokens(text: str) -> set[str]:
    """The tokens in `text` that carry full identifying weight."""

    return {
        token for token, weight in _weighted_tokens(text).items() if weight == 1.0
    }


def relevance(query: str, video: DirectVideo) -> float:
    """How well `video` matches `query`, from 0 (unrelated) to 1.

    Returns 0 rather than a small number when nothing distinctive matched. A
    near-zero score and "no match at all" are different claims, and only the
    second one should be filtered on.
    """

    wanted = _weighted_tokens(query)
    if not wanted:
        # Nothing to discriminate on -- a query of pure filler. Treat every
        # result as equally plausible rather than silently returning nothing.
        return 1.0

    found = set(_weighted_tokens(video.title))

    # A result that matches only filler has matched nothing that identifies
    # anything, however many filler words it happens to share.
    if not (distinctive_tokens(query) & found):
        if distinctive_tokens(query):
            return 0.0

    total = sum(wanted.values())
    matched = sum(weight for token, weight in wanted.items() if token in found)
    score = matched / total

    # The whole query appearing as one run is much stronger evidence than the
    # same tokens scattered across an unrelated sentence.
    if normalise(query) and normalise(query) in normalise(video.title):
        score = min(1.0, score + 0.25)

    # Adult series reuse one name across many instalments, so a volume
    # disagreement means a different film, not a worse copy of this one.
    wanted_volume = extract_volume(query)
    if wanted_volume is not None:
        # Scraped titles put stray numbers everywhere -- scene numbers, dates,
        # performer counts -- so only an explicit "Vol"/"Part" marker counts.
        found_volume = extract_volume(video.title, trailing_number=False)
        if found_volume == wanted_volume:
            score = min(1.0, score + 0.15)
        elif found_volume is not None:
            score *= 0.5

    return round(min(1.0, score), 3)


def _resolution_rank(video: DirectVideo) -> int:
    if video.resolution:
        return _RESOLUTION_ORDER.get(video.resolution, -1)
    # An HD badge is a claim, not a measurement, so it sorts above unknown but
    # below any real figure rather than being promoted to "1080p".
    return 0 if video.hd else -1


def sort_key(video: DirectVideo) -> tuple:
    """Ordering within one site's filtered results, best first.

    Relevance is bucketed to the nearest quarter on purpose. The difference
    between 0.83 and 0.79 is not a real difference in how well a title matches,
    and letting it decide would push a 40-minute 1080p scene below a 6-minute
    clip on noise.
    """

    bucket = round((video.relevance or 0.0) * 4)
    return (bucket, _resolution_rank(video), video.duration or 0, video.size or 0)


def best_matches(
    query: str, videos: list[DirectVideo], limit: int
) -> list[DirectVideo]:
    """Score, drop the junk and the duplicates, return the best `limit`."""

    scored = [video.with_relevance(relevance(query, video)) for video in videos]
    kept = [video for video in scored if (video.relevance or 0) >= MIN_RELEVANCE]
    kept.sort(key=sort_key, reverse=True)

    # These sites carry the same upload several times over under slightly
    # different ids. With only a couple of slots per site, letting a duplicate
    # take one costs a genuinely different result. Sorted first, so the copy
    # that survives is the longest and highest quality one.
    unique: list[DirectVideo] = []
    seen: set[str] = set()
    for video in kept:
        fingerprint = normalise(video.title)
        if fingerprint and fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(video)
        if len(unique) >= limit:
            break

    return unique
