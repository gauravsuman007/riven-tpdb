"""Tests for the direct streaming-site scrapers.

Every case here corresponds to a defect a live flow check across the library
actually turned up, or to an assumption that would silently degrade results
rather than fail loudly. All parsing is exercised against captured markup, so
this stays runnable when the sites are down or have changed.
"""

import sys

from program.services.directscrapers import _merge_ranked
from program.services.directscrapers.base import (
    parse_count,
    parse_duration,
    resolution_from_dimensions,
    resolution_from_height,
)
from program.services.directscrapers.iporntv import _assemble_url, _video_id
from program.services.directscrapers.models import DirectSource, DirectVideo
from program.services.directscrapers.ranking import (
    MIN_RELEVANCE,
    best_matches,
    relevance,
)
from program.services.directscrapers.upornia import _best_size, _deobfuscate
from program.services.directscrapers.xfreehd import XFreeHDScraper

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

print("\nupornia URL de-obfuscation")
# Captured from /api/videofile.php. The Cyrillic characters are the point:
# they render as Latin ones, so the payload looks like ordinary base64.
homoglyphs = (
    "L2dldF9maWxlLzЕwLzVlYjRlODljYzЕ4ZTgzYTRmNDdjZGViZWU3NWЕ3М2YyZDY0YWU3МGVhZС8z"
    "NzQzМDАwLzМ3NDМzNDkvМzc0МzМ0OS5tcDQvP2Q9МTgzМСZicj0yNjcmdGk9МTc4NzU4NТEzNw~~"
)
decoded = _deobfuscate(homoglyphs)
check("decodes to a get_file path", decoded.startswith("/get_file/10/"), decoded)
check("keeps the query string", "?d=1830" in decoded, decoded)

# The variant that broke resolution on part of the catalogue: a comma stands
# in for the "/" before the query string, which also unpads the base64.
comma_form = (
    "L2dldF9maWxlLzМvNTc4МjcwZWI5ZTg0NTQ4МzВlМTYwNGFlN2QzNDRhМmFiZmUzYzUyМzJlLzЕy"
    "МjkwМDАvМTIyOTkxМi8xМjI5OTЕyLm1wNС8,ZD00NzkmYnI9NDYxJnRpPTЕ3ODc1ODYwNTg~"
)
comma_decoded = _deobfuscate(comma_form)
check("comma variant decodes", comma_decoded.startswith("/get_file/3/"), comma_decoded)
check("comma becomes the path separator", ".mp4/?d=479" in comma_decoded, comma_decoded)

check("garbage decodes to nothing rather than mojibake", _deobfuscate("!!!!") == "")
check("empty input is handled", _deobfuscate("") == "")

print("\nupornia packed file sizes")
packed = "||.mp4|1280x720|1830|325467312|61|30||_tr.mp4|384x216|7|197803|0|0"
# The trailer's 197803 must not win, and neither must the bitrate or duration.
check("largest rendition wins", _best_size(packed) == 325467312)
check("no formats means unknown size", _best_size(None) is None)
check("trailer-only means unknown size", _best_size("|_tr.mp4|384x216|7|1978|0") is None)

print("\niporntv link shapes")
check(
    "numeric download link",
    _video_id("/download/38045331/bubble-butt-stepsis") == "38045331",
)
check(
    "hex video link",
    _video_id("/download/video/69663446b8d3a/brazzers-best") == "69663446b8d3a",
)
check("unrelated link yields nothing", _video_id("/categories/anal/") == "")

print("\niporntv URL assembly")
# The page splits the URL across fragments and joins them in one expression.
page = """
  var i1="https://ax1.porn-cdn.com";var i3="&type=high";var i2="/xxx/?getVideo=true";
  var i4="&vhash=ABC";
  var iurl = i1+i2+i3+i4;
"""
check(
    "fragments are joined in expression order",
    _assemble_url(page)
    == "https://ax1.porn-cdn.com/xxx/?getVideo=true&type=high&vhash=ABC",
    _assemble_url(page),
)
# A partial join would produce a plausible but broken URL, which fails later as
# an opaque 404 rather than as "could not resolve".
check(
    "an expression with an unknown operand is skipped",
    _assemble_url('var a="https://x/";var b = a + missing;') == "",
)
check("a page with no fragments yields nothing", _assemble_url("<html></html>") == "")
check(
    "a joined value that is not a URL is rejected",
    _assemble_url('var a="foo";var b="bar";var c = a + b;') == "",
)

print("\nxfreehd search parsing")
markup = """
<html><body>
  <a class="video-link" href="/video/111/good">
    <span class="video-title-new">A Good One</span>
    <img src="/img/ximgx.png" data-src="https://image.xfreehd.com/real.jpg">
    <span class="duration-new">37m</span>
    <span class="video-views-new">1.1K</span>
  </a>
  <a class="video-link" href="/video/222/locked">
    <span class="video-title-new">Members Only</span>
    <span class="label-private">Private</span>
  </a>
  <a class="video-link" href="/categories/none">no id here</a>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text):
        self.text = text


scraper = XFreeHDScraper()
scraper._get = lambda *a, **k: _FakeResponse(markup)  # type: ignore[method-assign]
parsed = scraper.search("anything")

check("only resolvable cards are returned", len(parsed) == 1, [p.title for p in parsed])
video = parsed[0]
check("id comes from the href", video.video_id == "111")
check("title comes from the title span", video.title == "A Good One")
# The real image is in data-src; `src` is the same placeholder on every card,
# which is what made results render as a wall of identical tiles.
check(
    "thumbnail prefers data-src",
    video.thumbnail == "https://image.xfreehd.com/real.jpg",
    video.thumbnail,
)
check("duration is parsed", video.duration == 2220)
check("views are parsed", video.views == 1100)
# An HD badge is a claim, not a measurement -- it covers 720p through 4K.
check("the HD badge is not reported as a resolution", video.resolution is None)
check("private videos are excluded", all(p.video_id != "222" for p in parsed))

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

print("\nsource defaults")
source = DirectSource(url="https://x/y.mp4", label="HD")
check("sources default to MP4", source.mime_type == "video/mp4")
check("headers default to empty, not shared", source.headers == {})
other = DirectSource(url="https://x/z.mp4", label="SD")
other.headers["Referer"] = "https://x/"
check("each source gets its own headers dict", source.headers == {})

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
