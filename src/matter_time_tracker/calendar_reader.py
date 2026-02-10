"""Read events from Apple Calendar on macOS.

Primary approach: EventKit via PyObjC (requires calendar access permission).
Fallback: parse .ics files stored in ~/Library/Calendars/.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# DataFrame schema returned by both backends
# ---------------------------------------------------------------------------
EVENT_COLUMNS = [
    "event_title",
    "start_time",
    "end_time",
    "duration_hours",
    "attendees",
    "attendee_emails",
    "location",
    "notes",
]


def _round_duration(start: datetime, end: datetime) -> float:
    """Return duration in hours rounded to the nearest tenth."""
    delta = (end - start).total_seconds() / 3600.0
    return round(delta, 1)


# ---------------------------------------------------------------------------
# EventKit backend (preferred)
# ---------------------------------------------------------------------------

def _read_eventkit(start_date: datetime, end_date: datetime) -> pd.DataFrame:
    """Fetch events via the macOS EventKit framework (PyObjC)."""
    # Imports are inside the function so the module can still be loaded on
    # non-macOS systems (the .ics fallback will be used instead).
    import EventKit  # type: ignore[import-untyped]
    from Foundation import NSDate  # type: ignore[import-untyped]

    store = EventKit.EKEventStore.alloc().init()

    # Request calendar access (blocks until the user responds to the prompt).
    granted = _request_calendar_access(store)
    if not granted:
        raise PermissionError(
            "Calendar access was denied. Grant access in "
            "System Settings > Privacy & Security > Calendars."
        )

    ns_start = NSDate.dateWithTimeIntervalSince1970_(start_date.timestamp())
    ns_end = NSDate.dateWithTimeIntervalSince1970_(end_date.timestamp())

    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        ns_start, ns_end, None,  # None = all calendars
    )
    ek_events = store.eventsMatchingPredicate_(predicate)

    rows: list[dict] = []
    for ev in ek_events or []:
        start_dt = datetime.fromtimestamp(ev.startDate().timeIntervalSince1970())
        end_dt = datetime.fromtimestamp(ev.endDate().timeIntervalSince1970())

        attendee_names: list[str] = []
        attendee_emails: list[str] = []
        for att in ev.attendees() or []:
            name = att.name() or ""
            # EKParticipant exposes the email via URL with a mailto: prefix
            url = att.URL()
            email = ""
            if url:
                email_str = url.absoluteString() or ""
                email = email_str.replace("mailto:", "")
            attendee_names.append(name)
            attendee_emails.append(email)

        rows.append({
            "event_title": ev.title() or "",
            "start_time": start_dt,
            "end_time": end_dt,
            "duration_hours": _round_duration(start_dt, end_dt),
            "attendees": "; ".join(attendee_names),
            "attendee_emails": "; ".join(attendee_emails),
            "location": ev.location() or "",
            "notes": ev.notes() or "",
        })

    return pd.DataFrame(rows, columns=EVENT_COLUMNS)


def _request_calendar_access(store) -> bool:  # type: ignore[no-untyped-def]
    """Request full calendar access and block until the user responds."""
    import EventKit  # type: ignore[import-untyped]
    from threading import Event as ThreadEvent

    result: dict[str, bool] = {"granted": False}
    done = ThreadEvent()

    def handler(granted: bool, error) -> None:  # type: ignore[no-untyped-def]
        result["granted"] = granted
        done.set()

    store.requestFullAccessToEventsWithCompletion_(handler)
    done.wait(timeout=120)
    return result["granted"]


# ---------------------------------------------------------------------------
# .ics file fallback
# ---------------------------------------------------------------------------

_DEFAULT_CALENDARS_DIR = Path.home() / "Library" / "Calendars"


def _read_ics_files(
    start_date: datetime,
    end_date: datetime,
    calendars_dir: Path | None = None,
) -> pd.DataFrame:
    """Parse .ics files found under ~/Library/Calendars/."""
    from icalendar import Calendar  # type: ignore[import-untyped]

    cal_dir = calendars_dir or _DEFAULT_CALENDARS_DIR
    if not cal_dir.exists():
        raise FileNotFoundError(
            f"Calendar directory not found: {cal_dir}. "
            "Make sure Apple Calendar has been used at least once."
        )

    ics_files = list(cal_dir.rglob("*.ics"))
    if not ics_files:
        raise FileNotFoundError(f"No .ics files found under {cal_dir}.")

    rows: list[dict] = []
    for ics_path in ics_files:
        try:
            cal = Calendar.from_ical(ics_path.read_bytes())
        except Exception:
            continue  # skip malformed files

        for component in cal.walk("VEVENT"):
            dt_start = component.get("DTSTART")
            dt_end = component.get("DTEND")
            if dt_start is None:
                continue

            start_dt = _to_datetime(dt_start.dt)
            if dt_end is not None:
                end_dt = _to_datetime(dt_end.dt)
            else:
                # Fall back to DURATION if DTEND is absent
                duration = component.get("DURATION")
                if duration and hasattr(duration, "dt"):
                    end_dt = start_dt + duration.dt
                else:
                    end_dt = start_dt + timedelta(hours=1)

            # Filter to requested date range
            if end_dt < start_date or start_dt > end_date:
                continue

            attendee_names: list[str] = []
            attendee_emails: list[str] = []
            raw_attendees = component.get("ATTENDEE")
            if raw_attendees:
                if not isinstance(raw_attendees, list):
                    raw_attendees = [raw_attendees]
                for att in raw_attendees:
                    email = str(att).replace("mailto:", "").replace("MAILTO:", "")
                    cn = att.params.get("CN", "") if hasattr(att, "params") else ""
                    attendee_names.append(str(cn))
                    attendee_emails.append(email)

            summary = str(component.get("SUMMARY", ""))
            location = str(component.get("LOCATION", ""))
            description = str(component.get("DESCRIPTION", ""))

            rows.append({
                "event_title": summary,
                "start_time": start_dt,
                "end_time": end_dt,
                "duration_hours": _round_duration(start_dt, end_dt),
                "attendees": "; ".join(attendee_names),
                "attendee_emails": "; ".join(attendee_emails),
                "location": location,
                "notes": description,
            })

    return pd.DataFrame(rows, columns=EVENT_COLUMNS)


def _to_datetime(dt_value) -> datetime:  # type: ignore[no-untyped-def]
    """Normalise a date or datetime from icalendar into a naive datetime."""
    if isinstance(dt_value, datetime):
        # Strip timezone — keep local time for simplicity
        return dt_value.replace(tzinfo=None)
    # date-only → midnight
    return datetime(dt_value.year, dt_value.month, dt_value.day)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_events(
    start_date: datetime,
    end_date: datetime,
    *,
    backend: str = "auto",
    calendars_dir: Path | None = None,
) -> pd.DataFrame:
    """Read Apple Calendar events in the given date range.

    Parameters
    ----------
    start_date, end_date:
        Inclusive date range for fetching events.
    backend:
        ``"eventkit"`` — use EventKit (requires macOS + permission).
        ``"ics"``      — parse .ics files from ~/Library/Calendars/.
        ``"auto"``     — try EventKit first, fall back to .ics parsing.
    calendars_dir:
        Override the directory to scan for .ics files (mainly for testing).

    Returns
    -------
    pd.DataFrame with columns defined in EVENT_COLUMNS.
    """
    if backend == "eventkit":
        return _read_eventkit(start_date, end_date)

    if backend == "ics":
        return _read_ics_files(start_date, end_date, calendars_dir)

    # auto: try EventKit, fall back to .ics
    try:
        return _read_eventkit(start_date, end_date)
    except Exception:
        return _read_ics_files(start_date, end_date, calendars_dir)
