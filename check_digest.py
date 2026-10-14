"""Check the digest against the answer key written by generate_fake_data.py.

Exits with code 1 if any planted weekly-stats mistake is missed, anything is falsely
flagged, or computed cash time doesn't equal planned time + HANDLING_MINUTES per stint.

Usage:
  python generate_fake_data.py && python check_digest.py
  python check_digest.py --data some_folder      # any folder that has an answer_key.json
"""

import argparse
import json
import sys
from pathlib import Path

import wireless_digest as wd


def main():
    parser = argparse.ArgumentParser(description="Check the digest against an answer key.")
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "data")
    data_dir = parser.parse_args().data
    key = json.loads((data_dir / "answer_key.json").read_text())
    data = wd.load(data_dir)
    wireless = {r["employee_id"]: r for r in data["roster"] if r["role"] == "wireless"}
    actual = wd.actual_weekly_stats(data["till_transactions"], data["carrier_activations"],
                                   set(wireless))
    issues, _ = wd.audit_forms(data["weekly_stats"], wireless, actual)
    cash = wd.cash_time_by_associate(data["till_transactions"], set(wireless))

    failures = []
    if actual != key["true_actuals"]:
        failures.append("actual weekly stats rebuilt from till/carrier don't match the true values")
    planted = {p["employee_id"] for p in key["planted_form_errors"]}
    flagged = {i.employee_id for i in issues}
    if planted - flagged:
        failures.append(f"missed planted mistakes: {sorted(planted - flagged)}")
    if flagged - planted:
        failures.append(f"false alarms: {sorted(flagged - planted)}")
    for emp, planned in key["planned_cash_minutes"].items():
        expected = planned + wd.HANDLING_MINUTES * cash[emp].sessions
        if cash[emp].minutes != expected:
            failures.append(f"cash minutes for {emp}: got {cash[emp].minutes}, expected {expected}")

    print(f"Planted mistakes: {len(planted)} | flagged: {len(flagged)} | "
          f"missed: {len(planted - flagged)} | false alarms: {len(flagged - planted)}")
    print(f"Cash time checked for {len(key['planned_cash_minutes'])} wireless associates")
    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
