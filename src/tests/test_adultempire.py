"""Adult Empire listing and detail parsing.

Fixtures are trimmed copies of the real markup rather than saved pages: the
live pages are ~220KB each and the parts that matter are small. Stdlib only.
"""

import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

if "loguru" not in sys.modules:
    stub = types.ModuleType("loguru")

    class _Logger:
        def __getattr__(self, _):
            return lambda *args, **kwargs: None

    stub.logger = _Logger()
    sys.modules["loguru"] = stub


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ae = _load(
    "adultempire_under_test",
    SRC / "program" / "services" / "recommendations" / "adultempire.py",
)

PASSED, FAILED = [], []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


def _card(pid, slug, title):
    return (
        f'<div class="product-card" id="card{pid}"><div class="boxcover-container">'
        f'<a href="/{pid}/{slug}.html" class="boxcover"></a></div>'
        f'<div class="product-details"><div class="product-details__item-title">'
        f'<a href="/{pid}/{slug}.html" Category="GridViewDVD"\n'
        f'\t\t\t\t\ttitle="{title}" Label="Title">\n{title}\n</a></div></div></div>'
    )


LISTING = (
    '<div class="row grid-list">'
    + _card("700215", "pirates-porn-movies", "Pirates")
    + _card("627116", "island-fever-3-porn-movies", "Island Fever 3")
    # A sex toy and a performer card, which listings mix in.
    + _card("999001", "some-toy-sex-toys", "A Toy")
    + _card("11685", "devin-striker-pornstars", "Devin Striker")
    + "</div>"
)

DETAIL = """
<h1>Pirates <a href="/45/studio/digital-playground-porn-movies.html"
  Category="Item Page" Label="Studio">Digital Playground</a> &nbsp;
  <small>(2005)</small> &nbsp;
  <span class="rating-stars"><span class="rating-stars-avg">4.69</span></span></h1>
<ul class="list-unstyled m-b-2">
<li><small>Length: </small> 2 hrs. 3 mins. </li>
<li><small>Rating: </small> XXX </li>
<li><small>Released:</small> Sep 26 2005 </li>
<li><small>Production Year:</small> 2005 </li>
<li><small>Empire SKU: </small> 700215 </li>
<li><small>Studio: </small><a href="/45/studio/digital-playground-porn-movies.html"
  Category="Item Page" Label="Studio - Details">Digital Playground </a></li>
</ul>
<a name="cast" class="anchor"></a>
<a href="/11685/devin-striker-pornstars.html">Devin Striker</a>
<a href="/56721/carmen-luvana-pornstars.html">Carmen &amp; Luvana</a>
<a href="/11685/devin-striker-pornstars.html">Devin Striker</a>
<a name="alsobought" class="anchor"></a>
<a href="/1412231/pirates-2-stagnettis-revenge-porn-movies.html">Pirates 2</a>
<a href="/1447579/nurses-porn-movies.html">Nurses</a>
<a href="/700215/pirates-porn-movies.html">Pirates</a>
<a href="/11685/devin-striker-pornstars.html">Devin Striker</a>
"""

AGE_GATE = '<html><button id="ageConfirmationButton">Yes, I am 18+</button></html>'


def test_listing_extracts_rank_in_order():
    items = ae.parse_listing(LISTING, "all-time-bestsellers")

    assert [i.rank for i in items] == [1, 2], [i.rank for i in items]
    assert items[0].title == "Pirates" and items[0].product_id == "700215"


def test_listing_skips_toys_and_performers():
    """Listings mix in non-movie cards; only -porn-movies slugs are titles."""

    items = ae.parse_listing(LISTING, "trending")
    ids = {i.product_id for i in items}

    assert "999001" not in ids, "a sex toy was parsed as a title"
    assert "11685" not in ids, "a performer card was parsed as a title"


def test_rank_continues_across_pages():
    page2 = ae.parse_listing(LISTING, "bestsellers", start_rank=49)

    assert [i.rank for i in page2] == [49, 50], [i.rank for i in page2]


def test_detail_extracts_rating_studio_and_year():
    item = ae.RankedTitle("700215", "Pirates", 1, "all-time-bestsellers", "/x.html")
    ae.parse_detail(DETAIL, item)

    assert item.rating == 4.69, item.rating
    assert item.studio == "Digital Playground", item.studio
    assert item.year == 2005, item.year
    assert item.released == "Sep 26 2005", item.released


def test_detail_parses_duration():
    item = ae.RankedTitle("700215", "Pirates", 1, "x", "/x.html")
    ae.parse_detail(DETAIL, item)

    assert item.duration_minutes == 123, item.duration_minutes


def test_cast_deduped_and_unescaped():
    item = ae.RankedTitle("700215", "Pirates", 1, "x", "/x.html")
    ae.parse_detail(DETAIL, item)

    assert item.performers.count("Devin Striker") == 1, item.performers
    assert "Carmen & Luvana" in item.performers, item.performers


def test_also_bought_excludes_self_and_performers():
    item = ae.RankedTitle("700215", "Pirates", 1, "x", "/x.html")
    ae.parse_detail(DETAIL, item)

    assert item.also_bought == ["1412231", "1447579"], item.also_bought


def test_missing_fields_leave_item_usable():
    """A ranked title with no rating is still a ranked title."""

    item = ae.RankedTitle("1", "Unknown", 1, "x", "/x.html")
    ae.parse_detail("<html>nothing useful</html>", item)

    assert item.rating is None and item.studio is None
    assert item.rank == 1 and item.title == "Unknown"


def test_age_gate_raises_rather_than_being_clicked_through():
    """The interstitial accepts the site's T&Cs, which is not ours to accept.

    So the guard must fail loudly rather than fall back to clicking through.
    """

    import urllib.request

    class _Response:
        def read(self):
            return AGE_GATE.encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    original = urllib.request.urlopen
    urllib.request.urlopen = lambda *args, **kwargs: _Response()
    message = None

    try:
        ae.AdultEmpireClient(delay=0)._get("/anything.html")
    except ae.AdultEmpireError as exc:
        message = str(exc)
    finally:
        urllib.request.urlopen = original

    assert message and "interstitial" in message, f"gate not rejected: {message!r}"


def test_user_agent_is_honest_not_a_browser_or_googlebot():
    ua = ae.USER_AGENT.lower()

    assert "riven" in ua, ae.USER_AGENT
    assert "googlebot" not in ua and "bingbot" not in ua, "impersonates a crawler"
    assert "mozilla" not in ua, "poses as a browser, which triggers the terms gate"


def test_unknown_listing_rejected():
    client = ae.AdultEmpireClient(delay=0)
    raised = False

    try:
        client.listing("no-such-listing")
    except ae.AdultEmpireError:
        raised = True

    assert raised


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
