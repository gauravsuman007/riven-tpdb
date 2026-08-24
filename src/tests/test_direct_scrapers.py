"""Tests for the direct streaming-site scrapers.

Every case here corresponds to a defect a live flow check across the library
actually turned up, or to an assumption that would silently degrade results
rather than fail loudly. All parsing is exercised against captured markup, so
this stays runnable when the sites are down or have changed.
"""

import sys

from program.services.directscrapers import _interleave
from program.services.directscrapers.base import (
    parse_count,
    parse_duration,
    resolution_from_dimensions,
    resolution_from_height,
)
from program.services.directscrapers.iporntv import _assemble_url, _video_id
from program.services.directscrapers.models import DirectSource, DirectVideo
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

print("\nresult merging")
a = [DirectVideo(site="a", video_id=str(i), title=f"a{i}", page_url="") for i in range(3)]
b = [DirectVideo(site="b", video_id=str(i), title=f"b{i}", page_url="") for i in range(1)]
merged = _interleave({"a": None, "b": None}, {"a": a, "b": b})  # type: ignore[dict-item]
# Concatenating would put every "a" first, so the smaller site never appears
# above the fold.
check(
    "sites are interleaved, not concatenated",
    [v.title for v in merged] == ["a0", "b0", "a1", "a2"],
    [v.title for v in merged],
)
check("every result survives the merge", len(merged) == 4)
check(
    "a site that returned nothing is skipped",
    [v.title for v in _interleave({"a": None, "b": None}, {"a": a, "b": []})]  # type: ignore[dict-item]
    == ["a0", "a1", "a2"],
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
