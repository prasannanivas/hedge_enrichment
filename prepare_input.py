"""
prepare_input.py
================
Extracts all unique managers from NHDataBaseReturn and writes them to
all_managers.csv, ready to feed into agent.py.

Deduplication: one row per ManagerID, using the most recent date seen.
Active flag: a manager is marked active if ANY row has Active=1.

Usage:
    python prepare_input.py                          # default paths
    python prepare_input.py --input "path/to/NHDataBaseReturn.csv"
    python prepare_input.py --active-only            # only active (reproduces old active_managers.csv)
"""

import argparse
import csv
import re
from pathlib import Path

DEFAULT_INPUT  = r"NHDataBaseReturn 1(in) (1)\NHDataBaseReturn 1(in).csv"
DEFAULT_OUTPUT = "all_managers.csv"


def parse_date(s: str):
    """Parse M/D/YYYY into a sortable (yyyy, mm, dd) tuple. Returns (0,0,0) on failure."""
    try:
        parts = s.strip().split("/")
        if len(parts) == 3:
            return (int(parts[2]), int(parts[0]), int(parts[1]))
    except Exception:
        pass
    return (0, 0, 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",       default=DEFAULT_INPUT)
    parser.add_argument("--output",      default=DEFAULT_OUTPUT)
    parser.add_argument("--active-only", action="store_true",
                        help="Only include managers that have at least one active row")
    args = parser.parse_args()

    # Strip trailing semicolons from the header column "Active;"
    ACTIVE_COL = "Active;"

    managers: dict[str, dict] = {}

    print(f"Reading {args.input} ...")
    with open(args.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mid = (row.get("ManagerID") or "").strip()
            if not mid or not row.get("Manager"):
                continue  # skip blank / malformed rows

            date = parse_date(row.get("Date", ""))

            active_raw = (row.get(ACTIVE_COL) or "0").rstrip(";").strip()
            is_active  = active_raw == "1"

            if mid not in managers:
                managers[mid] = {
                    "manager_id":   mid,
                    "manager_name": row["Manager"].strip(),
                    "type":         row["Type"].strip(),
                    "style":        row["Style"].strip(),
                    "strategy":     row["Strategy"].strip(),
                    "sector":       row["Sector"].strip(),
                    "active":       is_active,
                    "_latest_date": date,
                }
            else:
                # keep most-recent row's metadata
                if date > managers[mid]["_latest_date"]:
                    managers[mid]["manager_name"] = row["Manager"].strip()
                    managers[mid]["type"]         = row["Type"].strip()
                    managers[mid]["style"]        = row["Style"].strip()
                    managers[mid]["strategy"]     = row["Strategy"].strip()
                    managers[mid]["sector"]       = row["Sector"].strip()
                    managers[mid]["_latest_date"] = date
                # ever-active flag
                if is_active:
                    managers[mid]["active"] = True

    all_mgrs = list(managers.values())
    if args.active_only:
        all_mgrs = [m for m in all_mgrs if m["active"]]

    # sort by manager_id numerically
    all_mgrs.sort(key=lambda m: int(m["manager_id"]) if m["manager_id"].isdigit() else 0)

    OUTPUT_FIELDS = ["manager_id", "manager_name", "type", "style",
                     "strategy", "sector", "active"]

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_mgrs)

    active_count   = sum(1 for m in all_mgrs if m["active"])
    inactive_count = len(all_mgrs) - active_count

    print(f"Written {len(all_mgrs)} managers to {args.output}")
    print(f"  Active   : {active_count}")
    print(f"  Inactive : {inactive_count}")
    if args.active_only:
        print("  (--active-only flag set, inactive excluded)")


if __name__ == "__main__":
    main()
