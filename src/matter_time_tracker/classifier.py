"""AI-powered classification for unmatched calendar events and emails.

Uses the Anthropic API to classify items that the rules-based matcher could
not assign to a matter.  For each unmatched item Claude receives the
subject/title, participant details, a content snippet, and the full list of
active matters, then returns a JSON verdict: either a matter match with a
confidence level, or a flag indicating the item is not billable client work.

This module is imported lazily so the anthropic SDK is only required when
the ``--classify`` flag is used.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import anthropic
import pandas as pd

from matter_time_tracker.config import get_anthropic_api_key
from matter_time_tracker.registry import Matter, load_matters, find_matter_by_id

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


def _get_model() -> str:
    return os.getenv("CLASSIFIER_MODEL", _DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a legal matter classification assistant.  You will be given an item \
from a lawyer's calendar or inbox along with a list of active legal matters. \
Your job is to determine which matter the item most likely relates to.

Respond with ONLY a JSON object (no markdown fences, no commentary):

If the item relates to a matter:
{"matter_id": "<id>", "confidence": "high|medium|low", "reason": "<brief explanation>"}

If the item is NOT related to any billable client work (e.g. internal meetings, \
personal events, marketing, CLE):
{"matter_id": null, "confidence": "high", "reason": "<brief explanation>"}

Rules:
- Use "high" confidence when the connection is obvious (names, case numbers, \
directly relevant subject matter).
- Use "medium" when the connection is plausible but circumstantial.
- Use "low" when you are guessing based on limited signals.
- Prefer returning null over a low-confidence guess.
"""


def _format_matters_context(matters: list[Matter]) -> str:
    lines = ["Active matters:"]
    for m in matters:
        lines.append(
            f"  - {m.matter_id}: {m.matter_name} | Client: {m.client_name} | "
            f"Contacts: {', '.join(m.contact_emails)} | "
            f"Keywords: {', '.join(m.subject_keywords)}"
        )
    return "\n".join(lines)


def _format_event_item(row: dict) -> str:
    parts = [
        f"Type: calendar event",
        f"Title: {row.get('event_title', '')}",
        f"Start: {row.get('start_time', '')}",
        f"End: {row.get('end_time', '')}",
        f"Duration: {row.get('duration_hours', '')} hours",
        f"Attendees: {row.get('attendees', '')}",
        f"Attendee emails: {row.get('attendee_emails', '')}",
        f"Location: {row.get('location', '')}",
    ]
    notes = str(row.get("notes", ""))
    if notes:
        # Truncate long notes to keep token usage reasonable
        snippet = notes[:500] + ("..." if len(notes) > 500 else "")
        parts.append(f"Notes: {snippet}")
    return "\n".join(parts)


