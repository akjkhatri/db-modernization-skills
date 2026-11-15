#!/usr/bin/env python3
"""Targeted look-ups into the modernization memory, so you never need to load the whole
legacy catalog into context.

  search <term> [--limit 40]       legacy + target tables/columns whose name/comment matches
  table <schema.table>             full legacy picture of one table: columns, keys, relationships,
                                   findings, PII, domain, and where each column went (lineage)
  domain <name>                    tables of a legacy domain, findings, coverage, consuming contexts
  unmapped [--domain D] [--table schema.table] [--limit 200]
                                   legacy columns with no disposition yet (pending)
  lineage <ref>                    legacy ref -> target(s), or target ref -> legacy source(s)
  context <ctx>                    summary of a target context's model and mapping
  relationships <schema.table>     declared + inferred relationships touching a table
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402


def rels(root):
    return d.load_json(d.mem(root, "legacy", "relationships.json"), default={"relationships": []})["relationships"]


def resolve_table(cat, ref):
    ref = ref.lower()
    if ref in cat["tables"]:
        return ref
    hits = [k for k in cat["tables"] if k.split(".", 1)[1] == ref]
    if len(hits) == 1:
        return hits[0]
    if hits:
        d.die(f"ambiguous table '{ref}': {hits}")
    d.die(f"legacy table '{ref}' not in catalog (try: search {ref})")


def lineage_index(root):
    """legacy column ref -> list of (ctx, target, transform, status); target ref -> sources."""
    fwd, back, ret = {}, {}, {}
    for ctx, m in d.load_all_mappings(root).items():
        for cm in m.get("column_mappings", []):
            for s in cm.get("sources", []):
                fwd.setdefault(s.lower(), []).append((ctx, cm["target"], cm.get("transform") or cm.get("rule", "direct"), cm.get("status", "proposed")))
            back.setdefault(cm["target"].lower(), []).append((ctx, cm.get("sources", []), cm.get("transform") or "", cm.get("status", "proposed")))
        for r in m.get("retired", []):
            ret[r["source"].lower()] = (ctx, r.get("reason", ""), r.get("decision", ""))
    return fwd, back, ret


def cmd_search(root, a):
    cat = d.load_catalog(root)
    term = a.term.lower()
    rows = []
    for k, t in cat["tables"].items():
        if term in k or term in (t.get("comment") or "").lower():
            rows.append(["legacy", k, "", t.get("kind", "table"), (t.get("comment") or "")[:60]])
        for c in t["columns"]:
            if term in c["name"].lower() or term in (c.get("comment") or "").lower():
                rows.append(["legacy", k, c["name"], c["data_type"], (c.get("comment") or "")[:60]])
    for ctx in d.list_contexts(root):
        m = d.load_model(root, ctx)
        for t in m.get("tables", []):
            tk = f"{m['schema']}.{t['name']}"
            if term in tk:
                rows.append([f"target:{ctx}", tk, "", t.get("role", ""), (t.get("description") or "")[:60]])
            for c in t.get("columns", []):
                if term in c["name"].lower():
                    rows.append([f"target:{ctx}", tk, c["name"], c["type"], (c.get("description") or "")[:60]])
    print(d.md_table(["where", "table", "column", "type", "comment"], rows[: a.limit]) if rows else "(no matches)")
    if len(rows) > a.limit:
        print(f"... {len(rows) - a.limit} more (use --limit)")


def cmd_table(root, a):
    cat = d.load_catalog(root)
    k = resolve_table(cat, a.ref)
    t = cat["tables"][k]
    dom = d.legacy_table_domains(root).get(k, "(unassigned)")
    fwd, _, ret = lineage_index(root)
    print(f"# legacy {k} ({t.get('kind', 'table')}) · domain: {dom}"
          + (f" · rows≈{t['row_count']}" if t.get("row_count") is not None else ""))
    if t.get("comment"):
        print(t["comment"])
    pii = {p["ref"]: p["class"] for p in d.load_findings(root).get("pii_inventory", [])}
    rows = []
    table_ret = ret.get(f"{k}.*")
    for c in t["columns"]:
        ref = f"{k}.{c['name'].lower()}"
        if ref in fwd:
            disp = "; ".join(f"{ctx}:{tgt} [{st}]" for ctx, tgt, _, st in fwd[ref])
        elif ref in ret or table_ret:
            r = ret.get(ref) or table_ret
            disp = f"RETIRED ({r[0]}): {r[1]}"
        else:
            disp = "pending"
        rows.append([c["name"], c["data_type"], "" if c["nullable"] else "NOT NULL",
                     "PK" if c.get("is_pk") else ("UQ" if c.get("is_unique") else ""),
                     pii.get(ref, ""), (c.get("comment") or "")[:40], disp])
    print(d.md_table(["column", "type", "null", "key", "pii", "comment", "disposition"], rows))
    _print_rels(root, k)
    fs = [f for f in d.load_findings(root).get("findings", []) if f["object"].lower() == k or f["object"].lower().startswith(k + ".")]
    if fs:
        print("\n## Findings")
        print(d.md_table(["id", "sev", "rule", "object", "message"],
                         [[f["id"], f["severity"], f["rule"], f["object"], f["message"]] for f in fs]))


def _print_rels(root, k):
    rs = [r for r in rels(root) if r["table"] == k or r["ref_table"] == k]
    if rs:
        print("\n## Relationships")
        print(d.md_table(["from", "to", "source", "confidence", "status"],
                         [[f"{r['table']}({','.join(r['columns'])})", f"{r['ref_table']}({','.join(r['ref_columns'])})",
                           r["source"], r.get("confidence", ""), r.get("status", "")] for r in rs]))


def cmd_relationships(root, a):
    cat = d.load_catalog(root)
    _print_rels(root, resolve_table(cat, a.ref))


def cmd_domain(root, a):
    doms = {x["name"]: x for x in d.load_domains(root).get("domains", [])}
    if a.name not in doms:
        d.die(f"domain '{a.name}' not found. Known: {', '.join(doms) or '(none)'}")
    x = doms[a.name]
    cat = d.load_catalog(root)
    print(f"# legacy domain {a.name} [{x.get('status', 'candidate')}]")
    if x.get("description"):
        print(x["description"])
    print(d.md_table(["table", "columns", "comment"],
                     [[t, len(cat["tables"].get(t, {}).get("columns", [])), (cat["tables"].get(t, {}).get("comment") or "")[:60]]
                      for t in x.get("tables", [])]))
    if x.get("shared_tables"):
        print("shared/hub tables used here: " + ", ".join(x["shared_tables"]))
    cov = d.compute_coverage(root)
    if cov and a.name in cov["per_domain"]:
        print("coverage: " + str(cov["per_domain"][a.name]))
    users = [c for c in d.list_contexts(root) if a.name in d.load_model(root, c).get("legacy_domains", [])]
    print("target contexts drawing from it: " + (", ".join(users) or "(none yet)"))
    tabs = set(x.get("tables", []))
    fs = [f for f in d.load_findings(root).get("findings", []) if ".".join(f["object"].split(".")[:2]) in tabs]
    if fs:
        from collections import Counter
        print("findings: " + str(dict(Counter(f["severity"] for f in fs))))


def cmd_unmapped(root, a):
    cov = d.compute_coverage(root)
    if not cov:
        d.die("no catalog")
    rows = [r for r in cov["columns"] if r[2] == "pending"
            and (not a.domain or r[1] == a.domain)
            and (not a.table or r[0].startswith(a.table.lower() + "."))]
    print(f"{len(rows)} pending legacy columns")
    for ref, dom, _ in rows[: a.limit]:
        print(f"- {ref}  ({dom})")
    if len(rows) > a.limit:
        print(f"... {len(rows) - a.limit} more")


def cmd_lineage(root, a):
    fwd, back, ret = lineage_index(root)
    ref = a.ref.lower()
    found = False
    if ref in fwd:
        found = True
        print(d.md_table(["context", "target", "transform", "status"], [list(x) for x in fwd[ref]]))
    if ref in back:
        found = True
        print(d.md_table(["context", "sources", "transform", "status"], [[c, ", ".join(s), t, st] for c, s, t, st in back[ref]]))
    tbl_ret = ".".join(ref.split(".")[:2]) + ".*"
    for key in (ref, tbl_ret):
        if key in ret:
            found = True
            print(f"retired in {ret[key][0]}{' (whole table)' if key.endswith('*') else ''}: {ret[key][1]} {ret[key][2]}")
            break
    if not found:
        print("no lineage recorded for " + a.ref)


def cmd_context(root, a):
    m = d.load_model(root, a.ctx)
    mp = d.load_mapping(root, a.ctx)
    print(f"# context {a.ctx} · schema {m['schema']} · v{m['version']} [{m['status']}] · accepted v{m.get('accepted_version') or '-'}")
    print(m.get("description", ""))
    print(d.md_table(["table", "role", "aggregate", "columns", "legacy source(s)"],
                     [[t["name"], t.get("role", ""), t.get("aggregate", ""), len(t.get("columns", [])),
                       ", ".join(s for tm in mp.get("table_mappings", []) if tm["target"].lower() == f"{m['schema']}.{t['name']}"
                                 for s in tm.get("sources", [])) or "-"]
                      for t in m.get("tables", [])]))
    print(f"column mappings: {len(mp.get('column_mappings', []))} · retired legacy columns: {len(mp.get('retired', []))}")
    if m.get("external_refs"):
        print("external refs: " + ", ".join(f"{r['column']}->{r['context']}.{r['entity']}" for r in m["external_refs"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("term"); s.add_argument("--limit", type=int, default=40)
    s = sub.add_parser("table"); s.add_argument("ref")
    s = sub.add_parser("relationships"); s.add_argument("ref")
    s = sub.add_parser("domain"); s.add_argument("name")
    s = sub.add_parser("unmapped"); s.add_argument("--domain"); s.add_argument("--table"); s.add_argument("--limit", type=int, default=200)
    s = sub.add_parser("lineage"); s.add_argument("ref")
    s = sub.add_parser("context"); s.add_argument("ctx")
    a = ap.parse_args()
    root = d.find_root(a.root)
    globals()["cmd_" + a.cmd](root, a)


if __name__ == "__main__":
    main()
