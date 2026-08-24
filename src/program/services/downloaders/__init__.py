from datetime import datetime, timedelta, timezone
from loguru import logger
from RTN import ParsedData

from program.media.item import (
    Episode,
    MediaItem,
    Movie,
    ProcessedItemType,
    Season,
    Show,
)
from program.media.state import States
from program.media.stream import Stream
from program.media.media_entry import MediaEntry
from program.media.models import ActiveStream, MediaMetadata
from program.services.downloaders.models import (
    DebridFile,
    DownloadedTorrent,
    NoMatchingFilesException,
    NotCachedException,
    TorrentContainer,
    TorrentInfo,
    UserInfo,
)
from program.services.downloaders.shared import (
    DownloaderBase,
    sort_streams_by_quality,
    parse_filename,
)
from program.settings import settings_manager
from program.utils.request import CircuitBreakerOpen
from program.core.runner import MediaItemGenerator, Runner, RunnerResult

from .realdebrid import RealDebridDownloader
from .debridlink import DebridLinkDownloader
from .alldebrid import AllDebridDownloader
from .torbox import TorBoxDownloader, TorBoxQueued


def _format_progress(progress: float) -> str:
    """Render provider progress as a percentage.

    Providers disagree on the scale: TorBox reports a 0-1 fraction, others a
    0-100 percentage. Treat anything above 1 as already-a-percentage so the log
    never claims "8300% done".
    """

    pct = progress * 100 if progress <= 1 else progress

    return f"{max(0.0, min(pct, 100.0)):.0f}%"


def _has_expired(started_at: datetime, max_wait_hours: int) -> bool:
    """True if ``started_at`` is older than ``max_wait_hours``.

    Providers report timestamps with a UTC offset while ``datetime.now()`` is
    naive, and comparing the two raises TypeError -- so normalise both to naive
    UTC first. An unreadable timestamp is treated as "not expired": losing a
    torrent that is still downloading is worse than waiting one more cycle.
    """

    try:
        if started_at.tzinfo is not None:
            started_at = started_at.astimezone(timezone.utc).replace(tzinfo=None)
            now = datetime.utcnow()
        else:
            now = datetime.now()

        return (now - started_at) > timedelta(hours=max_wait_hours)
    except (AttributeError, TypeError, ValueError, OverflowError):
        # AttributeError covers a provider handing back a string or None where
        # a datetime was expected -- that must not take the downloader down.
        return False


