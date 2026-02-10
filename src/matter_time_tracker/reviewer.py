"""Interactive terminal-based review for time entries.

Presents each time entry grouped by matter and accepts keyboard commands
to approve, adjust, reassign, or exclude individual entries.  The reviewed
result is persisted to a JSON file so subsequent report runs can skip the
review step.

Keyboard commands (shown at each prompt):
    Enter   — approve the entry as-is
    0.0–99  — set new hours value (e.g. "1.5")
    d       — edit the activity description
    m       — move (reassign) to a different matter
    x       — exclude the entry from the report
    s       — skip (keep entry unchanged, mark as skipped)
    q       — quit review early and save progress so far
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from matter_time_tracker.registry import load_matters, Matter
from matter_time_tracker.reporter import ENTRY_COLUMNS

# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------

_REVIEW_COLUMNS = ENTRY_COLUMNS + ["review_status"]


def _review_path(output_dir: Path, start: str, end: str) -> Path:
    return output_dir / f"reviewed_{start}_to_{end}.json"


def save_review(
    entries: list[dict],
    output_dir: Path,
    start: str,
    end: str,
) -> Path:
    """Persist reviewed entries to a JSON file."""
    path = _review_path(output_dir, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "start_date": start,
        "end_date": end,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "entries": entries,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def load_review(output_dir: Path, start: str, end: str) -> list[dict] | None:
    """Load a previously saved review file, or return None."""
    path = _review_path(output_dir, start, end)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("entries")
    except (json.JSONDecodeError, KeyError):
        return None


def reviewed_to_dataframe(entries: list[dict]) -> pd.DataFrame:
    """Convert the reviewed entry list back into the standard DataFrame."""
    if not entries:
        return pd.DataFrame(columns=ENTRY_COLUMNS)
    df = pd.DataFrame(entries)
    # Keep only the report columns (drop review_status)
    for col in ENTRY_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[ENTRY_COLUMNS].copy()


# ---------------------------------------------------------------------------
# Terminal UI helpers
# ---------------------------------------------------------------------------

_DIVIDER = "-" * 72
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _header(text: str) -> str:
    return f"\n{_BOLD}{_CYAN}{text}{_RESET}"


def _entry_display(idx: int, total: int, entry: dict) -> str:
    lines = [
        f"  {_BOLD}[{idx}/{total}]{_RESET}  "
        f"{entry['date']}  {_BOLD}{entry['activity_description']}{_RESET}",
        f"          Hours: {_YELLOW}{entry['hours']:.1f}{_RESET}"
        f"    {_DIM}Matched by: {entry.get('match_type', '—')}{_RESET}",
    ]
    return "\n".join(lines)


def _prompt() -> str:
    print(
        f"    {_DIM}[Enter]=approve  [0-99]=set hours  [d]=edit desc  "
        f"[m]=move matter  [x]=exclude  [s]=skip  [q]=quit{_RESET}"
    )
    try:
        return input(f"    {_GREEN}>{_RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        return "q"


def _pick_matter(matters: list[Matter], current_id: str) -> Matter | None:
    """Display a numbered list of matters and let the user pick one."""
    print(f"\n    {_BOLD}Available matters:{_RESET}")
    for i, m in enumerate(matters, start=1):
        marker = " *" if m.matter_id == current_id else ""
        print(f"      {i}. [{m.matter_id}] {m.matter_name}{marker}")
    print(f"      0. Cancel")
    try:
        choice = input(f"    {_GREEN}#{_RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    try:
        n = int(choice)
    except ValueError:
        return None
    if n == 0 or n < 1 or n > len(matters):
        return None
    return matters[n - 1]


# ---------------------------------------------------------------------------
# Main review loop
# ---------------------------------------------------------------------------

def review_entries(
    entries: pd.DataFrame,
    output_dir: Path,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Run the interactive review and return the approved DataFrame.

    If a saved review file exists for this date range the user is offered the
    choice to reload it.

    The approved entries are saved to a JSON file so a subsequent ``report``
    run without ``--review`` can regenerate from the reviewed data.
    """
    # Check for an existing review
    existing = load_review(output_dir, start, end)
    if existing is not None:
        print(f"\n  Found a saved review for {start} to {end} "
              f"({len(existing)} entries).")
        try:
            choice = input(f"  {_GREEN}Load saved review? [Y/n]{_RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "y"
        if choice in ("", "y", "yes"):
            print("  Loaded saved review.")
            return reviewed_to_dataframe(existing)

    matters = load_matters()
    approved: list[dict] = []
    total = len(entries)
    quit_early = False

    grouped = entries.groupby("matter_id", sort=True)
    global_idx = 0

    for matter_id, group in grouped:
        matter_name = group.iloc[0]["matter_name"]
        client_name = group.iloc[0]["client_name"]
        print(_header(f"=== {matter_id} — {matter_name} ({client_name}) ==="))

        for _, row in group.iterrows():
            global_idx += 1
            entry = row.to_dict()
            entry["hours"] = float(entry.get("hours", 0))

            print()
            print(_entry_display(global_idx, total, entry))

            while True:
                cmd = _prompt()

                # Approve
                if cmd == "":
                    entry["review_status"] = "approved"
                    approved.append(entry)
                    print(f"    {_GREEN}Approved.{_RESET}")
                    break

                # Exclude
                if cmd.lower() == "x":
                    print(f"    {_RED}Excluded.{_RESET}")
                    break

                # Skip (keep but mark as skipped)
                if cmd.lower() == "s":
                    entry["review_status"] = "skipped"
                    approved.append(entry)
                    print(f"    {_DIM}Skipped (kept).{_RESET}")
                    break

                # Quit
                if cmd.lower() == "q":
                    # Save what we have so far
                    entry["review_status"] = "skipped"
                    approved.append(entry)
                    quit_early = True
                    break

                # Edit description
                if cmd.lower() == "d":
                    print(f"    Current: {entry['activity_description']}")
                    try:
                        new_desc = input(f"    {_GREEN}New description:{_RESET} ").strip()
                    except (EOFError, KeyboardInterrupt):
                        continue
                    if new_desc:
                        entry["activity_description"] = new_desc
                        entry["review_status"] = "modified"
                        print(f"    {_YELLOW}Description updated.{_RESET}")
                    # Show the entry again for final approval
                    print(_entry_display(global_idx, total, entry))
                    continue

                # Move to a different matter
                if cmd.lower() == "m":
                    picked = _pick_matter(matters, entry["matter_id"])
                    if picked:
                        entry["matter_id"] = picked.matter_id
                        entry["matter_name"] = picked.matter_name
                        entry["client_name"] = picked.client_name
                        entry["review_status"] = "modified"
                        print(f"    {_YELLOW}Moved to [{picked.matter_id}] "
                              f"{picked.matter_name}{_RESET}")
                    else:
                        print(f"    {_DIM}Cancelled.{_RESET}")
                    print(_entry_display(global_idx, total, entry))
                    continue

                # Try to parse as a number (set hours)
                try:
                    new_hours = float(cmd)
                    if new_hours < 0:
                        print(f"    {_RED}Hours must be >= 0.{_RESET}")
                        continue
                    entry["hours"] = round(new_hours, 1)
                    entry["review_status"] = "modified"
                    print(f"    {_YELLOW}Hours set to {entry['hours']:.1f}{_RESET}")
                    print(_entry_display(global_idx, total, entry))
                    continue
                except ValueError:
                    print(f"    {_DIM}Unknown command. Try again.{_RESET}")
                    continue

            if quit_early:
                # Append remaining entries from this group as skipped
                remaining_in_group = group.loc[group.index > row.name]
                for _, rrow in remaining_in_group.iterrows():
                    rentry = rrow.to_dict()
                    rentry["hours"] = float(rentry.get("hours", 0))
                    rentry["review_status"] = "skipped"
                    approved.append(rentry)
                break

        if quit_early:
            # Append all entries from remaining groups as skipped
            remaining_groups = False
            for mid2, group2 in grouped:
                if remaining_groups:
                    for _, rrow in group2.iterrows():
                        rentry = rrow.to_dict()
                        rentry["hours"] = float(rentry.get("hours", 0))
                        rentry["review_status"] = "skipped"
                        approved.append(rentry)
                if mid2 == matter_id:
                    remaining_groups = True
            break

    # Save
    save_path = save_review(approved, output_dir, start, end)

    approved_count = sum(1 for e in approved if e.get("review_status") == "approved")
    modified_count = sum(1 for e in approved if e.get("review_status") == "modified")
    skipped_count = sum(1 for e in approved if e.get("review_status") == "skipped")
    excluded_count = total - len(approved)

    print(f"\n{_DIVIDER}")
    print(f"Review complete: {approved_count} approved, {modified_count} modified, "
          f"{skipped_count} skipped, {excluded_count} excluded.")
    print(f"Saved to: {save_path}")

    return reviewed_to_dataframe(approved)
