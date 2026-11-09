# -*- coding: utf-8 -*-
"""Report retention for the Hotel RMS daily CSV exports.

Soft-delete policy: report files whose date is outside the newest ``keep``
distinct report dates are MOVED into ``archive/``. Nothing is deleted,
zipped, or sent to the recycle bin, so a report is recovered simply by
moving it back into the folder.

Design constraints:
  * Group files by the ISO date parsed from the FILE NAME. Never use mtime,
    never count files, never use calendar days. One daily report is one date:
    the Live_Dashboard plus the RMS_Manager_View, together with any
    _HHMMSS fallback files written while Excel holds a file open.
  * Strict whitelist regex only; no glob and no broad "*.csv" patterns.
  * Expected filesystem errors (PermissionError / WinError 32) are skipped
    with a WARN; the source file is left in place and the pricing cycle is
    never interrupted.
  * Standard library only, and no import of ``hotel rms``: that module runs
    the whole pricing engine at import time. This module has no side effects
    on import.
  * Environment variables are read only when the corresponding argument is
    None, so verification can pass explicit values instead.

All code, comments, docstrings and returned messages are English, matching
the convention documented in "hotel rms.py".
"""

from __future__ import annotations

import os
import re
from collections import namedtuple
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_KEEP = 3
KEEP_ENV = "RMS_REPORT_KEEP"
DRY_RUN_ENV = "RMS_REPORT_RETENTION_DRY_RUN"
ARCHIVE_DIRNAME = "archive"
TRUE_VALUES = frozenset(("1", "true", "yes", "on"))

# Anchored whitelist. The "hotel_name_" prefix is deliberately strict: the
# writer in "hotel rms.py" only ever produces this prefix. A manually added
# report with a different prefix is therefore left alone, which is the safe
# direction (never archive a file we cannot prove we created).
REPORT_RE = re.compile(
    r"^hotel_name_(Live_Dashboard|RMS_Manager_View)"
    r"_(\d{4}-\d{2}-\d{2})(_\d{6})?\.csv$"
)

# Indirection so verification can simulate a locked file by patching this
# name instead of the global os module.
_move = os.replace

RetentionPlan = namedtuple(
    "RetentionPlan",
    "output_dir archive_dir today keep dates future_dates keep_dates "
    "to_archive conflicts messages",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_report_date(filename):
    """Return the report date for a whitelisted file name, else None."""
    match = REPORT_RE.match(filename)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(2), "%Y-%m-%d").date()
    except ValueError:
        return None


def _read_keep():
    raw = os.environ.get(KEEP_ENV, "").strip()
    if not raw:
        return DEFAULT_KEEP, []
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_KEEP, [
            ("WARN", "{} must be a positive integer, got {!r}; using {}.".format(
                KEEP_ENV, raw, DEFAULT_KEEP))
        ]
    if value < 1:
        return DEFAULT_KEEP, [
            ("WARN", "{} must be >= 1, got {}; using {}.".format(
                KEEP_ENV, value, DEFAULT_KEEP))
        ]
    return value, []


def _read_dry_run():
    return os.environ.get(DRY_RUN_ENV, "").strip().lower() in TRUE_VALUES


