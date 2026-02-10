"""Reporting module — merge calendar and email data, generate Excel and text reports.

Takes matched calendar-event and email DataFrames, normalises them into a
unified time-entry schema, groups by matter, and produces:

* An Excel workbook (.xlsx) with one worksheet per matter plus a summary sheet.
* A plain-text report suitable for pasting into an invoice or billing system.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Unified time-entry schema
# ---------------------------------------------------------------------------

ENTRY_COLUMNS = [
    "date",
    "matter_id",
    "matter_name",
    "client_name",
    "activity_description",
    "hours",
    "match_type",
]


# ---------------------------------------------------------------------------
# Merge calendar + email matched DataFrames
# ---------------------------------------------------------------------------

def _normalise_event_row(row: dict) -> dict:
    """Convert a matched-event row into the unified entry schema."""
    # Build an activity description from the event title
    title = row.get("event_title", "")
    location = row.get("location", "")
    desc = title
    if location:
        desc = f"{title} ({location})"

    # Derive a date string from start_time
    start = row.get("start_time")
    if isinstance(start, datetime):
        date_str = start.strftime("%Y-%m-%d")
    else:
        date_str = str(start)[:10] if start else ""

    return {
        "date": date_str,
        "matter_id": row.get("matter_id", ""),
        "matter_name": row.get("matter_name", ""),
        "client_name": row.get("client_name", ""),
        "activity_description": desc,
        "hours": row.get("duration_hours", 0.0),
        "match_type": row.get("match_type", ""),
    }


def _normalise_email_row(row: dict) -> dict:
    """Convert a matched-email row into the unified entry schema."""
    return {
        "date": row.get("date", ""),
        "matter_id": row.get("matter_id", ""),
        "matter_name": row.get("matter_name", ""),
        "client_name": row.get("client_name", ""),
        "activity_description": row.get("activity_description", ""),
        "hours": row.get("estimated_hours", 0.0),
        "match_type": row.get("match_type", ""),
    }


def merge_entries(
    matched_events: pd.DataFrame | None = None,
    matched_emails: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Merge matched events and emails into a single sorted DataFrame.

    Returns a DataFrame with columns defined in ``ENTRY_COLUMNS``, sorted
    by matter_id then date.
    """
    rows: list[dict] = []

    if matched_events is not None and not matched_events.empty:
        for _, row in matched_events.iterrows():
            rows.append(_normalise_event_row(row.to_dict()))

    if matched_emails is not None and not matched_emails.empty:
        for _, row in matched_emails.iterrows():
            rows.append(_normalise_email_row(row.to_dict()))

    if not rows:
        return pd.DataFrame(columns=ENTRY_COLUMNS)

    df = pd.DataFrame(rows, columns=ENTRY_COLUMNS)
    df["hours"] = pd.to_numeric(df["hours"], errors="coerce").fillna(0.0)
    df = df.sort_values(["matter_id", "date"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Excel generation
# ---------------------------------------------------------------------------

# Style constants
_HEADER_FONT = Font(name="Calibri", bold=True, size=11, color="FFFFFF")
_HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
_SUBTOTAL_FONT = Font(name="Calibri", bold=True, size=11)
_SUBTOTAL_FILL = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")
_TITLE_FONT = Font(name="Calibri", bold=True, size=13)
_THIN_BORDER = Border(
    bottom=Side(style="thin", color="B0B0B0"),
)
_BODY_FONT = Font(name="Calibri", size=11)


def _apply_header_row(ws, col_count: int) -> None:
    """Style the first row as a header."""
    for col_idx in range(1, col_count + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN


def _auto_column_widths(ws, min_width: float = 10, max_width: float = 55) -> None:
    """Set column widths based on content, clamped to [min, max]."""
    for col_cells in ws.columns:
        col_letter = get_column_letter(col_cells[0].column)
        best = min_width
        for cell in col_cells:
            if cell.value is not None:
                length = len(str(cell.value)) + 2
                if length > best:
                    best = length
        ws.column_dimensions[col_letter].width = min(best, max_width)


def generate_excel(
    entries: pd.DataFrame,
    output_path: Path,
    start_date: str = "",
    end_date: str = "",
) -> Path:
    """Write a formatted Excel workbook grouped by matter.

    Parameters
    ----------
    entries:
        Merged DataFrame from ``merge_entries``.
    output_path:
        Full path for the .xlsx file.
    start_date, end_date:
        Date range strings for display in the summary header.

    Returns
    -------
    The *output_path* written to.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        # ---- Summary worksheet ----
        _write_summary_sheet(writer, entries, start_date, end_date)

        # ---- One worksheet per matter ----
        if not entries.empty:
            for matter_id in entries["matter_id"].unique():
                subset = entries[entries["matter_id"] == matter_id].copy()
                _write_matter_sheet(writer, matter_id, subset)

    return output_path


def _safe_sheet_name(matter_id: str) -> str:
    """Produce a valid Excel sheet name (max 31 chars, no special chars)."""
    name = matter_id.replace("/", "-").replace("\\", "-")
    name = name.replace("[", "(").replace("]", ")").replace("*", "")
    name = name.replace("?", "").replace(":", "-")
    return name[:31]


def _write_summary_sheet(
    writer: pd.ExcelWriter,
    entries: pd.DataFrame,
    start_date: str,
    end_date: str,
) -> None:
    """Create the Summary worksheet."""
    wb = writer.book
    ws = wb.create_sheet("Summary", 0)

    # Title row
    ws.cell(row=1, column=1, value="Time Report").font = _TITLE_FONT
    if start_date and end_date:
        ws.cell(row=2, column=1, value=f"Period: {start_date} to {end_date}").font = _BODY_FONT

    # Build summary table starting at row 4
    header_row = 4
    headers = ["Matter ID", "Matter Name", "Client", "Total Hours"]
    for ci, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=ci, value=h)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN

    if entries.empty:
        ws.cell(row=header_row + 1, column=1, value="No billable entries found.").font = _BODY_FONT
        _auto_column_widths(ws)
        return

    summary = (
        entries.groupby(["matter_id", "matter_name", "client_name"], sort=False)["hours"]
        .sum()
        .reset_index()
    )
    summary = summary.sort_values("matter_id")

    data_start = header_row + 1
    for ri, (_, srow) in enumerate(summary.iterrows()):
        r = data_start + ri
        ws.cell(row=r, column=1, value=srow["matter_id"]).font = _BODY_FONT
        ws.cell(row=r, column=2, value=srow["matter_name"]).font = _BODY_FONT
        ws.cell(row=r, column=3, value=srow["client_name"]).font = _BODY_FONT
        hrs_cell = ws.cell(row=r, column=4, value=round(srow["hours"], 1))
        hrs_cell.font = _BODY_FONT
        hrs_cell.number_format = "0.0"
        for ci in range(1, 5):
            ws.cell(row=r, column=ci).border = _THIN_BORDER

    # Grand total row
    total_row = data_start + len(summary)
    ws.cell(row=total_row, column=3, value="Grand Total").font = _SUBTOTAL_FONT
    gt_cell = ws.cell(row=total_row, column=4, value=round(summary["hours"].sum(), 1))
    gt_cell.font = _SUBTOTAL_FONT
    gt_cell.number_format = "0.0"
    for ci in range(1, 5):
        ws.cell(row=total_row, column=ci).fill = _SUBTOTAL_FILL

    _auto_column_widths(ws)


def _write_matter_sheet(
    writer: pd.ExcelWriter,
    matter_id: str,
    subset: pd.DataFrame,
) -> None:
    """Create a worksheet for a single matter."""
    wb = writer.book
    sheet_name = _safe_sheet_name(matter_id)
    ws = wb.create_sheet(sheet_name)

    matter_name = subset.iloc[0]["matter_name"] if not subset.empty else ""
    client_name = subset.iloc[0]["client_name"] if not subset.empty else ""

    # Title rows
    ws.cell(row=1, column=1, value=f"{matter_id} — {matter_name}").font = _TITLE_FONT
    ws.cell(row=2, column=1, value=f"Client: {client_name}").font = _BODY_FONT

    # Data table headers at row 4
    header_row = 4
    headers = ["Date", "Activity Description", "Hours"]
    for ci, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=ci, value=h)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN

    data_start = header_row + 1
    for ri, (_, erow) in enumerate(subset.iterrows()):
        r = data_start + ri
        date_cell = ws.cell(row=r, column=1, value=erow["date"])
        date_cell.font = _BODY_FONT
        date_cell.number_format = "YYYY-MM-DD"
        ws.cell(row=r, column=2, value=erow["activity_description"]).font = _BODY_FONT
        hrs_cell = ws.cell(row=r, column=3, value=round(erow["hours"], 1))
        hrs_cell.font = _BODY_FONT
        hrs_cell.number_format = "0.0"
        for ci in range(1, 4):
            ws.cell(row=r, column=ci).border = _THIN_BORDER

    # Subtotal row
    subtotal_row = data_start + len(subset)
    ws.cell(row=subtotal_row, column=2, value="Subtotal").font = _SUBTOTAL_FONT
    st_cell = ws.cell(row=subtotal_row, column=3, value=round(subset["hours"].sum(), 1))
    st_cell.font = _SUBTOTAL_FONT
    st_cell.number_format = "0.0"
    for ci in range(1, 4):
        ws.cell(row=subtotal_row, column=ci).fill = _SUBTOTAL_FILL

    # Column widths: Date=12, Activity=55, Hours=10
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 60
    ws.column_dimensions["C"].width = 10


# ---------------------------------------------------------------------------
# Plain-text generation
# ---------------------------------------------------------------------------

def generate_text(
    entries: pd.DataFrame,
    output_path: Path,
    start_date: str = "",
    end_date: str = "",
) -> Path:
    """Write a plain-text time report suitable for invoice / billing paste.

    Returns the *output_path* written to.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("TIME REPORT")
    if start_date and end_date:
        lines.append(f"Period: {start_date} to {end_date}")
    lines.append("=" * 72)
    lines.append("")

    if entries.empty:
        lines.append("No billable entries found.")
        output_path.write_text("\n".join(lines))
        return output_path

    grand_total = 0.0

    for matter_id in entries["matter_id"].unique():
        subset = entries[entries["matter_id"] == matter_id]
        matter_name = subset.iloc[0]["matter_name"]
        client_name = subset.iloc[0]["client_name"]
        matter_total = subset["hours"].sum()
        grand_total += matter_total

        lines.append(f"{matter_id} — {matter_name}")
        lines.append(f"Client: {client_name}")
        lines.append("-" * 72)

        for _, erow in subset.iterrows():
            hrs = f"{erow['hours']:.1f}"
            lines.append(f"  {erow['date']}  {erow['activity_description']:<50s}  {hrs:>5s}h")

        lines.append(f"{'':>54s}  Subtotal: {matter_total:>5.1f}h")
        lines.append("")

    lines.append("=" * 72)
    lines.append(f"{'':>54s}  GRAND TOTAL: {grand_total:.1f}h")
    lines.append("")

    output_path.write_text("\n".join(lines))
    return output_path
