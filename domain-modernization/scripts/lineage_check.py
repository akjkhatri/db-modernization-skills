#!/usr/bin/env python3
"""Check a context's legacy -> target lineage (mapping/<ctx>.json) against the model and the
legacy baseline. This check keeps the legacy schema in view for every design decision.

Errors (block acceptance):
  * mapping refers to non-existent legacy or target objects
  * target table with no declared legacy source (and not explicitly declared new)
  * target column of a migrated table with no mapping (and not generated/identity/default)
  * legacy PII mapped into a target column classified pii=none
  * type-changing direct mapping with no transform (e.g. text date -> date)
Warnings: pending legacy columns in this context's legacy domains, mappings pulling from
other domains, open high/critical legacy findings on source tables, unknown transform refs.

Usage: python lineage_check.py --context billing [--root <repo>] [--show-pending 30]
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402

GENERATED_RULES = {"generated", "constant", "new"}
RULES = {"direct", "transform", "lookup", "derived", "split", "merge"} | GENERATED_RULES


def check(root, ctx, show_pending):
    m = d.load_model(root, ctx)
    mp = d.load_mapping(root, ctx)
    cat = d.load_catalog(root)
    std = d.load_standards(root)
    F = d.load_findings(root)
    pii_src = {p["ref"]: p["class"] for p in F.get("pii_inventory", [])}
    doms = {x["name"]: x for x in d.load_domains(root).get("domains", [])}
    E, W, I = [], [], []
    schema = m["schema"]
    ttables = {t["name"]: t for t in m["tables"]}
    tcols = {f"{schema}.{t['name']}.{c['name']}": (t, c) for t in m["tables"] for c in t["columns"]}
    audit_names = {c["name"] for c in std.get("audit_columns", {}).get("columns", [])} | {std.get("optimistic_locking", {}).get("column", {}).get("name")}
    legacy_cols = {f"{k}.{c['name'].lower()}": c for k, t in cat["tables"].items() for c in t["columns"]}

    tm_by_target = {}
    for tm in mp.get("table_mappings", []):
        tgt = tm["target"].lower()
        if not tgt.startswith(schema + ".") or tgt.split(".", 1)[1] not in ttables:
            E.append(f"table mapping target {tm['target']} not in {ctx} model")
            continue
        tm_by_target[tgt] = tm
        for s in tm.get("sources", []):
            if s.lower() not in cat["tables"]:
                E.append(f"table mapping source {s} not in legacy catalog")
        if not tm.get("sources") and not tm.get("notes"):
            E.append(f"{tm['target']}: declared new (no sources) but no notes explaining why")
    for tn, t in ttables.items():
        if f"{schema}.{tn}" not in tm_by_target:
            E.append(f"{schema}.{tn}: no table_mappings entry (add legacy sources, or sources: [] with notes if genuinely new)")

    cm_by_target = {}
    for cm in mp.get("column_mappings", []):
        tgt = cm["target"].lower()
        rule = cm.get("rule", "direct")
        if rule not in RULES:
            E.append(f"{cm['target']}: unknown rule '{rule}'")
        if tgt not in tcols:
            E.append(f"column mapping target {cm['target']} not in {ctx} model")
            continue
        cm_by_target[tgt] = cm
        t, c = tcols[tgt]
        srcs = [s.lower() for s in cm.get("sources", [])]
        for s in srcs:
            if s not in legacy_cols:
                E.append(f"{cm['target']}: legacy source {s} not in catalog")
        if rule not in GENERATED_RULES and not srcs:
            E.append(f"{cm['target']}: rule '{rule}' needs sources")
        if rule == "lookup" and not cm.get("lookup"):
            E.append(f"{cm['target']}: rule lookup needs 'lookup': '<schema.table>' of the referenced target table")
        elif rule == "lookup":
            ls_, lt_ = (cm["lookup"].lower().split(".") + [""])[:2]
            if ls_ == schema:
                if lt_ not in ttables:
                    E.append(f"{cm['target']}: lookup table {cm['lookup']} not in this model")
            else:
                owner = next((x for x in d.list_contexts(root) if d.load_model(root, x)["schema"] == ls_), None)
                ext = [r for r in m.get("external_refs", []) if r["column"] == f"{t['name']}.{c['name']}"]
                if not ext:
                    E.append(f"{cm['target']}: lookup into another context ({cm['lookup']}) but the column is not declared in external_refs")
                if owner is None:
                    W.append(f"{cm['target']}: lookup {cm['lookup']}: that context is not designed yet; this data migration must wait for its id map")
                elif lt_ not in {x["name"] for x in d.load_model(root, owner)["tables"]}:
                    E.append(f"{cm['target']}: lookup table {cm['lookup']} not in context {owner}")
        if rule in ("transform", "derived", "split", "merge") and not cm.get("transform"):
            E.append(f"{cm['target']}: rule '{rule}' needs a transform expression")
        # PII must not be laundered
        src_pii = sorted({pii_src[s] for s in srcs if s in pii_src})
        if src_pii and c.get("pii") in (None, "none"):
            E.append(f"{cm['target']}: legacy source is PII ({', '.join(src_pii)}) but target pii={c.get('pii')}")
        # type changes need explicit transforms
        if rule == "direct" and len(srcs) == 1 and srcs[0] in legacy_cols:
            lf = d.type_family(legacy_cols[srcs[0]]["data_type"])
            tf = d.type_family(c["type"])
            compatible = lf == tf or {lf, tf} <= {"int", "num"} or {lf, tf} <= {"ts", "tstz"} or {lf, tf} <= {"money", "num"}
            if not compatible:
                E.append(f"{cm['target']}: direct mapping {legacy_cols[srcs[0]]['data_type']} -> {c['type']} needs rule transform with an explicit conversion")
            elif {lf, tf} == {"ts", "tstz"}:
                W.append(f"{cm['target']}: timestamp -> timestamptz; make the legacy time zone explicit (e.g. AT TIME ZONE 'Europe/London') per ADR")
            elif {lf, tf} == {"money", "num"}:
                W.append(f"{cm['target']}: money -> numeric; cast via ::numeric (money output is locale-formatted)")
        if cm.get("transform"):
            used = tm_by_target.get(f"{schema}.{t['name']}", {}).get("sources", [])
            used_names = {u.split(".")[-1].lower() for u in used}
            for tbl, col in re.findall(r'\b"?([A-Za-z_][A-Za-z0-9_]*)"?\."?([A-Za-z_][A-Za-z0-9_]*)"?', cm["transform"]):
                if tbl.lower() in ("migration_map", "legacy_src", "pg_catalog") or tbl.lower() == schema:
                    continue
                if tbl.lower() not in used_names:
                    W.append(f"{cm['target']}: transform references '{tbl}' which is not a source of {t['name']}'s table mapping")
                elif not any(k.endswith(f".{tbl.lower()}.{col.lower()}") for k in legacy_cols):
                    E.append(f"{cm['target']}: transform references {tbl}.{col} which is not in the legacy catalog")

    for tgt, (t, c) in tcols.items():
        tm = tm_by_target.get(f"{schema}.{t['name']}")
        if not tm or not tm.get("sources"):
            continue
        if tgt in cm_by_target:
            continue
        auto = c.get("identity") or c.get("default") is not None or c["name"] in audit_names
        if not auto:
            E.append(f"{tgt}: no column mapping (map it, or rule new/constant/generated)")

    # legacy-side view: coverage of this context's legacy domains
    ctx_tables = set()
    for dn in m.get("legacy_domains", []):
        if dn not in doms:
            E.append(f"legacy domain {dn} not in domains.json")
            continue
        ctx_tables |= set(doms[dn]["tables"])
    shared = set()
    for dn in m.get("legacy_domains", []):
        shared |= set(doms.get(dn, {}).get("shared_tables", []))
    used_tables = {s.lower() for tm in mp.get("table_mappings", []) for s in tm.get("sources", [])}
    for t in sorted(used_tables - ctx_tables - shared):
        W.append(f"source {t} is outside this context's legacy domains {m.get('legacy_domains')} (ownership?)")
    cov = d.compute_coverage(root)
    pending = [r[0] for r in cov["columns"] if r[2] == "pending" and ".".join(r[0].split(".")[:2]) in ctx_tables] if cov else []
    if pending:
        W.append(f"{len(pending)} legacy columns in {m.get('legacy_domains')} still pending (map or retire): "
                 + ", ".join(pending[:show_pending]) + (" ..." if len(pending) > show_pending else ""))
    for f in F.get("findings", []):
        if f["status"] == "open" and f["severity"] in ("critical", "high") and ".".join(f["object"].split(".")[:2]) in used_tables:
            I.append(f"{f['id']} {f['severity']} {f['object']}: {f['message']}")
    stats = {"target_columns": len(tcols), "mapped": len(cm_by_target), "pending_legacy": len(pending)}
    return E, W, I, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--context", required=True)
    ap.add_argument("--show-pending", type=int, default=30)
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    root = d.find_root(a.root)
    E, W, I, st = check(root, a.context, a.show_pending)
    for e in E:
        print("ERROR  " + e)
    for w in W:
        print("WARN   " + w)
    if I:
        print("\nOpen high/critical legacy findings on source tables (make sure the design resolves them):")
        for i in I:
            print("  " + i)
    print(f"\n{a.context}: {st['mapped']}/{st['target_columns']} target columns mapped explicitly · "
          f"{st['pending_legacy']} legacy columns pending in its domains · {len(E)} error(s), {len(W)} warning(s)")
    sys.exit(1 if E or (a.strict and W) else 0)


if __name__ == "__main__":
    main()
