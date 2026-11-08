# -*- coding: utf-8 -*-
"""Shared timezone settings for the Hotel RMS.

The staff UI, the pricing engine and the unattended scheduler all read one
JSON file next to this module. The UI is the only writer, so there is a single
source of truth and no command-line arguments have to be threaded through the
three launch paths.

This module uses the standard library only. ``None`` (follow the machine's
local time) needs no timezone database at all. A named IANA zone needs the
system IANA database, or the ``tzdata`` package on Windows.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 and older
    ZoneInfo = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "timezone_settings.json")
CONFIG_TMP_FILE = CONFIG_FILE + ".tmp"
SCHEMA_VERSION = 1

LOCAL_LABEL = "Follow local system time"

# A short, general-purpose list of options. It is a list of *choices*, not a
# statement about where any property is located.
TIMEZONE_CHOICES = [
    (None, LOCAL_LABEL),
    ("UTC", "UTC"),
    ("Asia/Shanghai", "Asia/Shanghai"),
    ("Asia/Singapore", "Asia/Singapore"),
    ("Asia/Hong_Kong", "Asia/Hong_Kong"),
    ("Asia/Taipei", "Asia/Taipei"),
    ("Asia/Tokyo", "Asia/Tokyo"),
    ("Asia/Seoul", "Asia/Seoul"),
    ("Asia/Bangkok", "Asia/Bangkok"),
    ("Asia/Jakarta", "Asia/Jakarta"),
    ("Asia/Kuala_Lumpur", "Asia/Kuala_Lumpur"),
    ("Asia/Manila", "Asia/Manila"),
    ("Asia/Dubai", "Asia/Dubai"),
    ("Asia/Kolkata", "Asia/Kolkata"),
    ("Europe/London", "Europe/London"),
    ("Europe/Paris", "Europe/Paris"),
    ("Europe/Berlin", "Europe/Berlin"),
    ("America/New_York", "America/New_York"),
    ("America/Chicago", "America/Chicago"),
    ("America/Los_Angeles", "America/Los_Angeles"),
    ("America/Sao_Paulo", "America/Sao_Paulo"),
    ("Australia/Sydney", "Australia/Sydney"),
    ("Australia/Perth", "Australia/Perth"),
    ("Pacific/Auckland", "Pacific/Auckland"),
]


def _local_tzinfo():
    """Return the machine's local tzinfo, including its current UTC offset."""
    return datetime.now().astimezone().tzinfo


def tz_available(name):
    """Return True only when the named zone can actually be constructed.

    The IANA database may be missing even when ``zoneinfo`` imports fine, so
    the zone is instantiated here instead of trusting any cached name list.
    """
    if name is None or name == "":
        return True
    if ZoneInfo is None:
        return False
    try:
        ZoneInfo(name)
    except Exception:
        return False
    return True


def _tzinfo(name):
    """Resolve a name to tzinfo, falling back to local time if unavailable."""
    if not name or ZoneInfo is None:
        return _local_tzinfo()
    try:
        return ZoneInfo(name)
    except Exception:
        return _local_tzinfo()


def _format_offset(offset):
    total = int((offset or timedelta(0)).total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total), 60)
    suffix = ":{:02d}".format(minutes) if minutes else ""
    return "UTC{}{}{}".format(sign, hours, suffix)


def format_utc_offset(name=None):
    """Return the current UTC offset, e.g. UTC+8, UTC-4 or UTC+5:30."""
    try:
        return _format_offset(datetime.now(_tzinfo(name)).utcoffset())
    except Exception:
        return _format_offset(datetime.now().astimezone().utcoffset())


def format_clock(moment):
    """Format an aware datetime with its UTC offset for the staff UI."""
    return "{} ({})".format(
        moment.strftime("%Y-%m-%d %H:%M"), _format_offset(moment.utcoffset())
    )


def choice_label(value):
    """Human-readable combobox entry for one timezone choice."""
    base = LOCAL_LABEL if value is None else str(value)
    if value is not None and not tz_available(value):
        return "{} (timezone data unavailable)".format(base)
    return "{} ({})".format(base, format_utc_offset(value))


def choice_labels():
    return [choice_label(value) for value, _ in TIMEZONE_CHOICES]


def choice_values():
    return [value for value, _ in TIMEZONE_CHOICES]


def load_timezone():
    """Return a usable IANA name, or None for follow-local-system-time.

    Any missing, malformed or unusable value degrades to local time so that a
    bad setting can never stop a pricing run or the scheduler.
    """
    if not os.path.isfile(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        name = payload.get("timezone")
    except Exception:
        return None
    if name is None or name == "" or name == "local":
        return None
    if not isinstance(name, str) or not tz_available(name):
        return None
    return name


def save_timezone(name):
    """Atomically persist the choice; readers never see a partial file."""
    if name is not None and not tz_available(name):
        raise ValueError("Timezone data is not available for {}".format(name))
    payload = {"timezone": name, "schema_version": SCHEMA_VERSION}
    with open(CONFIG_TMP_FILE, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(CONFIG_TMP_FILE, CONFIG_FILE)


def config_mtime():
    """Return the config file's mtime, or None when it does not exist."""
    try:
        return os.path.getmtime(CONFIG_FILE)
    except OSError:
        return None


def resolve_now(name=None, override=None):
    """Return an aware datetime in the selected timezone.

    ``override`` is an optional wall-clock timestamp used only for repeatable
    back-testing. It is never read during normal operation.
    """
    tzinfo = _tzinfo(name)
    if override:
        text = str(override).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                "RMS_AS_OF_DATETIME must use YYYY-MM-DD HH:MM:SS, got '{}'.".format(
                    override
                )
            ) from exc
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=tzinfo)
        return parsed.astimezone(tzinfo)
    return datetime.now(tzinfo)


def parse_property_now(raw, name=None, fallback=None):
    """Interpret a PMS ``property_now`` value in the selected timezone.

    A timestamp without an offset is treated as wall-clock time in the
    selected timezone. A timestamp with an offset is converted to it. Returns
    ``(aware_datetime, warning_or_none)`` and falls back instead of raising.
    """
    tzinfo = _tzinfo(name)
    if fallback is None:
        fallback = resolve_now(name)
    if raw is None or not str(raw).strip():
        return fallback, "property_now is missing; using the current property time"
    text = str(raw).strip().replace("Z", "+00:00")
    parsed = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        return (
            fallback,
            "property_now is not a recognised timestamp; using the current property time",
        )
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tzinfo), None
    return parsed.astimezone(tzinfo), None


def local_time_for(name, hour=6, minute=0):
    """Return HH:MM in machine-local time for a wall-clock time in the zone.

    ``schedule`` accepts named timezones only as pytz objects, which would add
    a dependency. Converting to a local wall-clock time keeps the standard
    library as the only requirement. Call it again periodically so daylight
    saving transitions are picked up.
    """
    fallback = "{:02d}:{:02d}".format(hour, minute)
    if not name or ZoneInfo is None:
        return fallback
    try:
        zone = ZoneInfo(name)
    except Exception:
        return fallback
    try:
        now_local = datetime.now().astimezone()
        property_date = now_local.astimezone(zone).date()
        target = datetime(
            property_date.year, property_date.month, property_date.day,
            hour, minute, tzinfo=zone,
        )
        return target.astimezone(now_local.tzinfo).strftime("%H:%M")
    except Exception:
        return fallback
