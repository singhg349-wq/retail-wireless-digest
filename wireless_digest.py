"""Wireless District Digest.

Reads a week of store data (four CSV files) and produces:
  <out>/digest.html   one-page summary for the District Manager
  <out>/emails/*.txt  draft correction emails for wrong weekly stats forms (NOT sent)

Usage:
  python wireless_digest.py                                   # fake demo data: ./data -> ./output
  python wireless_digest.py --data my_data --out my_output

The data folder must contain the files listed in REQUIRED_COLUMNS, with at least those columns.
"""

import argparse
import csv
import html
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---- Assumptions (change these to match how the real reports work) ----------------
# A wireless associate logged in on one of these registers is "on cash / shipping".
CASH_REGISTER_PREFIXES = ("FE", "SHIP")
# Transactions closer together than this belong to the same stint on cash.
SESSION_GAP_MINUTES = 10
# Time added for the last transaction of each stint (the till only stamps the start).
HANDLING_MINUTES = 3
# A store is flagged when its wireless team spent at least this long on cash in the week.
ATTENTION_HOURS = 5.0
# Weekly stats fields and where the "truth" for each comes from.
STAT_FIELDS = {
    "activations": "carrier activations export",
    "attach": "till (accessory units credited to you)",
    "device_protection": "till (device protection plans credited to you)",
}
FIELD_LABELS = {"activations": "Activations", "attach": "Attach",
                "device_protection": "Device protection"}
# Files the digest needs, and the columns it reads from each. Extra columns are ignored.
REQUIRED_COLUMNS = {
    "roster.csv": ["employee_id", "name", "store", "role", "email"],
    "till_transactions.csv": ["txn_id", "store", "register", "cashier_id",
                              "sales_associate_id", "timestamp", "category", "qty"],
    "carrier_activations.csv": ["employee_id"],
    "weekly_stats.csv": ["week_ending", "store", "employee_id", "name",
                        "activations", "attach", "device_protection"],
}
TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")
# -------------------------------------------------------------------------------------

BASE = Path(__file__).parent


class DataError(Exception):
    """The input files don't look the way the digest expects."""


def read_csv(data_dir, name):
    path = Path(data_dir) / name
    if not path.exists():
        raise DataError(f"Missing file: {path}")
    # utf-8-sig strips the invisible marker Excel puts at the start of "CSV UTF-8" exports.
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        found = [c.strip() for c in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS[name] if c not in found]
        if missing:
            raise DataError(f"{name} is missing column(s): {', '.join(missing)}\n"
                            f"  columns found: {', '.join(found) or '(none)'}")
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader]


def load(data_dir):
    return {name[:-4]: read_csv(data_dir, name) for name in REQUIRED_COLUMNS}


def parse_ts(text):
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise DataError(f"Can't read timestamp {text!r}; expected e.g. 2026-09-21 14:05")


@dataclass
class CashTime:
    minutes: int = 0
    sessions: int = 0
    transactions: int = 0
    shipping_transactions: int = 0


@dataclass
class FormIssue:
    employee_id: str
    name: str
    store: str
    missing_form: bool = False
    problems: list = field(default_factory=list)     # for the DM digest
    email_lines: list = field(default_factory=list)  # same problems, written to the associate

    def add(self, digest_text, email_text):
        self.problems.append(digest_text)
        self.email_lines.append(email_text)


