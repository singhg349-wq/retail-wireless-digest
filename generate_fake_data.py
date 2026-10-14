"""Generate one week of realistic but FAKE data for the Wireless District Digest demo.

Everything in here is invented: stores, people, IDs, sales. There is no real retailer,
carrier, or customer data. The same SEED always produces exactly the same files, so a demo
run is repeatable.

Output (default ./data; change with --out):
  roster.csv            who works where, and their role
  till_transactions.csv line-level POS rows (one row per item category in a transaction)
  carrier_activations.csv  activations, as if exported from the carrier's portal
  weekly_stats.csv       the weekly self-reported forms, with deliberate mistakes planted
  answer_key.json       which mistakes were planted, so we can prove the digest catches them

Usage:
  python generate_fake_data.py                       # the demo set in ./data
  python generate_fake_data.py --out samples/busy_week --week-start 2026-09-28 --seed 7 \\
      --busy --no-answer-key                         # a second, busier week
"""

import argparse
import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 42
WEEK_START = date(2026, 9, 21)  # a Monday
DAYS = 7
OPEN_HOUR, CLOSE_HOUR = 9, 21
STORES = list(range(201, 211))  # 10 made-up store numbers

# How hard each store leans on wireless staff to cover cash/shipping:
# (min, max) number of cash sessions per wireless associate this week.
CASH_PRESSURE = {
    201: (1, 2), 202: (0, 1), 203: (2, 4), 204: (6, 9), 205: (1, 3),
    206: (0, 2), 207: (3, 5), 208: (1, 2), 209: (7, 10), 210: (2, 3),
}
# Busier profile matching what the user saw on the floor (15–17 of 35 hours on cash). With
# seed 7 the heaviest stores come out at roughly 10–16 hours per wireless associate.
BUSY_CASH_PRESSURE = {
    201: (2, 4), 202: (1, 3), 203: (8, 12), 204: (22, 28), 205: (2, 5),
    206: (1, 3), 207: (14, 18), 208: (2, 4), 209: (24, 30), 210: (4, 7),
}
# Latest minute past the hour a cash stint may end. Stints start at :00–:10 and last at most
# 45 minutes, so 59 never trims anything. The busy profile uses 49: with that many stints,
# two can land in back-to-back hours, and ending by :49 keeps them at least 11 minutes apart,
# so the digest (which merges gaps of 10 minutes or less) counts them separately, as planned.
STINT_END_BY_MINUTE = 59
# Chance that a wireless associate's weekly stats form has a mistake.
FORM_ERROR_RATE = 0.55

FIRST_NAMES = [
    "Aman", "Priya", "Jordan", "Taylor", "Harjot", "Mei", "Liam", "Noor", "Ethan", "Simran",
    "Olivia", "Arjun", "Chloe", "Daniel", "Fatima", "Gurleen", "Hannah", "Isaac", "Jasleen",
    "Kevin", "Leah", "Manveer", "Nathan", "Ava", "Ravi", "Sophie", "Tyler", "Zara", "Owen", "Riya",
]
LAST_NAMES = [
    "Brar", "Chen", "Dhillon", "Gill", "Grewal", "Khan", "Lee", "MacDonald", "Nguyen", "Patel",
    "Sandhu", "Sharma", "Singh", "Smith", "Tremblay", "Wilson", "Wong", "Young", "Martin", "Roy",
]

DATA_DIR = Path(__file__).parent / "data"
rng = random.Random(SEED)


def fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M")


def random_time(day: date, start_hour=OPEN_HOUR, end_hour=CLOSE_HOUR) -> datetime:
    minute = rng.randrange((end_hour - start_hour) * 60)
    return datetime(day.year, day.month, day.day, start_hour) + timedelta(minutes=minute)


def build_roster():
    ids = rng.sample(range(1_000_000, 10_000_000), len(STORES) * 8)  # 7-digit employee IDs
    names = set()
    roster = []
    roles = ["manager", "wireless", "wireless", "cashier", "cashier", "cashier", "tech", "print"]
    for store in STORES:
        for role in roles:
            while True:
                name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
                if name not in names:
                    names.add(name)
                    break
            emp_id = ids.pop()
            email = name.lower().replace(" ", ".") + f".{store}@example.com"
            roster.append({"employee_id": str(emp_id), "name": name, "store": store,
                           "role": role, "email": email})
    return roster


