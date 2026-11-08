# -*- coding: utf-8 -*-
"""
Hotel Pricing Assistant - Staff UI
==============================================
A one-click pricing tool for non-technical staff:
1. Click "Run Pricing Now" to run the pricing engine.
2. The results table loads automatically with recommended rates for the
   next 90 days, per room type, demand status and suggested action.
3. Click any row to see a plain-English breakdown of WHY that price was set.
4. Click "Open Event Calendar" to add or edit public holidays and special
   events in the CSV, then run pricing again. No code changes are needed.

All UI labels, log messages, comments and report text are English.
"""

import csv
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import ttk, messagebox

from timezone_config import (
    choice_labels,
    choice_values,
    format_clock,
    format_utc_offset,
    load_timezone,
    resolve_now,
    save_timezone,
    tz_available,
)

# ==========================================
# Path setup (works no matter where the app is launched from)
# ==========================================
APP_DIR = os.path.dirname(os.path.abspath(__file__))
RMS_SCRIPT = os.path.join(APP_DIR, "hotel rms.py")
CALENDAR_FILE = os.path.join(APP_DIR, "Public holidays.csv")

DOW_EN = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}


def read_csv_rows(path):
    """Read a CSV file into a list of dicts (stdlib only, no pandas needed)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def find_latest_csv(pattern):
    """Find the newest result CSV matching the filename pattern."""
    candidates = []
    for name in os.listdir(APP_DIR):
        if re.match(pattern, name):
            full = os.path.join(APP_DIR, name)
            candidates.append((os.path.getmtime(full), full))
    if not candidates:
        return None
    return max(candidates)[1]


def load_latest_results():
    """Load the newest Manager View + Technical View CSVs and merge them
    by (Stay_Date, Room_Type) into one record list."""
    mgr_path = find_latest_csv(r".*_RMS_Manager_View_\d{4}-\d{2}-\d{2}.*\.csv")
    tech_path = find_latest_csv(r".*_Live_Dashboard_\d{4}-\d{2}-\d{2}.*\.csv")
    if not mgr_path:
        return None

    tech_map = {}
    if tech_path:
        for row in read_csv_rows(tech_path):
            tech_map[(row.get("Stay_Date", ""), row.get("Room_Type", ""))] = row

    records = []
    for row in read_csv_rows(mgr_path):
        key = (row.get("Stay_Date", ""), row.get("Room_Type", ""))
        tech = tech_map.get(key, {})
        stay = row.get("Stay_Date", "")
        # Reports use YYYY/MM/DD. The ISO spelling is still accepted so older
        # report files in the folder remain readable.
        parsed_stay = None
        for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
            try:
                parsed_stay = datetime.strptime(stay, fmt)
                break
            except ValueError:
                continue
        dow = DOW_EN[parsed_stay.weekday()] if parsed_stay else ""
        try:
            days_ahead = int(tech.get("Days_to_Checkin", "") or "")
        except ValueError:
            days_ahead = ""
        try:
            pace = float(tech.get("Pace_Ratio", "") or "")
        except ValueError:
            pace = None

        records.append(
            {
                "Stay_Date": stay,
                "DOW": dow,
                "Room_Type": row.get("Room_Type", ""),
                "Final_ADR": row.get("Final_ADR", ""),
                "Base_ADR": tech.get("Base_Room_ADR", ""),
                "Change": row.get("ADR_Change_vs_Base", ""),
                "Demand": row.get("Demand_Status", ""),
                "Action": row.get("Action", ""),
                "Occ": row.get("Current_Occupancy", ""),
                "Confidence": row.get("Confidence_Score", ""),
                "Reason": row.get("Reason", ""),
                "Event": tech.get("Event_Name") or "",
                "Event_Factor": tech.get("Event_Factor", ""),
                "Event_Category": tech.get("Event_Category", ""),
                "Event_Source": tech.get("Event_Source", ""),
                "Rate_Cap_Applied": tech.get("Rate_Cap_Applied", ""),
                "Data_Mode": tech.get("Data_Mode") or row.get("Data_Mode", ""),
                "Days_to_Checkin": days_ahead,
                "Pace_Ratio": pace,
                "Competitor_ADR": tech.get("Competitor_ADR", ""),
                "RevPAR": tech.get("Expected_RevPAR", ""),
            }
        )
    records.sort(key=lambda r: (r["Stay_Date"], r["Room_Type"]))
    return records


def parse_change_pct(change_str):
    """Parse '+12.5%' / '-8.3%' into a float; return None on failure."""
    if not change_str:
        return None
    match = re.match(r"([+-]?\d+\.?\d*)%", change_str.strip())
    return float(match.group(1)) if match else None


# ==========================================
# Main application window
# ==========================================
class RMSApp:
    def __init__(self, root):
        self.root = root
        self.records = []
        self.running = False
        self._tz_dirty = False

        root.title("Hotel Pricing Assistant")
        root.geometry("1180x760")
        root.minsize(980, 620)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Run.TButton", font=("Segoe UI", 13, "bold"), padding=14)
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Big.TLabel", font=("Segoe UI", 11, "bold"))

        self.build_top_bar()
        self.build_context_bar()
        self.build_table()
        self.build_detail_panel()
        self.build_status_bar()

        # Check the runtime environment in the background on startup.
        threading.Thread(target=self.check_environment, daemon=True).start()

    # ---------- Top bar: actions, filters, data-mode warning ----------
    def build_top_bar(self):
        top = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        top.pack(fill="x")

        self.run_btn = ttk.Button(
            top, text="Run Pricing Now", style="Run.TButton", command=self.run_pricing
        )
        self.run_btn.pack(side="left")

        ttk.Button(
            top, text="Open Event Calendar", command=self.open_event_calendar
        ).pack(side="left", padx=(12, 0))

        ttk.Label(top, text="   Show:").pack(side="left")
        self.range_var = tk.StringVar(value="Next 14 days")
        cb = ttk.Combobox(
            top,
            textvariable=self.range_var,
            state="readonly",
            width=12,
            values=["Next 7 days", "Next 14 days", "Next 30 days", "All 90 days"],
        )
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.populate_table())

        ttk.Label(top, text="   Room type:").pack(side="left")
        self.type_var = tk.StringVar(value="All room types")
        self.type_cb = ttk.Combobox(
            top,
            textvariable=self.type_var,
            state="readonly",
            width=14,
            values=["All room types"],
        )
        self.type_cb.pack(side="left")
        self.type_cb.bind("<<ComboboxSelected>>", lambda e: self.populate_table())

        self.mode_lbl = ttk.Label(
            top, text="", foreground="#b26a00", font=("Segoe UI", 9, "bold")
        )
        self.mode_lbl.pack(side="right")
        self.last_run_lbl = ttk.Label(
            top,
            text="(Not run yet - click the button to start)",
            foreground="#666666",
        )
        self.last_run_lbl.pack(side="right", padx=(0, 12))

    # ---------- Context bar: property timezone ----------
    def build_context_bar(self):
        """A second, independent row so the crowded top bar stays untouched."""
        bar = ttk.Frame(self.root, padding=(12, 2, 12, 4))
        bar.pack(fill="x")

        ttk.Label(bar, text="Timezone:").pack(side="left")
        self.tz_values = choice_values()
        self.tz_labels = choice_labels()
        self.tz_var = tk.StringVar()
        self.tz_cb = ttk.Combobox(
            bar,
            textvariable=self.tz_var,
            state="readonly",
            width=34,
            values=self.tz_labels,
        )
        self.tz_cb.pack(side="left", padx=(6, 0))
        self.tz_cb.bind("<<ComboboxSelected>>", self.on_timezone_changed)

        self.tz_hint_lbl = ttk.Label(bar, text="", foreground="#666666")
        self.tz_hint_lbl.pack(side="right")

        current = load_timezone()
        try:
            index = self.tz_values.index(current)
        except ValueError:
            index = 0
        self.tz_cb.current(index)
        self.refresh_tz_hint(current)

    def refresh_tz_hint(self, name):
        """Keep the current timezone and its consequences visible at all times."""
        offset = format_utc_offset(name)
        if name is None:
            hint = (
                "Prices and the daily 06:00 run follow this computer's local "
                "time ({}).".format(offset)
            )
        else:
            hint = (
                "Prices and the daily 06:00 run follow {} ({}). "
                "Pricing date: {}.".format(
                    name, offset, resolve_now(name).strftime("%Y/%m/%d")
                )
            )
        self.tz_hint_lbl.configure(text=hint)

    def on_timezone_changed(self, _event):
        """One-step switch: validate, persist, refresh, then re-run pricing."""
        index = self.tz_cb.current()
        if index < 0:
            return
        name = self.tz_values[index]
        if name is not None and not tz_available(name):
            messagebox.showwarning(
                "Timezone data not available",
                "The IANA timezone database is not installed for {}.\n\n"
                "Ask your administrator to run: pip install tzdata\n"
                "The setting stays on 'Follow local system time', which needs "
                "no extra package.".format(name),
            )
            self.tz_cb.current(0)
            self.refresh_tz_hint(None)
            return
        try:
            save_timezone(name)
        except Exception as exc:
            messagebox.showerror("Could not save timezone", str(exc))
            return
        self.refresh_tz_hint(name)
        self.log(
            "[INFO] Timezone set to {}. Refreshing rates...".format(
                name or "local system time"
            )
        )
        if self.running:
            self._tz_dirty = True
        else:
            self.run_pricing()

    # ---------- Middle: results table ----------
    def build_table(self):
        cols = (
            "date", "dow", "type", "final", "change", "demand",
            "action", "occ", "conf", "reason",
        )
        headers = (
            "Date", "Day", "Room Type", "Recommended Rate", "vs Base",
            "Demand", "Suggested Action", "Occupancy", "Confidence", "Pricing Reason",
        )
        widths = (95, 55, 85, 115, 80, 100, 120, 80, 80, 330)

        frame = ttk.Frame(self.root, padding=(12, 4))
        frame.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        for c, h, w in zip(cols, headers, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("reason", "date") else "center")

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Row colors by demand status.
        self.tree.tag_configure("Peak", background="#fde2e2")
        self.tree.tag_configure("High", background="#fff3e0")
        self.tree.tag_configure("Low", background="#e8f5e9")
        self.tree.bind("<<TreeviewSelect>>", self.on_row_select)

    # ---------- Bottom: pricing explanation panel ----------
    def build_detail_panel(self):
        panel = ttk.LabelFrame(
            self.root,
            text=" Why this price? (click any row above) ",
            padding=(14, 6, 14, 10),
        )
        panel.pack(fill="x", padx=12, pady=(2, 4))
        self.detail = panel

        self.summary_lbl = ttk.Label(
            panel,
            text="Click a row in the table above to see the pricing explanation",
            style="Big.TLabel",
            foreground="#555555",
            wraplength=1100,
            justify="left",
        )
        self.summary_lbl.grid(row=0, column=0, columnspan=6, sticky="w", pady=(2, 6))

        self.canvas = tk.Canvas(panel, width=1050, height=64, highlightthickness=0)
        self.canvas.grid(row=1, column=0, columnspan=6, sticky="w")

        titles = [
            "Special Event",
            "Supply & Demand",
            "Booking Pace",
            "Competitor",
            "Forecast RevPAR",
        ]
        self.factor_vars = []
        for i, title in enumerate(titles):
            frame = ttk.Frame(panel)
            frame.grid(row=2, column=i, sticky="nw", padx=(0, 24), pady=(4, 2))
            ttk.Label(
                frame,
                text=title,
                foreground="#1a6fb5",
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w")
            box = tk.Text(
                frame, width=18, height=3, wrap="word", relief="flat",
                background="#f5f7fa", font=("Segoe UI", 9),
            )
            box.configure(state="disabled")
            box.pack(anchor="w", pady=2)
            self.factor_vars.append(box)

        self.conf_lbl = ttk.Label(panel, text="", foreground="#666666")
        self.conf_lbl.grid(row=3, column=0, columnspan=6, sticky="w", pady=(2, 0))

    # ---------- Bottom: status log ----------
    def build_status_bar(self):
        self.status = tk.Text(
            self.root, height=4, relief="flat", background="#20232a",
            foreground="#9fe88d", font=("Consolas", 9), state="disabled",
        )
        self.status.pack(fill="x", padx=12, pady=(0, 10))

    def log(self, msg):
        self.status.configure(state="normal")
        self.status.insert("end", msg + "\n")
        self.status.see("end")
        self.status.configure(state="disabled")

    # ---------- Environment check ----------
    def check_environment(self):
        if not os.path.isfile(CALENDAR_FILE):
            self.root.after(
                0,
                lambda: self.log(
                    "[WARN] Event calendar not found: {}. The pricing run will stop "
                    "until the file is restored.".format(CALENDAR_FILE)
                ),
            )
        try:
            result = subprocess.run(
                [sys.executable, "-c", "import pandas, numpy, requests"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                self.root.after(
                    0,
                    lambda: self.log(
                        "[OK] Environment check passed (pandas / numpy / requests ready)"
                    ),
                )
            else:
                self.root.after(
                    0,
                    lambda: self.log(
                        "[WARN] Missing Python packages (pandas / numpy / requests).\n"
                        "       To fix: press Win+R, type cmd, then run\n"
                        '       "{}" -m pip install pandas numpy requests'.format(
                            sys.executable
                        )
                    ),
                )
        except Exception as exc:
            self.root.after(
                0, lambda: self.log("[WARN] Environment check failed: {}".format(exc))
            )

    # ---------- Event calendar ----------
    def open_event_calendar(self):
        """Open the staff-maintained calendar in the default CSV application."""
        if not os.path.isfile(CALENDAR_FILE):
            messagebox.showerror(
                "Calendar Not Found",
                "Could not find:\n{}\n\nThe pricing engine requires this file.".format(
                    CALENDAR_FILE
                ),
            )
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(CALENDAR_FILE)
            else:
                webbrowser.open("file:///" + CALENDAR_FILE.replace("\\", "/"))
        except OSError as exc:
            messagebox.showerror("Cannot Open Calendar", str(exc))
            return
        self.log(
            "[INFO] Event calendar opened. Save your edits, then click Run Pricing Now."
        )

    # ---------- Run pricing ----------
    def run_pricing(self):
        if self.running:
            return
        self.running = True
        self.run_btn.configure(state="disabled")
        self.log(
            "[RUN] {} Running pricing engine, please wait...".format(
                resolve_now(load_timezone()).strftime("%H:%M:%S")
            )
        )

        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.Popen(
                [sys.executable, RMS_SCRIPT],
                cwd=APP_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except Exception as exc:
            self.log("[ERROR] Failed to start pricing engine: {}".format(exc))
            self.running = False
            self.run_btn.configure(state="normal")
            return

        def reader():
            for line in proc.stdout:
                self.root.after(0, self.log, line.rstrip())
            code = proc.wait()
            self.root.after(0, lambda: self.on_run_finished(code))

        threading.Thread(target=reader, daemon=True).start()

    def on_run_finished(self, code):
        self.running = False
        self.run_btn.configure(state="normal")
        if code == 0:
            self.log("[OK] Pricing finished, loading results...")
            self.load_results()
        else:
            self.log(
                "[ERROR] Pricing engine failed. Check the log above or contact "
                "your system administrator."
            )
        if self._tz_dirty:
            self._tz_dirty = False
            self.root.after(0, self.run_pricing)

    # ---------- Load / display results ----------
    def load_results(self):
        try:
            records = load_latest_results()
        except Exception as exc:
            messagebox.showerror("Load Failed", "Error reading result files:\n{}".format(exc))
            return
        if not records:
            messagebox.showwarning(
                "No Results",
                'No pricing result files found.\nPlease click "Run Pricing Now" first.',
            )
            return

        self.records = records
        types = ["All room types"] + sorted({r["Room_Type"] for r in records})
        self.type_cb.configure(values=types)
        self.type_var.set("All room types")
        self.populate_table()
        self.update_mode_banner()
        self.last_run_lbl.configure(
            text="Data generated: {}".format(format_clock(resolve_now(load_timezone())))
        )
        self.log(
            "[INFO] Loaded {} rate recommendations. Click any row to see why it "
            "was priced this way.".format(len(records))
        )

    def update_mode_banner(self):
        """Make the demo/production distinction impossible to overlook."""
        modes = {str(r.get("Data_Mode") or "") for r in self.records}
        if modes == {"DEMO"}:
            self.mode_lbl.configure(
                text="DEMO MODE - synthetic data, not for live pricing",
                foreground="#b26a00",
            )
        elif modes == {"LIVE_PMS_SYNTHETIC_MARKET"}:
            self.mode_lbl.configure(
                text="LIVE PMS / SYNTHETIC MARKET DATA",
                foreground="#b26a00",
            )
        else:
            self.mode_lbl.configure(
                text="DATA MODE UNKNOWN - verify the report",
                foreground="#b00020",
            )

    def filtered_records(self):
        rng = self.range_var.get()
        limit = {"Next 7 days": 7, "Next 14 days": 14, "Next 30 days": 30}.get(rng, 999)
        out = []
        for r in self.records:
            d = r["Days_to_Checkin"]
            if isinstance(d, int) and d >= limit:
                continue
            if self.type_var.get() != "All room types" and r["Room_Type"] != self.type_var.get():
                continue
            out.append(r)
        return out

    def populate_table(self):
        self.tree.delete(*self.tree.get_children())
        for r in self.filtered_records():
            tag = {
                "Peak Demand": "Peak",
                "High Demand": "High",
                "Low Demand": "Low",
            }.get(r["Demand"], "")
            self.tree.insert(
                "", "end",
                values=(
                    r["Stay_Date"], r["DOW"], r["Room_Type"], r["Final_ADR"],
                    r["Change"], r["Demand"], r["Action"], r["Occ"],
                    r["Confidence"], r["Reason"],
                ),
                tags=(tag,),
            )

    # ---------- Row click -> pricing explanation ----------
    def on_row_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        index = self.tree.index(sel[0])
        # The table can be filtered, so map the visible row back to its record
        # by the date / room type values rather than by positional index.
        values = self.tree.item(sel[0], "values")
        if not values:
            return
        match = next(
            (
                r for r in self.records
                if r["Stay_Date"] == values[0] and r["Room_Type"] == values[2]
            ),
            None,
        )
        if match is None and index < len(self.records):
            match = self.records[index]
        if match is not None:
            self.show_detail(match)

    def set_factor(self, idx, text):
        box = self.factor_vars[idx]
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def show_detail(self, r):
        # 1) Headline conclusion.
        pct = parse_change_pct(r["Change"])
        if pct is None:
            direction = "held at base rate"
        elif pct >= 25:
            direction = "raised sharply by {:+.1f}%".format(pct)
        elif pct >= 10:
            direction = "raised by {:+.1f}%".format(pct)
        elif pct > -10:
            direction = "roughly flat ({:+.1f}%)".format(pct)
        else:
            direction = "lowered by {:+.1f}%".format(pct)
        self.summary_lbl.configure(
            text="{} ({}) {}: recommended rate ${}, {}. Key driver: {}.".format(
                r["Stay_Date"], r["DOW"], r["Room_Type"],
                r["Final_ADR"], direction, r["Reason"],
            ),
            foreground="#c0392b" if pct is not None and pct >= 10 else "#1a6fb5",
        )

        # 2) Base vs recommended comparison bars.
        self.canvas.delete("all")
        try:
            base = float(r["Base_ADR"])
            final = float(r["Final_ADR"])
        except (TypeError, ValueError):
            base = final = None
        if base and final:
            width, y = 1000, 12
            high = max(base, final) * 1.15
            scale = width / high
            self.canvas.create_rectangle(
                10, y, 10 + base * scale, y + 16, fill="#b0bec5", outline=""
            )
            self.canvas.create_text(
                10 + base * scale + 8, y + 8, anchor="w",
                text="Base rate ${:.0f}".format(base),
                font=("Segoe UI", 9), fill="#555555",
            )
            color = "#e74c3c" if final >= base else "#27ae60"
            self.canvas.create_rectangle(
                10, y + 28, 10 + final * scale, y + 44, fill=color, outline=""
            )
            self.canvas.create_text(
                10 + final * scale + 8, y + 36, anchor="w",
                text="Recommended ${:.0f}".format(final),
                font=("Segoe UI", 9, "bold"), fill=color,
            )

        # 3) Factor cards.
        event = str(r.get("Event") or "").strip()
        factor = str(r.get("Event_Factor") or "").strip() or "1.00"
        guarded = str(r.get("Rate_Cap_Applied") or "").strip().lower() in (
            "true", "1", "yes",
        )
        if event:
            event_txt = "{}\nFactor: {}x".format(event, factor)
        else:
            event_txt = "No special event\nFactor: {}x".format(factor)
        if guarded:
            event_txt += "\nRate guard was applied"
        self.set_factor(0, event_txt)

        occ, pace = r["Occ"], r["Pace_Ratio"]
        supply = "Current occupancy {}".format(occ)
        if pace is not None:
            if pace > 1.2:
                supply += "\nBooking pace {:.0f}% faster than history -> hot, raise rate".format(
                    (pace - 1) * 100
                )
            elif pace < 0.8:
                supply += "\nBooking pace {:.0f}% slower than history -> quiet, consider discount".format(
                    (1 - pace) * 100
                )
            else:
                supply += "\nBooking pace in line with history -> hold rate"
        self.set_factor(1, supply)

        ahead = r["Days_to_Checkin"]
        self.set_factor(
            2,
            "{} days until check-in".format(ahead)
            if isinstance(ahead, int)
            else "Lead time unknown",
        )

        try:
            comp = float(r["Competitor_ADR"])
            base_v = float(r["Base_ADR"])
            diff = comp - base_v
            if diff > 0:
                comp_txt = (
                    "Competitor avg ${:.0f}\n${:.0f} above our base -> room to raise".format(
                        comp, diff
                    )
                )
            else:
                comp_txt = (
                    "Competitor avg ${:.0f}\n${:.0f} below our base -> stay competitive".format(
                        comp, abs(diff)
                    )
                )
        except (TypeError, ValueError):
            comp_txt = "No competitor data"
        self.set_factor(3, comp_txt)

        self.set_factor(4, "${}".format(r["RevPAR"]) if r["RevPAR"] else "N/A")

        # 4) Confidence note.
        try:
            conf = int(float(r["Confidence"]))
            self.conf_lbl.configure(
                text="System confidence for this recommendation: {}/100".format(conf)
                + (
                    " (strong data basis - safe to apply)"
                    if conf >= 70
                    else " (moderate data basis - use your judgement)"
                )
            )
        except (TypeError, ValueError):
            self.conf_lbl.configure(text="")


def main():
    root = tk.Tk()
    RMSApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