def cash_time_by_associate(till_rows, wireless_ids):
    """Group each wireless associate's cash/shipping transactions into stints and time them."""
    seen = set()
    stamps = defaultdict(list)  # employee_id -> [(timestamp, register)]
    for row in till_rows:
        emp = row["cashier_id"]
        if emp not in wireless_ids or not row["register"].startswith(CASH_REGISTER_PREFIXES):
            continue
        if row["txn_id"] in seen:  # till rows are per item line; count each transaction once
            continue
        seen.add(row["txn_id"])
        stamps[emp].append((parse_ts(row["timestamp"]), row["register"]))

    result = {emp: CashTime() for emp in wireless_ids}
    for emp, items in stamps.items():
        items.sort()
        ct = result[emp]
        ct.transactions = len(items)
        ct.shipping_transactions = sum(1 for _, reg in items if reg.startswith("SHIP"))
        start = prev = items[0][0]
        for ts, _ in items[1:]:
            if (ts - prev).total_seconds() / 60 > SESSION_GAP_MINUTES:
                ct.minutes += int((prev - start).total_seconds() // 60) + HANDLING_MINUTES
                ct.sessions += 1
                start = ts
            prev = ts
        ct.minutes += int((prev - start).total_seconds() // 60) + HANDLING_MINUTES
        ct.sessions += 1
    return result


def actual_weekly_stats(till_rows, activation_rows, wireless_ids):
    actual = {emp: {f: 0 for f in STAT_FIELDS} for emp in wireless_ids}
    for row in activation_rows:
        if row["employee_id"] in actual:
            actual[row["employee_id"]]["activations"] += 1
    for row in till_rows:
        emp = row["sales_associate_id"]
        if emp not in actual:
            continue
        if row["category"] == "accessory":
            actual[emp]["attach"] += int(row["qty"])
        elif row["category"] == "device_protection":
            actual[emp]["device_protection"] += int(row["qty"])
    return actual


def audit_forms(forms, wireless, actual):
    """Compare each submitted form to the actual numbers. Returns (issues, correct_count)."""
    by_id = {f["employee_id"]: f for f in forms}
    by_name = {(f["name"], f["store"]): f for f in forms}
    store_activations = defaultdict(int)
    for emp, person in wireless.items():
        store_activations[person["store"]] += actual[emp]["activations"]

    issues, correct = [], 0
    for emp, person in wireless.items():
        issue = FormIssue(emp, person["name"], person["store"])
        form = by_id.get(emp)
        if form is None:
            form = by_name.get((person["name"], person["store"]))
            if form is None:
                issue.missing_form = True
                issue.add("No weekly stats form submitted this week.",
                          "We didn't receive your weekly stats form this week.")
                issues.append(issue)
                continue
            issue.add(f"Employee ID on the form is {form['employee_id']}, "
                      f"but the roster has {emp}.",
                      f"The employee ID on your form ({form['employee_id']}) doesn't match "
                      f"your ID on file ({emp}).")

        truth = actual[emp]
        reported = {}
        for f in STAT_FIELDS:
            if form[f] == "":
                issue.add(f"{FIELD_LABELS[f]} left blank (actual {truth[f]}).",
                          f"{FIELD_LABELS[f]} was left blank; the {STAT_FIELDS[f]} "
                          f"shows {truth[f]}.")
            else:
                reported[f] = int(form[f])
        wrong = [f for f in reported if reported[f] != truth[f]]

        if (set(wrong) == {"attach", "device_protection"}
                and reported["attach"] == truth["device_protection"]
                and reported["device_protection"] == truth["attach"]):
            issue.add(f"Attach and Device protection swapped "
                      f"(reported {reported['attach']}/{reported['device_protection']}, "
                      f"actual {truth['attach']}/{truth['device_protection']}).",
                      f"Your Attach ({reported['attach']}) and Device protection "
                      f"({reported['device_protection']}) look swapped; the till shows "
                      f"Attach {truth['attach']} and Device protection "
                      f"{truth['device_protection']}.")
            wrong = []
        if "activations" in wrong and reported["activations"] == store_activations[person["store"]]:
            issue.add(f"Activations {reported['activations']} is the whole store's total; "
                      f"actual {truth['activations']}.",
                      f"Your activations ({reported['activations']}) look like the store "
                      f"total; the carrier export shows {truth['activations']} for you.")
            wrong.remove("activations")
        for f in wrong:
            issue.add(f"{FIELD_LABELS[f]}: reported {reported[f]}, actual {truth[f]}.",
                      f"{FIELD_LABELS[f]}: you reported {reported[f]}, the "
                      f"{STAT_FIELDS[f]} shows {truth[f]}.")

        if issue.problems:
            issues.append(issue)
        else:
            correct += 1
    return issues, correct


def write_email(issue, person, manager, week_ending, folder):
    if issue.missing_form:
        intro = ["I couldn't find your weekly stats form for this week."]
        ask = "Could you fill it out and send it in today?"
    else:
        intro = ["Thanks for sending in your weekly stats. When I checked them against the till",
                 "and carrier reports, a few things didn't line up:"]
        ask = ("Could you double-check and resubmit the form? If you think the reports are\n"
               "wrong, reply to this email and we'll look into it.")
    lines = [
        f"To: {person['email']}",
        f"Cc: {manager['email'] if manager else ''}",
        f"Subject: Weekly stats correction needed - week ending {week_ending}",
        "",
        f"Hi {person['name'].split()[0]},",
        "",
        *intro,
        "",
    ]
    if not issue.missing_form:
        lines += [f"  - {line}" for line in issue.email_lines] + [""]
    lines += [ask, "", "Thanks!"]
    safe_name = person["name"].replace(" ", "_")
    path = folder / f"store{issue.store}_{issue.employee_id}_{safe_name}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def render_html(week_ending, store_rows, issues, correct, total_forms, emails, assoc_rows,
                banner):
    e = html.escape
    total_hours = sum(r["hours"] for r in store_rows)
    flagged = [r for r in store_rows if r["hours"] >= ATTENTION_HOURS]
    max_hours = max((r["hours"] for r in store_rows), default=1) or 1

    store_tr = []
    for r in store_rows:
        width = int(120 * r["hours"] / max_hours)  # px; the biggest store gets the full 120px
        flag = '<span class="flag">Needs attention</span>' if r["hours"] >= ATTENTION_HOURS else ""
        store_tr.append(
            f"<tr><td>Store {e(str(r['store']))}</td>"
            f"<td><div class='bar' style='width:{width}px'></div>{r['hours']:.1f} h</td>"
            f"<td>{r['sessions']}</td><td>{r['transactions']}</td><td>{r['shipping']}</td>"
            f"<td>{r['forms_ok']}/{r['forms_total']}</td><td>{flag}</td></tr>")

    assoc_tr = [
        f"<tr><td>Store {e(str(a['store']))}</td><td>{e(a['name'])}</td>"
        f"<td>{a['hours']:.1f} h</td><td>{a['sessions']}</td></tr>"
        for a in assoc_rows if a["hours"] > 0]

    issue_tr = [
        f"<tr><td>Store {e(str(i.store))}</td><td>{e(i.name)}</td>"
        f"<td><ul>{''.join(f'<li>{e(p)}</li>' for p in i.problems)}</ul></td></tr>"
        for i in sorted(issues, key=lambda i: (i.store, i.name))]

    attention = ", ".join(f"Store {r['store']} ({r['hours']:.1f} h)" for r in flagged) or "None"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Wireless District Digest</title>
<style>
 :root {{ color-scheme: light; }}
 html {{ background: #fff; }}
 body {{ font-family: Segoe UI, Arial, sans-serif; margin: 2rem auto; max-width: 1000px;
        color: #222; background: #fff; }}
 h1 {{ color: #cc0000; margin-bottom: 0; }}
 .demo {{ background: #fff3cd; border: 1px solid #e0c36b; padding: .5rem 1rem; margin: 1rem 0; }}
 .cards {{ display: flex; gap: 1rem; margin: 1.5rem 0; }}
 .card {{ flex: 1; border: 1px solid #ddd; border-radius: 8px; padding: 1rem; }}
 .card .big {{ font-size: 2rem; font-weight: bold; color: #cc0000; }}
 table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
 th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #eee; vertical-align: top; }}
 th {{ background: #f5f5f5; }}
 .bar {{ display: inline-block; height: .8rem; background: #cc0000; margin-right: .5rem;
         vertical-align: middle; }}
 .flag {{ background: #cc0000; color: white; padding: .1rem .5rem; border-radius: 4px; font-size: .85rem; }}
 ul {{ margin: 0; padding-left: 1.2rem; }}
 .method {{ font-size: .85rem; color: #555; }}
</style></head><body>
<h1>Wireless District Digest</h1>
<div>Week ending {e(week_ending)} &middot; {len(store_rows)} stores</div>
{banner}

<div class="cards">
 <div class="card"><div class="big">{total_hours:.1f} h</div>wireless time spent on cash/shipping instead of selling</div>
 <div class="card"><div class="big">{len(flagged)}</div>stores over {ATTENTION_HOURS:g} h: {e(attention)}</div>
 <div class="card"><div class="big">{correct}/{total_forms}</div>weekly stats forms correct</div>
 <div class="card"><div class="big">{emails}</div>correction emails drafted (not sent)</div>
</div>

<h2>Wireless time on cash, by store</h2>
<table><tr><th>Store</th><th>Hours on cash/shipping</th><th>Stints</th><th>Transactions</th>
<th>Shipping txns</th><th>Forms correct</th><th></th></tr>
{''.join(store_tr)}</table>

<h2>Weekly stats forms that need fixing</h2>
<table><tr><th>Store</th><th>Associate</th><th>What's wrong</th></tr>
{''.join(issue_tr) or '<tr><td colspan=3>All forms correct.</td></tr>'}</table>

<h2>Detail: wireless associates pulled onto cash</h2>
<table><tr><th>Store</th><th>Associate</th><th>Hours</th><th>Stints</th></tr>
{''.join(assoc_tr)}</table>

<div class="method"><b>How these numbers are calculated.</b>
A wireless associate is counted as "on cash" when their employee ID rings a transaction on a
register starting with {e(' or '.join(CASH_REGISTER_PREFIXES))}. Transactions less than
{SESSION_GAP_MINUTES} minutes apart are one stint; each stint is timed from first to last
transaction plus {HANDLING_MINUTES} minutes. Weekly stats are checked against:
{e('; '.join(f'{FIELD_LABELS[k]} = {v}' for k, v in STAT_FIELDS.items()))}.</div>
</body></html>"""


def build(data_dir, out_dir):
    data = load(data_dir)
    roster, till = data["roster"], data["till_transactions"]
    forms = data["weekly_stats"]

    wireless = {r["employee_id"]: r for r in roster if r["role"].lower() == "wireless"}
    if not wireless:
        raise DataError("roster.csv has nobody with role 'wireless'.")
    managers = {r["store"]: r for r in roster if r["role"].lower() == "manager"}
    stores = sorted({r["store"] for r in roster})
    week_ending = forms[0]["week_ending"] if forms else "unknown"

    cash = cash_time_by_associate(till, set(wireless))
    actual = actual_weekly_stats(till, data["carrier_activations"], set(wireless))
    issues, correct = audit_forms(forms, wireless, actual)

    email_dir = out_dir / "emails"
    email_dir.mkdir(parents=True, exist_ok=True)
    for old in email_dir.glob("*.txt"):  # only clear our own drafts, never the whole folder
        old.unlink()
    for issue in issues:
        write_email(issue, wireless[issue.employee_id], managers.get(issue.store),
                    week_ending, email_dir)

    issue_ids = {i.employee_id for i in issues}
    store_rows = []
    for store in stores:
        team = [emp for emp, p in wireless.items() if p["store"] == store]
        store_rows.append({
            "store": store,
            "hours": sum(cash[emp].minutes for emp in team) / 60,
            "sessions": sum(cash[emp].sessions for emp in team),
            "transactions": sum(cash[emp].transactions for emp in team),
            "shipping": sum(cash[emp].shipping_transactions for emp in team),
            "forms_ok": sum(1 for emp in team if emp not in issue_ids),
            "forms_total": len(team),
        })
    store_rows.sort(key=lambda r: r["hours"], reverse=True)
    assoc_rows = sorted(
        ({"store": p["store"], "name": p["name"], "hours": cash[emp].minutes / 60,
          "sessions": cash[emp].sessions} for emp, p in wireless.items()),
        key=lambda a: a["hours"], reverse=True)

    # generate_fake_data.py writes answer_key.json; real exports never have one.
    if (Path(data_dir) / "answer_key.json").exists():
        banner = ('<div class="demo"><b>Demo:</b> all data on this page is fake, '
                  'generated for illustration.</div>')
    else:
        banner = f'<div class="demo">Source data: {html.escape(str(data_dir))}</div>'
    page = render_html(week_ending, store_rows, issues, correct, len(wireless), len(issues),
                       assoc_rows, banner)
    (out_dir / "digest.html").write_text(page, encoding="utf-8")

    print(f"Week ending {week_ending}: {len(wireless)} wireless associates, {len(stores)} stores")
    print("Wireless hours on cash/shipping by store:")
    for r in store_rows:
        mark = "  <-- needs attention" if r["hours"] >= ATTENTION_HOURS else ""
        print(f"  Store {r['store']}: {r['hours']:5.1f} h{mark}")
    print(f"Weekly stats: {correct}/{len(wireless)} correct, {len(issues)} need fixing")
    print(f"Wrote {out_dir / 'digest.html'} and {len(issues)} draft emails in {email_dir}")


def main():
    parser = argparse.ArgumentParser(description="Build the Wireless District Digest.")
    parser.add_argument("--data", default=BASE / "data", type=Path,
                        help="folder with the four input CSVs (default: ./data, fake demo data)")
    parser.add_argument("--out", default=BASE / "output", type=Path,
                        help="folder to write digest.html and emails/ into (default: ./output)")
    args = parser.parse_args()
    try:
        build(args.data, args.out)
    except DataError as err:
        sys.exit(f"Data problem: {err}")


if __name__ == "__main__":
    main()
