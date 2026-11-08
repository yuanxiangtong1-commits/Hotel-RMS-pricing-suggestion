# Hotel RMS (Pricing Suggestion)

A self-contained, offline **decision-support demo** for a single hotel
property. It builds a 90-day rate recommendation per room type from booking
pace, occupancy, lead time, competitor benchmarks and a staff-maintained
public-holiday / special-event calendar, then writes two CSV reports and a
small staff-facing UI.

> **Read this first.** With no PMS credentials configured, the engine runs in
> `DEMO` mode: reservations, competitor rates and baseline ADR are synthetic.
> The reports carry a `Data_Mode` column and the UI shows a permanent warning.
> Rates produced in this mode are for demonstration and review only - do not
> publish them to a booking channel.

## Files

| File | Purpose |
| --- | --- |
| `hotel rms.py` | Pricing engine: PMS adapter, 90-day rate model, CSV exports |
| `event_calendar.py` | Standalone loader/validator for the event calendar |
| `Public holidays.csv` | **Staff-maintained** holidays and special events (the only event input) |
| `rms_gui.py` | Tkinter staff UI: run pricing, browse results, explain rates, open the calendar |
| `scheduler.py` | Unattended daily 06:00 run (`--run-now` for an immediate cycle) |
| `timezone_config.py` | Shared timezone settings and IANA availability checks |
| `Run_Pricing_Now.bat` | Windows: run one pricing cycle |
| `Start_Pricing_UI.bat` | Windows: open the staff UI |
| `User_Guide.txt` | Short operator guide |

`timezone_settings.json` is created locally by the staff UI when a timezone
is chosen; it is git-ignored, so a fresh clone starts on local system time.
Generated reports, runtime logs, `__pycache__` and local credentials are also
git-ignored (see `.gitignore`).

## Requirements

Python 3.9+ and:

```
pip install pandas numpy requests schedule
```

`tkinter` ships with standard CPython. The two `.bat` files expect Python on
`PATH`; on other platforms run the scripts directly.

Named IANA timezones additionally need the system IANA database; on Windows
install the optional `tzdata` package (`pip install tzdata`). The default
`Follow local system time` needs nothing extra and works out of the box.
No `pytz` dependency is required - see the scheduler note below.

## Quick start

```
python "hotel rms.py"          # one pricing cycle -> two CSV reports
python rms_gui.py              # staff UI (click "Run Pricing Now")
python scheduler.py --run-now  # immediate run, then daily at 06:00
```

No network access or API key is needed: the engine falls back to clearly
labelled synthetic data so everything runs offline out of the box.

## Public holidays / special events

`Public holidays.csv` is the **single source of event factors**. Staff edit it
directly in Excel or Notepad; there are no hard-coded events in the engine.
Click **Open Event Calendar** in the UI to open it, save the changes, then run
pricing again.

### Columns

| Column | Format | Meaning |
| --- | --- | --- |
| `Date` | `YYYY-MM-DD` or `MM-DD` | One-off date, or an annual date (requires `Recurring=Y`) |
| `Event_Name` | free text, may be empty | Shown in reports. If empty, the reason reads `Public Holiday Demand Window Triggered` |
| `Event_Factor` | number > 0 | Demand multiplier: `1.00` neutral, `1.25` = +25%, `0.90` = -10%. Usual range 0.50-3.00 |
| `Category` | `Public Holiday` / `Special Event` / `School Holiday` / `Custom` | Used in the pricing reason. Unknown values are mapped to `Special Event` with a warning |
| `Recurring` | `Y` or `N` (blank = `N`) | `Y` repeats the same `MM-DD` every year |

`Holiday_Factor` and `Special_Event_Factor` are accepted as aliases for
`Event_Factor`, so an older three-column calendar can be dropped in unchanged.
Lines starting with `#` and blank lines are ignored, which is why the shipped
file can carry its own instructions.

### Rules

- Dates are **absolute**. A stay date always gets the same event factor, no
  matter which day the engine runs. There are no `today + N` offsets left.
