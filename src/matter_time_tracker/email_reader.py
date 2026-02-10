"""Retrieve emails from Apple Mail on macOS.

Primary approach: Spotlight (mdfind) to find matching .emlx files, then
parse them with Python's email module.  This is dramatically faster than
AppleScript for large Exchange mailboxes because it uses macOS's pre-built
search index.
"""

from __future__ import annotations

import email as email_lib
import subprocess
from datetime import datetime
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

import pandas as pd

from matter_time_tracker.config import estimate_email_time
from matter_time_tracker.registry import Matter, load_matters

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMAIL_COLUMNS = [
    "date",
    "matter_id",
    "matter_name",
    "client_name",
    "sender",
    "recipients",
    "subject",
    "word_count",
    "estimated_hours",
    "activity_description",
    "match_type",
]

_MAIL_DIR = Path.home() / "Library" / "Mail"


# ---------------------------------------------------------------------------
# Spotlight (mdfind) search
# ---------------------------------------------------------------------------

def _mdfind(query: str) -> list[str]:
    """Run mdfind and return matching file paths."""
    try:
        result = subprocess.run(
            ["mdfind", "-onlyin", str(_MAIL_DIR), query],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        return [p for p in result.stdout.strip().split("\n") if p]
    except Exception:
        return []


def _parse_emlx(file_path: str) -> dict | None:
    """Parse an .emlx file and return a dict with email fields."""
    try:
        with open(file_path, "rb") as f:
            first_line = f.readline()
            # .emlx files start with a byte count of the RFC822 message
            try:
                byte_count = int(first_line.strip())
                raw_msg = f.read(byte_count)
            except ValueError:
                # Not a standard .emlx — try reading as plain message
                f.seek(0)
                raw_msg = f.read()

        msg = email_lib.message_from_bytes(raw_msg)

        # Sender
        from_header = msg.get("From", "")
        _, sender_email = parseaddr(from_header)
        if not sender_email:
            sender_email = from_header

        # Date
        date_header = msg.get("Date", "")
        try:
            msg_date = parsedate_to_datetime(date_header)
        except Exception:
            return None

        # Subject
        subject = msg.get("Subject", "") or ""
        # Decode if it's an encoded header
        from email.header import decode_header
        decoded_parts = decode_header(subject)
        subject = ""
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                subject += part.decode(charset or "utf-8", errors="replace")
            else:
                subject += part

        # Recipients
        to_header = msg.get("To", "") or ""
        cc_header = msg.get("Cc", "") or ""
        all_recips = getaddresses([to_header, cc_header])
        recip_emails = [addr for _, addr in all_recips if addr]

        # Body / word count
        body = ""
        try:
            if msg.is_multipart():
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="replace")
                            break
                # If no text/plain, try text/html
                if not body:
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct == "text/html":
                            payload = part.get_payload(decode=True)
                            if payload:
                                body = payload.decode("utf-8", errors="replace")
                                break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
        except Exception:
            body = ""

        word_count = len(body.split()) if body else 0

        return {
            "date": msg_date.strftime("%Y-%m-%d"),
            "datetime": msg_date,
            "sender": sender_email.strip(),
            "recipients": "; ".join(recip_emails),
            "subject": subject.strip(),
            "word_count": word_count,
        }
    except Exception:
        return None


