#!/usr/bin/env python3
"""Render Mermaid ER diagrams per legacy domain into .dbmod/legacy/assessment/erd/<domain>.md
(also usable for target contexts with --target <ctx>, reading model.json).

Inferred relationships are drawn dotted; shared/hub tables from other domains are drawn
with their key column only. Columns are capped (keys first) to keep diagrams readable.

Usage: python render_erd.py [--root <repo>] [--domain billing] [--max-columns 12]
       python render_erd.py [--root <repo>] --target billing   -> target/contexts/billing/erd.md
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402


def mtype(t):
    t = d.base_type(t).replace("character varying", "varchar").replace("timestamp with time zone", "timestamptz") \
        .replace("timestamp without time zone", "timestamp").replace("double precision", "float8").replace("character", "char")
    return re.sub(r"[^A-Za-z0-9_]", "_", t) or "unknown"


def ent(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def legacy_erd(cat, rels, tables, shared, maxc):
    lines = ["erDiagram"]
    fkcols = {(r["table"], r["columns"][0]) for r in rels}
    for k in tables + shared:
        t = cat["tables"][k]
        cols = t["columns"]
        if k in shared:
            cols = [c for c in cols if c.get("is_pk")]
        keyed = sorted(cols, key=lambda c: (not c.get("is_pk"), (k, c["name"].lower()) not in fkcols, c["ordinal"]))
        lines.append(f"  {ent(t['name'])} {{")
        for c in keyed[:maxc]:
            tag = "PK" if c.get("is_pk") else ("FK" if (k, c["name"].lower()) in fkcols else "")
            lines.append(f"    {mtype(c['data_type'])} {ent(c['name'])}{(' ' + tag) if tag else ''}")
        if len(keyed) > maxc:
            lines.append(f'    etc more_{len(keyed) - maxc}_columns "not shown"')
        lines.append("  }")
    scope = set(tables) | set(shared)
    for r in rels:
        if r["table"] in tables and r["ref_table"] in scope:
            link = "||--o{" if r["source"] == "declared" or r.get("status") == "confirmed" else "||..o{"
            lab = r["columns"][0] + ("" if r["source"] == "declared" else f" ~{r.get('confidence')}")
            lines.append(f"  {ent(cat['tables'][r['ref_table']]['name'])} {link} {ent(cat['tables'][r['table']]['name'])} : \"{lab}\"")
    return "\n".join(lines)


def target_erd(m, maxc):
    lines = ["erDiagram"]
    for t in m.get("tables", []):
        fks = {fk["columns"][0] for fk in t.get("foreign_keys", [])}
        lines.append(f"  {ent(t['name'])} {{")
        cols = sorted(t.get("columns", []), key=lambda c: (c["name"] not in t.get("primary_key", []), c["name"] not in fks))
        for c in cols[:maxc]:
            tag = "PK" if c["name"] in t.get("primary_key", []) else ("FK" if c["name"] in fks else "")
            lines.append(f"    {mtype(c['type'])} {ent(c['name'])}{(' ' + tag) if tag else ''}")
        if len(cols) > maxc:
            lines.append(f'    etc more_{len(cols) - maxc}_columns "not shown"')
        lines.append("  }")
    for t in m.get("tables", []):
        for fk in t.get("foreign_keys", []):
            lines.append(f"  {ent(fk['references']['table'])} ||--o{{ {ent(t['name'])} : \"{fk['columns'][0]}\"")
    for r in m.get("external_refs", []):
        tab, col = r["column"].split(".")
        lines.append(f"  {ent(r['context'] + '__' + r['entity'])} ||..o{{ {ent(tab)} : \"{col} (id ref)\"")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--domain")
    ap.add_argument("--target")
    ap.add_argument("--max-columns", type=int, default=12)
    a = ap.parse_args()
    root = d.find_root(a.root)
    if a.target:
        m = d.load_model(root, a.target)
        p = os.path.join(d.context_dir(root, a.target), "erd.md")
        d.write_text(p, f"# {a.target} (v{m['version']}, {m['status']})\n\n```mermaid\n{target_erd(m, a.max_columns)}\n```\n")
        print(p)
        return
    cat = d.load_catalog(root)
    rels = [r for r in d.load_json(d.mem(root, "legacy", "relationships.json"), default={"relationships": []})["relationships"]
            if r.get("status") != "rejected"]
    doms = d.load_domains(root)["domains"]
    if not doms:
        d.die("no domains.json yet; run discover_domains.py")
    written = []
    if not a.domain:   # drop diagrams of domains that were renamed/merged away
        names = {f"{x['name']}.md" for x in doms}
        erd_dir = d.mem(root, "legacy", "assessment", "erd")
        for fn in os.listdir(erd_dir) if os.path.isdir(erd_dir) else []:
            if fn.endswith(".md") and fn not in names:
                os.remove(os.path.join(erd_dir, fn))
    for x in doms:
        if a.domain and x["name"] != a.domain:
            continue
        tabs = [t for t in x["tables"] if t in cat["tables"]]
        body = legacy_erd(cat, rels, tabs, [s for s in x.get("shared_tables", []) if s in cat["tables"] and s not in tabs], a.max_columns)
        p = d.mem(root, "legacy", "assessment", "erd", f"{x['name']}.md")
        d.write_text(p, f"# Legacy domain: {x['name']} ({x.get('status', 'candidate')})\n\n"
                        "Solid lines: declared or confirmed FKs. Dotted: inferred (label shows confidence).\n\n"
                        f"```mermaid\n{body}\n```\n")
        written.append(p)
    print("\n".join(written))


if __name__ == "__main__":
    main()
