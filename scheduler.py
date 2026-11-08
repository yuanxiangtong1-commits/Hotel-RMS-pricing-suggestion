import argparse
import logging
import os
import subprocess
import sys
import time
import traceback

import schedule

# Make companion imports robust when this file is started from another folder.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from timezone_config import (  # noqa: E402
    config_mtime, load_timezone, local_time_for,
)

# ==========================================
# 1. LOGGING CONFIGURATION
# ==========================================
# Resolve every path from this file so the scheduler works no matter which
# directory it was started from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RMS_SCRIPT = os.path.join(SCRIPT_DIR, "hotel rms.py")
LOG_FILE = os.path.join(SCRIPT_DIR, "rms_scheduler.log")

log_formatter = logging.Formatter("%(asctime)s\n%(message)s\n", datefmt="%Y-%m-%d %H:%M")
logger = logging.getLogger("RMSScheduler")
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# ==========================================
# 1.4 RUN TIME / TIMEZONE (shared setting)
# ==========================================
# The staff UI writes timezone_settings.json; this process reads it. With no
# file the daily run follows the machine's local time and needs no timezone
# database. Named IANA zones need schedule >= 1.2.0 plus tzdata on Windows.
RUN_TIME = "06:00"

# ==========================================
# 1.5 PYTHON INTERPRETER RESOLVER
# ==========================================
def resolve_python_interpreter():
    """Prefer an interpreter that can import pandas, so the scheduler works
    even if it was started with a different Python on PATH."""
    candidates = [sys.executable, "py"]  # generic Python launcher
    for exe in candidates:
        try:
            check = subprocess.run(
                [exe, "-c", "import pandas"], capture_output=True, timeout=60
            )
            if check.returncode == 0:
                return exe
        except Exception:
            continue
    return sys.executable  # last resort: whatever started this scheduler


# ==========================================
# 2. JOB DEFINITION
# ==========================================
def run_rms_job():
    logger.info("Hotel RMS Started")
    if not os.path.isfile(RMS_SCRIPT):
        logger.error("ERROR: Pricing engine not found: %s", RMS_SCRIPT)
        return
    try:
        interpreter = resolve_python_interpreter()
        result = subprocess.run(
            [interpreter, RMS_SCRIPT],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        if result.stdout:
            logger.info(result.stdout.strip())
        logger.info("Hotel RMS Finished Successfully")
    except subprocess.CalledProcessError as exc:
        error_msg = "ERROR: RMS execution failed with return code {}\n".format(
            exc.returncode
        )
        error_msg += "Standard Output:\n{}\n".format(exc.stdout or "")
        error_msg += "Standard Error:\n{}".format(exc.stderr or "")
        logger.error(error_msg)
    except Exception:
        logger.error("ERROR: An unexpected exception occurred.\n%s", traceback.format_exc())


# ==========================================
# 3. MAIN SCHEDULER LOOP
# ==========================================
def rebuild_schedule():
    """Register the daily job using the timezone currently on disk.

    ``schedule`` only accepts pytz objects for named timezones. To keep the
    standard library as the only requirement, 06:00 in the property zone is
    converted to the equivalent machine-local wall-clock time. Called on
    startup, on settings changes and when that local time shifts, so no
    restart is needed and daylight saving is followed automatically.
    """
    schedule.clear()
    name = load_timezone()
    local_at = local_time_for(name, 6, 0)
    try:
        schedule.every().day.at(local_at).do(run_rms_job)
        logger.info(
            "Daily run at 06:00 %s -> %s machine-local time.",
            name or "(local system time)", local_at,
        )
    except Exception as exc:
        logger.warning(
            "Could not schedule %s (%s). Falling back to %s machine-local "
            "time.", local_at, exc, RUN_TIME,
        )
        schedule.clear()
        schedule.every().day.at(RUN_TIME).do(run_rms_job)


def main():
    parser = argparse.ArgumentParser(description="RMS Unattended Automated Scheduler")
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Execute the RMS script immediately, then begin scheduling.",
    )
    args = parser.parse_args()

    if args.run_now:
        print("[INFO] '--run-now' flag detected. Executing initial run...")
        run_rms_job()

    rebuild_schedule()

    print("[INFO] Scheduler is running in the background. Press Ctrl+C to exit.")

    last_mtime = config_mtime()
    last_local_at = local_time_for(load_timezone(), 6, 0)
    while True:
        current_mtime = config_mtime()
        name = load_timezone()
        current_local_at = local_time_for(name, 6, 0)
        if current_mtime != last_mtime or current_local_at != last_local_at:
            last_mtime = current_mtime
            last_local_at = current_local_at
            logger.info(
                "Timezone settings or daylight-saving offset changed; "
                "rebuilding the daily schedule."
            )
            rebuild_schedule()
        schedule.run_pending()
        time.sleep(60)  # Check every 60 seconds to save CPU cycles.


if __name__ == "__main__":
    main()
