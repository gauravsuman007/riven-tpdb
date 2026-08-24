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
from dataclasses import dataclass, field

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

@dataclass(frozen=True, slots=True)
class MatchTarget:
    """Everything known about the title being looked for.

    Scoring against the title alone is not enough. Tube sites rarely carry the
    exact scene under the exact name -- they carry *a* scene from the series
    with the performer in the title -- so "Bratty Sis And Riley Reid" scores
    0.4 against "Bratty Sis Vol. 10: Trick or Treat" and gets thrown away,
    while unrelated Halloween clips that happen to say "trick or treat" score
    higher. The performer is what settles it, and the library already knows it.
    """

    title: str
    performers: tuple[str, ...] = ()
    studio: str = ""
    #: Series name with the instalment stripped: "Bratty Sis Vol. 10: Trick or
    #: Treat" -> "Bratty Sis". Empty when the title states no instalment.
    series: str = field(default="")

    @classmethod
    def build(
        cls,
        title: str,
        performers: list[str] | None = None,
        studio: str | None = None,
    ) -> "MatchTarget":
        return cls(
            title=title or "",
            performers=tuple(p for p in (performers or []) if p),
            studio=studio or "",
            series=series_name(title or ""),
        )


_INSTALMENT = re.compile(r"\b(?:vol(?:ume)?|part|pt)\.?\s*\d{1,3}\b", re.I)


def strip_punctuation(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9]+", " ", text or "")).strip()


def series_name(title: str) -> str:
    """The series a title belongs to, when it names one.

    "Bratty Sis Vol. 10: Trick or Treat" -> "Bratty Sis". Empty when the title
    is not an instalment of anything, so callers can tell "no series" from "the
    series is the whole title" and skip a duplicate query.
    """

    head = _INSTALMENT.split(title, maxsplit=1)
    if len(head) > 1:
        return strip_punctuation(head[0])
    if ":" in title:
        return strip_punctuation(title.split(":", 1)[0])
    return ""


#: What a common word is worth next to a distinctive one. Not zero: dropping
#: filler entirely makes a one-distinctive-token query like "Deny It All You
#: Want" score any title containing "deny" at a perfect 1.0. Not one either,
#: which is the bug this whole module exists to fix.
_COMMON_WEIGHT = 0.25

#: How much of the title a result must carry before a performer match counts
#: as corroboration rather than a coincidence.
_CORROBORATION = 0.3

