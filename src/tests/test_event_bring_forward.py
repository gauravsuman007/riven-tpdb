"""A manual pick must not wait out a parked debrid poll."""

from datetime import timedelta

from program.managers.event_manager import EventManager
from program.types import Event
from program.utils.time import utcnow


def _manager_with(*events: Event) -> EventManager:
    em = EventManager()
    em._queued_events.extend(events)
    return em


def test_parked_event_is_made_due():
    later = Event("Downloader", 862, run_at=utcnow() + timedelta(minutes=9))
    em = _manager_with(later)

    assert em.bring_forward(862) is True
    assert later.run_at <= utcnow()


def test_other_items_and_due_events_are_left_alone():
    other = Event("Downloader", 7, run_at=utcnow() + timedelta(minutes=9))
    due = Event("Downloader", 862, run_at=utcnow() - timedelta(seconds=1))
    em = _manager_with(other, due)

    assert em.bring_forward(862) is False
    assert other.run_at > utcnow()
