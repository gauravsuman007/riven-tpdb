"""Tests for the direct-scrape framework: matching, ranking, and plugin
discovery.

Every site scraper (parsing markup, resolving a video's playable sources) now
lives in the separate `riven-tpdb-scrapers` repo, since none of them are
bundled with this image any more -- see `directscrapers/__init__.py`'s
`_load_all`. What stays here is what genuinely does not belong to any one
site: the shared parsing helpers in `base.py`, the matching and ranking logic
every scraper's results run through, and the plugin-loading mechanism itself.

Every case here corresponds to a defect a live flow check across the library
actually turned up, or to an assumption that would silently degrade results
rather than fail loudly.
"""

import sys

from program.services.directscrapers import DirectScraperService, _merge_ranked
from program.services.directscrapers.base import (
    parse_count,
    parse_duration,
    resolution_from_dimensions,
    resolution_from_height,
)
from program.services.directscrapers.models import DirectSource, DirectVideo
from program.services.directscrapers.ranking import (
    MIN_RELEVANCE,
    MatchTarget,
    best_matches,
    relevance,
    series_name,
)

PASS = FAIL = 0


def check(name, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


print("duration parsing")
check("colon form, mm:ss", parse_duration("30:30") == 1830)
check("colon form, hh:mm:ss", parse_duration("1:02:03") == 3723)
check("suffix form, minutes", parse_duration("37m") == 2220)
check("spaced suffix form", parse_duration("12 min") == 720)
check("combined suffixes", parse_duration("1h 5m") == 3900)
# A card with no duration must not read as a zero-length video.
check("missing duration is unknown, not zero", parse_duration("") is None)
check("unparseable duration is unknown", parse_duration("soon") is None)

print("\nview counts")
check("thousands suffix", parse_count("1.1K") == 1100)
check("bare thousands suffix", parse_count("43K") == 43000)
check("millions suffix", parse_count("2M") == 2000000)
check("plain integer", parse_count("141822") == 141822)
check("missing count is unknown", parse_count(None) is None)

print("\nresolution normalisation")
check("dimensions to label", resolution_from_dimensions("1280x720") == "720p")
check("unicode multiplication sign", resolution_from_dimensions("1920×1080") == "1080p")
check("4K rounds up to 2160p", resolution_from_height(2160) == "2160p")
check("odd heights fall back to the height", resolution_from_height(300) == "300p")
check("missing dimensions stay unknown", resolution_from_dimensions(None) is None)
print("\nrelevance scoring")


def video(title, **kwargs):
    return DirectVideo(site="s", video_id=title, title=title, page_url="", **kwargs)


query = "Deny It All You Want"
# The real thing, as two different sites title it.
check("exact title scores full", relevance(query, video(query)) == 1.0)
check(
    "title with performers appended still scores full",
    relevance(query, video("Deny It All You Want - Vanna Bardot")) == 1.0,
)
check(
    "site prefix does not dilute the match",
    relevance(query, video("PureTaboo-Deny It All You Want")) == 1.0,
)
# The junk this module exists to remove: everything it shares is filler.
for junk in (
    "Oh i want this i want you",
    "You don t want and i insist you cunnilingus",
    "if you didn t want me to touch it...why is it there",
):
    check(f"filler-only match is rejected: {junk[:28]}", relevance(query, video(junk)) < MIN_RELEVANCE, relevance(query, video(junk)))

check(
    "a result sharing nothing scores zero",
    relevance(query, video("Completely Unrelated Scene")) == 0.0,
)
# Filler words are worth a quarter, not nothing: scoring them zero would make
# any title containing "deny" a perfect match for this query.
check(
    "one distinctive word alone is not a perfect match",
    relevance(query, video("Deny")) < 1.0,
    relevance(query, video("Deny")),
)

check(
    "unrelated title with a shared distinctive word is filtered",
    relevance("Brazzers University", video("18 year old university student"))
    < MIN_RELEVANCE,
)
check(
    "a query with nothing to match on accepts everything rather than nothing",
    relevance("The And Of", video("Anything At All")) == 1.0,
)

print("\nweak-title containment (docs/direct-scrape-matching-study.md)")
# A one- or two-word title is the shape a wholly unrelated clip can satisfy by
# accident: the whole title reappears somewhere inside a much longer sentence
# that has nothing to do with the actual scene. Measured live: "Unfolding" and
# "Disciplinary Action" both scored a perfect title-run match against several
# such sentences, none of which named the credited cast.
weak_target = MatchTarget.build(
    "Unfolding", performers=["Cherie DeVille", "Seth Gamble"], studio="Adam & Eve"
)
check(
    "a weak title buried in an unrelated sentence is rejected",
    relevance(
        weak_target,
        video("A Love Story Captured: Beautiful and Passionate Sex Unfolding on Screen"),
    )
    < MIN_RELEVANCE,
)
check(
    "the same weak title, credited performer named, is not rejected",
    relevance(weak_target, video("Unfolding (Cherie DeVille, Seth Gamble)"))
    >= MIN_RELEVANCE,
)
check(
    "an exact match on a weak title has no bloat to penalise",
    relevance(weak_target, video("Unfolding")) == 1.0,
)
check(
    "a short site-prefix does not read as bloat",
    relevance(weak_target, video("PureTaboo-Unfolding")) >= MIN_RELEVANCE,
)
# A custom search has no performer or studio to tell an appended name apart
# from an unrelated sentence, so this gate does not apply there -- a real
# limit of that path, not an inconsistency in this one.
check(
    "a bare string query has nothing to gate the weak title against",
    relevance("Unfolding", video("A Love Story Captured: Beautiful and Passionate Sex Unfolding on Screen"))
    >= MIN_RELEVANCE,
)
check(
    "genre vocabulary alone is not distinctive",
    relevance("Black Anal MILFs", video("Black Anal Cheating Housewife Gets Fucked"))
    < MIN_RELEVANCE,
)

print("\nvolume handling")
# Adult series reuse one name across instalments, so the number is the title.
check(
    "matching volume is rewarded",
    relevance("Daddy Issues 8", video("Step daddy Issues 8 Sc 2")) == 1.0,
)
check(
    "a scene number is not mistaken for a volume",
    relevance("Daddy Issues 8", video("Step daddy Issues 8 Sc 2")) == 1.0,
)
check(
    "a different volume is penalised",
    relevance("Daddy Issues 8", video("Daddy Issues 3"))
    < relevance("Daddy Issues 8", video("Daddy Issues 8")),
)
check(
    "an unnumbered result is not penalised, only unrewarded",
    0 < relevance("Daddy Issues 8", video("Cute blonde works out her daddy issues")) < 1.0,
)

print("\nordering")
short_hd = video("Deny It All You Want a", resolution="1080p", duration=300)
long_sd = video("Deny It All You Want b", resolution="480p", duration=3600)
ordered = best_matches(query, [long_sd, short_hd], 5)
check(
    "higher resolution wins at equal relevance",
    ordered[0].title.endswith("a"),
    [v.title for v in ordered],
)

long_hd = video("Deny It All You Want c", resolution="1080p", duration=3600)
ordered = best_matches(query, [short_hd, long_hd], 5)
check(
    "at equal resolution the longer video wins",
    ordered[0].title.endswith("c"),
    [v.title for v in ordered],
)

# An HD badge is a claim, not a measurement, so it must not outrank a real
# figure -- but it should still beat a result that reported nothing at all.
badge = video("Deny It All You Want d", hd=True, duration=600)
unknown = video("Deny It All You Want e", duration=600)
measured = video("Deny It All You Want f", resolution="480p", duration=600)
ordered = best_matches(query, [unknown, badge, measured], 5)
check(
    "a measured resolution outranks an HD badge, which outranks nothing",
    [v.title[-1] for v in ordered] == ["f", "d", "e"],
    [v.title[-1] for v in ordered],
)

# Scores are bucketed on purpose: 0.83 against 0.79 is not a real difference,
# and letting it decide pushes a 45-minute scene below a 6-minute clip.
near = video("Deny It All You Want Vanna", resolution="720p", duration=2700)
exact_short = video(query, duration=120)
ordered = best_matches(query, [exact_short, near], 5)
check(
    "a marginally better score does not beat a much better video",
    ordered[0].title == near.title,
    [(v.title, v.relevance) for v in ordered],
)

print("\nfiltering and capping")
pool = [video(f"Deny It All You Want {i}", duration=100 + i) for i in range(10)]
check("the per-site cap is honoured", len(best_matches(query, pool, 2)) == 2)
check("junk is dropped entirely", best_matches(query, [video("Oh i want you")], 5) == [])

# The same upload appears repeatedly under different ids; with two slots a
# duplicate costs a genuinely different result.
dupes = [
    DirectVideo(site="s", video_id="1", title="Deny It All You Want", page_url="", duration=600),
    DirectVideo(site="s", video_id="2", title="deny it all you want!", page_url="", duration=300),
]
deduped = best_matches(query, dupes, 5)
check("duplicate uploads collapse to one", len(deduped) == 1, [v.video_id for v in deduped])
check("the longer copy is the one kept", deduped[0].video_id == "1")

print("\nresult merging")
a = [
    DirectVideo(site="a", video_id="1", title="Alpha Male", page_url="", duration=3600),
    DirectVideo(site="a", video_id="2", title="Alpha Male", page_url="", duration=60),
]
b = [DirectVideo(site="b", video_id="1", title="Alpha Male", page_url="", resolution="1080p", duration=600)]
scored = {
    "a": [v.with_relevance(1.0) for v in a],
    "b": [v.with_relevance(1.0) for v in b],
}
merged = _merge_ranked({"a": None, "b": None}, scored)  # type: ignore[dict-item]
check(
    "the merged list is ranked globally, not round-robined",
    [f"{v.site}{v.video_id}" for v in merged] == ["b1", "a1", "a2"],
    [f"{v.site}{v.video_id}" for v in merged],
)
check("every result survives the merge", len(merged) == 3)
check(
    "a site that returned nothing is skipped",
    len(_merge_ranked({"a": None, "b": None}, {"a": scored["a"], "b": []})) == 2,  # type: ignore[dict-item]
)

# A priority site must sort ahead of a non-priority one even when its best
# result scores lower -- the whole point of the tiering, not a side effect of
# it happening to also rank well.
weak_priority = [
    DirectVideo(site="eporner", video_id="1", title="Alpha Male", page_url="", duration=60)
]
strong_other = [
    DirectVideo(
        site="a", video_id="1", title="Alpha Male", page_url="",
        resolution="1080p", duration=3600,
    )
]
tiered = _merge_ranked(
    {"eporner": None, "a": None},  # type: ignore[dict-item]
    {
        "eporner": [v.with_relevance(1.0) for v in weak_priority],
        "a": [v.with_relevance(1.0) for v in strong_other],
    },
)
check(
    "a priority site outranks a higher-quality non-priority site",
    [v.site for v in tiered] == ["eporner", "a"],
    [(v.site, v.relevance, v.resolution) for v in tiered],
)

# Three tiers, each with a video ranked to look better than it should be
# allowed to: tier 2 has by far the best score, tier 1 the best resolution,
# yet tier order still wins outright.
tier0 = [DirectVideo(site="tnaflix", video_id="1", title="x", page_url="", duration=1)]
tier1 = [DirectVideo(site="hqporner", video_id="1", title="x", page_url="", resolution="2160p", duration=1)]
tier2 = [DirectVideo(site="xfreehd", video_id="1", title="x", page_url="", duration=99999)]
three_tiered = _merge_ranked(
    {"xfreehd": None, "hqporner": None, "tnaflix": None},  # type: ignore[dict-item]
    {
        "tnaflix": [v.with_relevance(0.7) for v in tier0],
        "hqporner": [v.with_relevance(0.7) for v in tier1],
        "xfreehd": [v.with_relevance(1.0) for v in tier2],
    },
)
check(
    "three tiers sort in tier order regardless of score or resolution",
    [v.site for v in three_tiered] == ["tnaflix", "hqporner", "xfreehd"],
    [(v.site, v.relevance, v.resolution) for v in three_tiered],
)


print("\nseries extraction")
check(
    "instalment marker splits the series off",
    series_name("Bratty Sis Vol. 10: Trick or Treat") == "Bratty Sis",
    series_name("Bratty Sis Vol. 10: Trick or Treat"),
)
check("a colon alone splits too", series_name("Brazzers: Day One") == "Brazzers")
# Empty rather than the whole title, so callers can tell "no series" from "the
# series is the title" and skip a duplicate query.
check("a title that is not an instalment has no series", series_name("Alpha Male") == "")

print("\nquery ladder")
target = MatchTarget.build(
    "Bratty Sis Vol. 10: Trick or Treat", ["Riley Reid", "Bunny Colby"], "Nubiles"
)
ladder = DirectScraperService.query_ladder(target)
# Measured, not guessed: xfreehd ANDs its terms, so the full punctuated title
# matched nothing there and only the bare series found the scene.
check("the title is tried first", ladder[0] == "Bratty Sis Vol. 10: Trick or Treat")
check(
    "punctuation is stripped and the lead performer added",
    ladder[1] == "Bratty Sis Vol 10 Trick or Treat Riley Reid",
    ladder[1],
)
check("the bare series is tried last", ladder[-1] == "Bratty Sis", ladder[-1])
# The studio is TPDB's network name, which no upload is labelled with; pairing
# it with a performer never surfaced a target and cost a request every time.
check("the studio is not queried", not any("Nubiles" in q for q in ladder))

plain = DirectScraperService.query_ladder(MatchTarget.build("Alpha Male"))
check("a title with no series or cast yields one query", plain == ["Alpha Male"], plain)
check(
    "duplicate phrasings are collapsed",
    len(DirectScraperService.query_ladder(MatchTarget.build("Alpha Male", [], ""))) == 1,
)

print("\nperformer corroboration")
bratty = MatchTarget.build(
    "Bratty Sis Vol. 10: Trick or Treat", ["Riley Reid", "Bunny Colby"], "Nubiles"
)
# The case this exists for: the right series with the right performer under a
# different episode name. Scored on title alone it was 0.4 and thrown away,
# while unrelated Halloween clips saying "trick or treat" outranked it.
check(
    "series plus performer beats the threshold",
    relevance(bratty, video("Bratty Sis And Riley Reid - Do Or Die")) >= MIN_RELEVANCE,
    relevance(bratty, video("Bratty Sis And Riley Reid - Do Or Die")),
)
# A performer on their own is not evidence about *this* title -- these people
# appear in hundreds of scenes, and treating a name as a match flooded the
# results with unrelated clips of the lead actor.
check(
    "a performer with nothing else is not enough",
    relevance(bratty, video("Riley Reid In Hardcore")) < MIN_RELEVANCE,
    relevance(bratty, video("Riley Reid In Hardcore")),
)
check(
    "a first name alone is not a performer match",
    relevance(bratty, video("Bratty riley has fun")) 
    < relevance(bratty, video("Bratty Sis And Riley Reid")),
)
check(
    "the studio name in a title is a small bonus",
    relevance(MatchTarget.build("Alpha Male", [], "Pure Taboo"), video("PureTaboo-Alpha Male"))
    == 1.0,
)

print("\ninstalment numbers")
daddy = MatchTarget.build("Daddy Issues 8", ["Scarlet Skies"], "Diabolic Video")
check(
    "the right instalment scores full",
    relevance(daddy, video("Step daddy Issues 8 Sc 2 With Scarlet Skies")) == 1.0,
)
# A bare "8" is weak evidence on its own: as a full-weight token it let
# "italian daddy leo casanova part 8" out-score the real scene.
check(
    "a coincidental instalment number does not rescue an unrelated clip",
    relevance(daddy, video("Facial cumshots from italian daddy leo casanova part 8"))
    < MIN_RELEVANCE,
    relevance(daddy, video("Facial cumshots from italian daddy leo casanova part 8")),
)
check(
    "a stated different instalment is still penalised",
    relevance(daddy, video("Daddy Issues 3")) < MIN_RELEVANCE,
    relevance(daddy, video("Daddy Issues 3")),
)

print("\nbucket granularity")
# Tenths, not quarters: coarser bands landed an exact match and a same-series
# near-miss together, and the near-miss won on running time.
exact = video("Daddy Issues 8", duration=600)
near = video("Step daddy Issues with someone else", duration=4000)
ordered = best_matches(daddy, [near, exact], 5)
check(
    "an exact match outranks a longer near-miss",
    ordered[0].title == exact.title,
    [(v.title, v.relevance) for v in ordered],
)

print("\nsource defaults")
source = DirectSource(url="https://x/y.mp4", label="HD")
check("sources default to MP4", source.mime_type == "video/mp4")
check("headers default to empty, not shared", source.headers == {})
other = DirectSource(url="https://x/z.mp4", label="SD")
other.headers["Referer"] = "https://x/"
check("each source gets its own headers dict", source.headers == {})

print("\nplugin discovery")

import tempfile
from pathlib import Path

from program.services.directscrapers.plugins import discover_plugins

GOOD_PLUGIN = '''
from program.services.directscrapers.base import DirectScraper
from program.services.directscrapers.models import DirectVideo, DirectSource

class ExampleScraper(DirectScraper):
    key = "example"
    name = "Example"
    base_url = "https://example.test"

    def search(self, query, limit=20):
        return [DirectVideo(site="example", video_id="1", title=query, page_url="https://example.test/1")]

    def resolve(self, video_id):
        return [DirectSource(url="https://example.test/file.mp4", label="HD")]
'''

BROKEN_SYNTAX = "def not valid python(:\n"

NO_SCRAPER_CLASS = "x = 1\n"

DUPLICATE_KEY = GOOD_PLUGIN.replace("ExampleScraper", "AnotherScraper")

with tempfile.TemporaryDirectory() as tmp:
    plugin_dir = Path(tmp)
    (plugin_dir / "good.py").write_text(GOOD_PLUGIN)
    result = discover_plugins(str(plugin_dir))
    check("a well-formed plugin is discovered", "example" in result.plugins)
    check("no errors for a well-formed plugin", result.errors == {})
    scraper = result.plugins["example"].scraper
    check(
        "the plugin's search actually runs",
        scraper.search("test")[0].title == "test",
    )
    check(
        "the plugin's resolve actually runs",
        scraper.resolve("1")[0].url == "https://example.test/file.mp4",
    )
    check("source file is recorded", result.plugins["example"].source_file == "good.py")

with tempfile.TemporaryDirectory() as tmp:
    plugin_dir = Path(tmp)
    (plugin_dir / "broken.py").write_text(BROKEN_SYNTAX)
    result = discover_plugins(str(plugin_dir))
    check("a broken plugin is not registered", result.plugins == {})
    check("a broken plugin's error is reported", "broken.py" in result.errors)

with tempfile.TemporaryDirectory() as tmp:
    plugin_dir = Path(tmp)
    (plugin_dir / "empty.py").write_text(NO_SCRAPER_CLASS)
    result = discover_plugins(str(plugin_dir))
    check(
        "a file with no scraper class is reported, not silently skipped",
        "empty.py" in result.errors,
    )

with tempfile.TemporaryDirectory() as tmp:
    plugin_dir = Path(tmp)
    (plugin_dir / "a.py").write_text(GOOD_PLUGIN)
    (plugin_dir / "b.py").write_text(DUPLICATE_KEY)
    result = discover_plugins(str(plugin_dir))
    check(
        "the first plugin to claim a key wins, the second is an error",
        len(result.plugins) == 1 and "b.py" in result.errors,
    )

check(
    "a missing plugin directory is not an error",
    discover_plugins("/no/such/directory/exists").errors == {},
)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