- `MM-DD` + `Recurring=Y` rows are expanded for every year covered by the
  90-day window, so fixed-date holidays carry over to future years.
- A `YYYY-MM-DD` one-off row overrides a recurring row on that same date.
  Among rows of the same kind, the last row wins. Use this to override a
  national holiday for a specific year.
- Only dates inside the current 90-day window are priced. The window starts
  on the current date in the selected property timezone, so switching across
  the date line shifts it by one day and changes which calendar rows fall
  inside it. This is expected behaviour.
- A missing file, a missing `Date` / `Event_Factor` column, a bad date, a
  non-numeric or non-positive factor stops the run with an error and a line
  number. The engine does **not** silently fall back to demo events.
- When a year covered by the window has no one-off rows, the run prints a
  warning reminding staff to add that year's movable holidays.

### Annual maintenance

Fixed-date rows (`01-01`, `05-01`, `08-09`, `12-24`, `12-25`, `12-26`,
`12-31`) are marked `Recurring=Y` and need no yearly work. Movable dates must
be added as new `YYYY-MM-DD` rows before each new year:

- Chinese New Year (both days)
- Hari Raya Puasa and Hari Raya Haji
- Vesak Day and Deepavali
- Singapore Grand Prix weekend / race day
- School holiday periods
- Observed-in-lieu holidays

Use the official Singapore public holiday calendar and the official F1 race
calendar. The shipped file contains the 2026 dates as a worked example.
The engine prints `[WARN] No one-off event rows are listed for YYYY` when a
covered year has not been maintained yet.

## Pricing model

```
Base_Room_ADR     = Historical_ADR x RoomType_Factor      (Standard 135 x 1.00, Premium 160 x 1.25)
Recommended_ADR   = Base x Weekend x Event_Factor x Occupancy
                         x Lead_Time x Booking_Pace x Competitor
Final_ADR         = round(clip(Recommended_ADR, MIN_ADR, MAX_ADR))
```

Factor tables (weekend, occupancy, pace, lead time, competitor), the same-day
time-decay layer and the guest-density surcharge are unchanged from the
baseline and are documented in `hotel rms.py`.

Rate guards were recalibrated so a strong event factor is not immediately
clipped by the generic ceiling:

```
MIN_ADR = Base x 0.70
MAX_ADR = Base x max(2.00, Event_Factor x 1.60)
```

An F1 factor of `2.20` therefore allows `3.52 x Base` instead of `2.00 x Base`.
Every clipped row is flagged with `Rate_Cap_Applied` in the technical view and
`| Rate ceiling applied` / `| Rate floor applied` in the manager view, so a
guard hit is never hidden.

## Reproducibility

Event factors are absolute and competitor ADR is a deterministic hash of the
stay date, so those inputs are comparable across runs and years.

To replay a complete window - for back-testing, training or year-over-year
comparison - set `RMS_AS_OF_DATE` before running. The synthetic reservation
feed and the report date suffix are then anchored to that date too:

```
set RMS_AS_OF_DATE=2026-09-01
python "hotel rms.py"
```

For a repeatable *time of day* (used to check the same-day discount tiers),
set `RMS_AS_OF_DATETIME` to a wall-clock timestamp in the selected timezone:

```
set RMS_AS_OF_DATETIME=2026-09-01 22:30:00
python "hotel rms.py"
```

Leave both variables unset for normal daily operation. In `DEMO` mode the
synthetic reservation feed is anchored to the run date, so OTB and pace
figures differ between runs on *different* days; `RMS_AS_OF_DATE` removes even
that variation. Connect a live PMS for fully reproducible operational data.

## Data sources (honest status)

| Input | `DEMO` | `LIVE_PMS_SYNTHETIC_MARKET` |
| --- | --- | --- |
| Reservations / occupancy | Synthetic, anchored to the run date | Live PMS v1.3 |
| Competitor ADR | Deterministic synthetic hash per stay date | Deterministic synthetic hash per stay date |
| Baseline ADR | Constant `BASELINE_ADR` | Constant `BASELINE_ADR` |
| Holidays / events | `Public holidays.csv` (staff-maintained) | `Public holidays.csv` (staff-maintained) |

