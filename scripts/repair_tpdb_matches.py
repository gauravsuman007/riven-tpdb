"""Re-resolve library items whose stored TPDB match the current gate rejects.

Run with --apply to write; otherwise reports what it would do.

Why this exists as a script rather than a migration: the acceptance rules
live in code and will change again, and nothing re-checks a match once it is
stored. Whenever the matcher is tightened, whatever it previously accepted
stays in the library, silently wrong -- which is exactly how "Pirates" kept
showing "Butthole Pirates" for days after the gate that rejects it landed.
"""
import sys

sys.path.insert(0, "src")

from kink import di
from program.apis.tpdb_api import TpdbApi
from program.db.db import db_session
from program.media.collection import CollectionEntry
from program.media.item import MediaItem
from program.program import Program
from program.services.awards.matching import MIN_TITLE_SYMMETRY, title_symmetry
from program.services.recommendations.tpdb_lookup import resolve_movie

APPLY = "--apply" in sys.argv

Program().initialize_apis()
api = di[TpdbApi]

with db_session() as session:
    items = session.query(MediaItem).filter(MediaItem.tpdb_id.isnot(None)).all()

    for item in items:
        try:
            detail = api.get_movie(item.tpdb_id)
        except Exception:
            continue

        # A movie lookup that 404s is usually a SCENE id, which is a different
        # endpoint and a perfectly valid thing for an item to hold. Only a
        # record that resolves and disagrees is evidence of a bad match.
        if detail is None:
            continue

        if title_symmetry(item.title or "", detail.title or "") >= MIN_TITLE_SYMMETRY:
            continue

        print(f"\n{item.id} {item.title!r}")
        print(f"   currently -> {detail.title!r} ({item.tpdb_id})")

        # The studio comes from the catalogue entry, not the MediaItem: these
        # items arrive from a storefront that records it, but `network` is
        # left unset on the item itself -- and studio is exactly the term
        # that makes the search find the right record at all (see
        # tpdb_lookup.resolve_movie).
        entry = (
            session.query(CollectionEntry)
            .filter(CollectionEntry.title == item.title, CollectionEntry.studio.isnot(None))
            .first()
        )

        match = resolve_movie(
            api,
            title=item.title or "",
            studio=entry.studio if entry else item.network,
            year=(entry.year if entry and entry.year else (item.aired_at.year if item.aired_at else None)),
            performers=list(item.performers or []),
            year_offset=0,
        )

        if match and match.tpdb_id != item.tpdb_id:
            print(f"   re-resolved -> {match.title!r} ({match.tpdb_id}) score={match.score:.1f}")
            if APPLY:
                item.tpdb_id = match.tpdb_id
        elif match:
            print("   re-resolved to the same id -- leaving alone")
        else:
            # Better to carry no TPDB id than a confidently wrong one: the
            # detail page falls back to the metadata the item already has,
            # rather than rendering a different film's cast and poster.
            # Deliberately left alone rather than cleared. The library grid
            # drops any item with no external id (see
            # library/+page.server.ts's transformItems), so clearing would
            # hide the title completely -- worse than showing stale
            # metadata. Reported instead, for a human to decide.
            print("   no acceptable match -- LEFT ALONE (clearing would hide it from the library)")

    if APPLY:
        session.commit()
        print("\napplied")
    else:
        print("\ndry run -- nothing written")
