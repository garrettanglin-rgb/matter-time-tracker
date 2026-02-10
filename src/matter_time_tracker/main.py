"""Main entry point for the Matter Time Tracker."""

import argparse
import sys

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


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="matter-tracker",
        description="Track time spent on legal matters.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="List all matters in the registry")
    subparsers.add_parser("info", help="Show configuration paths and status")

    args = parser.parse_args(argv)

    if args.command == "list":
        list_matters()
    elif args.command == "info":
        print(f"Matters registry: {get_matters_registry_path()}")
        print(f"Output directory:  {get_output_dir()}")
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
