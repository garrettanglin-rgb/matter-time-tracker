"""Match calendar events to legal matters from the registry.

Matching logic (evaluated per event, first match wins):
1. Attendee email match — any attendee email matches a matter's contact_emails.
2. Keyword match — any of a matter's subject_keywords appears in the event
   title or notes.

Events that don't match any matter are flagged as unmatched.
"""

from __future__ import annotations

import pandas as pd

from matter_time_tracker.registry import Matter, load_matters


def match_events(
    events: pd.DataFrame,
    matters: list[Matter] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Classify every event as matched or unmatched.

    Parameters
    ----------
    events:
        DataFrame returned by ``calendar_reader.read_events``.
    matters:
        List of Matter objects. Loaded from the default registry if *None*.

    Returns
    -------
    (matched, unmatched) — two DataFrames.

    ``matched`` has the original event columns plus:
        matter_id, matter_name, client_name, match_type
    ``unmatched`` keeps the original event columns only.
    """
    matters = matters or load_matters()

    matched_rows: list[dict] = []
    unmatched_rows: list[dict] = []

    for _, row in events.iterrows():
        event_dict = row.to_dict()
        result = _find_match(event_dict, matters)

        if result is not None:
            matter, match_type = result
            event_dict["matter_id"] = matter.matter_id
            event_dict["matter_name"] = matter.matter_name
            event_dict["client_name"] = matter.client_name
            event_dict["match_type"] = match_type
            matched_rows.append(event_dict)
        else:
            unmatched_rows.append(event_dict)

    matched_cols = list(events.columns) + ["matter_id", "matter_name", "client_name", "match_type"]
    matched = pd.DataFrame(matched_rows, columns=matched_cols)
    unmatched = pd.DataFrame(unmatched_rows, columns=list(events.columns))

    return matched, unmatched


def _find_match(
    event: dict,
    matters: list[Matter],
) -> tuple[Matter, str] | None:
    """Return the first matching (Matter, match_type) or None."""
    attendee_emails_raw: str = event.get("attendee_emails", "")
    emails = [e.strip().lower() for e in attendee_emails_raw.split(";") if e.strip()]

    # 1. Email match
    for matter in matters:
        for email in emails:
            if matter.matches_email(email):
                return matter, f"attendee_email ({email})"

    # 2. Keyword match against title and notes
    title: str = event.get("event_title", "")
    notes: str = event.get("notes", "")
    searchable = f"{title} {notes}"

    for matter in matters:
        if matter.matches_subject(searchable):
            # Identify which keyword hit
            lower = searchable.lower()
            for kw in matter.subject_keywords:
                if kw.lower() in lower:
                    return matter, f"keyword ({kw})"

    return None