def _format_email_item(row: dict) -> str:
    parts = [
        f"Type: email",
        f"Date: {row.get('date', '')}",
        f"Subject: {row.get('subject', '')}",
        f"Sender: {row.get('sender', '')}",
        f"Recipients: {row.get('recipients', '')}",
        f"Word count: {row.get('word_count', '')}",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# API interaction
# ---------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    matter_id: str | None
    confidence: str
    reason: str


def _classify_single(
    client: anthropic.Anthropic,
    matters_context: str,
    item_text: str,
    model: str,
) -> ClassificationResult:
    """Send a single item to Claude and parse the classification response."""
    user_message = f"{matters_context}\n\n---\n\nItem to classify:\n{item_text}"

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ClassificationResult(matter_id=None, confidence="high", reason="Parse error")

    return ClassificationResult(
        matter_id=data.get("matter_id"),
        confidence=data.get("confidence", "low"),
        reason=data.get("reason", ""),
    )


# ---------------------------------------------------------------------------
# Public API — classify unmatched events
# ---------------------------------------------------------------------------

def classify_unmatched_events(
    unmatched: pd.DataFrame,
    matters: list[Matter] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Classify unmatched calendar events using the Anthropic API.

    Returns
    -------
    (ai_matched, still_unmatched)

    ``ai_matched`` has the same columns as the rules-matched events DataFrame
    (original event columns + matter_id, matter_name, client_name, match_type).

    ``still_unmatched`` contains items Claude flagged as non-billable or that
    could not be classified.
    """
    if unmatched.empty:
        matched_cols = list(unmatched.columns) + [
            "matter_id", "matter_name", "client_name", "match_type",
        ]
        return pd.DataFrame(columns=matched_cols), unmatched.copy()

    matters = matters or load_matters()
    client = anthropic.Anthropic(api_key=get_anthropic_api_key())
    model = _get_model()
    matters_context = _format_matters_context(matters)

    ai_matched_rows: list[dict] = []
    still_unmatched_rows: list[dict] = []

    for _, row in unmatched.iterrows():
        row_dict = row.to_dict()
        item_text = _format_event_item(row_dict)
        result = _classify_single(client, matters_context, item_text, model)

        if result.matter_id and result.confidence in ("high", "medium"):
            matter = find_matter_by_id(result.matter_id, matters)
            if matter:
                row_dict["matter_id"] = matter.matter_id
                row_dict["matter_name"] = matter.matter_name
                row_dict["client_name"] = matter.client_name
                row_dict["match_type"] = (
                    f"ai_classified ({result.confidence}: {result.reason})"
                )
                ai_matched_rows.append(row_dict)
                continue

        still_unmatched_rows.append(row_dict)

    matched_cols = list(unmatched.columns) + [
        "matter_id", "matter_name", "client_name", "match_type",
    ]
    ai_matched = pd.DataFrame(ai_matched_rows, columns=matched_cols)
    still_unmatched = pd.DataFrame(still_unmatched_rows, columns=list(unmatched.columns))

    return ai_matched, still_unmatched


# ---------------------------------------------------------------------------
# Public API — classify unmatched emails
# ---------------------------------------------------------------------------

def classify_unmatched_emails(
    unmatched: pd.DataFrame,
    matters: list[Matter] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Classify unmatched emails using the Anthropic API.

    Returns
    -------
    (ai_matched, still_unmatched)

    ``ai_matched`` uses the same column schema as the rules-matched emails
    DataFrame (``email_reader.EMAIL_COLUMNS``).

    ``still_unmatched`` keeps the original unmatched columns.
    """
    from matter_time_tracker.config import estimate_email_time
    from matter_time_tracker.email_reader import EMAIL_COLUMNS

    if unmatched.empty:
        return pd.DataFrame(columns=EMAIL_COLUMNS), unmatched.copy()

    matters = matters or load_matters()
    client = anthropic.Anthropic(api_key=get_anthropic_api_key())
    model = _get_model()
    matters_context = _format_matters_context(matters)

    ai_matched_rows: list[dict] = []
    still_unmatched_rows: list[dict] = []

    for _, row in unmatched.iterrows():
        row_dict = row.to_dict()
        item_text = _format_email_item(row_dict)
        result = _classify_single(client, matters_context, item_text, model)

        if result.matter_id and result.confidence in ("high", "medium"):
            matter = find_matter_by_id(result.matter_id, matters)
            if matter:
                contact = _pick_email_contact(row_dict, matter)
                subject = row_dict.get("subject", "") or "(no subject)"
                ai_matched_rows.append({
                    "date": row_dict.get("date", ""),
                    "matter_id": matter.matter_id,
                    "matter_name": matter.matter_name,
                    "client_name": matter.client_name,
                    "sender": row_dict.get("sender", ""),
                    "recipients": row_dict.get("recipients", ""),
                    "subject": subject,
                    "word_count": row_dict.get("word_count", 0),
                    "estimated_hours": row_dict.get(
                        "estimated_hours",
                        estimate_email_time(row_dict.get("word_count", 0)),
                    ),
                    "activity_description": (
                        f"Email correspondence with {contact} re: {subject}"
                    ),
                    "match_type": (
                        f"ai_classified ({result.confidence}: {result.reason})"
                    ),
                })
                continue

        still_unmatched_rows.append(row_dict)

    ai_matched = pd.DataFrame(ai_matched_rows, columns=EMAIL_COLUMNS)
    still_unmatched = pd.DataFrame(
        still_unmatched_rows, columns=list(unmatched.columns),
    )

    return ai_matched, still_unmatched


def _pick_email_contact(row: dict, matter: Matter) -> str:
    """Return the best contact address for the activity description."""
    sender = row.get("sender", "")
    recipients_raw = row.get("recipients", "")
    all_addrs = [sender] + [r.strip() for r in recipients_raw.split(";") if r.strip()]
    for addr in all_addrs:
        if matter.matches_email(addr):
            return addr
    return sender
