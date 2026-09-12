"""The scraper plugin ABI: what a site scraper is, and how one is loaded.

This is deliberately NOT a feature. It is the contract that add-ons write
their scrapers against, in the same way ``program/addons/contract.py`` is the
contract add-ons themselves are written against -- and it lives in the host
for the same reason that one does.

It used to be ``program.services.directscrapers``, alongside the tube-site
feature built on it. When that feature was extracted to its own add-on the
obvious move was to take all of it, and that was wrong: **two add-ons write
scrapers against this contract** -- riven-addon-tubescraper and
riven-addon-onlyfans -- and neither can own what the other depends on. An
add-on may depend on the host; an add-on must not depend on another add-on,
which could be disabled or removed underneath it.

The alternative considered and rejected was vendoring a copy into each
add-on. It would have put two copies of ``_RoutedSession`` in the tree, and
that class is where the VPN proxy is applied: a divergence between them
would not break anything visible, it would just send one add-on's scraper
traffic out of the wrong address. One copy, one test guarding it.

What is NOT here, because it genuinely is feature-specific: ranking (which of
a site's results actually match the title you asked for), the registry that
merges several sites' results, and the API. Those live in the add-on.
"""

from program.services.scraper_plugins.base import (
    BROWSER_HEADERS,
    DirectScraper,
    parse_count,
    parse_duration,
    resolution_from_dimensions,
    resolution_from_height,
)
from program.services.scraper_plugins.models import DirectSource, DirectVideo
from program.services.scraper_plugins.plugins import discover_plugins

__all__ = [
    "BROWSER_HEADERS",
    "DirectScraper",
    "DirectSource",
    "DirectVideo",
    "discover_plugins",
    "parse_count",
    "parse_duration",
    "resolution_from_dimensions",
    "resolution_from_height",
]
