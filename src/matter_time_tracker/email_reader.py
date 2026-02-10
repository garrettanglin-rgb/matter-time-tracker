"""Retrieve emails from Apple Mail on macOS via AppleScript.

Uses ``osascript`` (subprocess) to query Apple Mail for messages in a date
range whose sender or any recipient matches a contact email in the matters
registry.  Each email is matched to a matter, assigned an estimated time
based on word-count thresholds, and returned in a pandas DataFrame ready
for time-tracking export.
"""

from __future__ import annotations

import subprocess
import textwrap
from datetime import datetime
from pathlib import Path

import pandas as pd

from matter_time_tracker.config import estimate_email_time
from matter_time_tracker.registry import Matter, load_matters

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Delimiter used between fields inside a single message record.
_FIELD_SEP = "|||"
# Delimiter used between message records.
_RECORD_SEP = "<<<RECORD>>>"

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

# ---------------------------------------------------------------------------
# AppleScript generation
# ---------------------------------------------------------------------------


def _build_applescript(
    start_date: datetime,
    end_date: datetime,
    contact_emails: list[str],
) -> str:
    """Return an AppleScript that queries Apple Mail for matching messages.

    The script iterates over every account / mailbox and collects messages
    whose date falls within the range **and** whose sender or any recipient
    matches one of *contact_emails*.
    """
    # AppleScript date literals: «date "Monday, January 6, 2025 12:00:00 AM"»
    # Easier to compare epoch-style, but AppleScript's date coercion from a
    # plain string is locale-dependent.  We'll pass the dates as ISO strings
    # and let AppleScript parse them via `date`.
    start_str = start_date.strftime("%B %e, %Y %I:%M:%S %p").replace("  ", " ")
    end_str = end_date.strftime("%B %e, %Y %I:%M:%S %p").replace("  ", " ")

    # Build an AppleScript list literal of the target email addresses.
    email_list_items = ", ".join(f'"{e.lower()}"' for e in contact_emails)

    field_sep = _FIELD_SEP
    record_sep = _RECORD_SEP

    # The script collects matching messages across *all* accounts and
    # mailboxes, then writes one record per message.
    script = textwrap.dedent(f"""\
        on isEmailInList(addr, emailList)
            set lowerAddr to do shell script "echo " & quoted form of addr & " | tr '[:upper:]' '[:lower:]'"
            repeat with e in emailList
                if lowerAddr is equal to (contents of e) then return true
            end repeat
            return false
        end isEmailInList

        set fieldSep to "{field_sep}"
        set recordSep to "{record_sep}"
        set targetEmails to {{{email_list_items}}}
        set startDate to date "{start_str}"
        set endDate to date "{end_str}"
        set output to ""

        tell application "Mail"
            set boxesToSearch to {{}}
            set allAccounts to every account
            repeat with acct in allAccounts
                set acctAddr to ""
                try
                    set acctAddr to email addresses of acct as text
                end try
                if acctAddr contains "garrett@anglinlaw.net" then
                    try
                        set end of boxesToSearch to inbox of acct
                    end try
                    try
                        set end of boxesToSearch to sent mailbox of acct
                    end try
                end if
            end repeat
            repeat with mb in boxesToSearch
                    try
                        set msgs to (every message of mb whose date received is greater than or equal to startDate and date received is less than or equal to endDate)
                    on error
                        set msgs to {{}}
                    end try
                    repeat with msg in msgs
                        set msgMatched to false
                        set senderAddr to ""
                        try
                            set senderAddr to sender of msg
                        end try

                        if my isEmailInList(senderAddr, targetEmails) then
                            set msgMatched to true
                        end if

                        if not msgMatched then
                            try
                                set toRecips to every to recipient of msg
                                repeat with r in toRecips
                                    set rAddr to address of r
                                    if my isEmailInList(rAddr, targetEmails) then
                                        set msgMatched to true
                                        exit repeat
                                    end if
                                end repeat
                            end try
                        end if

                        if not msgMatched then
                            try
                                set ccRecips to every cc recipient of msg
                                repeat with r in ccRecips
                                    set rAddr to address of r
                                    if my isEmailInList(rAddr, targetEmails) then
                                        set msgMatched to true
                                        exit repeat
                                    end if
                                end repeat
                            end try
                        end if

                        if msgMatched then
                            set msgDate to date received of msg
                            set msgSubject to ""
                            set msgBody to ""
                            set recipAddrs to ""
                            try
                                set msgSubject to subject of msg
                            end try
                            try
                                set msgBody to content of msg
                            end try
                            try
                                set toRecips to every to recipient of msg
                                repeat with r in toRecips
                                    if recipAddrs is not "" then set recipAddrs to recipAddrs & "; "
                                    set recipAddrs to recipAddrs & address of r
                                end repeat
                            end try
                            try
                                set ccRecips to every cc recipient of msg
                                repeat with r in ccRecips
                                    if recipAddrs is not "" then set recipAddrs to recipAddrs & "; "
                                    set recipAddrs to recipAddrs & address of r
                                end repeat
                            end try

                            set dateStr to (year of msgDate as text) & "-"
                            set m to (month of msgDate as integer)
                            if m < 10 then set dateStr to dateStr & "0"
                            set dateStr to dateStr & (m as text) & "-"
                            set d to (day of msgDate as integer)
                            if d < 10 then set dateStr to dateStr & "0"
                            set dateStr to dateStr & (d as text)

                            set wordCount to count of words of msgBody

                            set rec to dateStr & fieldSep & senderAddr & fieldSep & recipAddrs & fieldSep & msgSubject & fieldSep & (wordCount as text)
                            if output is not "" then set output to output & recordSep
                            set output to output & rec
                        end if
                    end repeat
            end repeat
        end tell

        return output
    """)
    return script


