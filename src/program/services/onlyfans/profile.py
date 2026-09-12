"""Reading a performer's real profile from onlyfans.com.

WHY THIS EXISTS, AND WHY IT IS NOT A SCRAPER
--------------------------------------------

The first attempt at this fetched ``https://onlyfans.com/<handle>`` and read
its Open Graph tags. That can never work: the site is a single-page app and
every handle -- real, fake, banned -- answers 200 with the same application
shell, whose ``og:image`` is the OnlyFans logo and whose ``og:description`` is
the site's own marketing copy. Two real handles returned byte-identical pages.
Adopting those tags would have stamped one logo and one blurb across the whole
index. That approach was removed once this one was measured working.

The data the shell then fetches for itself comes from ``/api2/v2/users/<name>``,
and that endpoint answers a *guest* -- no account, no login -- provided the
request is signed. So this module does what the page's own JavaScript does:

1. ``GET /api2/v2/init`` to be issued a guest ``sess`` cookie.
2. Sign every subsequent request. The signature is
   ``sha1("\\n".join([static_param, time_ms, path, user_id]))``, plus a
   checksum summed from fixed character positions of that digest, formatted
   into a fixed template.

The three constants in step 2 (``static_param``, ``checksum_indexes``,
``checksum_constant``) rotate whenever OnlyFans redeploys, which is why they
are fetched at runtime from a published rules feed rather than hard-coded.
A vendored copy of the rules measured on 2026-09-12 is the fallback, so a feed
that is unreachable costs freshness rather than the whole feature.

MEASURED 2026-09-12: a real per-account avatar, header and bio came back for
every real handle tried, and a handle that does not exist answers 404 rather
than a generic page -- which is the part the HTML shell could not give us, and
the reason this can be trusted to write to the index at all.

Deliberately NOT a `DirectScraper` plugin. The scrapers in
``/riven/onlyfans_scrapers`` each index one archive site's catalogue; this
reads the performer's own profile from the platform itself and is the same for
every site, so it belongs beside the enrichment pass that calls it.
"""

import hashlib
import json
import re
import threading
import time
from typing import Any, Literal

from loguru import logger


#: Published feeds for the signing constants, tried in order. They rotate when
#: OnlyFans redeploys; the vendored copy below is only a floor.
RULES_URLS = (
    "https://raw.githubusercontent.com/DATAHOARDERS/dynamic-rules/main/onlyfans.json",
    "https://raw.githubusercontent.com/deviint/onlyfans-dynamic-rules/main/dynamicRules.json",
)

#: Measured working 2026-09-12. Used when no feed can be reached, so that a
#: GitHub outage degrades to "possibly stale constants" rather than "no
#: profiles at all". Refreshed in memory as soon as a feed answers.
FALLBACK_RULES: dict[str, Any] = {
    "static_param": "u2U2XLXeDx884qvbKCqIaK0EppZtwzne",
    "format": "63708:{}:{:x}:6a7f22a1",
    "checksum_indexes": [
        25, 23, 28, 7, 32, 11, 25, 29, 20, 6, 1, 39, 30, 23, 39, 26,
        1, 27, 9, 19, 20, 12, 37, 8, 38, 16, 4, 16, 22, 36, 33, 0,
    ],
    "checksum_constant": 573,
    "app_token": "33d57ade8c02dbc5a333db99ff9ae26a",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: A guest session is issued a cookie; past this age it is re-issued rather
#: than waiting for the 401 that proves it expired.
SESSION_TTL = 1800

#: Rules are cheap but not free, and they change on the order of weeks.
RULES_TTL = 3600

#: Seconds between profile requests. The enrichment pass is a background job
#: with hours to work in, so there is nothing to gain by going faster and a
#: session to lose by being throttled.
MIN_INTERVAL = 0.5

#: How long to stop asking after a 429. Longer than the gap between enrichment
#: runs would stall the pass entirely; shorter than a few minutes is not a
#: pause at all.
COOLDOWN = 900

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)

