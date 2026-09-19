"""Which of an item's existing files a newly downloaded one replaces.

Pure, and in its own module with no imports, because the rule it holds is the
one that decided how much of a release reached the library -- and because the
downloader package cannot be imported without a settings file, a database and
the RTN parser, so a rule living there is a rule nothing can test.

THE BUG THIS EXISTS FOR
-----------------------
`Downloader.update_item_attributes` calls `_update_attributes` ONCE PER FILE
in the torrent. The non-candidate branch did `item.filesystem_entries.clear()`
before appending, so on a multi-file release each file deleted the one before
it and the item kept whichever file the provider happened to list last.
"Mistress Maitland" (Deeper) is a 16.3 GiB torrent of four scenes; it reached
the library as a single 4.1 GiB file, with the other three downloaded, mounted
and unreachable. `media_parts` exists precisely to present those files as a
playlist and it never saw them -- they were dropped before anything was
persisted, which is why the player, the `.m3u` hand-off and the parts panel
all looked correct while showing one file.
"""


def stale_entries(entries, infohash: str | None, filename: str | None) -> list:
    """The entries a newly downloaded file should replace.

    Two, and deliberately only two:

    * **Files of a DIFFERENT release.** This is the behaviour the old
      `clear()` was there for, and it has to survive: swapping the release a
      title plays must not leave the previous one's files behind, or
      `media_parts` groups by an infohash that is no longer active and the
      title plays a file from a release it does not use any more.
    * **An existing entry for THIS same file**, so re-processing a release
      replaces its files rather than doubling them.

    Everything else -- every other file of the SAME release -- is kept, which
    is the entire fix. An entry with no recorded infohash is stale by this
    rule: it predates the grouping and cannot be shown to belong.
    """

    return [
        entry
        for entry in entries
        if getattr(entry, "stream_infohash", None) != infohash
        or getattr(entry, "original_filename", None) == filename
    ]


def pin_satisfied(downloading_hash: str | None, active_infohash: str | None) -> bool:
    """Whether a pinned release has landed and its pin should be dropped.

    `MediaItem.downloading_stream_hash` means "a release is pending for this
    item", and `db_functions.retry_library` hands any item carrying one back to
    the pipeline -- deliberately, so a pinned fetch survives a restart.

    THE LOOP THIS PREVENTS
    ----------------------
    Only the candidate branch of `Downloader.run` ever cleared the pin, and
    `candidate_mode` is False precisely when the pinned hash is ALREADY the
    active stream -- which is what `queue_release` produces when the same
    release is pinned a second time, the normal way to re-download a release
    whose files an earlier bug dropped. The item downloaded, kept its pin, was
    handed straight back by that query and downloaded again, every three
    seconds, indefinitely, reporting itself Completed throughout. Observed on
    item 874.

    True only when the pinned release is the one now active. A pin naming a
    DIFFERENT release is a candidate fetch that has genuinely not happened yet,
    and clearing it would abandon the fetch instead of completing it.
    """

    return bool(
        downloading_hash and active_infohash and downloading_hash == active_infohash
    )
