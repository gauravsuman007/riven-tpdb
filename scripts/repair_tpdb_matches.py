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

        match = resolve_movie(
            api,
            title=item.title or "",
            studio=item.network,
            year=item.aired_at.year if item.aired_at else None,
            performers=list(item.performers or []),
            year_offset=0,
        )

        if match and match.tpdb_id != item.tpdb_id:
            print(f"   re-resolved -> {match.tpdb_title!r} ({match.tpdb_id}) score={match.score:.1f}")
            if APPLY:
                item.tpdb_id = match.tpdb_id
        elif match:
            print("   re-resolved to the same id -- leaving alone")
        else:
            # Better to carry no TPDB id than a confidently wrong one: the
            # detail page falls back to the metadata the item already has,
            # rather than rendering a different film's cast and poster.
            print("   no acceptable match -- clearing the wrong id")
            if APPLY:
                item.tpdb_id = None

    if APPLY:
        session.commit()
        print("\napplied")
    else:
        print("\ndry run -- nothing written")