#: What `fetch` distinguishes. "missing" is a definitive answer -- the handle
#: is not an account -- and the caller may stamp it and never ask again.
#: "error" means we do not know: rate limited, signing rejected, network. It
#: must NOT be stamped, or one bad afternoon would permanently blank every
#: account the pass happened to reach during it.
Outcome = Literal["ok", "missing", "error"]


class _Client:
    """One guest session, shared across the enrichment pass."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: Any = None
        self._session_at = 0.0
        self._rules: dict[str, Any] = dict(FALLBACK_RULES)
        self._rules_at = 0.0
        # Stable for the life of the process. The header identifies a browser
        # install, so a fresh value on every request looks less like a browser
        # than one value does, not more.
        self._xbc = hashlib.sha1(str(time.time()).encode()).hexdigest()
        self._last = 0.0
        self._min_interval = MIN_INTERVAL
        self._cool_until = 0.0

    # --- session plumbing ---------------------------------------------------

    def _proxies(self) -> dict[str, str] | None:
        try:
            from program.services.vpn import SCRAPING, vpn

            return vpn().proxies_for(SCRAPING) or None
        except Exception:
            return None

    def _refresh_rules(self, session: Any) -> None:
        if time.time() - self._rules_at < RULES_TTL:
            return

        for url in RULES_URLS:
            try:
                response = session.get(url, timeout=15)

                if response.status_code != 200:
                    continue

                rules = json.loads(response.text)

                # A feed missing any of these would silently produce
                # signatures that are wrong in a way only OnlyFans can see.
                if not all(
                    key in rules
                    for key in (
                        "static_param",
                        "format",
                        "checksum_indexes",
                        "checksum_constant",
                        "app_token",
                    )
                ):
                    continue

                self._rules = rules
                self._rules_at = time.time()
                logger.debug(f"OnlyFans: signing rules refreshed from {url}")
                return
            except Exception as exc:
                logger.debug(f"OnlyFans: rules feed {url} failed: {exc}")

        # Keep whatever we have. Deliberately stamped anyway so that an
        # unreachable feed is retried once an hour, not once per account.
        self._rules_at = time.time()

    def _ensure(self) -> Any:
        """The signed-in-as-nobody session, created or renewed as needed."""

        if self._session is not None and time.time() - self._session_at < SESSION_TTL:
            return self._session

        from curl_cffi import requests as curl_requests

        session = curl_requests.Session(
            impersonate="chrome124", proxies=self._proxies(), timeout=20
        )
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://onlyfans.com/",
            }
        )

        self._refresh_rules(session)

        # The response body is irrelevant and is sometimes an error; the point
        # is the Set-Cookie that comes with it.
        try:
            session.get("https://onlyfans.com/api2/v2/init", timeout=20)
        except Exception as exc:
            logger.debug(f"OnlyFans: guest init failed: {exc}")

        self._session = session
        self._session_at = time.time()
        return session

    def _headers(self, path: str) -> dict[str, str]:
        rules = self._rules
        stamp = str(int(time.time() * 1000))
        digest = hashlib.sha1(
            "\n".join([rules["static_param"], stamp, path, "0"]).encode()
        ).hexdigest()
        checksum = (
            sum(ord(digest[index]) for index in rules["checksum_indexes"])
            + rules["checksum_constant"]
        )

        return {
            "app-token": rules["app_token"],
            "sign": rules["format"].format(digest, abs(checksum)),
            "time": stamp,
            # "0" is the guest user id, and it is part of the signed message
            # above -- the two have to agree or the signature is rejected.
            "user-id": "0",
            "x-bc": self._xbc,
        }

    # --- the one thing this class is for ------------------------------------

    def fetch(self, username: str) -> tuple[Outcome, dict[str, Any] | None]:
        username = (username or "").strip().strip("/")

        if not username:
            return "missing", None

        if time.time() < self._cool_until:
            return "error", None

        path = f"/api2/v2/users/{username}"

        with self._lock:
            for attempt in (1, 2):
                # Paced, not parallel. A batch of 200 accounts is up to 800
                # lookups; unthrottled that is a burst against one host, and
                # the cost of being throttled is not this request but the
                # session, which then has to be rebuilt for everyone behind
                # it. The lock already serialises; this sets the floor.
                wait = self._min_interval - (time.time() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()

                try:
                    session = self._ensure()
                    response = session.get(
                        "https://onlyfans.com" + path, headers=self._headers(path)
                    )
                except Exception as exc:
                    logger.debug(f"OnlyFans: profile {username} failed: {exc}")
                    return "error", None

                if response.status_code == 404:
                    return "missing", None

                if response.status_code in (401, 403) and attempt == 1:
                    # Either the cookie expired or the constants rotated
                    # under us. Both are fixed by starting over, and both
                    # look identical from here, so do not try to tell them
                    # apart -- just force a rebuild and ask once more.
                    self._session = None
                    self._rules_at = 0.0
                    continue

                if response.status_code == 429:
                    # Stop the whole batch, not just this account. Continuing
                    # to ask while being told to stop turns a pause into a
                    # ban, and every one of the remaining lookups would fail
                    # anyway -- as "error", so nothing gets written off.
                    self._cool_until = time.time() + COOLDOWN
                    logger.warning(
                        f"OnlyFans: rate limited, pausing profile lookups for "
                        f"{COOLDOWN}s"
                    )
                    return "error", None

                if response.status_code != 200:
                    logger.debug(
                        f"OnlyFans: profile {username} -> {response.status_code}"
                    )
                    return "error", None

                try:
                    payload = response.json()
                except Exception:
                    return "error", None

                if not isinstance(payload, dict) or "username" not in payload:
                    # An error envelope, or the SPA shell served in place of
                    # JSON. Not a definitive "no such account".
                    return "error", None

                return "ok", payload

        return "error", None


_client = _Client()


def _clean(value: Any) -> str | None:
    """Bio text, flattened out of the HTML OnlyFans stores it as."""

    if not isinstance(value, str) or not value.strip():
        return None

    text = _TAG_RE.sub("", _BREAK_RE.sub("\n", value))
    text = text.replace("&amp;", "&").replace("&quot;", '"').replace("&#039;", "'")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def profile(username: str) -> tuple[Outcome, dict[str, Any] | None]:
    """Fetch one profile, reduced to the fields the index stores.

    Returns the outcome alongside the data so the caller can tell "this
    handle is not an account" (stamp it, never ask again) from "we could not
    find out" (leave it for the next pass).
    """

    outcome, payload = _client.fetch(username)

    if outcome != "ok" or payload is None:
        return outcome, None

    name = (payload.get("username") or username).strip()

    return "ok", {
        "of_user_id": str(payload["id"]) if payload.get("id") is not None else None,
        "of_username": name,
        "of_url": f"https://onlyfans.com/{name}",
        "display_name": (payload.get("name") or "").strip() or None,
        # `rawAbout` is the same text without the markup, and is present far
        # more often than not; `about` is the fallback and needs stripping.
        "bio": _clean(payload.get("rawAbout")) or _clean(payload.get("about")),
        "avatar": payload.get("avatar") or None,
        "header": payload.get("header") or None,
        "website": (payload.get("website") or "").strip() or None,
        "location": (payload.get("location") or "").strip() or None,
        "is_verified": bool(payload.get("isVerified")),
        "posts_count": payload.get("postsCount"),
        "photos_count": payload.get("photosCount"),
        "videos_count": payload.get("videosCount"),
        "likes_count": payload.get("favoritedCount"),
        "subscribe_price": payload.get("subscribePrice"),
    }
