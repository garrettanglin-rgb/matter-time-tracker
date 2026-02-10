"""Main entry point for the Matter Time Tracker."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

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


def run_emails(start: str, end: str, output_dir: Path | None) -> None:
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

    print(f"Found {total} email(s): {len(matched)} matched, {len(unmatched)} unmatched.")

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


def run_events(start: str, end: str, backend: str, output_dir: Path | None) -> None:
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

    args = parser.parse_args(argv)

    if args.command == "list":
        list_matters()
    elif args.command == "info":
        print(f"Matters registry: {get_matters_registry_path()}")
        print(f"Output directory:  {get_output_dir()}")
    elif args.command == "events":
        run_events(args.start, args.end, args.backend, args.output_dir)
    elif args.command == "emails":
        run_emails(args.start, args.end, args.output_dir)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