# ---------------------------------------------------------------------------
# Read-only planning
# ---------------------------------------------------------------------------
def plan_retention(output_dir, today, keep=DEFAULT_KEEP):
    """Compute what SHOULD be archived. Never creates dirs or moves files."""
    messages = []
    if isinstance(today, datetime):
        today = today.date()
    if not isinstance(today, date):
        raise TypeError("today must be a datetime.date, got {!r}.".format(type(today)))
    if keep is None or keep < 1:
        keep = DEFAULT_KEEP

    try:
        names = sorted(os.listdir(output_dir))  # one level only; never recursive
    except OSError as exc:
        messages.append(("WARN", "cannot list {!r} ({}); retention skipped.".format(
            output_dir, exc)))
        names = []

    found = []
    for name in names:
        match = REPORT_RE.match(name)
        if not match:
            # archive/, Public holidays.csv, logs, foreign CSVs, .bak files.
            continue
        if not os.path.isfile(os.path.join(output_dir, name)):
            # A directory masquerading as a CSV file name.
            continue
        try:
            report_date = datetime.strptime(match.group(2), "%Y-%m-%d").date()
        except ValueError:
            messages.append(("WARN", "invalid date in file name {!r}; skipped.".format(
                name)))
            continue
        found.append((report_date, name))
    # NOTE: getmtime is deliberately never used.

    future_dates = sorted({d for d, _ in found if d > today})
    if future_dates:
        messages.append((
            "WARN",
            "{} report date(s) are in the future relative to {} ({}); they are "
            "NOT counted and NOT archived.".format(
                len(future_dates), today.isoformat(),
                ", ".join(d.isoformat() for d in future_dates[:5])),
        ))

    candidates = [(d, n) for d, n in found if d <= today]
    dates = sorted({d for d, _ in candidates}, reverse=True)
    keep_dates = set(dates[:keep])
    to_archive = sorted((d, n) for d, n in candidates if d not in keep_dates)

    archive_dir = os.path.join(output_dir, ARCHIVE_DIRNAME)
    conflicts = []
    if os.path.isdir(archive_dir):
        conflicts = [
            n for _, n in to_archive
            if os.path.exists(os.path.join(archive_dir, n))
        ]

    messages.append((
        "INFO",
        "keep={} today={} | report dates (<= today)={} | keep set={} | "
        "to archive={} file(s) | future={} | conflicts={}".format(
            keep, today.isoformat(),
            [d.isoformat() for d in dates],
            [d.isoformat() for d in sorted(keep_dates, reverse=True)],
            len(to_archive), len(future_dates), len(conflicts)),
    ))
    return RetentionPlan(
        output_dir, archive_dir, today, keep, dates, future_dates,
        keep_dates, to_archive, conflicts, messages,
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def apply_retention(output_dir, today, *, keep=None, dry_run=None):
    """Move out-of-window reports into archive/. Return [(level, message)].

    Never raises for expected filesystem conditions. The caller still wraps
    this call as a final safety net.
    """
    lines = []
    if keep is None:
        keep, notes = _read_keep()
        lines.extend(notes)
    if dry_run is None:
        dry_run = _read_dry_run()
    if dry_run:
        lines.append(("INFO", "DRY-RUN active ({}): no file will be moved and "
                              "archive/ will not be created.".format(DRY_RUN_ENV)))

    try:
        plan = plan_retention(output_dir, today, keep=keep)
    except Exception as exc:  # defensive: never break a pricing cycle
        lines.append(("WARN", "planning failed ({}); nothing moved.".format(exc)))
        return lines
    lines.extend(plan.messages)

    if not plan.to_archive:
        lines.append(("OK", "nothing to archive; {} report date(s) remain in the "
                            "folder.".format(len(plan.dates))))
        return lines

    if dry_run:
        for _, name in plan.to_archive:
            lines.append(("INFO", "[DRY-RUN] would move {} -> archive/{}.".format(
                name, name)))
        for name in plan.conflicts:
            lines.append(("WARN", "[DRY-RUN] archive/{} already exists; would "
                                  "SKIP {}.".format(name, name)))
        lines.append(("OK", "[DRY-RUN] {} file(s) would be moved; nothing "
                            "changed.".format(len(plan.to_archive))))
        return lines

    archive_dir = plan.archive_dir
    try:
        os.makedirs(archive_dir, exist_ok=True)
    except OSError as exc:
        lines.append(("WARN", "cannot create {!r} ({}); retention aborted, "
                              "nothing moved.".format(archive_dir, exc)))
        return lines

    moved = skipped = 0
    for report_date, name in plan.to_archive:
        src = os.path.join(output_dir, name)
        dst = os.path.join(archive_dir, name)
        if os.path.exists(dst):
            # Conflict: SKIP. Never overwrite archived data, never invent
            # suffixes, never delete the source.
            skipped += 1
            lines.append(("WARN", "archive/{} already exists; SKIPPED (source "
                                  "left in place, resolve manually).".format(name)))
            continue
        try:
            _move(src, dst)  # atomic on the same volume (archive is a subdir)
            moved += 1
            lines.append(("OK", "archived {} (report date {}).".format(
                name, report_date.isoformat())))
        except OSError as exc:  # includes PermissionError / WinError 32
            skipped += 1
            lines.append(("WARN", "could not archive {} ({}); left in "
                                  "place.".format(name, exc)))

    try:
        remaining = len(plan_retention(output_dir, today, keep=keep).dates)
    except Exception:
        remaining = len(plan.keep_dates)
    lines.append(("OK", "retention done: moved={} skipped={} | {} report "
                        "date(s) still in the folder.".format(
                            moved, skipped, remaining)))
    return lines
