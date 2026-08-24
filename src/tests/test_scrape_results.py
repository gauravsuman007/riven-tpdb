"""Tests for the release metadata scrapers now carry through to the UI.

Every case here guards a specific way the old `dict[str, str]` contract lost
information, or a way "unknown" could be mistaken for "zero".
"""

import sys
from types import SimpleNamespace

from program.services.scrapers.results import ScrapeResult
from program.services.downloaders.shared import sort_streams_by_quality

PASS = FAIL = 0


def check(name, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


print("ScrapeResult")
bare = ScrapeResult(raw_title="Some Release 1080p")
check("only raw_title is required", bare.raw_title == "Some Release 1080p")
check("seeders default to unknown, not zero", bare.seeders is None)
check("leechers default to unknown, not zero", bare.leechers is None)
check("size defaults to unknown", bare.size is None)
check("indexer defaults to unknown", bare.indexer is None)

full = ScrapeResult(
    raw_title="Alpha Male XXX 1080p",
    seeders=12,
    leechers=3,
    size=2_147_483_648,
    indexer="Pornolab",
)
check("all fields round-trip", (full.seeders, full.leechers, full.size, full.indexer)
      == (12, 3, 2_147_483_648, "Pornolab"))
check("a zero seeder count is preserved as zero", ScrapeResult("x", seeders=0).seeders == 0)

try:
    full.seeders = 5  # type: ignore[misc]
    check("frozen", False, "expected a mutation to raise")
except Exception:
    check("frozen: a scrape result cannot be edited after the fact", True)


print("\nsort_streams_by_quality: dead torrents go last")


def stream(infohash, resolution="1080p", rank=100, seeders=None):
    return SimpleNamespace(
        infohash=infohash, resolution=resolution, rank=rank, seeders=seeders
    )


dead = stream("dead", seeders=0)
alive = stream("alive", seeders=4, rank=1)  # deliberately worse rank
order = [s.infohash for s in sort_streams_by_quality([dead, alive])]
check("a seeded release outranks an unseeded one, whatever its rank",
      order == ["alive", "dead"], order)

unknown = stream("unknown", seeders=None, rank=1)
order = [s.infohash for s in sort_streams_by_quality([dead, unknown])]
check("an unreported seeder count is not treated as zero",
      order == ["unknown", "dead"], order)

# The whole point of the seeder tiebreak is that it must not disturb ordering
# among releases that are all healthy.
hi = stream("hi", resolution="2160p", rank=1, seeders=2)
lo = stream("lo", resolution="720p", rank=999, seeders=2)
order = [s.infohash for s in sort_streams_by_quality([lo, hi])]
check("resolution still wins among seeded releases", order == ["hi", "lo"], order)

order = [s.infohash for s in sort_streams_by_quality([alive, dead], preferred_hash="dead")]
check("an explicit user pick beats the seeder rule",
      order == ["dead", "alive"], order)

all_unknown = [stream("a", rank=1), stream("b", rank=9)]
order = [s.infohash for s in sort_streams_by_quality(all_unknown)]
check("an indexer that reports no seeders at all is ranked normally",
      order == ["b", "a"], order)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
