# -*- coding: utf-8 -*-
"""Public holiday / special event calendar loader for the Hotel RMS.

The calendar is a plain UTF-8 CSV maintained by hotel staff. It is the only
source of event pricing factors. The engine does NOT fall back to hard-coded
events when the file is missing or invalid: that would silently produce
untrustworthy rates.

Accepted header spellings (case, spaces, hyphens and underscores ignored):
    Date
    Event_Name / EventName / Event
    Event_Factor / Holiday_Factor / Special_Event_Factor / Factor
    Category / Event_Category
    Recurring / Repeat / Annual

Date format:
    YYYY/MM/DD   full date, the only format used by the shipped calendar
    YYYY-MM-DD   legacy ISO spelling, still accepted for older files

Recurring=Y ignores the written year and repeats the month/day every year;
otherwise the full date is priced once. There is no MM-DD shorthand, so every
row shows a complete date and no locale-dependent "month/day" notation.

This module is independent of the pricing engine so it can be tested alone.
All identifiers, comments, messages and exceptions are English.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd


CATEGORIES = ("Public Holiday", "Special Event", "School Holiday", "Custom")
DEFAULT_CATEGORY = "Special Event"
EVENT_COLUMNS = [
    "Date",
    "Event_Name",
    "Event_Factor",
    "Event_Category",
    "Event_Source",
]


class CalendarError(ValueError):
    """Raised when the event calendar is missing, malformed or unusable."""


@dataclass
class CalendarData:
    """Events inside the pricing window plus non-fatal review warnings."""

    events: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


# Canonical column name -> accepted normalised spellings.
_HEADER_MAP = {
    "date": "date",
    "eventname": "event_name",
    "event": "event_name",
    "eventfactor": "event_factor",
    "holidayfactor": "event_factor",
    "specialeventfactor": "event_factor",
    "factor": "event_factor",
    "category": "category",
    "eventcategory": "category",
    "recurring": "recurring",
    "repeat": "recurring",
    "annual": "recurring",
}


def _normalise(value: object) -> str:
    """Normalise a header or flag for tolerant comparison."""
    return re.sub(r"[\s_\-]+", "", str(value or "").strip()).lower()


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in ("y", "yes", "true", "1")


def _read_rows(path: str):
    """Return (DictReader, resolved columns, original line numbers)."""
    if not os.path.isfile(path):
        raise CalendarError(
            "Event calendar not found: {}. Create 'Public holidays.csv' next "
            "to the pricing engine; the run has been stopped so no rates are "
            "produced from an incomplete calendar.".format(path)
        )

    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        pairs = [
            (number, line)
            for number, line in enumerate(handle, start=1)
            if line.strip() and not line.lstrip().startswith("#")
        ]

    if not pairs:
        raise CalendarError(
            "Event calendar is empty (only comments or blank lines): {}".format(path)
        )

    reader = csv.DictReader([line for _, line in pairs])
    if not reader.fieldnames:
        raise CalendarError("Event calendar has no header row: {}".format(path))

    columns: dict[str, str] = {}
    for original in reader.fieldnames:
        canonical = _HEADER_MAP.get(_normalise(original))
        if canonical:
            columns[canonical] = original

    missing = [name for name in ("date", "event_factor") if name not in columns]
    if missing:
        raise CalendarError(
            "Event calendar is missing required column(s) [{}] in {}. "
            "Expected at least: Date, Event_Factor. "
            "Holiday_Factor and Special_Event_Factor are accepted aliases.".format(
                ", ".join(missing), path
            )
        )

    return reader, columns, [number for number, _ in pairs][1:]


def _parse_calendar_date(raw: object, line_no: int, path: str):
    """Return (full_date, month, day) for a YYYY/MM/DD date.

    YYYY-MM-DD is accepted as a legacy spelling so older calendars keep
    working, but every row must still carry a four-digit year. A bare MM-DD
    is rejected: it is the source of locale-dependent date displays.
    """
    text = str(raw or "").strip()
    match = re.fullmatch(r"(\d{4})/(\d{2})/(\d{2})", text) or re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})", text
    )
    if not match:
        raise CalendarError(
            "Line {} in {}: unsupported Date '{}'. Use YYYY/MM/DD "
            "(for example 2026/12/25).".format(line_no, path, text)
        )
    year, month, day = (int(part) for part in match.groups())
    try:
        parsed = date(year, month, day)
    except ValueError as exc:
        raise CalendarError(
            "Line {} in {}: invalid date '{}'.".format(line_no, path, text)
        ) from exc
    return parsed, month, day


def _category_of(raw: object, line_no: int, warnings: list[str]) -> str:
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_CATEGORY
    for category in CATEGORIES:
        if text.lower() == category.lower():
            return category
    warnings.append(
        "Line {}: unknown Category '{}' mapped to '{}'.".format(
            line_no, text, DEFAULT_CATEGORY
        )
    )
    return DEFAULT_CATEGORY


def annual_review_warnings(
    data: CalendarData, window_start, window_end
) -> list[str]:
    """Warn when a year covered by the window has no event rows at all.

    This keeps a small demonstration calendar quiet while still flagging a
    year that has not been maintained. Recurring rows are counted, so a
    calendar made only of annual holidays does not produce a warning each run.
    """
    start = pd.Timestamp(window_start).date()
    end = pd.Timestamp(window_end).date()
    covered_years = (
        set(data.events["Date"].dt.year) if not data.events.empty else set()
    )
    messages = []
    for year in range(start.year, end.year + 1):
        if year not in covered_years:
            messages.append(
                "No event rows cover {}. Add that year's holidays to "
                "'Public holidays.csv' before relying on {} pricing.".format(
                    year, year
                )
            )
    return messages


def load_event_calendar(path: str, window_start, window_end) -> CalendarData:
    """Load, validate and expand the staff-maintained event calendar.

    Returns events whose Date falls inside [window_start, window_end], with
    columns Date, Event_Name, Event_Factor, Event_Category, Event_Source.
    Recurring=Y rows use only their month/day and are expanded for every year
    touched by the window, so the written year is a placeholder. One-off rows
    override recurring rows on the same date; otherwise the last row for a
    date wins. Invalid input raises CalendarError with a line number.
    """
    start = pd.Timestamp(window_start).date()
    end = pd.Timestamp(window_end).date()
    if end < start:
        raise CalendarError("Pricing window end is before its start.")

    reader, columns, line_numbers = _read_rows(path)
    warnings: list[str] = []
    one_off_rows: list[dict] = []
    recurring_rows: list[dict] = []

    date_col = columns["date"]
    factor_col = columns["event_factor"]
    name_col = columns.get("event_name")
    category_col = columns.get("category")
    recurring_col = columns.get("recurring")

    for row, line_no in zip(reader, line_numbers):
        if not any(str(value or "").strip() for value in row.values()):
            continue

        recurring = _truthy(row.get(recurring_col, "")) if recurring_col else False
        parsed_date, month, day = _parse_calendar_date(
            row.get(date_col, ""), line_no, path
        )

        try:
            factor = float(str(row.get(factor_col, "")).strip())
        except (TypeError, ValueError) as exc:
            raise CalendarError(
                "Line {} in {}: Event_Factor '{}' is not a number.".format(
                    line_no, path, row.get(factor_col, "")
                )
            ) from exc
        if factor <= 0:
            raise CalendarError(
                "Line {} in {}: Event_Factor must be greater than 0 (got {}).".format(
                    line_no, path, factor
                )
            )
        if factor > 3.0 or factor < 0.5:
            warnings.append(
                "Line {}: Event_Factor {} is outside the usual 0.50-3.00 range.".format(
                    line_no, factor
                )
            )

        name = str(row.get(name_col, "") or "").strip() if name_col else ""
        category = _category_of(
            row.get(category_col, "") if category_col else "", line_no, warnings
        )
        record = {
            "Event_Name": name,
            "Event_Factor": factor,
            "Event_Category": category,
        }

        if recurring:
            # The written year is only a placeholder: repeat the month/day
            # for every year touched by the pricing window.
            for year in range(start.year, end.year + 1):
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue  # 02/29 on a non-leap year
                if start <= candidate <= end:
                    recurring_rows.append(
                        {
                            "Date": pd.Timestamp(candidate),
                            "Event_Source": "Recurring",
                            **record,
                        }
                    )
        elif start <= parsed_date <= end:
            one_off_rows.append(
                {
                    "Date": pd.Timestamp(parsed_date),
                    "Event_Source": "One-off",
                    **record,
                }
            )

    if not one_off_rows and not recurring_rows:
        warnings.append(
            "No event dates fall inside {} .. {}. Every day uses the neutral "
            "1.00 factor.".format(start.isoformat(), end.isoformat())
        )

    events = pd.DataFrame(recurring_rows + one_off_rows, columns=EVENT_COLUMNS)
    events["Date"] = pd.to_datetime(events["Date"])
    events["Event_Factor"] = pd.to_numeric(
        events["Event_Factor"], errors="raise"
    ).astype(float)
    if not events.empty:
        # One-off rows are appended last so they win over recurring rows;
        # drop_duplicates(keep='last') also implements "last row wins".
        events = (
            events.drop_duplicates(subset="Date", keep="last")
            .sort_values("Date")
            .reset_index(drop=True)
        )

    data = CalendarData(events=events, warnings=warnings)
    data.warnings.extend(annual_review_warnings(data, start, end))
    return data
