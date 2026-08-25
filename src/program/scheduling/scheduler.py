"""
Scheduling subsystem for Program.

Encapsulates APScheduler setup, background jobs, and time-based orchestration
for content services and item-specific schedules.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from program.db import db_functions
from program.db.db import db_session, vacuum_and_analyze_index_maintenance
from program.media.item import Episode, MediaItem, Movie, Show
from program.media.studio import Studio
from program.media.state import States
from program.scheduling.models import ScheduledStatus, ScheduledTask
from program.settings import settings_manager
from program.types import Event
from program.utils.logging import log_cleaner, logger
from program.apis.tvdb_api import SeriesRelease
from schemas.tvdb.models.series_airs_days import SeriesAirsDays

if TYPE_CHECKING:
    from program.program import Program
    from program.services.awards.service import AwardsService
    from program.services.recommendations.brochure import BrochureService
    from program.services.recommendations.studios import StudioService
    from program.services.recommendations.enrichment import TpdbEnricher


class ScheduledFunctionConfig(TypedDict, total=False):
    """How one internal periodic function is triggered.

    ``interval`` is the default and covers everything that just needs to
    happen every N seconds. ``cron`` exists for jobs that must land at a
    particular time of day rather than N seconds after the last restart -- a
    weekly job on an interval drifts to whenever the process last came up,
    which for a several-minute crawl is the difference between running at 3am
    and running in the middle of the evening.
    """

    interval: int
    cron: dict[str, object]


class ProgramScheduler:
    """
    Owns the BackgroundScheduler and all scheduling concerns for Program.

    This class keeps scheduling logic out of Program and wires jobs to the
    Program instance via dependency injection.
    """

    def __init__(self, program: "Program") -> None:
        self.program = program
        self.scheduler = BackgroundScheduler()
        # Built on first use: constructing it eagerly would validate TPDB
        # settings even when award collections are switched off.
        self._awards: "AwardsService | None" = None
        self._brochure: "BrochureService | None" = None
        self._studios: "StudioService | None" = None
        self._tpdb_enricher: "TpdbEnricher | None" = None

    def start(self) -> None:
        """Create and start the background scheduler with all jobs registered."""

        self._schedule_services()
        self._schedule_functions()
        # After the periodic jobs are registered, because this only adds a
        # one-off when the weekly studio cron would otherwise leave the
        # directory empty until its first firing.
        self._kickoff_studios_if_empty()
        self.scheduler.start()

    def stop(self) -> None:
        """Stop the background scheduler if running."""

        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _schedule_functions(self) -> None:
        """Register internal periodic functions and maintenance tasks."""

        assert self.scheduler is not None

        scheduled_functions = dict[Callable[..., None], ScheduledFunctionConfig](
            {
                vacuum_and_analyze_index_maintenance: {"interval": 60 * 60 * 24},
            }
        )

        # Add retry_library if enabled (interval > 0)
        retry_interval = settings_manager.settings.retry_interval

        if retry_interval > 0:
            scheduled_functions[self._retry_library] = {"interval": retry_interval}

        # Add log_cleaner if enabled (interval > 0)
        clean_interval = settings_manager.settings.logging.clean_interval

        if clean_interval > 0:
            scheduled_functions[log_cleaner] = {"interval": clean_interval}

        # AVN award collections: the corpus refresh is cheap and weekly, while
        # resolution runs often in small batches because TPDB allows only two
        # requests a second and the backlog is thousands of entries.
        awards = settings_manager.settings.content.awards

        if awards.enabled:
            scheduled_functions[self._sync_awards] = {
                "interval": awards.refresh_interval
            }
            scheduled_functions[self._resolve_awards] = {
                "interval": awards.resolve_interval
            }

        # Adult Empire brochure: listings are cheap and re-read twice a day;
        # enrichment is one rate-limited request per title so it runs often in
        # small batches.
        brochure = settings_manager.settings.content.brochure

        if brochure.enabled:
            scheduled_functions[self._sync_brochure] = {
                "interval": brochure.refresh_interval
            }
            scheduled_functions[self._enrich_brochure] = {
                "interval": brochure.enrich_interval
            }
            scheduled_functions[self._resolve_brochure] = {
                "interval": brochure.resolve_interval
            }

            if brochure.studios_enabled:
                # Weekly and overnight: the directory is one storefront
                # request per studio at a one-second courtesy delay, so it is
                # several minutes of crawling for data that changes about
                # never.
                scheduled_functions[self._sync_studios] = {
                    "cron": {
                        "day_of_week": brochure.studio_sync_day,
                        "hour": brochure.studio_sync_hour,
                        "minute": 0,
                    }
                }
                # Artwork, on the other hand, is a TPDB lookup per studio and
                # is what makes the section look like anything, so it fills in
                # on the ordinary batch cadence rather than waiting a week.
                scheduled_functions[self._enrich_studios] = {
                    "interval": brochure.enrich_interval
                }

        # Add scheduler processing and monitoring
        scheduled_functions[self._process_scheduled_tasks] = {"interval": 60}
        scheduled_functions[self._monitor_ongoing_schedules] = {"interval": 15 * 60}

        for func, config in scheduled_functions.items():
            cron = config.get("cron")

            if cron:
                self.scheduler.add_job(
                    func,
                    "cron",
                    id=f"{func.__name__}",
                    max_instances=1,
                    replace_existing=True,
                    # No `next_run_time` here, unlike the interval jobs. The
                    # whole point of a cron job is that it runs at its hour;
                    # firing a multi-minute overnight crawl immediately on
                    # every restart would defeat it.
                    misfire_grace_time=60 * 60,
                    **cron,
                )

                logger.debug(f"Scheduled {func.__name__} at {cron}.")

                continue

            self.scheduler.add_job(
                func,
                "interval",
                seconds=config["interval"],
                args=config.get("args"),
                id=f"{func.__name__}",
                max_instances=config.get("max_instances", 1),
                replace_existing=True,
                next_run_time=datetime.now(),
                misfire_grace_time=30,
            )

            logger.debug(
                f"Scheduled {func.__name__} to run every {config['interval']} seconds."
            )

    def _schedule_services(self) -> None:
        """Schedule each content service based on its update interval or webhook mode."""

        assert self.scheduler
        assert self.program.services

        for service_instance in self.program.services.content_services:
            service_name = service_instance.__class__.__name__

            # If the service supports webhooks and webhook mode is enabled, run once now
            use_webhook = getattr(
                getattr(service_instance, "settings", object()), "use_webhook", False
            )

            if use_webhook:
                self.scheduler.add_job(
                    self.program.em.submit_job,
                    "date",
                    run_date=datetime.now(),
                    args=[service_instance, self.program],
                    id=f"{service_name}_update_once",
                    replace_existing=True,
                    misfire_grace_time=30,
                )

                logger.debug(
                    f"Scheduled {service_name} to run once (webhook mode enabled)."
                )

                continue

            update_interval = getattr(
                service_instance.settings, "update_interval", False
            )

            if not update_interval:
                continue

            self.scheduler.add_job(
                self.program.em.submit_job,
                "interval",
                seconds=update_interval,
                args=[service_instance, self.program],
                id=f"{service_name}_update",
                max_instances=1,
                replace_existing=True,
                next_run_time=datetime.now(),
                coalesce=False,
            )

            logger.debug(
                f"Scheduled {service_name} to run every {update_interval} seconds."
            )

    def refresh_content_jobs(self) -> None:
        """(Re)register the awards and brochure jobs from current settings.

        Toggling one of these from the API has to take effect without a
        restart, and it has to work in both directions -- registering the jobs
        on enable, and removing them on disable. ``_schedule_functions`` cannot
        be re-run wholesale for this: every job it registers carries
        ``next_run_time=now``, so a settings change would immediately fire the
        vacuum and the library retry as a side effect.
        """

        if self.scheduler is None or not self.scheduler.running:
            return

        awards = settings_manager.settings.content.awards
        brochure = settings_manager.settings.content.brochure

        wanted: dict[Callable[..., None], int] = {}

        if awards.enabled:
            wanted[self._sync_awards] = awards.refresh_interval
            wanted[self._resolve_awards] = awards.resolve_interval

        if brochure.enabled:
            wanted[self._sync_brochure] = brochure.refresh_interval
            wanted[self._enrich_brochure] = brochure.enrich_interval
            wanted[self._resolve_brochure] = brochure.resolve_interval

            if brochure.studios_enabled:
                wanted[self._enrich_studios] = brochure.enrich_interval

        # Cron rather than interval, and therefore kept apart from `wanted`:
        # the studio directory is a several-minute crawl that belongs at its
        # hour, not N seconds after whenever the process last restarted.
        cron_wanted: dict[Callable[..., None], dict[str, object]] = {}

        if brochure.enabled and brochure.studios_enabled:
            cron_wanted[self._sync_studios] = {
                "day_of_week": brochure.studio_sync_day,
                "hour": brochure.studio_sync_hour,
                "minute": 0,
            }

        managed = (
            self._sync_awards,
            self._resolve_awards,
            self._sync_brochure,
            self._enrich_brochure,
            self._resolve_brochure,
            self._enrich_studios,
        )

        for func in managed:
            job_id = func.__name__

            if func in wanted:
                self.scheduler.add_job(
                    func,
                    "interval",
                    seconds=wanted[func],
                    id=job_id,
                    max_instances=1,
                    replace_existing=True,
                    # Deliberately immediate: the user just switched this on
                    # and expects data to start appearing, not to wait out a
                    # twelve-hour interval first.
                    next_run_time=datetime.now(),
                    misfire_grace_time=30,
                )
                logger.debug(f"Scheduled {job_id} every {wanted[func]}s")
            elif self.scheduler.get_job(job_id) is not None:
                self.scheduler.remove_job(job_id)
                logger.debug(f"Removed scheduled job {job_id}")

        for func in (self._sync_studios,):
            job_id = func.__name__

            if func in cron_wanted:
                self.scheduler.add_job(
                    func,
                    "cron",
                    id=job_id,
                    max_instances=1,
                    replace_existing=True,
                    misfire_grace_time=60 * 60,
                    **cron_wanted[func],
                )
                logger.debug(f"Scheduled {job_id} at {cron_wanted[func]}")

                # The interval jobs above start immediately because the user
                # just enabled them and expects to see something. A weekly
                # overnight crawl cannot do that -- but neither can it leave
                # the section empty until Sunday, so it runs once now if there
                # is nothing to show yet. On a later settings save the
                # directory is already populated and nothing is re-crawled.
                self._kickoff_studios_if_empty()
            elif self.scheduler.get_job(job_id) is not None:
                self.scheduler.remove_job(job_id)
                logger.debug(f"Removed scheduled job {job_id}")

        # The services cache their settings at construction, so a toggle has to
        # drop them or the next run would still see the old values.
        self._awards = None
        self._brochure = None
        self._studios = None
        self._tpdb_enricher = None

    def _awards_service(self):
        """The awards service, built lazily so a disabled one costs nothing."""

        from program.services.awards.service import AwardsService

        if self._awards is None:
            self._awards = AwardsService()

        return self._awards

    def _sync_awards(self) -> None:
        """Refresh the AVN corpus into collections."""

        service = self._awards_service()

        if not service.initialized:
            return

        try:
            service.sync_corpus()
        except Exception as exc:
            logger.error(f"AVN corpus sync failed: {exc}")

    def _resolve_awards(self) -> None:
        """Resolve one batch of pending entries, then queue any new winners.

        Requesting is bounded per run and happens only after resolution, so the
        pipeline receives a steady trickle rather than thousands of items the
        first time the corpus lands.
        """

        service = self._awards_service()

        if not service.initialized:
            return

        try:
            matched, _ = service.resolve_batch()

            if matched:
                service.request_matched_winners()
        except Exception as exc:
            logger.error(f"AVN award resolution failed: {exc}")

    def _brochure_service(self):
        from program.services.recommendations.brochure import BrochureService

        if self._brochure is None:
            self._brochure = BrochureService()

        return self._brochure

    def _sync_brochure(self) -> None:
        """Re-read Adult Empire's ranked listings."""

        service = self._brochure_service()

        if not service.initialized:
            return

        try:
            service.sync_listings()
        except Exception as exc:
            logger.error(f"Adult Empire brochure sync failed: {exc}")

    def _enrich_brochure(self) -> None:
        """Fill in ratings and cast, then backfill TPDB for owned titles.

        The TPDB pass runs here rather than on its own timer because it is
        purely additive: brochure titles are already downloadable, so there is
        nothing to hurry.
        """

        service = self._brochure_service()

        if not service.initialized:
            return

        try:
            service.enrich_batch()
        except Exception as exc:
            logger.error(f"Adult Empire enrichment failed: {exc}")

        try:
            from program.services.recommendations.enrichment import TpdbEnricher

            if self._tpdb_enricher is None:
                self._tpdb_enricher = TpdbEnricher()

            if self._tpdb_enricher.initialized:
                self._tpdb_enricher.run()
        except Exception as exc:
            logger.error(f"TPDB backfill failed: {exc}")

    def _kickoff_studios_if_empty(self) -> None:
        """Sync the studio directory once, now, if there is nothing in it.

        The weekly cron cannot cover this on its own. Its whole point is to
        run at its hour rather than on startup, so a fresh install -- or the
        deploy that first introduced the table -- would show an empty studios
        section until the next Sunday. Once the directory has anything in it
        this does nothing, so restarts do not re-crawl.
        """

        brochure = settings_manager.settings.content.brochure

        if not brochure.enabled or not brochure.studios_enabled:
            return

        if self.scheduler is None or not self._studio_directory_is_empty():
            return

        self.scheduler.add_job(
            self._sync_studios,
            "date",
            run_date=datetime.now(),
            id="_sync_studios_once",
            replace_existing=True,
            misfire_grace_time=60,
        )
        logger.debug("Scheduled a one-off studio sync; the directory is empty")

    @staticmethod
    def _studio_directory_is_empty() -> bool:
        """Whether the studio table has nothing in it yet.

        Failure counts as "not empty" on purpose: a database that cannot be
        read is not a reason to kick off a several-minute crawl.
        """

        try:
            with db_session() as session:
                return session.execute(select(Studio).limit(1)).first() is None
        except SQLAlchemyError as exc:
            logger.debug(f"Could not check the studio directory: {exc}")
            return False

    def _studio_service(self):
        """The studio directory service, built lazily like the rest."""

        from program.services.recommendations.studios import StudioService

        if self._studios is None:
            self._studios = StudioService()

        return self._studios

    def _sync_studios(self) -> None:
        """Refresh the Adult Empire studio directory. Weekly, overnight."""

        service = self._studio_service()

        if not service.initialized:
            return

        try:
            service.sync()
        except Exception as exc:
            logger.error(f"Adult Empire studio sync failed: {exc}")

    def _enrich_studios(self) -> None:
        """Attach TPDB logos and descriptions to studios that lack them."""

        service = self._studio_service()

        if not service.initialized:
            return

        try:
            service.enrich_batch()
        except Exception as exc:
            logger.error(f"Adult Empire studio enrichment failed: {exc}")

    def _resolve_brochure(self) -> None:
        """Resolve catalogue entries against TPDB.

        On its own timer rather than folded into ``_enrich_brochure``: that one
        reads Adult Empire and is paced by the storefront's one-request-a-second
        courtesy delay, while this one reads TPDB and is paced by TPDB's rate
        limit. Sharing a timer would make each wait out the other's budget.
        """

        service = self._brochure_service()

        if not service.initialized:
            return

        try:
            service.resolve_batch()
        except Exception as exc:
            logger.error(f"Adult Empire TPDB resolution failed: {exc}")

    def _retry_library(self) -> None:
        """Retry items that failed to download by emitting events into the EM."""

        item_ids = db_functions.retry_library()

        for item_id in item_ids:
            self.program.em.add_event(Event(emitted_by="RetryLibrary", item_id=item_id))

        if item_ids:
            logger.log(
                "PROGRAM",
                f"Successfully retried {len(item_ids)} incomplete items",
            )
        else:
            logger.log("NOT_FOUND", "No items required retrying")

    def _get_pending_scheduled_tasks(self, session: Session) -> Sequence[ScheduledTask]:
        """Return all pending scheduled tasks."""

        try:
            return (
                session.execute(
                    select(ScheduledTask)
                    .where(ScheduledTask.status == ScheduledStatus.Pending)
                    .where(ScheduledTask.scheduled_for <= datetime.now())
                    .order_by(ScheduledTask.scheduled_for.asc())
                )
                .unique()
                .scalars()
                .all()
            )
        except SQLAlchemyError as e:
            logger.error(f"Scheduler DB error: {e}")
            return []

    def _process_scheduled_tasks(self) -> None:
        """
        Process due scheduled tasks by delegating to focused helpers.

        Responsibilities split into:
        - fetching due tasks;
        - loading/merging the target item for a task;
        - handling reindex vs. release tasks;
        - updating task status with consistent error handling.
        """
        try:
            with db_session() as session:
                now = datetime.now()
                due_tasks = self._get_pending_scheduled_tasks(session)
                if not due_tasks:
                    return

                for task in due_tasks:
                    self._process_single_scheduled_task(session, task, now)
        except SQLAlchemyError as e:
            logger.error(f"Scheduler DB error: {e}")

    def _process_single_scheduled_task(
        self,
        session: Session,
        task: ScheduledTask,
        now: datetime,
    ) -> None:
        """
        Process a single ScheduledTask instance.

        Args:
            session: Active SQLAlchemy session.
            task: The scheduled task to process.
            now: Current timestamp used for status updates.
        """
        try:
            item = self._load_item_for_task(session, task)

            if not item:
                self._mark_task_status(
                    session,
                    task,
                    ScheduledStatus.Failed,
                    now,
                )

                logger.debug(
                    f"ScheduledTask {task.id} item {task.item_id} no longer exists"
                )

                return

            if task.task_type in ("reindex_show", "reindex", "reindex_movie"):
                self._run_reindex_for_item(session, item)
            else:
                self._enqueue_item_if_needed(session, item)

            self._mark_task_status(
                session,
                task,
                ScheduledStatus.Completed,
                datetime.now(),
            )
        except Exception as e:
            session.rollback()
            self._mark_task_status(
                session, task, ScheduledStatus.Failed, datetime.now()
            )
            logger.exception(f"Failed processing ScheduledTask {task.id}: {e}")

    def _load_item_for_task(self, session: Session, task: ScheduledTask):
        """
        Load and merge the MediaItem for a scheduled task.

        Returns:
            The merged item or None if missing.
        """

        item = db_functions.get_item_by_id(task.item_id, session=session)

        if not item:
            return None

        return session.merge(item)

    def _run_reindex_for_item(self, session: Session, item: MediaItem) -> None:
        """Run indexer service for an item if available and persist updates."""

        assert self.program.services, "Services not initialized in Program"

        indexer_service = self.program.services.indexer

        updated = next(indexer_service.run(item, log_msg=False), None)

        if updated:
            session.merge(updated.media_items[0])
            session.commit()

            logger.info(f"Reindexed {item.log_string} from scheduler")

    def _enqueue_item_if_needed(self, session: Session, item: MediaItem) -> None:
        """Refresh state and enqueue item to the event manager if not completed."""

        was_completed = item.last_state == States.Completed
        item.store_state()
        session.commit()

        if not was_completed:
            self.program.em.add_event(Event(emitted_by="Scheduler", item_id=item.id))
            logger.info(f"Enqueued {item.log_string} from scheduler")

    def _mark_task_status(
        self,
        session: Session,
        task: ScheduledTask,
        status: ScheduledStatus,
        executed_at: datetime,
    ) -> None:
        """Persist a task status update in a single place."""

        task.status = status
        task.executed_at = executed_at
        session.add(task)
        session.commit()

    def _monitor_ongoing_schedules(self) -> None:
        """
        Ensure schedules exist for upcoming releases and metadata refreshes.

        Decomposed into helpers for clarity:
        - schedule upcoming episodes
        - schedule upcoming movies (known release date)
        - schedule ongoing/unreleased shows (computed next air)
        - schedule unknown-date movies (daily reindex)
        """

        offset_seconds = settings_manager.settings.indexer.schedule_offset_minutes * 60
        now = datetime.now()

        try:
            with db_session() as session:
                self._schedule_upcoming_episodes(session, now, offset_seconds)
                self._schedule_upcoming_movies(session, now, offset_seconds)
                self._schedule_ongoing_shows(session, now)
                self._schedule_unknown_movies(session, now)
        except Exception as e:
            logger.error(f"Monitor ongoing schedules failed: {e}")

    def _has_future_task(
        self,
        session: Session,
        item_id: int,
        task_type: str,
        now: datetime,
    ) -> bool:
        """Return True if a pending future task of this type already exists for item."""

        existing = (
            session.execute(
                select(ScheduledTask)
                .where(ScheduledTask.item_id == item_id)
                .where(ScheduledTask.task_type == task_type)
                .where(ScheduledTask.status == ScheduledStatus.Pending)
                .where(ScheduledTask.scheduled_for >= now)
                .limit(1)
            )
            .scalars()
            .first()
        )

        return existing is not None

    def _schedule_upcoming_episodes(
        self,
        session: Session,
        now: datetime,
        offset_seconds: int,
    ) -> None:
        """Schedule episode_release for future-dated episodes that are not completed."""

        upcoming_eps = (
            session.execute(
                select(Episode)
                .where(Episode.aired_at.is_not(None))
                .where(Episode.aired_at >= now)
                .where(~(Episode.last_state == States.Completed))
            )
            .unique()
            .scalars()
            .all()
        )

        for ep in upcoming_eps:
            if (
                not self._has_future_task(session, ep.id, "episode_release", now)
                and ep.aired_at
            ):
                run_at = ep.aired_at + timedelta(seconds=offset_seconds)

                try:
                    ep.schedule(
                        run_at,
                        task_type="episode_release",
                        offset_seconds=offset_seconds,
                        reason="monitor:episode_air",
                    )
                except Exception as e:
                    logger.debug(f"Skipping schedule for {ep.log_string}: {e}")

    def _schedule_upcoming_movies(
        self, session: Session, now: datetime, offset_seconds: int
    ) -> None:
        """Schedule movie_release for future-dated movies that are not completed."""

        upcoming_movies = (
            session.execute(
                select(Movie)
                .where(Movie.aired_at.is_not(None))
                .where(Movie.aired_at >= now)
                .where(~(Movie.last_state == States.Completed))
            )
            .unique()
            .scalars()
            .all()
        )
        for mv in upcoming_movies:
            if (
                not self._has_future_task(
                    session=session,
                    item_id=mv.id,
                    task_type="movie_release",
                    now=now,
                )
                and mv.aired_at
            ):
                run_at = mv.aired_at + timedelta(seconds=offset_seconds)

                try:
                    mv.schedule(
                        run_at=run_at,
                        task_type="movie_release",
                        offset_seconds=offset_seconds,
                        reason="monitor:movie_release",
                    )
                except Exception as e:
                    logger.debug(f"Skipping schedule for {mv.log_string}: {e}")

    def _schedule_ongoing_shows(self, session: Session, now: datetime) -> None:
        """Schedule reindex_show for ongoing/unreleased shows based on next air, with daily fallback."""

        ongoing_shows = (
            session.execute(
                select(Show).where(
                    Show.last_state.in_([States.Ongoing, States.Unreleased])
                )
            )
            .unique()
            .scalars()
            .all()
        )

        for show in ongoing_shows:
            rd = show.release_data
            next_air = self._compute_next_air_datetime(rd, now)

            if next_air and next_air > now:
                if not self._has_future_task(session, show.id, "reindex_show", now):
                    try:
                        show.schedule(
                            next_air,
                            task_type="reindex_show",
                            reason="monitor:next_air",
                        )
                    except Exception as e:
                        logger.debug(
                            f"Skipping reindex schedule for {show.log_string}: {e}"
                        )
            else:
                fallback_time = (now + timedelta(days=1)).replace(
                    minute=0,
                    second=0,
                    microsecond=0,
                )

                if not self._has_future_task(session, show.id, "reindex_show", now):
                    try:
                        show.schedule(
                            fallback_time,
                            task_type="reindex_show",
                            reason="monitor:fallback_daily",
                        )
                    except Exception as e:
                        logger.debug(
                            f"Skipping fallback reindex for {show.log_string}: {e}"
                        )

    def _schedule_unknown_movies(self, session: Session, now: datetime) -> None:
        """Schedule daily reindex for movies without any known release date."""

        unknown_movies = (
            session.execute(
                select(Movie)
                .where(Movie.aired_at.is_(None))
                .where(
                    Movie.last_state.in_(
                        [
                            States.Unreleased,
                            States.Indexed,
                            States.Requested,
                            States.Unknown,
                        ]
                    )
                )
            )
            .unique()
            .scalars()
            .all()
        )

        for mv in unknown_movies:
            fallback_time = (now + timedelta(days=1)).replace(
                minute=0, second=0, microsecond=0
            )

            if not self._has_future_task(session, mv.id, "reindex_movie", now):
                try:
                    mv.schedule(
                        fallback_time,
                        task_type="reindex_movie",
                        reason="monitor:fallback_daily",
                    )
                except Exception as e:
                    logger.debug(f"Skipping fallback reindex for {mv.log_string}: {e}")

    @staticmethod
    def _compute_next_air_datetime(
        release_data: SeriesRelease | None,
        ref: datetime,
    ) -> datetime | None:
        """Compute the next air datetime from a TVDB-like payload.

        Strategy:
        1) Try explicit next_aired (date or datetime). If date-only, combine with airs_time.
        2) Otherwise, use airs_days + airs_time to find the next matching weekday.
        All times honor release_data['timezone'] when provided, then converted to local naive.
        """

        if not release_data:
            return None

        dt = ProgramScheduler._parse_next_aired_datetime(release_data)

        if dt is not None and dt >= ref:
            return dt

        # Fall through to weekday computation if next_aired is in the past
        hm = ProgramScheduler._parse_airs_time(release_data.airs_time)

        if hm is None:
            return None

        hour, minute = hm

        valid_days = ProgramScheduler._valid_weekdays(release_data.airs_days)

        if not valid_days:
            return None

        # Find next occurrence >= ref within 3 weeks
        for i in range(0, 21):
            candidate = ref + timedelta(days=i)

            if candidate.weekday() in valid_days:
                candidate_dt = candidate.replace(
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0,
                )

                if candidate_dt and candidate_dt >= ref:
                    return candidate_dt

        return None

    @staticmethod
    def _parse_next_aired_datetime(release_data: SeriesRelease) -> datetime | None:
        """Parse release_data['next_aired'] into a datetime, combining with airs_time if needed."""

        next_aired = release_data.next_aired

        if not next_aired:
            return None

        # If datetime-like
        if "T" in next_aired or " " in next_aired:
            try:
                return datetime.fromisoformat(next_aired)
            except Exception:
                return None

        airs_time = release_data.airs_time

        # Date-only
        try:
            base = datetime.fromisoformat(next_aired + "T00:00:00")

            if airs_time:
                try:
                    hour, minute = [int(x) for x in str(airs_time).split(":", 1)]
                except Exception:
                    hour, minute = 0, 0

                return base.replace(hour=hour, minute=minute)

            return base
        except Exception:
            return None

    @staticmethod
    def _parse_airs_time(airs_time: str | None) -> tuple[int, int] | None:
        """Parse HH:MM from release_data['airs_time'] if present and valid."""

        if not airs_time:
            return None

        try:
            hour, minute = [int(x) for x in str(airs_time).split(":", 1)]
            return hour, minute
        except Exception:
            return None

    @staticmethod
    def _valid_weekdays(series_airs_days: SeriesAirsDays | None) -> list[int]:
        """Return list of weekday indices [0..6] marked True in release_data['airs_days']."""

        if not series_airs_days:
            return []

        day_map = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]

        return [
            i
            for i, name in enumerate(day_map)
            if getattr(series_airs_days, name) is True
        ]
