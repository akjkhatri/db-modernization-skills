#!/usr/bin/env python3
"""Cluster legacy tables into candidate business domains (input for bounded contexts).

Signals: declared + inferred relationships (weighted by confidence, hub edges dampened),
and table-name prefixes (cust_*, ord_*, ... expanded through the abbreviation glossary).
Deterministic label propagation, so re-runs are stable.

Domains marked "confirmed" in an existing domains.json are kept exactly as they are;
only tables not in a confirmed domain are re-clustered. Use --reset to discard all.

Usage: python discover_domains.py [--root <repo>] [--hub-threshold 4] [--reset]
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402
from analyze_schema import ABBREV, COPY_RE  # noqa: E402

TECH = re.compile(r"(audit|log|param|config|setting|sys_|lkp|lookup|code_|seq|migration|import|export|job|queue|session|tmp|temp|batch)", re.I)


def norm_token(tok):
    tok = tok.lower()
    return ABBREV.get(tok, tok)


def prefix_of(name, glossary):
    clean = re.sub(r"^(tmp_|temp_|bak_|old_|copy_|tbl_|t_)", "", name, flags=re.I)
    toks = d.tokens(clean)
    if not toks:
        return None
    t = toks[0]
    exp = (glossary.get(t) or {}).get("expansion") or norm_token(t)
    return d.singular(exp.split("_")[0])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--hub-threshold", type=int, default=4, help="min distinct referencing tables to treat a table as a hub")
    ap.add_argument("--iterations", type=int, default=15)
    ap.add_argument("--child-pull", type=float, default=0.3, help="weight of referencing->referenced pull in reverse direction")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    root = d.find_root(a.root)
    cat = d.load_catalog(root)
    rels = [r for r in d.load_json(d.mem(root, "legacy", "relationships.json"), default={"relationships": []})["relationships"]
            if r.get("status") != "rejected"]
    glossary = d.load_json(d.mem(root, "legacy", "glossary.json"), default={"terms": {}})["terms"]
    old = d.load_domains(root) if not a.reset else {"domains": []}
    confirmed = [x for x in old.get("domains", []) if x.get("status") == "confirmed"]
    locked = {t: x["name"] for x in confirmed for t in x.get("tables", [])}

    tables = [k for k, t in cat["tables"].items() if t.get("kind", "table") == "table"]
    views = [k for k, t in cat["tables"].items() if t.get("kind", "table") != "table" and k not in locked]
    indeg = Counter()
    for r in rels:
        if r["table"] != r["ref_table"]:
            indeg[r["ref_table"]] += 1
    hubs = sorted(k for k, n in indeg.items() if n >= a.hub_threshold)

    adj = defaultdict(lambda: defaultdict(float))
    for r in rels:
        x, y = r["table"], r["ref_table"]
        if x == y or x not in cat["tables"] or y not in cat["tables"]:
            continue
        w = float(r.get("confidence", 1.0))
        if y in hubs:
            w = w / (1 + indeg[y] / 2)   # a hub should not swallow everything that references it
        # asymmetric: a child is pulled into its parent's domain, a parent only weakly towards
        # tables that reference it (master data must not be absorbed by its consumers)
        adj[x][y] += w
        adj[y][x] += w * a.child_pull

    prefixes = {k: prefix_of(cat["tables"][k]["name"], glossary) for k in tables}
    pcount = Counter(p for p in prefixes.values() if p)
    label = {}
    for k in tables:
        if k in locked:
            label[k] = "locked:" + locked[k]
        elif prefixes[k] and pcount[prefixes[k]] >= 2:
            label[k] = prefixes[k]
        else:
            label[k] = "t:" + k
    for _ in range(a.iterations):
        changed = False
        for k in sorted(tables):
            if k in locked:
                continue
            score = defaultdict(float)
            score[label[k]] += 0.6 if not label[k].startswith("t:") else 0.0
            if prefixes[k] and pcount[prefixes[k]] >= 2:
                score[prefixes[k]] += 0.4
            for nb, w in adj[k].items():
                score[label[nb]] += w
            if not score:
                continue
            best = sorted(score.items(), key=lambda x: (-x[1], x[0]))[0][0]
            if best != label[k] and score[best] > score.get(label[k], 0):
                label[k] = best
                changed = True
        if not changed:
            break

    groups = defaultdict(list)
    for k in tables:
        groups[label[k]].append(k)
    domains, misc = [], []
    for lab, members in groups.items():
        if lab.startswith("locked:"):
            continue
        if len(members) == 1 and not adj[members[0]]:
            misc.append(members[0])
            continue
        toks = Counter(p for p in (prefixes[m] for m in members) if p)
        name = lab if not lab.startswith("t:") else (toks.most_common(1)[0][0] if toks else d.tokens(cat["tables"][members[0]]["name"])[0])
        domains.append({"name": name, "members": sorted(members)})
    # keep names a reviewer gave to candidate domains: reuse the old name when most tables match
    old_cands = [x for x in old.get("domains", []) if x.get("status") != "confirmed"]
    used = set()
    for x in domains:
        best, bo = None, 0.5
        for o in old_cands:
            ov = len(set(x["members"]) & set(o["tables"])) / max(1, len(set(x["members"]) | set(o["tables"])))
            if ov > bo and o["name"] not in used:
                best, bo = o["name"], ov
        if best:
            x["name"] = best
            used.add(best)
    # unique names
    seen = Counter()
    for x in domains:
        seen[x["name"]] += 1
        if seen[x["name"]] > 1:
            x["name"] = f"{x['name']}_{seen[x['name']]}"
    tech = sorted(m for m in misc if TECH.search(cat["tables"][m]["name"]))
    other = sorted(m for m in misc if m not in tech)

    dom_of = {m: x["name"] for x in domains for m in x["members"]}
    dom_of.update(locked)
    out = []
    for x in confirmed:
        out.append(x)
    for x in sorted(domains, key=lambda x: -len(x["members"])):
        uses = sorted({r["ref_table"] for r in rels if r["table"] in x["members"] and r["ref_table"] in dom_of
                       and dom_of[r["ref_table"]] != x["name"] and r["ref_table"] in hubs})
        out.append({"name": x["name"], "status": "candidate", "description": "",
                    "tables": x["members"], "shared_tables": uses,
                    "retire_candidates": [m for m in x["members"] if COPY_RE.search(cat["tables"][m]["name"])],
                    "notes": ""})
    if tech:
        out.append({"name": "technical", "status": "candidate", "tables": tech, "shared_tables": [],
                    "description": "Infrastructure/technical tables (logging, config, generic lookups, import/export). "
                                   "Usually replaced by platform features rather than migrated into a business context.",
                    "retire_candidates": [], "notes": ""})
    if other:
        out.append({"name": "unassigned", "status": "candidate", "tables": other, "shared_tables": [],
                    "description": "No relationships and no shared naming; needs SME input.", "retire_candidates": [], "notes": ""})
    if views:
        # a view goes to the domain whose tables share most column names with it
        vdom = defaultdict(list)
        for v in views:
            vcols = {c["name"].lower() for c in cat["tables"][v]["columns"]}
            best, bscore = "reporting", 0
            for x in out:
                sc = sum(len(vcols & {c["name"].lower() for c in cat["tables"][t]["columns"]}) for t in x["tables"] if t in cat["tables"])
                if sc > bscore:
                    best, bscore = x["name"], sc
            vdom[best].append(v)
        for x in out:
            if x["name"] in vdom and x.get("status") != "confirmed":
                x["tables"] = sorted(x["tables"] + vdom.pop(x["name"]))
        for name, vs in vdom.items():
            ex = next((x for x in out if x["name"] == name), None)
            if ex:   # confirmed domain of that name: keep views separate under a clearly new candidate
                name = f"{name}_views"
            out.append({"name": name, "status": "candidate", "tables": sorted(vs), "shared_tables": [],
                        "description": "Views", "retire_candidates": [], "notes": ""})

    final_dom = {t: x["name"] for x in out for t in x["tables"]}
    cross = Counter()
    for r in rels:
        a1, b1 = final_dom.get(r["table"]), final_dom.get(r["ref_table"])
        if a1 and b1 and a1 != b1:
            cross[(a1, b1)] += 1
    # carry forward human-written descriptions/notes for candidates with the same name
    prev = {x["name"]: x for x in old.get("domains", [])}
    for x in out:
        if x.get("status") != "confirmed" and x["name"] in prev:
            for f in ("description", "notes"):
                if prev[x["name"]].get(f):
                    x[f] = prev[x["name"]][f]
    res = {"generated_at": d.now_iso(), "method": "label-propagation(relationships+prefix)",
           "hubs": hubs, "domains": out,
           "cross_domain_edges": [{"from": k[0], "to": k[1], "count": v} for k, v in cross.most_common()]}
    d.save_json(d.mem(root, "legacy", "domains.json"), res)
    d.journal(root, "legacy-assessment", "domains", f"{len(out)} candidate/confirmed domains, hubs: {', '.join(hubs) or '-'}")
    print(d.md_table(["domain", "status", "tables", "shared", "retire candidates"],
                     [[x["name"], x.get("status"), ", ".join(t.split('.', 1)[1] for t in x["tables"]),
                       ", ".join(t.split('.', 1)[1] for t in x.get("shared_tables", [])),
                       ", ".join(t.split('.', 1)[1] for t in x.get("retire_candidates", []))] for x in out]))
    print("hubs: " + (", ".join(hubs) or "-"))
    print("cross-domain edges: " + ", ".join(f"{k[0]}->{k[1]}×{v}" for k, v in cross.most_common(10)))


if __name__ == "__main__":
    main()