#: Below this, a result is not the title that was searched for. A real match
#: reliably clears it -- "Deny It All You Want - Vanna Bardot" carries every
#: token -- while a title sharing only filler cannot get near it.
MIN_RELEVANCE = 0.65

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

    A bare year is dropped -- it identifies a release, not a work. Bare digits
    survive but only at filler weight: the instalment number matters, and the
    volume comparison below scores it properly, but as a loose token an "8"
    also matches "italian daddy leo casanova part 8", which is how an unrelated
    clip out-scored the real scene.
    """

    weights: dict[str, float] = {}
    for token in tokenise(text):
        if re.fullmatch(r"(19|20)\d{2}", token):
            continue
        common = token in _COMMON or token.isdigit()
        weights[token] = _COMMON_WEIGHT if common else 1.0
    return weights


def distinctive_tokens(text: str) -> set[str]:
    """The tokens in `text` that carry full identifying weight."""

    return {
        token for token, weight in _weighted_tokens(text).items() if weight == 1.0
    }


def _performer_hits(target: MatchTarget, video: DirectVideo) -> int:
    """How many of the credited performers the video's title names.

    Full names only. A first name on its own ("vanna") is far too common to be
    evidence, and matching on one would readmit exactly the noise this is here
    to remove.
    """

    haystack = normalise(video.title)
    return sum(
        1 for performer in target.performers if normalise(performer) in haystack
    )


def relevance(target: MatchTarget | str, video: DirectVideo) -> float:
    """How well `video` matches `target`, from 0 (unrelated) to 1.

    Returns 0 rather than a small number when nothing identifying matched. A
    near-zero score and "no match at all" are different claims, and only the
    second one should be filtered on.
    """

    if isinstance(target, str):
        target = MatchTarget.build(target)

    wanted = _weighted_tokens(target.title)
    if not wanted:
        # Nothing to discriminate on -- a query of pure filler. Treat every
        # result as equally plausible rather than silently returning nothing.
        return 1.0

    found = set(_weighted_tokens(video.title))
    total = sum(wanted.values())
    matched = sum(weight for token, weight in wanted.items() if token in found)
    score = matched / total

    # The whole title appearing as one run is much stronger evidence than the
    # same tokens scattered across an unrelated sentence.
    if normalise(target.title) and normalise(target.title) in normalise(video.title):
        score = min(1.0, score + 0.25)

    performers = _performer_hits(target, video)
    in_series = bool(
        target.series and normalise(target.series) in normalise(video.title)
    )

    if performers and (in_series or score >= _CORROBORATION):
        # A credited performer *plus* something of the title is what rescues
        # the right series under the wrong episode name -- "Bratty Sis And
        # Riley Reid" against "Bratty Sis Vol. 10: Trick or Treat". Floored, so
        # it survives sharing few title words.
        score = min(1.0, max(score, 0.7) + 0.1 * min(performers, 2))
    elif performers:
        # A performer on their own is not evidence about *this* title. These
        # people appear in hundreds of scenes, and treating a name match as a
        # match flooded the results with unrelated clips of the lead actor.
        score = min(1.0, score + 0.15)
    elif not (distinctive_tokens(target.title) & found):
        # Nothing distinctive and nobody recognisable: filler overlap only.
        return 0.0

    if in_series:
        score = min(1.0, score + 0.1)

    if target.studio and normalise(target.studio) in normalise(video.title):
        score = min(1.0, score + 0.1)

    # Adult series reuse one name across many instalments, so a volume
    # disagreement means a different film, not a worse copy of this one.
    wanted_volume = extract_volume(target.title)
    if wanted_volume is not None:
        found_volume = _found_volume(target, video)
        # Multiplicative, so agreement rewards a result that already matches
        # and cannot rescue one that does not. Added, a coincidental "part 8"
        # was worth as much to an unrelated clip as to the real scene.
        if found_volume == wanted_volume:
            score *= 1.15
        elif found_volume is not None:
            score *= 0.5

    return round(min(1.0, score), 3)


_TRAILING = re.compile(r"\s*(\d{1,3})\s*$")


def _found_volume(target: MatchTarget, video: DirectVideo) -> int | None:
    """The instalment a scraped title refers to, if it can be trusted.

    An explicit "Vol"/"Part" marker always counts. A bare trailing number only
    counts when removing it leaves a string ending in the queried title -- so
    "Daddy Issues 3" states an instalment, while the 2 in "Step daddy Issues 8
    Sc 2" is a scene number and the 8 in "italian daddy leo casanova part 8"
    belongs to something else entirely. Reading either of those as the volume
    threw away an exact match or promoted an unrelated clip.
    """

    explicit = extract_volume(video.title, trailing_number=False)
    if explicit is not None:
        return explicit

    trailing = _TRAILING.search(video.title.strip())
    if not trailing:
        return None

    stem = normalise(video.title.strip()[: trailing.start()])
    wanted_stem = normalise(_INSTALMENT.sub("", target.title).rstrip(" 0123456789"))
    if wanted_stem and stem.endswith(wanted_stem):
        return int(trailing.group(1))
    return None


def _resolution_rank(video: DirectVideo) -> int:
    if video.resolution:
        return _RESOLUTION_ORDER.get(video.resolution, -1)
    # An HD badge is a claim, not a measurement, so it sorts above unknown but
    # below any real figure rather than being promoted to "1080p".
    return 0 if video.hd else -1


def sort_key(video: DirectVideo) -> tuple:
    """Ordering within one site's filtered results, best first.

    Relevance is bucketed into tenths on purpose. The difference between 0.83
    and 0.79 is not a real difference in how well a title matches, and letting
    it decide would push a 40-minute 1080p scene below a 6-minute clip on
    noise. Coarser bands than this went too far the other way: an exact match
    and a same-series near-miss landed together, and the near-miss won on
    length.
    """

    bucket = round((video.relevance or 0.0) * 10)
    return (bucket, _resolution_rank(video), video.duration or 0, video.size or 0)


def best_matches(
    target: MatchTarget | str, videos: list[DirectVideo], limit: int
) -> list[DirectVideo]:
    """Score, drop the junk and the duplicates, return the best `limit`."""

    scored = [video.with_relevance(relevance(target, video)) for video in videos]
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