class Till:
    def __init__(self):
        self.rows = []
        self.next_txn = 100000

    def txn(self, store, register, cashier_id, ts, lines, sales_associate_id=""):
        """lines: list of (category, qty, amount)."""
        self.next_txn += 1
        for category, qty, amount in lines:
            self.rows.append({
                "txn_id": self.next_txn, "store": store, "register": register,
                "cashier_id": cashier_id, "sales_associate_id": sales_associate_id,
                "timestamp": fmt(ts), "category": category, "qty": qty,
                "amount": f"{amount:.2f}",
            })


def generate(answer_key=True):
    roster = build_roster()
    till = Till()
    activations = []
    days = [WEEK_START + timedelta(d) for d in range(DAYS)]
    actuals = {}       # employee_id -> true weekly numbers
    planned_cash = {}  # employee_id -> planned minutes on cash (for sanity checks)

    for store in STORES:
        staff = [r for r in roster if r["store"] == store]
        cashiers = [r for r in staff if r["role"] == "cashier"]
        wireless = [r for r in staff if r["role"] == "wireless"]

        # 1) Normal front-end and shipping traffic, rung by regular cashiers.
        for day in days:
            for _ in range(40):
                c = rng.choice(cashiers)
                till.txn(store, f"FE-{rng.randint(1, 3)}", c["employee_id"], random_time(day),
                         [("general", rng.randint(1, 5), rng.uniform(5, 120))])
            for _ in range(5):
                c = rng.choice(cashiers)
                till.txn(store, "SHIP", c["employee_id"], random_time(day),
                         [("shipping", 1, rng.uniform(12, 60))])

        for w in wireless:
            emp = w["employee_id"]

            # 2) The associate's real wireless results for the week.
            n_act = rng.randint(3, 12)
            attach_target = rng.randint(5, 25)
            dp_target = rng.randint(1, 8)
            for _ in range(n_act):
                activations.append({"store": store, "employee_id": emp,
                                    "activated_at": fmt(random_time(rng.choice(days))),
                                    "line_type": rng.choice(["new", "port-in", "add-a-line"])})

            attach_left, dp_left = attach_target, dp_target
            while attach_left > 0 or dp_left > 0:
                lines = []
                if attach_left > 0:
                    q = min(attach_left, rng.randint(1, 3))
                    attach_left -= q
                    lines.append(("accessory", q, q * rng.uniform(20, 80)))
                if dp_left > 0 and rng.random() < 0.5:
                    dp_left -= 1
                    lines.append(("device_protection", 1, rng.uniform(99, 199)))
                if not lines:
                    continue
                ts = random_time(rng.choice(days))
                if rng.random() < 0.2:
                    # Customer paid at the front; cashier rings it, wireless gets the credit.
                    till.txn(store, f"FE-{rng.randint(1, 3)}", rng.choice(cashiers)["employee_id"],
                             ts, lines, sales_associate_id=emp)
                else:
                    till.txn(store, "WIRELESS", emp, ts, lines, sales_associate_id=emp)

            actuals[emp] = {"activations": n_act, "attach": attach_target,
                            "device_protection": dp_target}

            # 3) Time pulled onto cash/shipping: back-to-back transactions in sessions.
            lo, hi = CASH_PRESSURE[store]
            n_sessions = rng.randint(lo, hi)
            used_slots = set()
            planned = 0
            for _ in range(n_sessions):
                while True:  # one session per (day, hour) slot so sessions never overlap
                    slot = (rng.choice(days), rng.randrange(OPEN_HOUR, CLOSE_HOUR - 1))
                    if slot not in used_slots:
                        used_slots.add(slot)
                        break
                day, hour = slot
                ts = datetime(day.year, day.month, day.day, hour, rng.randint(0, 10))
                duration = rng.randint(15, 45)
                end = min(ts + timedelta(minutes=duration),
                          ts.replace(minute=STINT_END_BY_MINUTE))
                register = "SHIP" if rng.random() < 0.25 else f"FE-{rng.randint(1, 3)}"
                category = "shipping" if register == "SHIP" else "general"
                first = ts
                while ts <= end:
                    till.txn(store, register, emp, ts,
                             [(category, rng.randint(1, 4), rng.uniform(5, 120))])
                    last = ts
                    ts += timedelta(minutes=rng.randint(1, 4))
                planned += int((last - first).total_seconds() // 60)
            planned_cash[emp] = planned

    # 4) Weekly stats forms, with deliberate mistakes.
    forms, planted = [], []
    store_activations = {}
    for r in roster:
        if r["role"] == "wireless":
            store_activations[r["store"]] = (store_activations.get(r["store"], 0)
                                             + actuals[r["employee_id"]]["activations"])
    for r in roster:
        if r["role"] != "wireless":
            continue
        emp = r["employee_id"]
        form = {"week_ending": str(WEEK_START + timedelta(DAYS - 1)), "store": r["store"],
                "employee_id": emp, "name": r["name"], **actuals[emp]}
        if rng.random() < FORM_ERROR_RATE:
            kind = rng.choice(["typo", "swapped", "store_total", "wrong_id", "missing"])
            if kind == "swapped" and form["attach"] == form["device_protection"]:
                kind = "typo"
            if kind == "typo":
                field = rng.choice(["activations", "attach", "device_protection"])
                form[field] = max(0, form[field] + rng.choice([-3, -2, -1, 1, 2, 3]))
                if form[field] == actuals[emp][field]:
                    form[field] += 1
            elif kind == "swapped":
                form["attach"], form["device_protection"] = (form["device_protection"],
                                                             form["attach"])
            elif kind == "store_total":
                form["activations"] = store_activations[r["store"]]
            elif kind == "wrong_id":
                digits = list(emp)
                i = rng.randrange(1, 7)
                digits[i] = str((int(digits[i]) + rng.randint(1, 9)) % 10)
                form["employee_id"] = "".join(digits)
            planted.append({"employee_id": emp, "name": r["name"], "store": r["store"],
                            "error": kind})
            if kind == "missing":
                continue
        forms.append(form)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_csv("roster.csv", roster)
    write_csv("till_transactions.csv", till.rows)
    write_csv("carrier_activations.csv", activations)
    write_csv("weekly_stats.csv", forms)
    key_path = DATA_DIR / "answer_key.json"
    if answer_key:
        key_path.write_text(json.dumps(
            {"planted_form_errors": planted, "planned_cash_minutes": planned_cash,
             "true_actuals": actuals}, indent=2))
    elif key_path.exists():
        key_path.unlink()  # a leftover key would make the digest label this set "fake demo"

    print(f"Wrote fake data to {DATA_DIR}")
    print(f"  roster:            {len(roster)} people across {len(STORES)} stores")
    print(f"  till rows:         {len(till.rows)}")
    print(f"  carrier activations:  {len(activations)}")
    print(f"  weekly stats forms: {len(forms)} submitted, {len(planted)} with planted mistakes")


def write_csv(name, rows):
    with open(DATA_DIR / name, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    global DATA_DIR, WEEK_START, CASH_PRESSURE, STINT_END_BY_MINUTE, rng
    parser = argparse.ArgumentParser(description="Generate a week of fake store data.")
    parser.add_argument("--out", type=Path, default=DATA_DIR, help="folder to write into")
    parser.add_argument("--week-start", type=date.fromisoformat, default=WEEK_START,
                        help="Monday the week starts on, e.g. 2026-09-28")
    parser.add_argument("--seed", type=int, default=SEED,
                        help="different seed = different people and numbers")
    parser.add_argument("--busy", action="store_true",
                        help="heavy stores lose roughly 10-16 h per wireless associate to cash")
    parser.add_argument("--no-answer-key", action="store_true",
                        help="leave out answer_key.json so the folder looks like a real export")
    args = parser.parse_args()
    DATA_DIR, WEEK_START, rng = args.out, args.week_start, random.Random(args.seed)
    if args.busy:
        CASH_PRESSURE, STINT_END_BY_MINUTE = BUSY_CASH_PRESSURE, 49
    generate(answer_key=not args.no_answer_key)


if __name__ == "__main__":
    main()
