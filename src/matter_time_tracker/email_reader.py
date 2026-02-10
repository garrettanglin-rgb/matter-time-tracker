"""Retrieve emails from Apple Mail on macOS.

Uses AppleScript with bulk property access to efficiently search large
Exchange mailboxes.  Instead of using the slow ``whose`` clause, fetches
all senders in a single Apple Event call and filters in-script.
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

_FIELD_SEP = "|||"
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
# AppleScript — bulk property access approach
# ---------------------------------------------------------------------------

def _build_applescript(
    start_date: datetime,
    end_date: datetime,
    contact_emails: list[str],
) -> str:
    """Build an AppleScript that uses bulk property access.

    Instead of ``whose sender contains X`` (which is extremely slow on
    large Exchange mailboxes), this script fetches all senders in one
    Apple Event call and filters them in the script.
    """
    start_str = start_date.strftime("%B %e, %Y %I:%M:%S %p").replace("  ", " ")
    end_str = end_date.strftime("%B %e, %Y %I:%M:%S %p").replace("  ", " ")
    email_list_items = ", ".join(f'"{e.lower()}"' for e in contact_emails)
    field_sep = _FIELD_SEP
    record_sep = _RECORD_SEP

    script = textwrap.dedent(f"""\
        on extractEmail(addr)
            if addr contains "<" then
                set AppleScript's text item delimiters to "<"
                set afterBracket to text item 2 of addr
                set AppleScript's text item delimiters to ">"
                set emailOnly to text item 1 of afterBracket
                set AppleScript's text item delimiters to ""
                return emailOnly
            end if
            return addr
        end extractEmail

        set fieldSep to "{field_sep}"
        set recordSep to "{record_sep}"
        set targetEmails to {{{email_list_items}}}
        set startDate to date "{start_str}"
        set endDate to date "{end_str}"
        set output to ""
        set diagLog to ""

        tell application "Mail"
            -- Find Exchange account
            set exchangeAcct to missing value
            repeat with acct in every account
                set acctAddr to ""
                try
                    set acctAddr to email addresses of acct as text
                end try
                if acctAddr contains "garrett@anglinlaw.net" then
                    set exchangeAcct to acct
                    exit repeat
                end if
            end repeat

            if exchangeAcct is missing value then
                return "DIAG:No Exchange account found"
            end if

            -- Get mailboxes
            set inboxMB to missing value
            set sentMB to missing value
            try
                set inboxMB to mailbox "Inbox" of exchangeAcct
            end try
            try
                set sentMB to mailbox "Sent Items" of exchangeAcct
            end try

            set boxesToSearch to {{}}
            if inboxMB is not missing value then set end of boxesToSearch to inboxMB
            if sentMB is not missing value then set end of boxesToSearch to sentMB

            if (count of boxesToSearch) is 0 then
                return "DIAG:No mailboxes found"
            end if

            set seenKeys to {{}}

            repeat with mb in boxesToSearch
                set mbName to name of mb
                set diagLog to diagLog & "Searching " & mbName & "... "

                -- Get total count
                set msgCount to count of messages of mb
                set diagLog to diagLog & msgCount & " messages. "

                -- Bulk fetch senders (single Apple Event - fast)
                set allSenders to {{}}
                try
                    set allSenders to sender of every message of mb
                end try

                if (count of allSenders) is 0 then
                    set diagLog to diagLog & "Could not bulk-fetch senders. "
                else
                    set diagLog to diagLog & "Got " & (count of allSenders) & " senders. "

                    -- Find matching indices
                    set matchCount to 0
                    set matchIndices to {{}}
                    repeat with i from 1 to count of allSenders
                        set s to item i of allSenders
                        set sEmail to my extractEmail(s)
                        repeat with targetEmail in targetEmails
                            if sEmail is equal to (contents of targetEmail) then
                                set end of matchIndices to i
                                set matchCount to matchCount + 1
                                exit repeat
                            end if
                        end repeat
                    end repeat

                    set diagLog to diagLog & matchCount & " sender matches. "

                    -- Also check if user sent TO a contact (for Sent Items)
                    -- Only do this for Sent Items mailbox
                    if mbName is "Sent Items" then
                        -- For sent items, check recipients of each message
                        -- Use bulk subject access to build keys for dedup
                        set allSubjects to {{}}
                        try
                            set allSubjects to subject of every message of mb
                        end try

                        repeat with i from 1 to msgCount
                            -- Skip if already matched by sender
                            if i is not in matchIndices then
                                try
                                    set msg to message i of mb
                                    set msgDate to date received of msg
                                    if msgDate is greater than or equal to startDate and msgDate is less than or equal to endDate then
                                        set foundRecip to false
                                        try
                                            set toRecips to every to recipient of msg
                                            repeat with r in toRecips
                                                set rAddr to address of r
                                                repeat with targetEmail in targetEmails
                                                    if rAddr is equal to (contents of targetEmail) then
                                                        set foundRecip to true
                                                        exit repeat
                                                    end if
                                                end repeat
                                                if foundRecip then exit repeat
                                            end repeat
                                        end try
                                        if foundRecip then
                                            set end of matchIndices to i
                                            set matchCount to matchCount + 1
                                        end if
                                    end if
                                end try
                            end if
                        end repeat
                        set diagLog to diagLog & "After recip check: " & matchCount & " total. "
                    end if

                    -- Get full details for matching messages
                    repeat with idx in matchIndices
                        try
                            set msg to message (contents of idx) of mb
                            set msgDate to date received of msg
                            if msgDate is greater than or equal to startDate and msgDate is less than or equal to endDate then
                                set senderAddr to sender of msg
                                set senderEmail to my extractEmail(senderAddr)
                                set msgSubject to ""
                                try
                                    set msgSubject to subject of msg
                                end try

                                set msgKey to senderEmail & "|" & msgSubject
                                if msgKey is not in seenKeys then
                                    set end of seenKeys to msgKey

                                    set msgBody to ""
                                    set recipAddrs to ""
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

                                    set wordCount to 0
                                    try
                                        set wordCount to count of words of msgBody
                                    end try

                                    set rec to dateStr & fieldSep & senderEmail & fieldSep & recipAddrs & fieldSep & msgSubject & fieldSep & (wordCount as text)
                                    if output is not "" then set output to output & recordSep
                                    set output to output & rec
                                end if
                            end if
                        end try
                    end repeat
                end if
            end repeat
        end tell

        if output is "" then
            return "DIAG:" & diagLog
        end if
        return output
    """)
    return script


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


def _parse_output(raw: str) -> list[dict]:
    """Parse the delimited output into row dicts."""
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
            parts[0], parts[1], parts[2], parts[3], parts[4],
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
# Matching
# ---------------------------------------------------------------------------


def _match_email_to_matter(
    row: dict,
    matters: list[Matter],
) -> tuple[Matter, str] | None:
    """Return (Matter, match_type) for the first matching matter, or None."""
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

    Parameters
    ----------
    start_date, end_date:
        Inclusive date range.
    matters:
        Matters to match against.  Loaded from the registry if *None*.

    Returns
    -------
    (matched, unmatched) tuple of DataFrames.
    """
    matters = matters or load_matters()

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

    # Check for diagnostic output
    if raw_output.startswith("DIAG:"):
        diag_msg = raw_output[5:]
        print(f"  Email search diagnostic: {diag_msg}")
        raw_output = ""

    raw_rows = _parse_output(raw_output)

    # De-duplicate
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
