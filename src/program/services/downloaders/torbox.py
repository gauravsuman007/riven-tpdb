"""TorBox downloader.

Ported from the implementation upstream removed in October 2025, which predated
RivenVFS. The substantive change is `unrestrict_link`.

TorBox has no per-link unrestrict endpoint: a playable URL is minted from a
`(torrent_id, file_id)` pair via `torrents/requestdl`, and those URLs are
short-lived. So `download_url` holds a stable synthetic reference,
`torbox://{torrent_id}/{file_id}`, and `unrestrict_link` resolves it to a fresh
CDN URL on demand. That is exactly the contract the VFS wants -- it calls
`unrestrict_link` again whenever a cached URL stops validating.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import requests
from loguru import logger
from pydantic import BaseModel, Field, field_validator

from program.media.item import ProcessedItemType
from program.services.downloaders.models import (
    DebridFile,
    InvalidDebridFileException,
    TorrentContainer,
    TorrentInfo,
    UnrestrictedLink,
    UserInfo,
)
from program.settings import settings_manager
from program.utils import get_version
from program.utils.request import CircuitBreakerOpen, SmartResponse, SmartSession

from . import torbox_maintenance as maintenance
from .shared import DownloaderBase, premium_days_left

# Scheme for the synthetic reference stored as a file's download_url.
LINK_SCHEME = "torbox://"


class TorBoxError(Exception):
    """Raised when the TorBox API returns a failing status."""


class TorBoxQueued(TorBoxError):
    """The torrent was accepted but only queued, so it has no torrent id yet.

    ``createtorrent`` answers in two shapes: an already-known torrent comes back
    with ``data.torrent_id``, while a brand-new one is queued and comes back
    with ``data.queued_id`` and no torrent id at all. Treating the second shape
    as an error makes every genuinely new torrent look like a failure.
    """

    def __init__(self, queued_id: int | str) -> None:
        super().__init__(f"Torrent queued by TorBox (queue id {queued_id})")
        self.queued_id = queued_id


class TorBoxFile(BaseModel):
    """A file inside a TorBox torrent."""

    id: int | None = None
    name: str = ""
    short_name: str | None = None
    size: int = 0


class TorBoxTorrent(BaseModel):
    """A torrent as reported by `torrents/mylist`."""

    id: int | str
    name: str = ""
    hash: str | None = None
    size: int | None = None
    cached: bool | None = None
    progress: float | None = None
    download_state: str | None = None
    seeds: int | None = None
    created_at: datetime | None = None
    # TorBox reports `"files": null` while a torrent is still being fetched --
    # it has no file list until it finishes. A bare `list[TorBoxFile]` rejects
    # that outright, so every attempt to read a torrent's progress raised a
    # validation error, and any caller that was not catching it treated the
    # torrent as unavailable.
    files: list[TorBoxFile] = Field(default_factory=list)

    @field_validator("files", mode="before")
    @classmethod
    def _null_files_are_empty(cls, value: object) -> object:
        """Treat a null file list as an empty one, so `files` is always a list."""

        return [] if value is None else value


class TorBoxAPI:
    """Minimal TorBox client built on SmartSession."""

    BASE_URL = "https://api.torbox.app/v1/api"

    def __init__(self, api_key: str, proxy_url: str | None = None) -> None:
        self.api_key = api_key
        self.proxy_url = proxy_url

        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        # TorBox documents 60 requests per minute.
        self.session = SmartSession(
            base_url=self.BASE_URL,
            rate_limits={"api.torbox.app": {"rate": 1, "capacity": 60}},
            proxies=proxies,
            retries=3,
            backoff_factor=0.3,
        )

        try:
            version = get_version()
        except Exception:
            version = "Unknown"

        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "User-Agent": f"Riven/{version} TorBox/1.0",
            }
        )


class TorBoxDownloader(DownloaderBase):
    """TorBox implementation of `DownloaderBase`."""

    def __init__(self) -> None:
        self.key = "torbox"
        self.settings = settings_manager.settings.downloaders.torbox
        self.api: TorBoxAPI | None = None
        self.initialized = self.validate()

    # ------------------------------------------------------------------ setup

    def validate(self) -> bool:
        """Validate settings, then confirm the account is on a paid plan."""

        if not self._validate_settings():
            return False

        self.api = TorBoxAPI(
            api_key=self.settings.api_key,
            proxy_url=self.PROXY_URL or None,
        )

        return self._validate_premium()

    def _validate_settings(self) -> bool:
        if not self.settings.enabled:
            return False

        if not self.settings.api_key:
            logger.warning("TorBox API key is not set")
            return False

        return True

    def _validate_premium(self) -> bool:
        try:
            user_info = self.get_user_info()
        except Exception as e:
            logger.error(f"Failed to validate TorBox account: {e}")
            return False

        if not user_info:
            logger.error("Failed to get TorBox user info")
            return False

        if user_info.premium_status != "premium":
            logger.error("TorBox paid plan required")
            return False

        if user_info.premium_expires_at:
            logger.info(premium_days_left(user_info.premium_expires_at))

        return True

    # -------------------------------------------------------------- internals

    @staticmethod
    def build_link(torrent_id: int | str, file_id: int) -> str:
        """Build the synthetic reference stored as a file's download_url."""

        return f"{LINK_SCHEME}{torrent_id}/{file_id}"

    @staticmethod
    def parse_link(link: str) -> tuple[str, int] | None:
        """Parse a synthetic reference back into `(torrent_id, file_id)`."""

        if not link or not link.startswith(LINK_SCHEME):
            return None

        remainder = link[len(LINK_SCHEME) :]
        torrent_id, separator, file_id = remainder.rpartition("/")

        if not separator or not torrent_id:
            return None

        try:
            return torrent_id, int(file_id)
        except ValueError:
            return None

    def _maybe_backoff(self, response: SmartResponse) -> None:
        """Promote 429/5xx into the per-domain breaker used by SmartSession."""

        code = response.status_code

        # TorBox reports a full account as HTTP 500 ("You have reached your
        # active download limit of 3."). That is the plan talking, not an
        # outage: tripping the breaker on it made the service look down for a
        # minute at a time and stopped the caller reading the message.
        if "active download limit" in self._detail(response).lower():
            return

        if code == 429 or 500 <= code < 600:
            raise CircuitBreakerOpen("api.torbox.app")

    def _handle_error(self, response: SmartResponse) -> str:
        """Map status codes onto readable messages."""

        messages = {
            400: "[400] Torrent file is not valid",
            401: "[401] Invalid or expired TorBox API key",
            403: "[403] Plan does not permit this action",
            404: "[404] Torrent not found or service unavailable",
            429: "[429] Rate limit exceeded",
            451: "[451] Infringing torrent",
            502: "[502] Bad gateway",
            503: "[503] Service unavailable",
        }

        label = messages.get(
            response.status_code, response.reason or f"HTTP {response.status_code}"
        )
        detail = self._detail(response)

        # TorBox says what is actually wrong ("Download already queued.", "You
        # have reached your active download limit of 3."). The fixed label alone
        # reported every 400 as a bad torrent file, which hid a full account
        # behind an error about the file for as long as nobody looked.
        return f"{label}: {detail}" if detail else label

    @staticmethod
    def _detail(response: SmartResponse) -> str:
        """TorBox's own explanation from an error envelope, or an empty string."""

        body = response.data

        # TorBox is inconsistent about which member carries the sentence: for
        # some errors `detail` does ("Download already queued."), for others
        # `error` does and `detail` is just a code ("NOT_OWNER").
        parts = [
            body.get(key) if isinstance(body, dict) else getattr(body, key, None)
            for key in ("error", "detail")
        ]

        return " - ".join(str(part) for part in parts if part)

    @staticmethod
    def _payload(response: SmartResponse):
        """Return the `data` member of a TorBox envelope, or None."""

        body = response.data

        if isinstance(body, dict):
            return TorBoxDownloader._to_plain(body.get("data"))

        return TorBoxDownloader._to_plain(getattr(body, "data", None))

    @staticmethod
    def _to_plain(value: Any) -> Any:
        """Recursively turn SmartResponse's dot-notation objects into plain data.

        `SmartSession` decodes JSON into nested `SimpleNamespace`s. Converting
        only the top level leaves nested members (notably a torrent's `files`)
        as namespaces, which pydantic rejects outright -- so the conversion has
        to go all the way down.
        """

        if isinstance(value, SimpleNamespace):
            return {key: TorBoxDownloader._to_plain(val) for key, val in vars(value).items()}

        if isinstance(value, dict):
            return {key: TorBoxDownloader._to_plain(val) for key, val in value.items()}

        if isinstance(value, list):
            return [TorBoxDownloader._to_plain(item) for item in value]

        return value

    # ------------------------------------------------------------- operations

    def get_instant_availability(
        self,
        infohash: str,
        item_type: ProcessedItemType,
        **kwargs,
    ) -> TorrentContainer | None:
        """Return the cached files for an infohash, or None if not cached."""

        assert self.api

        try:
            response = self.api.session.get(
                "torrents/checkcached",
                params={
                    "hash": infohash,
                    "format": "object",
                    "list_files": "true",
                },
            )

            if not response.ok:
                logger.debug(
                    f"Failed to check cache for {infohash}: {self._handle_error(response)}"
                )
                return None

            data = self._payload(response)

            if not data:
                logger.debug(f"Torrent {infohash} is not cached on TorBox")
                return None

            # The payload is keyed by infohash; TorBox echoes it lowercased.
            entry = None

            if isinstance(data, dict):
                entry = data.get(infohash) or data.get(infohash.lower())
            else:
                entry = getattr(data, infohash, None) or getattr(
                    data, infohash.lower(), None
                )

            if not entry:
                logger.debug(f"Torrent {infohash} is not cached on TorBox")
                return None

            raw_files = (
                entry.get("files", [])
                if isinstance(entry, dict)
                else getattr(entry, "files", [])
            )

            files = list[DebridFile]()

            for index, raw in enumerate(raw_files):
                name = raw.get("name", "") if isinstance(raw, dict) else getattr(raw, "name", "")
                size = raw.get("size", 0) if isinstance(raw, dict) else getattr(raw, "size", 0)

                # Use TorBox's own file id, never the position: `checkcached`
                # does not return files in id order (a .nfo at id 0 can follow
                # the video at id 1). Numbering them positionally made the
                # container disagree with `get_torrent_info`, which would
                # select the wrong file and hand playback the wrong URL.
                raw_id = (
                    raw.get("id") if isinstance(raw, dict) else getattr(raw, "id", None)
                )
                file_id = raw_id if isinstance(raw_id, int) else index

                try:
                    files.append(
                        DebridFile.create(
                            path=name,
                            filename=name.split("/")[-1],
                            filesize_bytes=size,
                            filetype=item_type,
                            file_id=file_id,
                        )
                    )
                except InvalidDebridFileException as e:
                    logger.debug(f"{infohash}: {e}")
                    continue

            if not files:
                logger.debug(f"No valid files in cached torrent {infohash}")
                return None

            return TorrentContainer(infohash=infohash, files=files)

        except CircuitBreakerOpen as e:
            logger.warning(f"Circuit breaker OPEN for TorBox, skipping {infohash}: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to get instant availability for {infohash}: {e}")
            return None

    def _torrent_file(self, download_url: str) -> bytes | None:
        """Fetch the .torrent an indexer offered, or None if it cannot be had.

        Never raises: a missing file is a reason to fall back to the magnet,
        not to fail the whole download.
        """

        # One already downloaded when the release was picked. On a tracker
        # that caps .torrent downloads per day, fetching it again would spend
        # a second slot on the same file -- or be refused, and fall back to a
        # trackerless magnet that a private torrent never finds peers for.
        from program.services.scrapers.download_quota import ledger

        if kept := ledger.torrent_file(download_url):
            return kept

        try:
            response = requests.get(download_url, timeout=30, allow_redirects=True)
        except Exception as e:
            logger.debug(f"Could not fetch the torrent file: {e}")
            return None

        if not response.ok or not response.content:
            return None

        # An indexer that redirects to a magnet, or answers with an error
        # page, gives back something that is not a torrent. Bencoded torrents
        # always start with "d8:announce" or "d10:created by" -- in any case
        # with "d", which HTML and magnets never do.
        if not response.content.startswith(b"d"):
            return None

        return response.content

    def add_torrent(self, infohash: str, download_url: str | None = None) -> int | str:
        """Add a torrent and return its TorBox id.

        Prefers the actual .torrent file when the indexer gave us a link to
        one, because a bare magnet carries NO trackers. TorBox then has only
        the DHT to find peers with, and a swarm that announces solely to its
        own tracker has no DHT presence -- so the torrent sits at "stalled (no
        seeds)" forever while the indexer cheerfully reports 19 seeders. That
        is not hypothetical: it is what happened to a PornoLab release here,
        and uploading the same torrent file instead had it downloading from
        three seeders at 4.3 MB/s within minutes.
        """

        assert self.api

        # A new download is about to need a slot: clear out what is only
        # holding one.
        self.maintain()

        payload = None

        if download_url:
            payload = self._torrent_file(download_url)

        if payload is not None:
            response = self.api.session.post(
                "torrents/createtorrent",
                files={"file": ("release.torrent", payload, "application/x-bittorrent")},
            )
        else:
            response = self.api.session.post(
                "torrents/createtorrent",
                data={"magnet": f"magnet:?xt=urn:btih:{infohash}".lower()},
            )

        self._maybe_backoff(response)

        if not response.ok:
            # Added on an earlier pass and still waiting in TorBox's queue.
            # Every retry is refused with this, so treating it as a failure
            # meant a queued torrent was never started and never reported.
            if (
                response.status_code == 400
                and "already queued" in self._detail(response).lower()
            ):
                queued_id = self._find_queued_id(infohash)

                if queued_id is not None:
                    return self._promote_queued(queued_id, infohash)

            raise TorBoxError(self._handle_error(response))

        data = self._payload(response)

        torrent_id = (
            data.get("torrent_id")
            if isinstance(data, dict)
            else getattr(data, "torrent_id", None)
        )

        if not torrent_id:
            queued_id = (
                data.get("queued_id")
                if isinstance(data, dict)
                else getattr(data, "queued_id", None)
            )

            if queued_id:
                # Promote it immediately rather than leaving it sitting there.
                #
                # MEASURED, not assumed: this account had 202 torrents in the
                # queued list, the oldest five days old, none of which had
                # ever reached the download list. TorBox does not drain that
                # queue on its own while slots are busy or the account is in
                # cooldown, and nothing here used to touch it -- the queue id
                # was raised, caught, and discarded -- so a request could be
                # accepted and then silently never download at all.
                return self._promote_queued(queued_id, infohash)

            raise TorBoxError("No torrent ID returned by TorBox")

        return str(torrent_id)

    def _promote_queued(self, queued_id: int | str, infohash: str) -> int | str:
        """Start a queued torrent and return its id, or report it as queued."""

        if self._start_queued(queued_id):
            started = self._find_torrent_id(infohash)

            if started is not None:
                return started

        # Still queued: the caller's existing handling is right, it just
        # should not be the FIRST thing tried.
        raise TorBoxQueued(queued_id)

    def _start_queued(self, queued_id: int | str) -> bool:
        """Ask TorBox to promote a queued torrent into a real download.

        Returns whether it was accepted. Never raises: this runs inside
        `add_torrent`'s queued path, where the fallback (report it as queued)
        is already a correct outcome -- a failure here should cost the
        promotion, not the add.

        The usual reason it is refused is the plan's cap on ACTIVE downloads
        (3 here). A torrent that has finished still counts while it seeds, and
        nothing here wants it to, so one is stopped to make room and the start
        is tried once more.
        """

        assert self.api

        freed = False

        for _ in range(2):
            try:
                response = self.api.session.post(
                    "queued/controlqueued",
                    json={
                        "queued_id": queued_id,
                        "operation": "start",
                        "type": "torrent",
                    },
                )

                self._maybe_backoff(response)

                if response.ok:
                    return True

                reason = self._handle_error(response)

                if not freed and "active download limit" in reason.lower():
                    freed = True

                    if self._free_active_slots():
                        continue

                logger.warning(
                    f"TorBox would not start queued download {queued_id}: {reason}"
                )
                return False
            except Exception as exc:
                logger.warning(
                    f"Could not start queued TorBox download {queued_id}: {exc}"
                )
                return False

        return False

    def _free_active_slots(self) -> int:
        """Stop seeding torrents that have finished; returns how many.

        TorBox counts a finished torrent that is still seeding against the
        active-download cap. Stopping it keeps every file (playback is
        unaffected) and gives the slot back. Nothing is deleted here: a torrent
        that is merely stuck is not provably dead, and a deletion cannot be
        taken back.
        """

        assert self.api

        stopped = 0

        try:
            response = self.api.session.get(
                "torrents/mylist", params={"bypass_cache": "true"}
            )

            self._maybe_backoff(response)

            data = self._payload(response) if response.ok else None

            for entry in data if isinstance(data, list) else []:
                plain = self._to_plain(entry)

                if not (plain.get("active") and plain.get("download_finished")):
                    continue

                control = self.api.session.post(
                    "torrents/controltorrent",
                    json={"torrent_id": plain.get("id"), "operation": "stop_seeding"},
                )

                if control.ok:
                    stopped += 1
        except Exception as exc:
            logger.warning(f"Could not free TorBox download slots: {exc}")

        if stopped:
            logger.info(
                f"TorBox is at its active download limit; stopped seeding {stopped} "
                f"finished torrent(s) to make room (files kept)."
            )

            return stopped

        return self._pause_stalled_for_room()

    # ------------------------------------------------------------ maintenance

    MAINTENANCE_EVERY = 120.0  # seconds between passes unless a slot is needed

    def _maintenance_state(self) -> maintenance.State:
        if getattr(self, "_maint_state", None) is None:
            from program.utils import data_dir_path

            self._maint_state = maintenance.State.load(
                data_dir_path / "torbox_maintenance.json"
            )

        return self._maint_state

    def _plain_torrents(self) -> list[dict[str, Any]] | None:
        assert self.api

        response = self.api.session.get(
            "torrents/mylist", params={"bypass_cache": "true"}
        )

        self._maybe_backoff(response)

        if not response.ok:
            return None

        data = self._payload(response)

        return [self._to_plain(t) for t in data] if isinstance(data, list) else None

    def _control(self, torrent_id: int | str, operation: str) -> tuple[bool, str]:
        """One ``controltorrent`` call; never raises. Returns (ok, why not)."""

        assert self.api

        try:
            response = self.api.session.post(
                "torrents/controltorrent",
                json={"torrent_id": torrent_id, "operation": operation},
            )

            if response.ok:
                return True, ""

            return False, self._handle_error(response)
        except Exception as exc:
            return False, str(exc)

    def maintain(self, need_slot: bool = False) -> None:
        """Delete dead torrents, stop long-stalled ones, resume paused ones.

        Runs when a download is requested and when one is polled, never on a
        timer of its own. Throttled, because the poll path is frequent; a
        caller that needs a slot right now skips the throttle. Never raises:
        housekeeping must not be what fails a download.
        """

        now_mono = time.monotonic()

        if not need_slot and now_mono - getattr(self, "_maint_at", -1e9) < self.MAINTENANCE_EVERY:
            return

        self._maint_at = now_mono

        try:
            torrents = self._plain_torrents()

            if torrents is None:
                return

            now = datetime.now(timezone.utc)
            state = self._maintenance_state()

            state.observe(torrents, now)
            decided = maintenance.plan(torrents, state, now)

            for torrent in decided.delete:
                ok, why = self._control(torrent["id"], "delete")

                logger.log(
                    "DEBRID",
                    f"TorBox: removed '{str(torrent.get('name'))[:60]}' -- its metadata "
                    f"never resolved in {int(maintenance.UNRESOLVED_AFTER.total_seconds() // 60)} minutes"
                    if ok
                    else f"TorBox: could not remove {torrent.get('hash')}: {why}",
                )

            for torrent in decided.stop:
                ok, why = self._control(torrent["id"], "stop_seeding")

                state.paused.pop(str(torrent.get("hash")).lower(), None)

                logger.log(
                    "DEBRID",
                    f"TorBox: stopped '{str(torrent.get('name'))[:60]}' -- stalled for "
                    f"{int(maintenance.STOP_AFTER.total_seconds() // 3600)}h+ (files kept)"
                    if ok
                    else f"TorBox: could not stop {torrent.get('hash')}: {why}",
                )

            if not need_slot and state.paused and not maintenance.in_flight(torrents):
                self._resume_paused(torrents, state)

            state.save()
        except Exception as exc:
            logger.warning(f"TorBox maintenance skipped: {exc}")

    def _resume_paused(
        self, torrents: list[dict[str, Any]], state: maintenance.State
    ) -> None:
        """Bring back what was stopped to make room, now that the room is free."""

        by_hash = {str(t.get("hash") or "").lower(): t for t in torrents}

        for key in list(state.paused):
            torrent = by_hash.get(key)

            if torrent is None or torrent.get("active"):
                state.paused.pop(key, None)
                continue

            ok, why = self._control(torrent["id"], "resume")

            if ok:
                state.paused.pop(key, None)
                logger.log("DEBRID", f"TorBox: resumed '{str(torrent.get('name'))[:60]}'")
                continue

            if "active download limit" in why.lower():
                return

            attempts = state.paused[key].get("attempts", 0) + 1
            state.paused[key]["attempts"] = attempts

            if attempts >= maintenance.MAX_RESUME_ATTEMPTS:
                state.paused.pop(key, None)
                logger.warning(
                    f"TorBox would not resume '{str(torrent.get('name'))[:60]}' "
                    f"({why}); leaving it stopped. Its files and progress are kept."
                )

    def _pause_stalled_for_room(self) -> int:
        """Stop the longest-stalled torrent, if any has earned it; returns 1 or 0.

        One at a time: a single slot is all the queued download needs, and every
        extra stop is one more torrent to bring back later.
        """

        try:
            torrents = self._plain_torrents() or []
            now = datetime.now(timezone.utc)
            state = self._maintenance_state()
            candidates = maintenance.pause_candidates(torrents, state, now)

            for torrent in candidates:
                ok, _ = self._control(torrent["id"], "stop_seeding")

                if ok:
                    state.paused[str(torrent.get("hash")).lower()] = {
                        "id": torrent["id"],
                        "attempts": 0,
                    }
                    state.save()

                    logger.log(
                        "DEBRID",
                        f"TorBox is full; stopped '{str(torrent.get('name'))[:60]}' "
                        f"(stalled {int(state.stalled_for(torrent, now).total_seconds() // 3600)}h) "
                        f"to make room. It will be resumed when there is room.",
                    )

                    return 1
        except Exception as exc:
            logger.warning(f"Could not stop a stalled TorBox torrent for room: {exc}")

        return 0

    def _find_queued_id(self, infohash: str) -> int | str | None:
        """The queue id TorBox holds for this infohash, if it is queued."""

        assert self.api

        try:
            response = self.api.session.get("queued/getqueued", params={"type": "torrent"})

            self._maybe_backoff(response)

            data = self._payload(response) if response.ok else None

            wanted = infohash.lower()

            for entry in data if isinstance(data, list) else []:
                plain = self._to_plain(entry)

                if str(plain.get("hash") or "").lower() == wanted:
                    return plain.get("id")
        except Exception as exc:
            logger.warning(f"Could not read TorBox's queue for {infohash}: {exc}")

        return None

    def _find_torrent_id(self, infohash: str) -> str | None:
        """The torrent id TorBox gave this infohash, if it now has one.

        Needed because `controlqueued` reports success without returning the
        new id, and the caller's whole contract is to hand one back.
        """

        assert self.api

        try:
            response = self.api.session.get(
                "torrents/mylist", params={"bypass_cache": "true"}
            )

            self._maybe_backoff(response)

            if not response.ok:
                return None

            data = self._payload(response)

            if not isinstance(data, list):
                return None

            wanted = infohash.lower()

            for entry in data:
                plain = self._to_plain(entry)
                if str(plain.get("hash") or "").lower() == wanted:
                    found = plain.get("id")
                    return str(found) if found is not None else None
        except Exception as exc:
            logger.debug(f"Could not resolve a torrent id for {infohash}: {exc}")

        return None

    def select_files(self, torrent_id: int | str, file_ids: list[int]) -> None:
        """No-op: TorBox makes every file in a torrent available."""

        return None

    def get_torrent_info(self, torrent_id: int | str) -> TorrentInfo:
        """Return normalized info for a torrent, with per-file download refs."""

        assert self.api

        if not torrent_id:
            raise TorBoxError("No torrent ID provided")

        # The 10-minute poll of an uncached fetch is the natural moment to bring
        # back anything that was stopped to make room for it.
        self.maintain()

        try:
            response = self.api.session.get(
                "torrents/mylist",
                params={"id": torrent_id, "bypass_cache": "true"},
            )

            self._maybe_backoff(response)

            if not response.ok:
                raise TorBoxError(self._handle_error(response))

            data = self._payload(response)

            if not data:
                raise TorBoxError(f"No torrent info returned for {torrent_id}")

            # `mylist` returns a list when queried without an id.
            if isinstance(data, list):
                if not data:
                    raise TorBoxError(f"No torrent info returned for {torrent_id}")

                data = data[0]

            torrent = TorBoxTorrent.model_validate(self._to_plain(data))

            files = {}

            for index, file in enumerate(torrent.files):
                file_id = file.id if file.id is not None else index

                files[file_id] = {
                    "id": file_id,
                    "path": file.name,
                    "bytes": file.size,
                    "selected": 1,
                    # Resolved lazily by unrestrict_link, since TorBox's real
                    # URLs expire.
                    "download_url": self.build_link(torrent.id, file_id),
                }

            return TorrentInfo(
                id=torrent.id,
                name=torrent.name,
                status=torrent.download_state,
                infohash=torrent.hash,
                bytes=torrent.size,
                progress=torrent.progress,
                seeders=torrent.seeds,
                created_at=torrent.created_at,
                files=files,
            )

        except CircuitBreakerOpen as e:
            logger.warning(
                f"Circuit breaker OPEN for TorBox, cannot get info for {torrent_id}: {e}"
            )
            raise

    def delete_torrent(self, torrent_id: int | str) -> None:
        """Delete a torrent from the TorBox account."""

        assert self.api

        response = self.api.session.post(
            "torrents/controltorrent",
            data={"torrent_id": torrent_id, "operation": "delete"},
        )

        self._maybe_backoff(response)

        if not response.ok:
            raise TorBoxError(self._handle_error(response))

    def unrestrict_link(self, link: str) -> UnrestrictedLink | None:
        """Mint a fresh playable URL for a `torbox://` reference.

        TorBox's download URLs are short-lived, so the stored reference is
        resolved here every time the VFS finds a stale one.
        """

        assert self.api

        parsed = self.parse_link(link)

        if not parsed:
            logger.debug(f"Not a TorBox reference, cannot unrestrict: {link}")
            return None

        torrent_id, file_id = parsed

        try:
            response = self.api.session.get(
                "torrents/requestdl",
                params={
                    "token": self.api.api_key,
                    "torrent_id": torrent_id,
                    "file_id": file_id,
                    "zip_link": "false",
                },
            )

            self._maybe_backoff(response)

            if not response.ok:
                logger.debug(
                    f"Failed to resolve {link}: {self._handle_error(response)}"
                )
                return None

            download_url = self._payload(response)

            if not isinstance(download_url, str) or not download_url:
                logger.debug(f"TorBox returned no download URL for {link}")
                return None

            return UnrestrictedLink(
                download=download_url,
                filename=download_url.split("/")[-1].split("?")[0],
                filesize=0,
            )

        except CircuitBreakerOpen:
            raise
        except Exception as e:
            logger.error(f"Failed to unrestrict TorBox link {link}: {e}")
            return None

    def get_user_info(self) -> UserInfo | None:
        """Return normalized account information."""

        assert self.api

        try:
            response = self.api.session.get("user/me")

            if not response.ok:
                logger.error(
                    f"Failed to get TorBox user info: {self._handle_error(response)}"
                )
                return None

            data = self._payload(response)

            if not data:
                return None

            def field(name, default=None):
                if isinstance(data, dict):
                    return data.get(name, default)
                return getattr(data, name, default)

            expires_at = None
            days_left = None
            raw_expiry = field("premium_expires_at")

            if raw_expiry:
                try:
                    expires_at = datetime.strptime(
                        raw_expiry, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                    days_left = max(
                        0, (expires_at - datetime.now(tz=timezone.utc)).days
                    )
                except Exception as e:
                    logger.debug(f"Failed to parse TorBox expiry {raw_expiry!r}: {e}")

            cooldown_until = None
            raw_cooldown = field("cooldown_until")

            if raw_cooldown:
                try:
                    cooldown_until = datetime.strptime(
                        raw_cooldown, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                except Exception as e:
                    logger.debug(f"Failed to parse TorBox cooldown {raw_cooldown!r}: {e}")

            # TorBox plan 0 is the free tier.
            plan = field("plan", 0) or 0

            return UserInfo(
                service="torbox",
                username=field("username"),
                email=field("email"),
                user_id=field("id", ""),
                premium_status="premium" if plan > 0 else "free",
                premium_expires_at=expires_at,
                premium_days_left=days_left,
                total_downloaded_bytes=field("total_bytes_downloaded"),
                cooldown_until=cooldown_until,
            )

        except CircuitBreakerOpen as e:
            logger.warning(f"Circuit breaker OPEN while getting TorBox user info: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to get TorBox user info: {e}")
            return None