# ---------------------------------------------------------------------------
# Run AppleScript and parse output
# ---------------------------------------------------------------------------


def _run_applescript(script: str) -> str:
    """Execute an AppleScript via ``osascript`` and return stdout."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"AppleScript failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _parse_applescript_output(raw: str) -> list[dict]:
    """Parse the delimited output from the AppleScript into row dicts."""
    if not raw:
        return []

    rows: list[dict] = []
    for record in raw.split(_RECORD_SEP):
        record = record.strip()
        if not record:
            continue
        parts = record.split(_FIELD_SEP)
        if len(parts) < 5:
            continue
        date_str, sender, recipients, subject, word_count_str = (
            parts[0],
            parts[1],
            parts[2],
            parts[3],
            parts[4],
        )
        try:
            word_count = int(word_count_str)
        except ValueError:
            word_count = 0

        rows.append({
            "date": date_str,
            "sender": sender.strip(),
            "recipients": recipients.strip(),
            "subject": subject.strip(),
            "word_count": word_count,
        })
    return rows


# ---------------------------------------------------------------------------
# Matching and DataFrame construction
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
    """Pick the best contact name to use in the activity description.

    Returns the first address from (sender ∪ recipients) that belongs to
    the matter's contact list, falling back to the sender address.
    """
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

    # Collect every contact email across all matters for the AppleScript query.
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

    script = _build_applescript(start_date, end_date, all_contact_emails)
    raw_output = _run_applescript(script)
    raw_rows = _parse_applescript_output(raw_output)

    # De-duplicate by (date, sender, subject) — the same message can appear
    # in multiple mailboxes (Inbox + All Mail, etc.).
    seen: set[tuple[str, str, str]] = set()
    unique_rows: list[dict] = []
    for r in raw_rows:
        key = (r["date"], r["sender"].lower(), r["subject"].lower())
        if key not in seen:
            seen.add(key)
            unique_rows.append(r)

    matched_rows: list[dict] = []
    unmatched_rows: list[dict] = []

    for row in unique_rows:
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
