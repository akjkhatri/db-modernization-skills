#!/usr/bin/env python3
"""Integrity check of the whole .dbmod/ memory. Run before finishing any session that
changed memory, and in CI on the application repo (exit code 1 = errors).

Checks: every reference (lineage, domains, relationships, requirement/ADR ids, snapshots)
points at something that exists; no table claimed by two domains or two contexts;
mapped-and-retired contradictions; registry vs folders.

Usage: python memory_validate.py [--root <repo>] [--strict]   (--strict: warnings fail too)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    root = d.find_root(a.root)
    errors, warns = [], []
    E, W = errors.append, warns.append

    man = d.load_manifest(root)
    d.load_standards(root)
    if man.get("schema_version") != d.SCHEMA_VERSION:
        W(f"manifest schema_version {man.get('schema_version')} != tool {d.SCHEMA_VERSION}")

    cat = d.load_catalog(root, required=False)
    legacy_cols = set()
    if cat:
        for k, t in cat["tables"].items():
            if k != d.table_key(t["schema"], t["name"]):
                E(f"catalog key {k} does not match schema/name")
            names = [c["name"].lower() for c in t["columns"]]
            if len(names) != len(set(names)):
                E(f"duplicate column names in legacy {k}")
            for c in names:
                legacy_cols.add(f"{k}.{c}")
        for r in d.load_json(d.mem(root, "legacy", "relationships.json"), default={"relationships": []})["relationships"]:
            for tk in (r["table"], r["ref_table"]):
                if tk not in cat["tables"]:
                    E(f"relationship references unknown table {tk}")
        seen = {}
        for dm in d.load_domains(root).get("domains", []):
            for tk in dm.get("tables", []):
                if tk not in cat["tables"]:
                    E(f"domain {dm['name']} lists unknown table {tk}")
                if tk in seen:
                    E(f"table {tk} is in domains {seen[tk]} and {dm['name']} (use shared_tables for hubs)")
                seen[tk] = dm["name"]
        unassigned = [k for k in cat["tables"] if k not in seen]
        if seen and unassigned:
            W(f"{len(unassigned)} legacy tables not assigned to any domain (e.g. {', '.join(unassigned[:5])})")

    req_ids = {x["id"] for x in d.load_json(d.mem(root, "requirements", "index.json"), default={"items": []})["items"]}
    adr_items = d.load_json(d.mem(root, "decisions", "index.json"), default={"items": []})["items"]
    adr_ids = {x["id"] for x in adr_items}
    for kind, folder in (("REQ", "requirements"), ("ADR", "decisions")):
        for it in d.load_json(d.mem(root, folder, "index.json"), default={"items": []})["items"]:
            if not os.path.exists(d.mem(root, it["file"])):
                E(f"{it['id']} file missing: {it['file']}")
            for rel in it.get("related", []):
                if rel not in req_ids | adr_ids:
                    W(f"{it['id']} relates to unknown {rel}")

    ctxs = d.list_contexts(root)
    registry = man["target"].get("contexts", {})
    for c in set(registry) ^ set(ctxs):
        E(f"context '{c}' is {'registered but has no model.json' if c in registry else 'on disk but not in manifest'}")
    target_cols, schemas, table_owner = {}, {}, {}
    for c in ctxs:
        m = d.load_model(root, c)
        if m.get("context") != c:
            E(f"{c}/model.json has context={m.get('context')}")
        sch = m.get("schema")
        if sch in schemas:
            E(f"schema '{sch}' used by contexts {schemas[sch]} and {c}")
        schemas[sch] = c
        for t in m.get("tables", []):
            tk = f"{sch}.{t['name']}"
            cols = [x["name"] for x in t.get("columns", [])]
            if len(cols) != len(set(cols)):
                E(f"duplicate columns in {tk}")
            for col in cols:
                target_cols[f"{tk}.{col}"] = c
            table_owner[tk] = c
        for r in m.get("requirements", []):
            if r not in req_ids:
                E(f"context {c} cites unknown requirement {r}")
        av = m.get("accepted_version")
        if av and not os.path.exists(os.path.join(d.context_dir(root, c), "snapshots", f"v{av:03d}.json")):
            E(f"context {c} accepted_version {av} has no snapshot")
        if m.get("status") == "accepted" and m.get("version") != av:
            E(f"context {c} status accepted but version {m.get('version')} != accepted_version {av}")

    mapped_by = {}
    for c, mp in d.load_all_mappings(root).items():
        if c not in ctxs:
            E(f"mapping/{c}.json has no matching context")
        for tm in mp.get("table_mappings", []):
            if tm["target"].lower() not in table_owner:
                E(f"[{c}] table mapping target {tm['target']} not in any model")
            for s in tm.get("sources", []):
                if cat and s.lower() not in cat["tables"]:
                    E(f"[{c}] table mapping source {s} not in legacy catalog")
        for cm in mp.get("column_mappings", []):
            if cm["target"].lower() not in target_cols:
                E(f"[{c}] mapping target {cm['target']} not in any model")
            elif target_cols[cm["target"].lower()] != c:
                E(f"[{c}] maps into {cm['target']} owned by context {target_cols[cm['target'].lower()]}")
            if not cm.get("sources") and cm.get("rule") not in ("generated", "constant", "new"):
                W(f"[{c}] {cm['target']} has no legacy source and rule={cm.get('rule')} (use rule new/generated/constant)")
            for s in cm.get("sources", []):
                if cat and s.lower() not in legacy_cols:
                    E(f"[{c}] mapping source {s} not in legacy catalog")
                mapped_by.setdefault(s.lower(), set()).add(c)
            for r in cm.get("requirements", []):
                if r not in req_ids:
                    W(f"[{c}] mapping {cm['target']} cites unknown {r}")
        for r in mp.get("retired", []):
            s = r["source"].lower()
            if cat and not (s in legacy_cols or (s.endswith(".*") and s[:-2] in cat["tables"])):
                E(f"[{c}] retired source {r['source']} not in legacy catalog")
            if s in mapped_by:
                W(f"{r['source']} is retired in {c} but mapped in {sorted(mapped_by[s])}")
            if not r.get("reason"):
                E(f"[{c}] retired {r['source']} has no reason")
            if r.get("decision") and r["decision"] not in adr_ids:
                W(f"[{c}] retired {r['source']} cites unknown {r['decision']}")
    multi = {k: v for k, v in mapped_by.items() if len(v) > 1}
    if multi:
        W(f"{len(multi)} legacy columns feed more than one context (fine for reference data, check ownership): "
          + ", ".join(list(multi)[:5]))

    # business-gate waivers (v1.2): an exception must not outlive its review date or its answer
    if os.path.exists(d.sme_path(root)):
        wv, fu = d.waiver_overview(root)
        for w in wv:
            if w["missing_review_date"]:
                W(f"{w['id']} is waived under {w['adr']} with no review date: set one with review.py sme review")
            elif w["overdue"]:
                E(f"{w['id']} waiver under {w['adr']} was due for review on {w['review_by']}"
                  + (f" (contexts relying on it: {', '.join(w['contexts'])})" if w["contexts"] else ""))
            if w["renewed_past_limit_without_senior"]:
                E(f"{w['id']} waiver was renewed past the limit of {w['max_renewals']} without "
                  f"{' or '.join(d.gate_settings(root)['renewal_roles_after_limit'])}: "
                  + ", ".join(x or "?" for x in w["renewed_past_limit_without_senior"]))
            elif w["at_limit"] and not w["overdue"]:
                W(f"{w['id']} waiver has been renewed {w['renewals']} time(s) (limit {w['max_renewals']}): "
                  "get the business's answer, or the next renewal needs senior sign-off")
        for f in fu:
            W(f"{f['adr']} still in force although the business answered {f['id']}: supersede or reject it")
    # finance rulings on reconcile differences (v1.5) must rest on a decision that is still in force
    rp = d.mem(root, "decisions", "reconcile_rulings.json")
    if os.path.exists(rp):
        adr_st = {it["id"]: it.get("status") for it in d.load_json(d.mem(root, "decisions", "index.json"), default={"items": []})["items"]}
        for r in d.load_json(rp, default={"rulings": []})["rulings"]:
            if adr_st.get(r["adr"]) in ("rejected", "superseded", None):
                W(f"{r['id']} (reconcile ruling, {r['context']} {r['target']}) rests on {r['adr']}, which is "
                  f"{adr_st.get(r['adr']) or 'missing'}: re-rule or remove it")
            if r["decision"] == "new-correct" and r["period_status"] == "closed" and not r.get("disclosure"):
                E(f"{r['id']} accepts a difference in closed period {r['period']} without a disclosure")
    for e in errors:
        print("ERROR  " + e)
    for w in warns:
        print("WARN   " + w)
    print(f"\n{len(errors)} error(s), {len(warns)} warning(s)")
    sys.exit(1 if errors or (a.strict and warns) else 0)


if __name__ == "__main__":
    main()
