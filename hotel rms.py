# -*- coding: utf-8 -*-
"""
Hotel RMS - daily rate suggestion engine.

OFFLINE DECISION-SUPPORT DEMO
-----------------------------
Unless a real PMS endpoint and API key are supplied through the PMS_BASE_URL
and PMS_API_KEY environment variables, this engine runs in DEMO mode:
reservations, competitor rates and baseline ADR are synthetic. Every report
carries a Data_Mode column, and the staff UI shows a persistent warning, so
demo output cannot be mistaken for a live pricing decision.

EVENT CALENDAR
--------------
All public holiday and special event factors come from "Public holidays.csv"
in this folder. Staff maintain that file; there are NO hard-coded events in
this engine. Dates are absolute, so a stay date always receives the same event
factor no matter which day the engine is run. Missing or invalid calendar data
stops the run instead of silently producing rates from an incomplete calendar.

OUTPUT
------
Two date-stamped CSV reports: a technical view and a manager view.
All code, comments, log messages and report text are English.
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

# The scheduler launches this file by relative name; make the companion module
# importable regardless of the current working directory.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from event_calendar import CalendarError, load_event_calendar  # noqa: E402
from timezone_config import (  # noqa: E402
    format_utc_offset,
    load_timezone,
    parse_property_now,
    resolve_now,
)

# Keep console output readable on Windows terminals with a legacy code page.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ==========================================
# 0. CONFIGURATION
# ==========================================
CALENDAR_FILE = os.path.join(APP_DIR, "Public holidays.csv")

# Credentials are read from the environment, never hard-coded here.
API_KEY = os.environ.get("PMS_API_KEY", "YOUR_PMS_API_KEY_HERE")
BASE_URL = os.environ.get("PMS_BASE_URL", "https://api.example-pms.com/api/v1.3")
HEADERS = {"accept": "application/json", "x-api-key": API_KEY}

PMS_IS_LIVE = bool(API_KEY) and not API_KEY.startswith("YOUR_") and "example-pms.com" not in BASE_URL
# Even with a live PMS, the competitor feed and baseline ADR below are still
# synthetic. The label makes that limitation explicit in every report.
DATA_MODE = "LIVE_PMS_SYNTHETIC_MARKET" if PMS_IS_LIVE else "DEMO"
DATA_MODE_LABEL = {
    "DEMO": "synthetic PMS reservations, synthetic competitor rates, constant baseline ADR",
    "LIVE_PMS_SYNTHETIC_MARKET": "live PMS reservations, synthetic competitor rates, constant baseline ADR",
}[DATA_MODE]

PRICING_WINDOW_DAYS = 90
BASELINE_ADR = {"Standard": 135.0, "Premium": 160.0}
ROOM_TYPE_FACTOR = {"Standard": 1.00, "Premium": 1.25}

# Synthetic competitor feed. The value is a deterministic function of the stay
# date, so it is reproducible and comparable year over year; replace this
# function with a real rate-shopper feed when one is available.
COMPETITOR_SEED = "hotel-rms-v2"
COMPETITOR_MIN_ADR = 125.0
COMPETITOR_MAX_ADR = 175.0

# Rate guards. Event days get extra headroom, otherwise a 2.20 F1 factor is
# clipped immediately by the generic 2.00x ceiling and the calendar has no
# visible effect on the final rate.
MIN_RATE_MULTIPLIER = 0.70
MAX_RATE_MULTIPLIER = 2.00
EVENT_CAP_HEADROOM = 1.60

# Every "today / now" value in this run comes from one aware timestamp in the
# selected property timezone. The staff UI writes the choice; this process
# only reads it. With no setting, the machine's local time is used.
TZ_NAME = load_timezone()
AS_OF_DATETIME_OVERRIDE = os.environ.get("RMS_AS_OF_DATETIME", "").strip()
try:
    NOW = resolve_now(TZ_NAME, AS_OF_DATETIME_OVERRIDE or None)
except ValueError as exc:
    raise SystemExit("[ERROR] {}".format(exc)) from exc

# Optional reproducibility anchor. Set RMS_AS_OF_DATE=YYYY-MM-DD to replay a
# past pricing window for back-testing and year-over-year comparison; the whole
# run, including the synthetic demo feed, then becomes reproducible. With both
# variables unset, each daily run uses the current date in the selected
# timezone.
AS_OF_OVERRIDE = os.environ.get("RMS_AS_OF_DATE", "").strip()
if AS_OF_OVERRIDE:
    try:
        AS_OF_DATE = datetime.strptime(AS_OF_OVERRIDE, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(
            "[ERROR] RMS_AS_OF_DATE must use YYYY-MM-DD, got '{}'.".format(
                AS_OF_OVERRIDE
            )
        ) from exc
else:
    AS_OF_DATE = NOW.date()

TODAY = pd.Timestamp(AS_OF_DATE)
WINDOW_START = TODAY
WINDOW_END = TODAY + pd.Timedelta(days=PRICING_WINDOW_DAYS - 1)


def log_info(message):
    print("[INFO] {}".format(message))


def log_warn(message):
    print("[WARN] {}".format(message))


def log_ok(message):
    print("[OK] {}".format(message))


# ==========================================
# 1. PMS DASHBOARD ADAPTER (v1.3 schema)
# ==========================================
def fetch_dashboard_data():
    """Return live dashboard metrics, or clearly-labelled synthetic metrics."""
    log_info("Connecting to PMS API v1.3 for dashboard metrics...")
    try:
        if not PMS_IS_LIVE:
            raise requests.exceptions.RequestException("No PMS credentials configured")
        response = requests.get(
            "{}/getDashboard".format(BASE_URL), headers=HEADERS, timeout=15
        )
        if response.status_code != 200:
            raise requests.exceptions.RequestException(
                "HTTP {}".format(response.status_code)
            )
        data = response.json().get("data", {})
        if not data:
            raise ValueError("Empty dashboard payload")
        log_info("Dashboard payload received from the PMS.")
    except Exception as exc:
        log_warn("Dashboard unavailable ({}). Using synthetic demo metrics.".format(exc))
        data = {
            "property_now": NOW.strftime("%Y-%m-%d %H:%M:%S"),
            "roomsOccupied": 4,
            "percentageOccupied": 80,
            "inHouse": 3,
            "guestsInHouse": 8,
            "roomsBlocked": 0,
            "capacity": 5,
        }

    return {
        "capacity": data.get("capacity", 5),
        "rooms_blocked": data.get("roomsBlocked", 0),
        "in_house_rooms": data.get("inHouse", 0),
        "in_house_guests": data.get("guestsInHouse", 0),
        "live_today_occ": data.get("percentageOccupied", 0),
        "property_now_str": data.get(
            "property_now", NOW.strftime("%Y-%m-%d %H:%M:%S")
        ),
    }


DASH_METRICS = fetch_dashboard_data()
CAPACITY = DASH_METRICS["capacity"]
ROOMS_BLOCKED = DASH_METRICS["rooms_blocked"]
IN_HOUSE_ROOMS = DASH_METRICS["in_house_rooms"]
IN_HOUSE_GUESTS = DASH_METRICS["in_house_guests"]
LIVE_TODAY_OCC = DASH_METRICS["live_today_occ"]
PROPERTY_NOW, PROPERTY_NOW_WARNING = parse_property_now(
    DASH_METRICS["property_now_str"], TZ_NAME, NOW
)
if PROPERTY_NOW_WARNING:
    log_warn(PROPERTY_NOW_WARNING)
PROPERTY_NOW_STR = PROPERTY_NOW.strftime("%Y-%m-%d %H:%M:%S")


# ==========================================
# 2. PMS RESERVATION ADAPTER (v1.3 -> df_pace)
# ==========================================
def fetch_and_build_pace():
    """Expand reservations into a stay-night matrix for the pricing window."""
    log_info("Connecting to PMS API v1.3 for reservation data...")
    try:
        if not PMS_IS_LIVE:
            raise requests.exceptions.RequestException("No PMS credentials configured")
        response = requests.get(
            "{}/getReservations".format(BASE_URL), headers=HEADERS, timeout=15
        )
        if response.status_code != 200:
            raise requests.exceptions.RequestException(
                "HTTP {}".format(response.status_code)
            )
        json_data = response.json()
        log_info("Reservation payload received from the PMS.")
    except Exception as exc:
        log_warn(
            "Reservations unavailable ({}). Using synthetic demo reservations.".format(exc)
        )
        # Synthetic demo feed. These bookings are anchored to the run date, so
        # OTB and pace figures in DEMO mode differ between runs on different
        # days. Connect a live PMS for reproducible operational figures.
        mock_today = AS_OF_DATE
        json_data = [
            {"reservationID": "1001", "dateCreated": "{} 10:00:00".format(mock_today - timedelta(days=45)), "status": "confirmed", "startDate": str(mock_today + timedelta(days=5)), "endDate": str(mock_today + timedelta(days=7)), "balance": 310.00},
            {"reservationID": "1002", "dateCreated": "{} 14:32:11".format(mock_today - timedelta(days=20)), "status": "confirmed", "startDate": str(mock_today + timedelta(days=5)), "endDate": str(mock_today + timedelta(days=6)), "balance": 155.00},
            {"reservationID": "1003", "dateCreated": "{} 09:15:00".format(mock_today - timedelta(days=2)), "status": "confirmed", "startDate": str(mock_today + timedelta(days=10)), "endDate": str(mock_today + timedelta(days=12)), "balance": 290.00},
            {"reservationID": "1004", "dateCreated": "{} 18:22:00".format(mock_today - timedelta(days=380)), "status": "checked_out", "startDate": str(mock_today - timedelta(days=365) + timedelta(days=5)), "endDate": str(mock_today - timedelta(days=365) + timedelta(days=7)), "balance": 0.00},
        ]

    reservations_list = (
        json_data.get("data", json_data) if isinstance(json_data, dict) else json_data
    )
    parsed_reservations = []
    for res in sorted(reservations_list, key=lambda item: item.get("startDate", "")):
        status = str(res.get("status", "")).lower().strip()
        if status not in ("confirmed", "checked_in", "checked_out"):
            continue
        created_raw = res.get("dateCreated", "")
        created_date = (
            pd.to_datetime(created_raw).date() if created_raw else AS_OF_DATE
        )
        parsed_reservations.append(
            {
                "Reservation_ID": res.get("reservationID"),
                "Created_Date": created_date,
                "CheckIn_Date": pd.to_datetime(res.get("startDate")).date(),
                "CheckOut_Date": pd.to_datetime(res.get("endDate")).date(),
                "Status": status,
            }
        )

    df_res = pd.DataFrame(parsed_reservations)
    expanded_nights = []
    for _, row in df_res.iterrows():
        current_stay_date = row["CheckIn_Date"]
        while current_stay_date < row["CheckOut_Date"]:
            expanded_nights.append(
                {
                    "Stay_Date": current_stay_date,
                    "Created_Date": row["Created_Date"],
                }
            )
            current_stay_date += timedelta(days=1)

    df_nights = pd.DataFrame(expanded_nights)
    if not df_nights.empty:
        df_nights["Stay_Date"] = pd.to_datetime(df_nights["Stay_Date"])
        df_nights["Created_Date"] = pd.to_datetime(df_nights["Created_Date"])

    future_dates = pd.date_range(TODAY, periods=PRICING_WINDOW_DAYS)
    pace_rows = []
    for s_date in future_dates:
        for r_type in BASELINE_ADR:
            hist_adr = BASELINE_ADR[r_type]
            if not df_nights.empty:
                current_otb = len(df_nights[df_nights["Stay_Date"] == s_date])
                lead_time_days = (s_date - TODAY).days
                hist_stay_date = s_date - pd.DateOffset(years=1)
                hist_checkpoint_date = hist_stay_date - pd.DateOffset(days=lead_time_days)
                hist_pool = df_nights[df_nights["Stay_Date"] == hist_stay_date]
                hist_otb_at_leadtime = len(
                    hist_pool[hist_pool["Created_Date"] <= hist_checkpoint_date]
                )
            else:
                current_otb = 0
                hist_otb_at_leadtime = 0
                lead_time_days = (s_date - TODAY).days

            # Day 0 has no reliable OTB figure yet; use the live occupancy.
            current_occ = (
                LIVE_TODAY_OCC
                if lead_time_days == 0
                else (current_otb / CAPACITY) * 100
            )
            pace_rows.append(
                {
                    "Stay_Date": s_date,
                    "Room_Type": r_type,
                    "Historical_ADR": float(hist_adr),
                    "Current_Occupancy": round(current_occ, 2),
                    "Current_OTB_Rooms": int(current_otb),
                    "Hist_OTB_At_Same_LeadTime": int(max(1, hist_otb_at_leadtime)),
                }
            )

    log_info("Stay-night pace matrix built for {} room types.".format(len(BASELINE_ADR)))
    return pd.DataFrame(pace_rows)


def deterministic_competitor_adr(stay_date):
    """Stable synthetic competitor ADR for a stay date (no random seed drift).

    Replace this function with a real rate-shopper feed when one is available.
    A hash of the ISO date keeps the value identical across runs and years,
    which makes reports reproducible and comparable.
    """
    key = "{}:{}".format(COMPETITOR_SEED, pd.Timestamp(stay_date).date().isoformat())
    digest = hashlib.sha256(key.encode("ascii")).digest()
    fraction = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return round(
        COMPETITOR_MIN_ADR + fraction * (COMPETITOR_MAX_ADR - COMPETITOR_MIN_ADR), 2
    )


# ==========================================
# 3. LOAD EXTERNAL INPUTS
# ==========================================
log_info("Property timezone: {} ({})".format(
    TZ_NAME or "local system time", format_utc_offset(TZ_NAME)))
log_info("Property time: {}".format(NOW.strftime("%Y-%m-%d %H:%M:%S")))
log_info("Run date: {} ({})".format(
    AS_OF_DATE.isoformat(),
    "RMS_AS_OF_DATE override" if AS_OF_OVERRIDE else "property date"))
log_info("Pricing window: {} .. {} ({} days)".format(
    WINDOW_START.date(), WINDOW_END.date(), PRICING_WINDOW_DAYS))
log_info("Data mode: {} ({})".format(DATA_MODE, DATA_MODE_LABEL))

df_pace = fetch_and_build_pace()

try:
    calendar = load_event_calendar(CALENDAR_FILE, WINDOW_START, WINDOW_END)
except CalendarError as exc:
    print("[ERROR] {}".format(exc))
    raise SystemExit(2)
for message in calendar.warnings:
    log_warn(message)
log_info(
    "Loaded {} event date(s) from {} inside the pricing window.".format(
        len(calendar.events), os.path.basename(CALENDAR_FILE)
    )
)

window_dates = pd.date_range(WINDOW_START, periods=PRICING_WINDOW_DAYS)
df_comp = pd.DataFrame(
    {
        "Date": window_dates,
        "Competitor_ADR": [deterministic_competitor_adr(d) for d in window_dates],
    }
)


# ==========================================
# 4. RMS PRICING ENGINE
# ==========================================
df_model = df_pace.copy()
df_model["Stay_Date"] = pd.to_datetime(df_model["Stay_Date"])

# Absolute-date joins: the event calendar and the competitor feed are keyed by
# the real stay date, not by an offset from the run date.
df_model = (
    df_model.merge(
        calendar.events[
            ["Date", "Event_Name", "Event_Factor", "Event_Category", "Event_Source"]
        ],
        left_on="Stay_Date",
        right_on="Date",
        how="left",
    ).drop(columns=["Date"])
)
df_model = (
    df_model.merge(df_comp, left_on="Stay_Date", right_on="Date", how="left")
    .drop(columns=["Date"])
)

df_model["Event_Factor"] = df_model["Event_Factor"].fillna(1.0)
df_model["Event_Name"] = df_model["Event_Name"].fillna("")
df_model["Event_Category"] = df_model["Event_Category"].fillna("")
df_model["Event_Source"] = df_model["Event_Source"].fillna("None")


def get_room_factor(room_type):
    return ROOM_TYPE_FACTOR.get(room_type, 1.00)


df_model["RoomType_Factor"] = df_model["Room_Type"].apply(get_room_factor)
df_model["Base_Room_ADR"] = df_model["Historical_ADR"] * df_model["RoomType_Factor"]

df_model["Day_of_Week"] = df_model["Stay_Date"].dt.dayofweek


def get_business_weekend_factor(dow):
    if dow == 4:
        return 1.05
    if dow == 5:
        return 1.00
    if dow == 6:
        return 0.95
    return 1.00


df_model["Weekend_Factor"] = df_model["Day_of_Week"].apply(get_business_weekend_factor)

# Net availability yielding: blocked rooms are removed from the denominator.
active_capacity = max(1, CAPACITY - ROOMS_BLOCKED)
df_model["Effective_Occupancy"] = (
    df_model["Current_OTB_Rooms"] / active_capacity
) * 100


def get_occupancy_factor(occ):
    if occ < 40:
        return 0.90
    if occ < 60:
        return 1.00
    if occ < 80:
        return 1.10
    if occ < 90:
        return 1.20
    return 1.35


df_model["Occupancy_Factor"] = df_model["Effective_Occupancy"].apply(
    get_occupancy_factor
)


def calculate_pace_ratio(row):
    current = row["Current_OTB_Rooms"]
    hist = row["Hist_OTB_At_Same_LeadTime"]
    if current == 0 and hist == 0:
        return 1.0
    if current > 0 and hist == 0:
        return 1.5
    return current / hist


df_model["Pace_Ratio"] = df_model.apply(calculate_pace_ratio, axis=1)


def get_pace_factor(ratio):
    if ratio > 1.5:
        return 1.25
    if ratio > 1.2:
        return 1.15
    if ratio > 1.0:
        return 1.05
    if ratio >= 0.8:
        return 1.00
    return 0.90


df_model["Booking_Pace_Factor"] = df_model["Pace_Ratio"].apply(get_pace_factor)

df_model["Days_to_Checkin"] = (df_model["Stay_Date"] - TODAY).dt.days


def get_lead_time_factor(row):
    days = row["Days_to_Checkin"]
    occ = row["Current_Occupancy"]
    if days <= 3 and occ < 40:
        return 0.85
    if days > 30:
        return 0.95
    if days >= 14:
        return 1.00
    if days >= 7:
        return 1.05
    if days > 3:
        return 1.10
    return 1.15


df_model["Lead_Time_Factor"] = df_model.apply(get_lead_time_factor, axis=1)

df_model["Competitor_Ratio"] = df_model["Competitor_ADR"] / df_model["Base_Room_ADR"]


def get_comp_factor(ratio):
    if pd.isna(ratio):
        return 1.00
    if ratio > 1.5:
        return 1.20
    if ratio > 1.2:
        return 1.10
    if ratio < 0.8:
        return 0.90
    return 1.00


df_model["Competitor_Factor"] = df_model["Competitor_Ratio"].apply(get_comp_factor)

# Core multiplicative yield model.
df_model["Recommended_ADR"] = (
    df_model["Base_Room_ADR"]
    * df_model["Weekend_Factor"]
    * df_model["Event_Factor"]
    * df_model["Occupancy_Factor"]
    * df_model["Lead_Time_Factor"]
    * df_model["Booking_Pace_Factor"]
    * df_model["Competitor_Factor"]
)

# Same-day time decay and guest density surcharge.
prop_hour = PROPERTY_NOW.hour
guest_density = (IN_HOUSE_GUESTS / IN_HOUSE_ROOMS) if IN_HOUSE_ROOMS > 0 else 0


def apply_advanced_optimizations(row):
    adr = row["Recommended_ADR"]
    if row["Days_to_Checkin"] == 0:
        if row["Effective_Occupancy"] < 100:
            if prop_hour >= 22:
                adr *= 0.85
            elif prop_hour >= 20:
                adr *= 0.90
            elif prop_hour >= 18:
                adr *= 0.95
        else:
            adr *= 1.05
    if guest_density > 2.5 and row["Effective_Occupancy"] >= 70:
        adr *= 1.04
    return adr


df_model["Recommended_ADR"] = df_model.apply(apply_advanced_optimizations, axis=1)

# Rate guards with event headroom. The cap flag is exported so a clipped rate
# is visible instead of looking like the calendar had no effect.
df_model["MIN_ADR"] = df_model["Base_Room_ADR"] * MIN_RATE_MULTIPLIER
df_model["MAX_ADR"] = df_model["Base_Room_ADR"] * np.maximum(
    MAX_RATE_MULTIPLIER, df_model["Event_Factor"] * EVENT_CAP_HEADROOM
)
df_model["Rate_Cap_Applied"] = (
    (df_model["Recommended_ADR"] > df_model["MAX_ADR"])
    | (df_model["Recommended_ADR"] < df_model["MIN_ADR"])
)
df_model["Final_ADR"] = np.clip(
    df_model["Recommended_ADR"], df_model["MIN_ADR"], df_model["MAX_ADR"]
).round(0)
df_model["Expected_RevPAR"] = (
    df_model["Final_ADR"] * (df_model["Current_Occupancy"] / 100)
).round(2)


def calculate_confidence(row):
    score = 50
    if row["Current_Occupancy"] >= 80:
        score += 15
    elif row["Current_Occupancy"] >= 60:
        score += 5
    if row["Pace_Ratio"] >= 1.5:
        score += 15
    elif row["Pace_Ratio"] > 1.0:
        score += 5
    elif row["Pace_Ratio"] < 0.8:
        score -= 10
    if row["Event_Factor"] > 1.0:
        score += 10
    comp_ratio = row["Competitor_Ratio"]
    if pd.notna(comp_ratio) and (comp_ratio > 1.1 or comp_ratio < 0.9):
        score += 10
    return min(score, 100)


df_model["Confidence_Score"] = df_model.apply(calculate_confidence, axis=1)


# ==========================================
# 5. TECHNICAL VIEW EXPORT
# ==========================================
output_cols = [
    "Stay_Date",
    "Room_Type",
    "Day_of_Week",
    "Event_Name",
    "Event_Category",
    "Event_Factor",
    "Event_Source",
    "Days_to_Checkin",
    "Current_Occupancy",
    "Pace_Ratio",
    "Base_Room_ADR",
    "Competitor_ADR",
    "Recommended_ADR",
    "MIN_ADR",
    "MAX_ADR",
    "Rate_Cap_Applied",
    "Final_ADR",
    "Expected_RevPAR",
    "Confidence_Score",
]
df_dashboard = df_model[output_cols].copy()
df_dashboard["Stay_Date"] = df_dashboard["Stay_Date"].dt.date
df_dashboard["Data_Mode"] = DATA_MODE

date_suffix = AS_OF_DATE.strftime("%Y-%m-%d")
tech_filename = "hotel_name_Live_Dashboard_{}.csv".format(date_suffix)
try:
    df_dashboard.to_csv(tech_filename, index=False)
    log_ok("Technical view written: {}".format(tech_filename))
except PermissionError:
    alternative_name = "hotel_name_Live_Dashboard_{}_{}.csv".format(
        date_suffix, NOW.strftime("%H%M%S")
    )
    df_dashboard.to_csv(alternative_name, index=False)
    log_warn("Technical view file was open in Excel; saved as {}".format(alternative_name))


# ==========================================
# 6. MANAGER VIEW EXPORT
# ==========================================
log_info("Compiling the manager view...")

raw_pct_change = (
    (df_model["Final_ADR"] - df_model["Base_Room_ADR"]) / df_model["Base_Room_ADR"]
) * 100
df_model["ADR_Change_vs_Base"] = raw_pct_change.apply(
    lambda value: "+{:.1f}%".format(value) if value > 0 else "{:.1f}%".format(value)
)


def evaluate_demand_status(row):
    occ = row["Current_Occupancy"]
    pace = row["Pace_Ratio"]
    if occ >= 90:
        status_idx = 3
    elif occ >= 70:
        status_idx = 2
    elif occ >= 40:
        status_idx = 1
    else:
        status_idx = 0
    if pace > 1.2:
        status_idx = min(status_idx + 1, 3)
    return {0: "Low Demand", 1: "Normal Demand", 2: "High Demand", 3: "Peak Demand"}[
        status_idx
    ]


df_model["Demand_Status"] = df_model.apply(evaluate_demand_status, axis=1)


def assign_manager_action(pct_val):
    if pct_val >= 25.0:
        return "Raise Aggressively"
    if pct_val >= 10.0:
        return "Raise Rate"
    if pct_val > -10.0:
        return "Hold Rate"
    return "Discount"


df_model["Action"] = raw_pct_change.apply(assign_manager_action)


def extract_business_reason(row):
    """Explain a rate decision.

    A named event and an unnamed public holiday are deliberately separate
    branches: staff may leave Event_Name blank in the calendar, and that blank
    must still produce a meaningful reason instead of being unreachable.
    """
    name = str(row["Event_Name"] or "").strip()
    factor = float(row["Event_Factor"])
    if factor > 1.0:
        if name:
            category = str(row["Event_Category"] or "Special Event").strip()
            return "{} Demand: {}".format(category, name)
        return "Public Holiday Demand Window Triggered"
    if factor < 1.0:
        return (
            "Calendar Demand Adjustment: {}".format(name)
            if name
            else "Calendar Demand Adjustment"
        )
    if row["Current_Occupancy"] >= 80 and row["Pace_Ratio"] > 1.2:
        return "Peak Occupancy + Strong Booking Pace"
    if row["Current_Occupancy"] < 40 and row["Pace_Ratio"] < 0.8:
        return "Low Occupancy + Weak Booking Pace"
    if row["Competitor_Ratio"] > 1.25:
        return "High Competitor Pricing Observed"
    return "Market Conditions Stable"


df_model["Reason"] = df_model.apply(extract_business_reason, axis=1)
# Distinguish the ceiling from the floor so a discounted low-demand day is not
# described as if a high event factor had been clipped.
ceiling_hit = df_model["Recommended_ADR"] > df_model["MAX_ADR"]
floor_hit = df_model["Recommended_ADR"] < df_model["MIN_ADR"]
df_model.loc[ceiling_hit, "Reason"] = (
    df_model.loc[ceiling_hit, "Reason"] + " | Rate ceiling applied"
)
df_model.loc[floor_hit, "Reason"] = (
    df_model.loc[floor_hit, "Reason"] + " | Rate floor applied"
)
df_model["Current_Occupancy_Str"] = df_model["Current_Occupancy"].apply(
    lambda value: "{}%".format(int(round(value)))
)

manager_cols = [
    "Stay_Date",
    "Room_Type",
    "Current_Occupancy_Str",
    "Final_ADR",
    "ADR_Change_vs_Base",
    "Demand_Status",
    "Action",
    "Confidence_Score",
    "Reason",
]
df_manager_view = df_model[manager_cols].copy()
df_manager_view.rename(
    columns={"Current_Occupancy_Str": "Current_Occupancy"}, inplace=True
)
df_manager_view["Stay_Date"] = df_manager_view["Stay_Date"].dt.date
df_manager_view.sort_values(
    by=["Stay_Date", "Room_Type"], ascending=[True, True], inplace=True
)
df_manager_view["Data_Mode"] = DATA_MODE

manager_filename = "hotel_name_RMS_Manager_View_{}.csv".format(date_suffix)
try:
    df_manager_view.to_csv(manager_filename, index=False)
    log_ok("Manager view written: {}".format(manager_filename))
except PermissionError:
    alternative_mgr_name = "hotel_name_RMS_Manager_View_{}_{}.csv".format(
        date_suffix, NOW.strftime("%H%M%S")
    )
    df_manager_view.to_csv(alternative_mgr_name, index=False)
    log_warn("Manager view file was open in Excel; saved as {}".format(alternative_mgr_name))

capped_rows = int(df_model["Rate_Cap_Applied"].sum())
log_info(
    "Event-affected rows: {} of {}; rate cap applied to {} rows.".format(
        int((df_model["Event_Factor"] != 1.0).sum()), len(df_model), capped_rows
    )
)
log_info("Pricing cycle complete. Data mode: {}".format(DATA_MODE))
