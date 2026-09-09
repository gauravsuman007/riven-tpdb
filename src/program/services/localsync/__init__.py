"""Copy kept titles from the debrid-backed VFS onto local disk.

RivenVFS presents every library file as if it were on disk, but nothing is
stored: each read is fetched from the debrid provider on demand. That is the
whole design, and it has one consequence -- lose the provider (account
expires, torrent is dropped, connection is down) and the library is gone.

"Keep on disk" copies one title's active file out of the VFS and into the
configured local download path, and remembers that it did.

Reading through the VFS mount rather than talking to the provider directly is
deliberate: the VFS already re-mints spent provider links, honours the VPN
routing setting, and shares its chunk cache with playback. A second download
path here would have to reimplement all three and would drift from them.
"""

import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from program.db.db import db
from program.media.item import MediaItem
from program.media.local_copy import LocalCopy, LocalCopyState

# Read size per chunk. Large enough that FUSE round-trips are not the
# bottleneck, small enough that a cancel is noticed promptly.
_CHUNK = 8 * 1024 * 1024

# How often progress reaches the database. Every chunk would be a write per
# 8 MiB -- pointless traffic for a progress bar nobody watches that closely.
_PROGRESS_INTERVAL_SECONDS = 2.0


class LocalSyncService:
    """Runs the copies, one worker per configured concurrency slot."""

    def __init__(self) -> None:
        from program.settings import settings_manager

        self.settings_manager = settings_manager
        self._queue: list[int] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._cancelled: set[int] = set()
        self._workers: list[threading.Thread] = []

    # ------------------------------------------------------------------ config

    @property
    def root(self) -> Path | None:
        """The configured local download path, or None when the feature is off."""

        configured = self.settings_manager.settings.filesystem.local_download_path

        if not configured or str(configured).strip() in ("", "."):
            return None

        return Path(configured)

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def validate(self) -> tuple[bool, str]:
        """Whether kept copies can actually be written right now.

        Checked before accepting a request rather than only inside the worker,
        so a misconfigured path is an error on the button press instead of a
        row that silently goes to Failed a minute later.
        """

        root = self.root

        if root is None:
            return False, "No local download path is configured"

        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"Local download path is not creatable: {exc}"

        if not os.access(root, os.W_OK):
            return False, f"Local download path is not writable: {root}"

        return True, ""

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Spawn the workers and re-queue anything left mid-copy by a restart."""

        if not self.enabled:
            logger.debug("Local sync disabled (no local_download_path configured)")
            return

        self._requeue_interrupted()

        count = self.settings_manager.settings.filesystem.local_download_concurrency

        for index in range(count):
            worker = threading.Thread(
                target=self._work,
                name=f"LocalSync-{index}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

        logger.info(f"Local sync started with {count} worker(s) -> {self.root}")

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    def _requeue_interrupted(self) -> None:
        """A restart mid-copy leaves rows in Syncing; nothing is copying them.

        They are resumable (the partial file is kept), so put them back in the
        queue rather than failing them or leaving a progress bar that will
        never move again.
        """

        with db.Session() as session:
            stranded = (
                session.query(LocalCopy)
                .filter(
                    LocalCopy.state.in_(
                        [LocalCopyState.Syncing.value, LocalCopyState.Queued.value]
                    )
                )
                .all()
            )

            for copy in stranded:
                copy.state = LocalCopyState.Queued.value
                self._enqueue(copy.media_item_id)

            if stranded:
                session.commit()
                logger.info(f"Re-queued {len(stranded)} interrupted local copy job(s)")

    # ------------------------------------------------------------------ queueing

    def _enqueue(self, item_id: int) -> None:
        with self._lock:
            if item_id not in self._queue:
                self._queue.append(item_id)

            self._cancelled.discard(item_id)

        self._wake.set()

    def request(self, item_id: int) -> LocalCopy:
        """Ask for a title to be kept. Idempotent for anything already on disk."""

        ok, reason = self.validate()

        if not ok:
            raise ValueError(reason)

        with db.Session() as session:
            copy = (
                session.query(LocalCopy)
                .filter(LocalCopy.media_item_id == item_id)
                .one_or_none()
            )

            if copy and copy.state == LocalCopyState.OnDisk.value:
                if copy.path and Path(copy.path).exists():
                    session.expunge(copy)
                    return copy

                # Recorded as on disk but the file is gone -- deleted by hand,
                # or the path setting changed. Copy it again rather than
                # reporting a file that is not there.
                logger.debug(
                    f"Local copy for item {item_id} is missing from disk, re-syncing"
                )

            if not copy:
                copy = LocalCopy(media_item_id=item_id)
                session.add(copy)

            copy.state = LocalCopyState.Queued.value
            copy.error = None
            session.commit()
            session.refresh(copy)
            session.expunge(copy)

        self._enqueue(item_id)

        return copy

    def cancel(self, item_id: int, delete_file: bool = True) -> None:
        """Stop copying and forget the copy, removing the file by default."""

        with self._lock:
            if item_id in self._queue:
                self._queue.remove(item_id)

            self._cancelled.add(item_id)

        with db.Session() as session:
            copy = (
                session.query(LocalCopy)
                .filter(LocalCopy.media_item_id == item_id)
                .one_or_none()
            )

            if not copy:
                return

            path = copy.path
            session.delete(copy)
            session.commit()

        if delete_file and path:
            for candidate in (Path(path), Path(f"{path}.part")):
                try:
                    if candidate.exists():
                        candidate.unlink()
                except OSError as exc:
                    logger.warning(f"Could not remove {candidate}: {exc}")

            self._prune_empty_parents(Path(path).parent)

    def _prune_empty_parents(self, directory: Path) -> None:
        """Remove the title's directory once its last file is gone."""

        root = self.root

        if root is None:
            return

        try:
            current = directory.resolve()
            root = root.resolve()
        except OSError:
            return

        while current != root and root in current.parents:
            try:
                current.rmdir()
            except OSError:
                return

            current = current.parent

    # ------------------------------------------------------------------ workers

    def _next(self) -> int | None:
        with self._lock:
            if self._queue:
                return self._queue.pop(0)

        return None

    def _work(self) -> None:
        while not self._stopping.is_set():
            item_id = self._next()

            if item_id is None:
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue

            try:
                self._copy(item_id)
            except Exception as exc:
                logger.error(f"Local copy of item {item_id} failed: {exc}")
                self._fail(item_id, str(exc))

    def _resolve(self, item_id: int) -> tuple[Path, Path, int]:
        """Work out what to read, where to write it, and how big it is."""

        from program.settings import settings_manager

        with db.Session() as session:
            item = session.query(MediaItem).filter(MediaItem.id == item_id).one_or_none()

            if not item:
                raise ValueError("Item no longer exists")

            entry = item.media_entry

            if not entry:
                raise ValueError("Item has no downloaded file to copy")

            vfs_paths = entry.get_all_vfs_paths()

            if not vfs_paths:
                raise ValueError("Item has no path in the virtual filesystem")

            # The canonical /movies or /shows path, never a library-profile
            # view: those are additional filtered mirrors of the same file and
            # copying one would put the title under /kids on local disk.
            relative = vfs_paths[0].lstrip("/")
            size = entry.file_size or 0

        mount = Path(settings_manager.settings.filesystem.mount_path)
        root = self.root

        if root is None:
            raise ValueError("No local download path is configured")

        return mount / relative, root / relative, size

    def _copy(self, item_id: int) -> None:
        source, destination, expected_size = self._resolve(item_id)

        if not source.exists():
            raise ValueError(f"Not present in the virtual filesystem: {source}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(f"{destination}.part")

        # Resume from whatever a previous attempt managed. The VFS serves
        # ranges, so this costs nothing beyond a seek.
        done = partial.stat().st_size if partial.exists() else 0
        total = expected_size or source.stat().st_size

        if done > total:
            # The release changed underneath a half-finished copy.
            done = 0
            partial.unlink(missing_ok=True)

        self._mark(
            item_id,
            state=LocalCopyState.Syncing,
            path=str(destination),
            bytes_done=done,
            bytes_total=total,
        )

        self._check_space(destination.parent, total - done)

        logger.info(
            f"Keeping on disk: {source.name} "
            f"({done / 1e9:.2f}/{total / 1e9:.2f} GB) -> {destination}"
        )

        last_report = time.monotonic()

        with open(source, "rb") as reader, open(partial, "ab") as writer:
            reader.seek(done)

            while True:
                if self._is_cancelled(item_id):
                    logger.info(f"Local copy of item {item_id} cancelled")
                    return

                chunk = reader.read(_CHUNK)

                if not chunk:
                    break

                writer.write(chunk)
                done += len(chunk)

                now = time.monotonic()

                if now - last_report >= _PROGRESS_INTERVAL_SECONDS:
                    writer.flush()
                    self._mark(item_id, bytes_done=done, bytes_total=total)
                    last_report = now

        if total and done < total:
            raise ValueError(
                f"Copy ended early at {done} of {total} bytes -- source read failed"
            )

        partial.replace(destination)

        self._mark(
            item_id,
            state=LocalCopyState.OnDisk,
            path=str(destination),
            bytes_done=done,
            bytes_total=total or done,
            completed=True,
        )

        logger.info(f"Kept on disk: {destination}")

    def _check_space(self, directory: Path, needed: int) -> None:
        """Refuse before writing rather than filling the disk and failing late."""

        if needed <= 0:
            return

        try:
            free = shutil.disk_usage(directory).free
        except OSError:
            return

        # Leave a small margin: a disk driven to exactly zero takes the rest
        # of the server down with it, not just this copy.
        if free < needed + (512 * 1024 * 1024):
            raise ValueError(
                f"Not enough free space: needs {needed / 1e9:.1f} GB, "
                f"{free / 1e9:.1f} GB available"
            )

    def _is_cancelled(self, item_id: int) -> bool:
        with self._lock:
            return item_id in self._cancelled

    # ------------------------------------------------------------------ state

    def _mark(
        self,
        item_id: int,
        state: LocalCopyState | None = None,
        path: str | None = None,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
        error: str | None = None,
        completed: bool = False,
    ) -> None:
        with db.Session() as session:
            copy = (
                session.query(LocalCopy)
                .filter(LocalCopy.media_item_id == item_id)
                .one_or_none()
            )

            if not copy:
                return

            if state is not None:
                copy.state = state.value

            if path is not None:
                copy.path = path

            if bytes_done is not None:
                copy.bytes_done = bytes_done

            if bytes_total is not None:
                copy.bytes_total = bytes_total

            if error is not None:
                copy.error = error

            if completed:
                copy.completed_at = datetime.now(timezone.utc)

            session.commit()

    def _fail(self, item_id: int, message: str) -> None:
        self._mark(item_id, state=LocalCopyState.Failed, error=message[:500])


_service: LocalSyncService | None = None


def local_sync() -> LocalSyncService:
    """The process-wide sync service."""

    global _service

    if _service is None:
        _service = LocalSyncService()

    return _service
