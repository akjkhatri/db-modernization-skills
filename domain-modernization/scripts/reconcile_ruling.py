#!/usr/bin/env python3
"""Record finance's ruling on a reconcile difference: which value is correct, legacy or migrated.

A difference between legacy and the new system is not automatically a migration bug: sometimes the
legacy number was the odd one all along. Finance decides, for a stated reporting period, and the
ruling is built into 04_reconcile.sql the next time the data migration is generated.

  new-correct     legacy was wrong, the migrated value stands (a legacy correction). The rows count as
                  ruled_rows, not differences. In a CLOSED period the reported figure is not restated, so
                  --disclosure is required (where the difference is disclosed / corrected instead).
  legacy-correct  the migrated value is wrong: fix the mapping. The check stays MISMATCH until it is fixed.

Usage:
  reconcile_ruling.py add --context billing --check "money per row" --target invoice_line.line_amount \\
      --key "5002|1" [--key ...|--key "*"] --decision new-correct|legacy-correct \\
      --period 2025-12 --period-status open|closed [--disclosure "..."] \\
      --adr ADR-0007 --by "Name:data-owner" --note "why"
  reconcile_ruling.py list [--context billing]

  --check   money per row | money nulls | invariant   (keys: legacy keys, or the invariant's keys)
  --target  <table>.<column> as reconcile prints it, or the invariant's name
  --key "*" covers every difference of that check (only with new-correct; use sparingly)
Rulers must hold a role in standards.json data_migration.reconcile.ruling_roles (default data-owner).
"""
import argparse
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402

CHECKS = ("money per row", "money nulls", "invariant")


def path(root):
    return d.mem(root, "decisions", "reconcile_rulings.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ = sub.add_parser("add")
    a_.add_argument("--context", required=True)
    a_.add_argument("--check", required=True, choices=CHECKS)
    a_.add_argument("--target", required=True)
    a_.add_argument("--key", action="append", required=True, dest="keys")
    a_.add_argument("--decision", required=True, choices=["new-correct", "legacy-correct"])
    a_.add_argument("--period", required=True, help="reporting period the ruling applies to, e.g. 2025-12 or FY2025")
    a_.add_argument("--period-status", required=True, choices=["open", "closed"], dest="period_status")
    a_.add_argument("--disclosure", help="closed period: where the difference is disclosed or corrected")
    a_.add_argument("--adr", required=True)
    a_.add_argument("--by", required=True, help='"Name:role"')
    a_.add_argument("--note", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--context")
    a = ap.parse_args()
    root = d.find_root(a.root)
    data = d.load_json(path(root), default={"rulings": []})

    if a.cmd == "list":
        rows = [r for r in data["rulings"] if not a.context or r["context"] == a.context]
        for r in rows:
            print(f"{r['id']}  {r['context']}  {r['check']:<13} {r['target']:<28} {','.join(r['keys']):<14} "
                  f"{r['decision']:<14} {r['period']} ({r['period_status']})  {r['by']}  {r['adr']}")
        print(f"{len(rows)} ruling(s)")
        return

    if a.context not in d.list_contexts(root):
        d.die(f"unknown context {a.context}")
    name, role = d.parse_approver(a.by)
    roles = ((d.load_standards(root).get("data_migration") or {}).get("reconcile") or {}).get("ruling_roles", ["data-owner"])
    if role not in roles:
        d.die(f"a ruling on which value is correct needs {' or '.join(roles)} (--by \"Name:{roles[0]}\"), "
              f"got {role or 'no role'}: finance decides, not the migration team")
    if "*" in a.keys and (a.decision != "new-correct" or len(a.keys) > 1):
        d.die('--key "*" (every difference of the check) is only for new-correct, on its own')
    if a.decision == "new-correct" and a.period_status == "closed" and not a.disclosure:
        d.die("the period is closed: its reported figures are not restated. Give --disclosure with where the "
              "difference is disclosed or corrected (e.g. 'prior-period adjustment in 2026-01')")
    aidx = d.load_json(d.mem(root, "decisions", "index.json"), default={"items": []})
    adr = next((it for it in aidx["items"] if it["id"] == a.adr.upper()), None)
    if adr is None:
        d.die(f"{a.adr} not found: record the decision first with memory_record.py new ADR")
    if adr.get("status") in ("rejected", "superseded"):
        d.die(f"{adr['id']} is {adr['status']}")
    rid = f"RR-{len(data['rulings']) + 1:04d}"
    data["rulings"].append({"id": rid, "context": a.context, "check": a.check, "target": a.target, "keys": a.keys,
                            "decision": a.decision, "period": a.period, "period_status": a.period_status,
                            "disclosure": a.disclosure, "adr": adr["id"], "by": f"{name} ({role})", "role": role,
                            "at": d.now_iso(), "note": a.note})
    d.save_json(path(root), data)
    msg = (f"{rid}: {a.decision} for {a.check} {a.target} [{', '.join(a.keys)}], period {a.period} ({a.period_status}), "
           f"by {name}, {adr['id']}")
    d.journal(root, "domain-modernization", "reconcile-ruling", msg, by=a.by)
    print(msg)
    print(f"Regenerate the data migration so 04_reconcile.sql applies it: generate_data_migration.py --context {a.context}")


if __name__ == "__main__":
    main()