class Downloader(Runner[None, DownloaderBase]):
    # How many of the top-ranked uncached streams to offer the provider each
    # pass. One is too few: a single rejected torrent would strand the item.
    UNCACHED_STREAMS_PER_PASS = 3

    # How many candidate releases to work through in a single pass before
    # handing the item back to the queue. The old value was 3 and, worse, it
    # only yielded instead of stopping -- so a fourth candidate was tried
    # anyway and each one after it yielded again, re-queuing the same item
    # repeatedly. An item with a dozen candidates deserves to reach them.
    MAX_STREAMS_PER_PASS = 10

    def __init__(self):
        super().__init__()

        self.initialized = False
        self.services = {
            RealDebridDownloader: RealDebridDownloader(),
            DebridLinkDownloader: DebridLinkDownloader(),
            AllDebridDownloader: AllDebridDownloader(),
            TorBoxDownloader: TorBoxDownloader(),
        }

        # Get all initialized services instead of just the first one
        self.initialized_services = [
            service for service in self.services.values() if service.initialized
        ]

        # Keep backward compatibility - primary service is the first initialized one
        self.service = (
            self.initialized_services[0] if self.initialized_services else None
        )

        self.initialized = self.validate()

        # Track per-service cooldowns when circuit breaker is open
        self._service_cooldowns = dict[str, datetime]()
        self.subtitles_enabled = (
            settings_manager.settings.post_processing.subtitle.enabled
        )

        # (item id, infohash) -> when we first asked a provider to fetch it.
        self._uncached_since = dict[tuple[int | None, str], datetime]()

        downloader_settings = settings_manager.settings.downloaders
        self.download_uncached = downloader_settings.download_uncached
        self.uncached_poll_minutes = downloader_settings.uncached_poll_minutes
        self.uncached_max_wait_hours = downloader_settings.uncached_max_wait_hours

    def _request_uncached(
        self,
        item: MediaItem,
        streams: list[Stream],
    ) -> datetime | None:
        """Ask a provider to fetch the best uncached stream for ``item``.

        Upstream Riven is cached-only: a torrent the provider does not already
        hold is discarded. Adult releases are rarely in any provider's cache,
        so this asks the provider to fetch one instead and reschedules the item
        to be re-checked once it has had time to finish.

        Returns when to look again, or None if the caller should give up. No
        state is persisted on the item: the provider itself is the record of
        what has been requested, and re-adding the same infohash returns the
        existing torrent rather than starting a second one.
        """

        # More than the single best stream: a provider that rejects one
        # torrent (transiently or otherwise) should not strand the item.
        candidates = streams[: self.UNCACHED_STREAMS_PER_PASS]
        all_past_deadline = True

        for stream in candidates:
            started = self._uncached_wait_started(item, stream)

            if _has_expired(started, self.uncached_max_wait_hours):
                continue

            # At least one stream is still inside its budget, so whatever
            # happens below this is not a terminal failure.
            all_past_deadline = False

            for service in self.initialized_services:
                torrent_id = None

                try:
                    torrent_id = service.add_torrent(stream.infohash)
                except TorBoxQueued:
                    # Accepted, just not started yet: it has a queue id rather
                    # than a torrent id. That is a normal first response for a
                    # torrent the provider has never seen, not a failure.
                    pass
                except Exception as e:
                    logger.debug(
                        f"Could not ask {service.key} to fetch {stream.infohash} "
                        f"for {item.log_string}: {e}"
                    )
                    continue

                progress = None

                if torrent_id is not None:
                    try:
                        progress = service.get_torrent_info(torrent_id).progress
                    except Exception as e:
                        # The torrent was accepted; not being able to read
                        # progress back is not a reason to abandon it.
                        logger.debug(
                            f"Queued {stream.infohash} on {service.key} but could "
                            f"not read its progress for {item.log_string}: {e}"
                        )

                logger.log(
                    "DEBRID",
                    f"Waiting on {service.key} to cache '{stream.raw_title}' for "
                    f"{item.log_string}"
                    + (
                        f" ({_format_progress(progress)} done)"
                        if progress is not None
                        else ""
                    )
                    + f"; re-checking in {self.uncached_poll_minutes}m",
                )

                return datetime.now() + timedelta(
                    minutes=self.uncached_poll_minutes
                )

        if all_past_deadline:
            for stream in candidates:
                self._uncached_since.pop((item.id, stream.infohash), None)

            return None

        # Nothing was accepted this pass, but the budget has not run out --
        # provider errors are often transient, so come back rather than
        # blacklisting streams the provider may well take next time.
        logger.debug(
            f"No provider accepted an uncached stream for {item.log_string}; "
            f"retrying in {self.uncached_poll_minutes}m"
        )

        return datetime.now() + timedelta(minutes=self.uncached_poll_minutes)

    def _uncached_wait_started(self, item: MediaItem, stream: Stream) -> datetime:
        """When we first asked a provider to fetch this stream.

        Deliberately not the provider's own ``created_at``: re-adding an
        infohash the account already holds returns the *existing* torrent, and
        for one added months ago (or since expired) that timestamp is long past
        the deadline -- so the very first attempt would give up immediately.

        Held in memory only. A restart restarts the clock, which errs towards
        waiting longer rather than discarding a torrent that is still coming.
        """

        key = (item.id, stream.infohash)

        return self._uncached_since.setdefault(key, datetime.now())

    def validate(self):
        if not self.initialized_services:
            logger.error(
                "No downloader service is initialized. Please initialize a downloader service."
            )
            return False

        logger.info(
            f"Initialized {len(self.initialized_services)} downloader service(s): {', '.join(s.key for s in self.initialized_services)}"
        )

        return True

    def run(
        self,
        item: MediaItem,
    ) -> MediaItemGenerator:
        logger.debug(f"Starting download process for {item.log_string} ({item.id})")


        # Check if all services are in cooldown due to circuit breaker
        now = datetime.now()

        available_services = [
            service
            for service in self.initialized_services
            if service.key not in self._service_cooldowns
            or self._service_cooldowns[service.key] <= now
        ]

        if not available_services:
            # All services are in cooldown, reschedule for the earliest available time
            next_attempt = min(self._service_cooldowns.values())

            logger.warning(
                f"All downloader services in cooldown for {item.log_string} ({item.id}), rescheduling for {next_attempt.strftime('%m/%d/%y %H:%M:%S')}"
            )

            yield RunnerResult(media_items=[item], run_at=next_attempt)
            return

        download_success = False

        # Track if we hit circuit breaker on any service
        hit_circuit_breaker = False

        # Bound before the try so the failure log below cannot raise NameError
        # if sorting throws before the loop starts.
        tried_streams = 0
        uncached_streams = list[Stream]()

        try:
            # Sort streams by resolution and rank (highest first) using simple, fast sorting
            sorted_streams = sort_streams_by_quality(
                item.streams, preferred_hash=item.preferred_stream_hash
            )

            for stream in sorted_streams:
                # Try each available service for this stream before blacklisting
                stream_failed_on_all_services = True
                stream_hit_circuit_breaker = False
                # A stream the provider simply has not cached is not a bad
                # stream -- with download_uncached on we ask the provider to
                # fetch it rather than throwing it away.
                stream_only_uncached = True

                for service in available_services:
                    logger.debug(
                        f"Trying stream {stream.infohash} on {service.key} for {item.log_string}"
                    )

                    download_result: DownloadedTorrent | None = None

                    try:
                        # Validate stream on this specific service
                        container = self.validate_stream_on_service(
                            stream,
                            item,
                            service,
                        )

                        if not container:
                            logger.debug(
                                f"Stream {stream.infohash} not available on {service.key}"
                            )
                            continue

                        # Reached the provider and it had the torrent; anything
                        # that fails from here is a real failure, not a cache miss.
                        stream_only_uncached = False

                        # Try to download using this service
                        download_result = self.download_cached_stream_on_service(
                            stream,
                            container,
                            service,
                        )

                        if self.update_item_attributes(item, download_result, service):
                            logger.log(
                                "DEBRID",
                                f"Downloaded {item.log_string} from '{stream.raw_title}' [{stream.infohash}] using {service.key}",
                            )

                            download_success = True
                            stream_failed_on_all_services = False

                            break
                        else:
                            raise NoMatchingFilesException(
                                f"No valid files found for {item.log_string} ({item.id})"
                            )
                    except CircuitBreakerOpen as e:
                        # This specific service hit circuit breaker, set cooldown and try next service
                        cooldown_duration = timedelta(minutes=1)
                        self._service_cooldowns[service.key] = (
                            datetime.now() + cooldown_duration
                        )
                        logger.warning(
                            f"Circuit breaker OPEN for {service.key}, trying next service for stream {stream.infohash}"
                        )
                        stream_hit_circuit_breaker = True
                        hit_circuit_breaker = True

                        # If this is the only initialized service, don't mark stream as failed
                        # We want to retry this stream after cooldown
                        if len(self.initialized_services) == 1:
                            stream_failed_on_all_services = False
                        continue

                    except Exception as e:
                        logger.debug(
                            f"Stream {stream.infohash} failed on {service.key}: {e}"
                        )

                        if download_result and download_result.id:
                            try:
                                service.delete_torrent(download_result.id)

                                logger.debug(
                                    f"Deleted failed torrent {stream.infohash} for {item.log_string} ({item.id}) on {service.key}."
                                )
                            except Exception as del_e:
                                logger.debug(
                                    f"Failed to delete torrent {stream.infohash} for {item.log_string} ({item.id}) on {service.key}: {del_e}"
                                )
                        continue

                # If stream succeeded on any service, we're done
                if download_success:
                    # Add probed data if required
                    if self.subtitles_enabled:
                        from program.services.media_analysis import (
                            media_analysis_service,
                        )

                        if media_analysis_service.should_submit(item):
                            success = media_analysis_service.run(item)

                            if success:
                                logger.debug(
                                    f"Media analysis completed for {item.log_string}"
                                )
                                break
                            else:
                                logger.error(
                                    f"Failed to analyze media file for {item.log_string}"
                                )
                    else:
                        break

                # Only blacklist if stream genuinely failed on ALL available services
                # Don't blacklist if we hit circuit breaker in single-provider mode
                if stream_failed_on_all_services:
                    if (
                        stream_hit_circuit_breaker
                        and len(self.initialized_services) == 1
                    ):
                        logger.debug(
                            f"Stream {stream.infohash} hit circuit breaker on single provider, will retry after cooldown"
                        )
                    elif stream_only_uncached and self.download_uncached:
                        # Keep it: _request_uncached below may ask the provider
                        # to fetch this exact stream.
                        uncached_streams.append(stream)
                        logger.debug(
                            f"Stream {stream.infohash} is not cached on any service, keeping for uncached fetch"
                        )
                    else:
                        logger.debug(
                            f"Stream {stream.infohash} failed on all {len(available_services)} available service(s), blacklisting"
                        )
                        item.blacklist_stream(stream)

                tried_streams += 1

                # Stop the pass, rather than yielding and carrying on. The item
                # is handed back so the next pass picks up where this one left
                # off, with the streams that failed here now blacklisted.
                if tried_streams >= self.MAX_STREAMS_PER_PASS:
                    logger.debug(
                        f"Tried {tried_streams} streams for {item.log_string} "
                        f"without success; handing back for another pass"
                    )
                    break

        except Exception as e:
            logger.error(
                f"Unexpected error in downloader for {item.log_string} ({item.id}): {e}"
            )

        if not download_success:
            # Check if we hit circuit breaker in single-provider mode
            if hit_circuit_breaker and len(self.initialized_services) == 1:
                # Reschedule for after cooldown instead of failing
                next_attempt = min(self._service_cooldowns.values())

                logger.warning(
                    f"Single provider hit circuit breaker for {item.log_string} ({item.id}), rescheduling for {next_attempt.strftime('%m/%d/%y %H:%M:%S')}"
                )

                yield RunnerResult(media_items=[item], run_at=next_attempt)

                return
            elif uncached_streams:
                next_attempt = self._request_uncached(item, uncached_streams)

                if next_attempt:
                    yield RunnerResult(media_items=[item], run_at=next_attempt)

                    return

                # The provider will not deliver these; fall through to failure.
                for stream in uncached_streams:
                    item.blacklist_stream(stream)

                logger.warning(
                    f"Giving up on {item.log_string} ({item.id}): the provider "
                    f"did not cache any of {len(uncached_streams)} stream(s) in time"
                )
            else:
                # Warning, not debug: this is the terminal outcome for the item.
                # It stays in Scraped with every stream blacklisted and nothing
                # further happens, so at the default INFO level the pipeline
                # would otherwise appear to stall in complete silence.
                logger.warning(
                    f"Failed to download any streams for {item.log_string} ({item.id}): "
                    f"tried {tried_streams} stream(s), none were cached on "
                    f"{', '.join(s.key for s in self.initialized_services)}"
                )
        else:
            # Clear service cooldowns on successful download
            self._service_cooldowns.clear()

            # This item is done waiting on any uncached fetch.
            for key in [k for k in self._uncached_since if k[0] == item.id]:
                self._uncached_since.pop(key, None)

            yield RunnerResult(media_items=[item])

    def validate_stream(
        self,
        stream: Stream,
        item: MediaItem,
    ) -> TorrentContainer | None:
        """
        Validate a single stream by ensuring its files match the item's requirements.
        Uses the primary service for backward compatibility.
        """

        assert self.service, "No primary downloader service initialized."

        return self.validate_stream_on_service(stream, item, self.service)

    def validate_stream_on_service(
        self,
        stream: Stream,
        item: MediaItem,
        service: "DownloaderBase",
        greedy: bool = True,
    ) -> TorrentContainer | None:
        """
        Validate a single stream on a specific service by ensuring its files match the item's requirements.
        """

        if item.type == "mediaitem":
            logger.debug(
                f"Item {item.log_string} has generic type 'mediaitem', cannot validate stream {stream.infohash}."
            )

            return None

        if service.key == "realdebrid":
            container: TorrentContainer | None = service.get_instant_availability(stream.infohash, item.type, greedy=greedy)
        else:
            container: TorrentContainer | None = service.get_instant_availability(stream.infohash, item.type)

        if not container:
            logger.debug(
                f"Stream {stream.infohash} is not cached or valid on {service.key}."
            )
            return None

        if container.files:
            return container

        return None

    def update_item_attributes(
        self,
        item: MediaItem,
        download_result: DownloadedTorrent,
        service: DownloaderBase | None = None,
        processed_episode_ids: set[str] | None = None,
    ) -> bool:
        """Update the item attributes with the downloaded files and active stream."""

        if service is None:
            service = self.service

        try:
            if not download_result.container:
                raise NotCachedException(
                    f"No container found for {item.log_string} ({item.id})"
                )

            episode_cap: int | None = None
            show: Show | None = None

            if isinstance(item, (Show, Season, Episode)):
                show = item.top_parent

                try:
                    method_1 = sum(len(season.episodes) for season in show.seasons)

                    try:
                        method_2 = show.seasons[-1].episodes[-1].number
                    except IndexError:
                        # happens if there's a new season with no episodes yet
                        method_2 = show.seasons[-2].episodes[-1].number

                    episode_cap = max([method_1, method_2])
                except Exception as e:
                    pass

            found = False
            files = download_result.container.files

            # Track episodes we've already processed to avoid duplicates
            if processed_episode_ids is None:
                processed_episode_ids = set[str]()

            for file in files:
                try:
                    assert file.filename

                    file_data = parse_filename(file.filename)
                except Exception as e:
                    continue

                if isinstance(item, (Show, Season, Episode)):
                    if not file_data.episodes:
                        continue
                    elif 0 in file_data.episodes and len(file_data.episodes) == 1:
                        continue
                    elif file_data.seasons and file_data.seasons[0] == 0:
                        continue

                if self.match_file_to_item(
                    item,
                    file_data,
                    file,
                    download_result,
                    show,
                    episode_cap,
                    processed_episode_ids,
                    service,
                ):
                    found = True

            return found
        except Exception as e:
            logger.debug(f"update_item_attributes: exception for item {item.id}: {e}")
            raise

    def match_file_to_item(
        self,
        item: MediaItem,
        file_data: ParsedData,
        file: DebridFile,
        download_result: DownloadedTorrent,
        show: Show | None = None,
        episode_cap: int | None = None,
        processed_episode_ids: set[str] | None = None,
        service: DownloaderBase | None = None,
    ) -> bool:
        """
        Determine whether a parsed file corresponds to the given media item (movie, show, season, or episode) and update the item's attributes when matches are found.

        Checks movie matches for movie items and episode-level matches for shows/seasons/episodes. For each matched episode or movie file, calls _update_attributes to attach filesystem metadata and marks the item.active_stream when appropriate.

        Parameters:
            item (MediaItem): The target media item to match against.
            file_data (ParsedData): Parsed metadata from RTN (item type, season, episode list, etc.).
            file (DebridFile): The debrid file candidate containing filename, download URL, and size.
            download_result (DownloadedTorrent): The download context containing infohash and torrent id.
            show (Show | None): The show object used to resolve absolute episode numbers when matching episodes.
            episode_cap (int, optional): Maximum episode number allowed for matching; episodes greater than this are skipped.
            processed_episode_ids (set[str] | None): Set of episode IDs already processed in this container to avoid duplicate updates.
            service (optional): Service instance used for attribute updates; defaults to the Downloader's primary service.

        Returns:
            bool: `true` if at least one file-to-item match was found and attributes were updated, `false` otherwise.
        """

        if service is None:
            service = self.service

        logger.debug(
            f"match_file_to_item: item={item.id} type={item.type} file='{file.filename}'"
        )

        found = False

        if isinstance(item, Movie) and file_data.type == "movie":
            logger.debug("match_file_to_item: movie match -> updating attributes")

            self._update_attributes(
                item,
                file,
                download_result,
                service,
                file_data,
            )

            return True

        if isinstance(item, (Show, Season, Episode)):
            season_number = file_data.seasons[0] if file_data.seasons else None

            for file_episode in file_data.episodes:
                if episode_cap and file_episode > episode_cap:
                    logger.debug(
                        f"Invalid episode number {file_episode} for {show.log_string if show else 'show?'} Skipping '{file.filename}'"
                    )

                    continue

                assert show

                episode = show.get_absolute_episode(file_episode, season_number)

                if episode is None:
                    logger.debug(
                        f"Episode {file_episode} from file does not match any episode in {show.log_string if show else 'show?'}"
                    )

                    continue

                if episode.filesystem_entry:
                    logger.debug(
                        f"Episode {episode.log_string} already has filesystem_entry; skipping"
                    )

                    continue

                if episode.state not in [
                    States.Completed,
                    States.Symlinked,
                    States.Downloaded,
                ]:
                    # Skip if we've already processed this episode in this container
                    if (
                        processed_episode_ids is not None
                        and str(episode.id) in processed_episode_ids
                    ):
                        continue

                    logger.debug(
                        f"match_file_to_item: updating episode {episode.id} from file '{file.filename}'"
                    )

                    self._update_attributes(
                        episode,
                        file,
                        download_result,
                        service,
                        file_data,
                    )

                    if processed_episode_ids is not None:
                        processed_episode_ids.add(str(episode.id))

                    logger.debug(
                        f"Matched episode {episode.log_string} to file {file.filename}"
                    )

                    found = True

        if found and isinstance(item, (Show, Season)):
            item.active_stream = ActiveStream(
                infohash=download_result.infohash,
                id=download_result.info.id,
            )

        return found

    def download_cached_stream_on_service(
        self,
        stream: Stream,
        container: TorrentContainer,
        service: DownloaderBase,
    ) -> DownloadedTorrent:
        """
        Prepare and return a DownloadedTorrent for a stream using the given service.

        Uses values already present on `container` when available (e.g., `torrent_id`, `torrent_info`); otherwise adds the torrent and/or fetches its info from the service.

        Returns:
            DownloadedTorrent: An object containing the torrent id, torrent info, the stream's infohash, and the (possibly updated) container.
        """

        torrent_id = None

        # Check if we already have a torrent_id from validation (Real-Debrid optimization)
        if container.torrent_id:
            torrent_id = container.torrent_id

            logger.debug(
                f"Reusing torrent_id {torrent_id} from validation for {stream.infohash}"
            )
        else:
            # Only Real-Debrid populates torrent_id while checking availability;
            # every other service reports "cached" without adding the torrent,
            # so add it here. Without this the bare assert below raised an
            # AssertionError with no message, which surfaced as an empty
            # failure reason and made a cached torrent look undownloadable.
            torrent_id = service.add_torrent(stream.infohash)

            logger.debug(
                f"Added torrent {stream.infohash} to {service.key} as {torrent_id}"
            )

        assert torrent_id

        # Check if we already have torrent_info from validation (Real-Debrid optimization)
        if container.torrent_info:
            info = container.torrent_info
            logger.debug(f"Reusing cached torrent_info for {stream.infohash}")
        else:
            # Fallback: fetch info if not cached
            info = service.get_torrent_info(torrent_id)

        # Real-Debrid can fill in each file's download_url while checking
        # availability, because its availability check already fetches the
        # torrent info. Services that check the cache by infohash cannot: at
        # that point the torrent has no id, so its per-file URLs do not exist
        # yet. Backfill them from the info we now hold, otherwise
        # _update_attributes skips creating the MediaEntry (it requires a
        # download_url) and the item never reaches Downloaded -- leaving it to
        # be downloaded over and over.
        if info.files:
            for debrid_file in container.files:
                if debrid_file.download_url or debrid_file.file_id is None:
                    continue

                torrent_file = info.files.get(debrid_file.file_id)

                if torrent_file and torrent_file.download_url:
                    debrid_file.download_url = torrent_file.download_url

        if container.file_ids:
            service.select_files(torrent_id, container.file_ids)

        return DownloadedTorrent(
            id=torrent_id,
            info=info,
            infohash=stream.infohash,
            container=container,
        )

    def _update_attributes(
        self,
        item: Movie | Episode,
        debrid_file: DebridFile,
        download_result: DownloadedTorrent,
        service: DownloaderBase | None = None,
        file_data: ParsedData | None = None,
    ) -> None:
        """
        Update the media item's active stream and filesystem entries using a debrid file from a completed download.

        Sets item.active_stream from the download_result and, if the debrid file exposes a download URL,
        creates a MediaEntry with the original filename, download URL, and provider information.
        Path generation is now handled by RivenVFS when the entry is registered.

        Parameters:
            item (Movie|Episode): The media item to update.
            debrid_file (DebridFile): Debrid file metadata (must include filename and optionally download_url and filesize).
            download_result (DownloadedTorrent): Result of the download containing id and infohash.
            service: Optional debrid service instance; defaults to the downloader's configured service.
            file_data (ParsedData, optional): Parsed filename metadata from RTN to cache in MediaEntry.
        """

        if service is None:
            service = self.service

        if file_data:
            item.active_stream = ActiveStream(
                infohash=download_result.infohash,
                id=download_result.info.id,
            )

        # Create MediaEntry for virtual file if download URL is available
        if debrid_file.download_url:
            from program.services.library_profile_matcher import LibraryProfileMatcher

            # Match library profiles for this item
            matcher = LibraryProfileMatcher()
            library_profiles = matcher.get_matching_profiles(item)

            # Create MediaEntry with original_filename as source of truth
            # Path generation is now handled by RivenVFS during registration
            # Convert parsed file_data to MediaMetadata if available
            media_metadata = None

            if file_data:
                media_metadata = MediaMetadata.from_parsed_data(
                    parsed_data=file_data,
                    filename=debrid_file.filename,
                )

            assert debrid_file.filename
            assert service

            entry = MediaEntry.create_virtual_entry(
                original_filename=debrid_file.filename,
                download_url=debrid_file.download_url,
                provider=service.key,
                provider_download_id=str(download_result.info.id),
                file_size=debrid_file.filesize,
                media_metadata=media_metadata,
            )

            # Populate library profiles
            entry.library_profiles = library_profiles

            # Clear existing entries and add the new one
            item.filesystem_entries.clear()
            item.filesystem_entries.append(entry)

            logger.debug(
                f"Created MediaEntry for {item.log_string} with original_filename={debrid_file.filename}"
            )
            if library_profiles:
                logger.debug(
                    f"Matched library profiles for {item.log_string}: {library_profiles}"
                )

    def get_instant_availability(
        self,
        infohash: str,
        item_type: ProcessedItemType,
    ) -> TorrentContainer | None:
        """
        Retrieve cached availability information for a torrent identified by its infohash and item type.

        Queries the active downloader service for instant availability and returns any matching cached torrent containers.

        Returns:
            list[TorrentContainer]: A list of TorrentContainer objects representing available cached torrents; empty list if none are found.
        """

        assert self.service

        return self.service.get_instant_availability(infohash, item_type)

    def add_torrent(self, infohash: str) -> int | str:
        """Add a torrent by infohash"""

        assert self.service

        return self.service.add_torrent(infohash)

    def get_torrent_info(
        self,
        torrent_id: int | str,
    ) -> TorrentInfo:
        """Get information about a torrent"""

        assert self.service

        return self.service.get_torrent_info(
            torrent_id,
        )

    def select_files(self, torrent_id: int | str, container: list[int]) -> None:
        """Select files from a torrent"""

        assert self.service

        self.service.select_files(torrent_id, container)

    def delete_torrent(self, torrent_id: int | str) -> None:
        """Delete a torrent"""

        assert self.service

        self.service.delete_torrent(torrent_id)

    def get_user_info(self, service: "DownloaderBase") -> UserInfo | None:
        """Get user information"""

        return service.get_user_info()

    def start_manual_download(
        self,
        item: MediaItem,
        stream: Stream,
        service: DownloaderBase,
        file_ids: list[int] | None = None,
    ) -> set[str] | None:
        """
        Validate, download, and match files for a manual stream selection.

        Does NOT manage state or sessions — the caller is responsible for
        setting episode/season/show states and committing.

        Returns:
            set of matched episode ID strings on success, None on failure.
        """

        container = self.validate_stream_on_service(
            stream,
            item,
            service,
            greedy=not bool(file_ids),
        )

        if not container:
            logger.warning(f"Manual download: stream {stream.infohash} not available on {service.key}")
            return None

        if file_ids and container.files:
            container.files = [f for f in container.files if f.file_id in file_ids]

        try:
            result = self.download_cached_stream_on_service(stream, container, service)
        except Exception as e:
            logger.error(f"Manual download: download failed: {e}")
            return None

        if not result:
            logger.warning(f"Manual download: download returned None for {stream.infohash}")
            return None

        processed_ids: set[str] = set()
        if self.update_item_attributes(item, result, service, processed_ids):
            logger.info(f"Manual download: matched {len(processed_ids)} items from '{stream.raw_title}'")
            return processed_ids

        logger.warning(f"Manual download: no files matched for {item.log_string}")
        return None
