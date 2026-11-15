#!/usr/bin/env python3
"""Cross-context consistency check: run whenever a context is created or changed, because
contexts are designed one requirement at a time and must keep fitting together.

Errors: schema reuse; external_ref to a missing context/entity or with a mismatching key type.
Warnings: same concept (table name) defined in several contexts; one legacy table driving
tables in several contexts; legacy columns mapped by several contexts; dependency cycles;
dependencies on contexts that are not accepted yet.

Usage: python check_conflicts.py [--root <repo>] [--context billing]   (context = focus the report)
"""
import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--context")
    a = ap.parse_args()
    root = d.find_root(a.root)
    ctxs = {c: d.load_model(root, c) for c in d.list_contexts(root)}
    maps = {c: d.load_mapping(root, c) for c in ctxs}
    E, W = [], []

    schemas = defaultdict(list)
    names = defaultdict(list)
    for c, m in ctxs.items():
        schemas[m["schema"]].append(c)
        for t in m["tables"]:
            names[t["name"]].append(c)
    for s, cs in schemas.items():
        if len(cs) > 1:
            E.append(f"schema {s} used by {cs}")
    for n, cs in names.items():
        if len(cs) > 1:
            W.append(f"table name '{n}' exists in {cs}: same concept modelled twice? If intentional "
                     "(different meaning per context), document it in each design.md")

    drivers = defaultdict(set)
    col_users = defaultdict(set)
    for c, mp in maps.items():
        for tm in mp.get("table_mappings", []):
            if tm.get("sources"):
                drivers[tm["sources"][0].lower()].add(c)
        for cm in mp.get("column_mappings", []):
            if cm.get("rule") == "lookup":
                continue
            for s in cm.get("sources", []):
                col_users[s.lower()].add(c)
    for t, cs in drivers.items():
        if len(cs) > 1:
            W.append(f"legacy table {t} drives target tables in {sorted(cs)}: a split is fine, but confirm which context owns the legacy rows")
    multi = {k: v for k, v in col_users.items() if len(v) > 1}
    if multi:
        W.append(f"{len(multi)} legacy columns copied into more than one context (data duplication across contexts): "
                 + ", ".join(f"{k}->{sorted(v)}" for k, v in list(multi.items())[:6]))

    deps = defaultdict(set)
    for c, m in ctxs.items():
        tab = {t["name"]: t for t in m["tables"]}
        for r in m.get("external_refs", []):
            oc = r["context"]
            deps[c].add(oc)
            if oc not in ctxs:
                W.append(f"{c}: external_ref {r['column']} -> context '{oc}' not designed yet (upstream pending: "
                         "record it in an ADR; the data migration must wait for that context's id map)")
                continue
            other = {t["name"]: t for t in ctxs[oc]["tables"]}
            if r["entity"] not in other:
                E.append(f"{c}: external_ref {r['column']} -> {oc}.{r['entity']} does not exist")
                continue
            ot = other[r["entity"]]
            pk = ot.get("primary_key", [])
            key_col = r.get("key") or (pk[0] if len(pk) == 1 else None)
            tn, cn = r["column"].split(".")
            lc = next((x for x in tab.get(tn, {}).get("columns", []) if x["name"] == cn), None)
            rc = next((x for x in ot["columns"] if x["name"] == key_col), None)
            if lc and rc and d.base_type(lc["type"]) != d.base_type(rc["type"]):
                E.append(f"{c}: {r['column']} is {lc['type']} but {oc}.{r['entity']}.{key_col} is {rc['type']}")
            if ctxs[oc].get("status") != "accepted" and not ctxs[oc].get("accepted_version"):
                W.append(f"{c} depends on {oc}, which has never been accepted")
    for c in deps:
        for oc in deps[c]:
            if c in deps.get(oc, set()):
                if c < oc:
                    W.append(f"bidirectional dependency between contexts {c} and {oc}: consider events/one-way reference")

    if a.context:
        E = [e for e in E if re.search(rf"\b{re.escape(a.context)}\b", e)]
        W = [w for w in W if re.search(rf"\b{re.escape(a.context)}\b", w)]
    for e in E:
        print("ERROR  " + e)
    for w in W:
        print("WARN   " + w)
    print(f"\n{len(ctxs)} context(s): {len(E)} error(s), {len(W)} warning(s)")
    if len(ctxs) > 1:
        print("dependencies: " + "; ".join(f"{c} -> {', '.join(sorted(v))}" for c, v in deps.items() if v) if any(deps.values()) else "dependencies: none")
    sys.exit(1 if E else 0)


if __name__ == "__main__":
    main()
