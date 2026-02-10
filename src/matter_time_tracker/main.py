"""Main entry point for the Matter Time Tracker."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from matter_time_tracker.config import get_matters_registry_path, get_output_dir
from matter_time_tracker.registry import load_matters


def list_matters() -> None:
    """Print all matters in the registry."""
    path = get_matters_registry_path()
    matters = load_matters(path)

    if not matters:
        print("No matters found in registry.")
        return

    print(f"Matters registry: {path}\n")
    for m in matters:
        print(f"  [{m.matter_id}] {m.matter_name}")
        print(f"    Client:   {m.client_name}")
        print(f"    Contacts: {', '.join(m.contact_emails)}")
        print(f"    Keywords: {', '.join(m.subject_keywords)}")
        print()


def run_emails(start: str, end: str, output_dir: Path | None, classify: bool = False) -> None:
    """Read emails from Apple Mail, match to matters, and write CSV files."""
    from matter_time_tracker.email_reader import read_emails

    start_date = datetime.strptime(start, "%Y-%m-%d")
    end_date = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    print(f"Querying Apple Mail for emails from {start} to {end} ...")
    matched, unmatched = read_emails(start_date, end_date)

    total = len(matched) + len(unmatched)
    if total == 0:
        print("No matching emails found in the specified date range.")
        return

    print(f"Found {total} email(s): {len(matched)} rules-matched, {len(unmatched)} unmatched.")

    if classify and not unmatched.empty:
        from matter_time_tracker.classifier import classify_unmatched_emails

        print(f"\nClassifying {len(unmatched)} unmatched email(s) via Anthropic API ...")
        ai_matched, unmatched = classify_unmatched_emails(unmatched)
        if not ai_matched.empty:
            matched = pd.concat([matched, ai_matched], ignore_index=True)
            print(f"  AI classified: {len(ai_matched)} email(s) matched, "
                  f"{len(unmatched)} still unmatched.")

    out = output_dir or get_output_dir()
    matched_path = out / f"matched_emails_{start}_to_{end}.csv"
    unmatched_path = out / f"unmatched_emails_{start}_to_{end}.csv"

    matched.to_csv(matched_path, index=False)
    unmatched.to_csv(unmatched_path, index=False)

    print(f"\nMatched:   {len(matched)} email(s) → {matched_path}")
    print(f"Unmatched: {len(unmatched)} email(s) → {unmatched_path}")

    if not matched.empty:
        print("\n--- Matched emails summary ---")
        for matter_id in matched["matter_id"].unique():
            subset = matched[matched["matter_id"] == matter_id]
            total_hrs = subset["estimated_hours"].sum()
            print(f"  [{matter_id}] {subset.iloc[0]['matter_name']}")
            print(f"    {len(subset)} email(s), {total_hrs:.1f} estimated hours")

    if not unmatched.empty:
        print(f"\n--- {len(unmatched)} unmatched email(s) ---")
        for _, row in unmatched.iterrows():
            print(f"  {row['date']}  {row['sender']}  \"{row['subject']}\"  ({row['word_count']}w)")


def run_report(
    start: str,
    end: str,
    output_file: str | None,
    backend: str,
    classify: bool,
    review: bool,
    output_dir: Path | None,
) -> None:
    """Full pipeline: calendar + email ingestion, optional AI classify, report."""
    from matter_time_tracker.calendar_reader import read_events
    from matter_time_tracker.email_reader import read_emails
    from matter_time_tracker.matcher import match_events
    from matter_time_tracker.reporter import generate_excel, generate_text, merge_entries

    start_date = datetime.strptime(start, "%Y-%m-%d")
    end_date = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    out = output_dir or get_output_dir()

    step_total = 5 if review else 4

    # ---- Calendar events ----
    print(f"[1/{step_total}] Reading calendar events from {start} to {end} ...")
    try:
        events = read_events(start_date, end_date, backend=backend)
    except Exception as exc:
        print(f"  Calendar read failed ({exc}); skipping calendar events.")
        events = pd.DataFrame()

    matched_events = pd.DataFrame()
    unmatched_events = pd.DataFrame()
    if not events.empty:
        matched_events, unmatched_events = match_events(events)
        print(f"  {len(events)} event(s): {len(matched_events)} matched, "
              f"{len(unmatched_events)} unmatched.")

        if classify and not unmatched_events.empty:
            from matter_time_tracker.classifier import classify_unmatched_events

            print(f"  Classifying {len(unmatched_events)} unmatched event(s) via API ...")
            ai_events, unmatched_events = classify_unmatched_events(unmatched_events)
            if not ai_events.empty:
                matched_events = pd.concat([matched_events, ai_events], ignore_index=True)
                print(f"  AI classified: {len(ai_events)} event(s) matched.")
    else:
        print("  No calendar events found.")

    # ---- Emails ----
    print(f"\n[2/{step_total}] Querying Apple Mail for emails from {start} to {end} ...")
    try:
        matched_emails, unmatched_emails = read_emails(start_date, end_date)
    except Exception as exc:
        print(f"  Email read failed ({exc}); skipping emails.")
        matched_emails = pd.DataFrame()
        unmatched_emails = pd.DataFrame()

    email_total = len(matched_emails) + len(unmatched_emails)
    if email_total:
        print(f"  {email_total} email(s): {len(matched_emails)} matched, "
              f"{len(unmatched_emails)} unmatched.")

        if classify and not unmatched_emails.empty:
            from matter_time_tracker.classifier import classify_unmatched_emails

            print(f"  Classifying {len(unmatched_emails)} unmatched email(s) via API ...")
            ai_emails, unmatched_emails = classify_unmatched_emails(unmatched_emails)
            if not ai_emails.empty:
                matched_emails = pd.concat([matched_emails, ai_emails], ignore_index=True)
                print(f"  AI classified: {len(ai_emails)} email(s) matched.")
    else:
        print("  No matching emails found.")

    # ---- Merge ----
    print(f"\n[3/{step_total}] Merging entries and grouping by matter ...")
    entries = merge_entries(
        matched_events=matched_events if not matched_events.empty else None,
        matched_emails=matched_emails if not matched_emails.empty else None,
    )

    if entries.empty:
        print("No billable entries to report.")
        return

    print(f"  {len(entries)} total time entries across "
          f"{entries['matter_id'].nunique()} matter(s).")

    # ---- Interactive review (optional) ----
    if review:
        from matter_time_tracker.reviewer import review_entries

        print(f"\n[4/{step_total}] Interactive review ...")
        entries = review_entries(entries, out, start, end)

        if entries.empty:
            print("All entries were excluded. Nothing to report.")
            return

    # ---- Generate reports ----
    report_step = 5 if review else 4
    print(f"\n[{report_step}/{step_total}] Generating reports ...")
    basename = output_file or f"time_report_{start}_to_{end}"
    # Strip extension if user provided one — we generate both .xlsx and .txt
    basename = basename.removesuffix(".xlsx").removesuffix(".txt")

    xlsx_path = out / f"{basename}.xlsx"
    txt_path = out / f"{basename}.txt"

    generate_excel(entries, xlsx_path, start, end)
    generate_text(entries, txt_path, start, end)

    print(f"\n  Excel report: {xlsx_path}")
    print(f"  Text report:  {txt_path}")

    # Quick summary to terminal
    grand_total = entries["hours"].sum()
    print(f"\n--- Report summary ---")
    for mid in entries["matter_id"].unique():
        sub = entries[entries["matter_id"] == mid]
        print(f"  [{mid}] {sub.iloc[0]['matter_name']}: {sub['hours'].sum():.1f}h")
    print(f"  Grand total: {grand_total:.1f}h")


def run_events(
    start: str, end: str, backend: str, output_dir: Path | None, classify: bool = False,
) -> None:
    """Read calendar events, match to matters, and write CSV files."""
    from matter_time_tracker.calendar_reader import read_events
    from matter_time_tracker.matcher import match_events

    start_date = datetime.strptime(start, "%Y-%m-%d")
    # End date is inclusive — set to end of day
    end_date = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    print(f"Reading calendar events from {start} to {end} (backend={backend}) ...")
    events = read_events(start_date, end_date, backend=backend)

    if events.empty:
        print("No events found in the specified date range.")
        return

    print(f"Found {len(events)} event(s). Matching against matters registry ...")
    matched, unmatched = match_events(events)

    print(f"Rules-matched: {len(matched)}, unmatched: {len(unmatched)}.")

    if classify and not unmatched.empty:
        from matter_time_tracker.classifier import classify_unmatched_events

        print(f"\nClassifying {len(unmatched)} unmatched event(s) via Anthropic API ...")
        ai_matched, unmatched = classify_unmatched_events(unmatched)
        if not ai_matched.empty:
            matched = pd.concat([matched, ai_matched], ignore_index=True)
            print(f"  AI classified: {len(ai_matched)} event(s) matched, "
                  f"{len(unmatched)} still unmatched.")

    out = output_dir or get_output_dir()
    matched_path = out / f"matched_events_{start}_to_{end}.csv"
    unmatched_path = out / f"unmatched_events_{start}_to_{end}.csv"

    matched.to_csv(matched_path, index=False)
    unmatched.to_csv(unmatched_path, index=False)

    print(f"\nMatched:   {len(matched)} event(s) → {matched_path}")
    print(f"Unmatched: {len(unmatched)} event(s) → {unmatched_path}")

    if not matched.empty:
        print("\n--- Matched events summary ---")
        for matter_id in matched["matter_id"].unique():
            subset = matched[matched["matter_id"] == matter_id]
            total_hrs = subset["duration_hours"].sum()
            print(f"  [{matter_id}] {subset.iloc[0]['matter_name']}")
            print(f"    {len(subset)} event(s), {total_hrs:.1f} total hours")

    if not unmatched.empty:
        print(f"\n--- {len(unmatched)} unmatched event(s) ---")
        for _, row in unmatched.iterrows():
            print(f"  {row['start_time']}  {row['event_title']}  ({row['duration_hours']}h)")


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="matter-tracker",
        description="Track time spent on legal matters.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="List all matters in the registry")
    subparsers.add_parser("info", help="Show configuration paths and status")

    events_parser = subparsers.add_parser(
        "events",
        help="Read calendar events, match to matters, and export to CSV",
    )
    events_parser.add_argument(
        "--start",
        required=True,
        help="Start date (inclusive), format YYYY-MM-DD",
    )
    events_parser.add_argument(
        "--end",
        required=True,
        help="End date (inclusive), format YYYY-MM-DD",
    )
    events_parser.add_argument(
        "--backend",
        choices=["auto", "eventkit", "ics"],
        default="auto",
        help="Calendar backend: eventkit (PyObjC), ics (parse files), auto (default)",
    )
    events_parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for CSV output (defaults to OUTPUT_DIR or ./output)",
    )
    events_parser.add_argument(
        "--classify",
        action="store_true",
        default=False,
        help="Use the Anthropic API to classify unmatched events (uses API credits)",
    )

    emails_parser = subparsers.add_parser(
        "emails",
        help="Read emails from Apple Mail, match to matters, and export to CSV",
    )
    emails_parser.add_argument(
        "--start",
        required=True,
        help="Start date (inclusive), format YYYY-MM-DD",
    )
    emails_parser.add_argument(
        "--end",
        required=True,
        help="End date (inclusive), format YYYY-MM-DD",
    )
    emails_parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for CSV output (defaults to OUTPUT_DIR or ./output)",
    )
    emails_parser.add_argument(
        "--classify",
        action="store_true",
        default=False,
        help="Use the Anthropic API to classify unmatched emails (uses API credits)",
    )

    report_parser = subparsers.add_parser(
        "report",
        help="Full pipeline: ingest calendar + email, match, classify, and generate report",
    )
    report_parser.add_argument(
        "--start",
        required=True,
        help="Start date (inclusive), format YYYY-MM-DD",
    )
    report_parser.add_argument(
        "--end",
        required=True,
        help="End date (inclusive), format YYYY-MM-DD",
    )
    report_parser.add_argument(
        "--output",
        default=None,
        help="Base filename for reports (without extension; .xlsx and .txt are generated)",
    )
    report_parser.add_argument(
        "--backend",
        choices=["auto", "eventkit", "ics"],
        default="auto",
        help="Calendar backend (default: auto)",
    )
    report_parser.add_argument(
        "--classify",
        action="store_true",
        default=False,
        help="Use the Anthropic API to classify unmatched items (uses API credits)",
    )
    report_parser.add_argument(
        "--review",
        action="store_true",
        default=False,
        help="Interactively review each entry before generating the report",
    )
    report_parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for report output (defaults to OUTPUT_DIR or ./output)",
    )

    args = parser.parse_args(argv)

    if args.command == "list":
        list_matters()
    elif args.command == "info":
        print(f"Matters registry: {get_matters_registry_path()}")
        print(f"Output directory:  {get_output_dir()}")
    elif args.command == "events":
        run_events(args.start, args.end, args.backend, args.output_dir, args.classify)
    elif args.command == "emails":
        run_emails(args.start, args.end, args.output_dir, args.classify)
    elif args.command == "report":
        run_report(
            args.start, args.end, args.output, args.backend,
            args.classify, args.review, args.output_dir,
        )
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