def _search_spotlight(
    start_date: datetime,
    end_date: datetime,
    contact_emails: list[str],
) -> list[dict]:
    """Find matching emails using Spotlight (mdfind) + .emlx parsing.

    Searches for emails FROM each contact and emails TO each contact
    (sent by the user).  Uses the macOS Spotlight index, which is
    orders of magnitude faster than AppleScript for large mailboxes.
    """
    seen_keys: set[tuple[str, str, str]] = set()
    results: list[dict] = []

    start_iso = start_date.strftime("%Y-%m-%d")
    end_iso = end_date.strftime("%Y-%m-%d")

    for contact in contact_emails:
        # Search for emails FROM this contact within date range
        query_from = (
            f'kMDItemAuthors == "{contact}"cd'
            f' && kMDItemContentCreationDate >= $time.iso({start_iso})'
            f' && kMDItemContentCreationDate <= $time.iso({end_iso}T23:59:59)'
        )
        # Search for emails TO this contact within date range
        query_to = (
            f'kMDItemRecipients == "{contact}"cd'
            f' && kMDItemContentCreationDate >= $time.iso({start_iso})'
            f' && kMDItemContentCreationDate <= $time.iso({end_iso}T23:59:59)'
        )

        for query in [query_from, query_to]:
            paths = _mdfind(query)
            for path in paths:
                parsed = _parse_emlx(path)
                if parsed is None:
                    continue

                # Date range check (belt and suspenders)
                msg_dt = parsed["datetime"]
                if msg_dt.replace(tzinfo=None) < start_date:
                    continue
                if msg_dt.replace(tzinfo=None) > end_date:
                    continue

                # De-duplicate
                key = (parsed["date"], parsed["sender"].lower(), parsed["subject"].lower())
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                results.append({
                    "date": parsed["date"],
                    "sender": parsed["sender"],
                    "recipients": parsed["recipients"],
                    "subject": parsed["subject"],
                    "word_count": parsed["word_count"],
                })

    return results


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _match_email_to_matter(
    row: dict,
    matters: list[Matter],
) -> tuple[Matter, str] | None:
    """Return (Matter, match_type) for the first matching matter, or None.

    1. Email match — sender or any recipient matches a matter's contact_emails.
    2. Keyword match — any of a matter's subject_keywords appears in the
       email subject.
    """
    sender = row["sender"].lower()
    recipient_list = [r.strip().lower() for r in row["recipients"].split(";") if r.strip()]

    all_addresses = [sender] + recipient_list

    for matter in matters:
        for addr in all_addresses:
            if matter.matches_email(addr):
                return matter, f"email ({addr})"

    subject = row["subject"]
    for matter in matters:
        if matter.matches_subject(subject):
            for kw in matter.subject_keywords:
                if kw.lower() in subject.lower():
                    return matter, f"keyword ({kw})"

    return None


def _identify_contact(row: dict, matter: Matter) -> str:
    """Pick the best contact name to use in the activity description."""
    sender = row["sender"]
    all_addresses = [sender] + [
        r.strip() for r in row["recipients"].split(";") if r.strip()
    ]
    for addr in all_addresses:
        if matter.matches_email(addr):
            return addr
    return sender


def _build_activity(row: dict, matter: Matter) -> str:
    """Format: 'Email correspondence with [contact] re: [subject]'."""
    contact = _identify_contact(row, matter)
    subject = row["subject"] or "(no subject)"
    return f"Email correspondence with {contact} re: {subject}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_emails(
    start_date: datetime,
    end_date: datetime,
    matters: list[Matter] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Query Apple Mail and return matched and unmatched email DataFrames.

    Uses Spotlight (mdfind) to search the local Mail index, then parses
    .emlx files with Python's email module.

    Parameters
    ----------
    start_date, end_date:
        Inclusive date range.
    matters:
        Matters to match against.  Loaded from the registry if *None*.

    Returns
    -------
    (matched, unmatched) tuple of DataFrames.

    *matched* has columns defined in ``EMAIL_COLUMNS``.
    *unmatched* has: date, sender, recipients, subject, word_count,
    estimated_hours.
    """
    matters = matters or load_matters()

    # Collect every contact email across all matters.
    all_contact_emails: list[str] = []
    for m in matters:
        all_contact_emails.extend(m.contact_emails)
    all_contact_emails = list(set(all_contact_emails))

    if not all_contact_emails:
        empty_matched = pd.DataFrame(columns=EMAIL_COLUMNS)
        empty_unmatched = pd.DataFrame(
            columns=["date", "sender", "recipients", "subject", "word_count", "estimated_hours"]
        )
        return empty_matched, empty_unmatched

    raw_rows = _search_spotlight(start_date, end_date, all_contact_emails)

    matched_rows: list[dict] = []
    unmatched_rows: list[dict] = []

    for row in raw_rows:
        est_hours = estimate_email_time(row["word_count"])
        result = _match_email_to_matter(row, matters)

        if result is not None:
            matter, match_type = result
            matched_rows.append({
                "date": row["date"],
                "matter_id": matter.matter_id,
                "matter_name": matter.matter_name,
                "client_name": matter.client_name,
                "sender": row["sender"],
                "recipients": row["recipients"],
                "subject": row["subject"],
                "word_count": row["word_count"],
                "estimated_hours": est_hours,
                "activity_description": _build_activity(row, matter),
                "match_type": match_type,
            })
        else:
            unmatched_rows.append({
                "date": row["date"],
                "sender": row["sender"],
                "recipients": row["recipients"],
                "subject": row["subject"],
                "word_count": row["word_count"],
                "estimated_hours": est_hours,
            })

    matched = pd.DataFrame(matched_rows, columns=EMAIL_COLUMNS)
    unmatched = pd.DataFrame(
        unmatched_rows,
        columns=["date", "sender", "recipients", "subject", "word_count", "estimated_hours"],
    )

    return matched, unmatched