`deterministic_competitor_adr()` and `BASELINE_ADR` are the two clearly marked
replacement points for a real rate-shopper feed and a real historical ADR
feed. Until those are replaced, even a live PMS connection still produces a
partly synthetic recommendation; that is why the mode is named
`LIVE_PMS_SYNTHETIC_MARKET` rather than `LIVE`.

## PMS data interface

The request layout follows Cloudbeds API v1.3 conventions as a **format
reference only**. This repository is not affiliated with any vendor and
contains no real endpoint or credential.

- Header: `x-api-key: <your key>`
- `GET {BASE_URL}/getDashboard` -> `data` with `capacity`, `roomsBlocked`,
  `inHouse`, `guestsInHouse`, `percentageOccupied`, `property_now`
- `GET {BASE_URL}/getReservations` -> `data[]` with `reservationID`,
  `dateCreated`, `status`, `startDate`, `endDate`, `balance`
  (statuses used: `confirmed`, `checked_in`, `checked_out`)

`property_now` is interpreted in the selected property timezone: a value
without a UTC offset is treated as wall-clock time in that timezone, while a
value with an offset is converted to it. Missing or unparseable values fall
back to the current property time with a `[WARN]` line, so a bad timestamp
never stops a run.

Set `PMS_API_KEY` and `PMS_BASE_URL` as environment variables. Never commit
real credentials. Without them the engine short-circuits to synthetic data
instead of waiting on a network timeout.

## Scheduler and timezone

Pick the property timezone from the second-row **Timezone** dropdown in the
staff UI. The choice takes effect as soon as it is selected - there is no
Apply button - and pricing is re-run automatically so the table shows rates
for the new timezone.

The default is `Follow local system time`, which needs no timezone database at
all. Named IANA zones (for example `Europe/London`) need `schedule >= 1.2.0`
and, on Windows, the optional `tzdata` package. If the database is missing the
UI explains what to install and keeps the local-time setting; a bad value can
never stop a pricing run. UTC offsets shown in the dropdown are computed at
runtime, so daylight-saving changes are handled automatically.

The choice is stored in `timezone_settings.json` next to the scripts:

```json
{"timezone": "Europe/London", "schema_version": 1}
```

`"timezone": null` means local system time. This file is written only by the
UI and is git-ignored.

`schedule` accepts named timezones only as `pytz` objects. To avoid adding
that dependency, the scheduler converts 06:00 in the selected zone into the
equivalent machine-local wall-clock time and registers a plain daily job. It
recomputes that time every minute, so daylight-saving transitions are picked
up automatically without a restart.

The same setting drives **all** of the following:

- The unattended `scheduler.py` 06:00 run. A long-running scheduler notices a
  configuration change within about a minute and rebuilds its job, so no
  restart is needed.
- The pricing engine's "today": the 90-day window, day-of-week factors,
  days-to-check-in, the report date suffix and the timestamped fallback
  filenames.
- The PMS `property_now` hour, which drives the same-day discount tiers
  (18:00 / 20:00 / 22:00). In `DEMO` mode this hour is the current wall-clock
  time in the selected timezone, so the sample rates react to a switch too.

Because the 90-day window is derived from the current property date, switching
across the date line can move the window by one day and therefore change which
holiday rows from `Public holidays.csv` are inside it. That is expected, not a
calendar error.

## Generated outputs

Each run creates two reports next to the scripts:
`hotel_name_Live_Dashboard_YYYY-MM-DD.csv` (technical view, including event
columns, `Recommended_ADR` before guarding, `MIN_ADR` / `MAX_ADR`,
`Rate_Cap_Applied`, `Data_Mode`) and
`hotel_name_RMS_Manager_View_YYYY-MM-DD.csv` (manager view with demand status,
action, reason, confidence and `Data_Mode`). Both are git-ignored.

## Privacy

All property / brand names, vendor names in code, personal or machine-specific
paths, real credentials, historic business reports and personal metadata have
been removed or replaced with generic placeholders. No real customer, business
or personal data is included; all figures are demo values.
