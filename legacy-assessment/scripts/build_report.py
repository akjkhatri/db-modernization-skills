#!/usr/bin/env python3
"""Assemble the legacy assessment report from memory (catalog, findings, relationships,
domains, glossary) into .dbmod/legacy/assessment/report.md, plus findings.csv for
spreadsheet/Jira import.

Data sections are generated. Sections that need judgement contain
<!-- CLAUDE: ... --> markers. Fill them in after running this script. Re-running keeps
narrative you already wrote: text between <!-- BEGIN:x --> and <!-- END:x --> is preserved.

Usage: python build_report.py [--root <repo>] [--top 25]
"""
import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402

NARRATIVE = {
    "executive_summary": "<!-- CLAUDE: 5-8 sentences for a CTO/architect: overall health, the 3-5 biggest risks for the "
                         "rewrite, recommended decomposition into bounded contexts, and what must be decided first. -->",
    "domain_commentary": "<!-- CLAUDE: for each candidate domain, one line on what it represents and whether it should become one "
                         "target context, be split, merged, or retired. Name the hub tables and who should own them. -->",
    "recommendations": "<!-- CLAUDE: prioritised modernization roadmap: which context to design first and why (dependencies, "
                       "business value, risk), cross-cutting decisions needed (time zone, money, identity/keys, PII handling, "
                       "soft delete, retention), and data-quality profiling to commission before migration. -->",
    "sme_questions": "<!-- CLAUDE: curate the generated questions below into a short list grouped by domain/owner. -->",
}


def keep_blocks(old_text):
    return dict(re.findall(r"<!-- BEGIN:(\w+) -->\n(.*?)\n<!-- END:\1 -->", old_text, flags=re.S))


def block(name, kept):
    return f"<!-- BEGIN:{name} -->\n{kept.get(name, NARRATIVE[name])}\n<!-- END:{name} -->"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    root = d.find_root(a.root)
    man = d.load_manifest(root)
    cat = d.load_catalog(root)
    F = d.load_findings(root)
    if not F.get("findings"):
        d.die("no findings yet: run analyze_schema.py first")
    rels = d.load_json(d.mem(root, "legacy", "relationships.json"), default={"relationships": []})["relationships"]
    doms = d.load_domains(root)
    gl = d.load_json(d.mem(root, "legacy", "glossary.json"), default={"terms": {}})["terms"]
    rpath = d.mem(root, "legacy", "assessment", "report.md")
    kept = keep_blocks(open(rpath, encoding="utf-8").read()) if os.path.exists(rpath) else {}
    fs = [f for f in F["findings"] if f.get("status") != "not-detected"]
    s = F["summary"]
    dom_of = d.legacy_table_domains(root)

    L = [f"# Legacy database assessment: {man['project']}", "",
         f"_Generated {d.now_iso()[:16]} UTC from `.dbmod/` memory. Sources: "
         + ", ".join(f"`{x['file']}`" for x in man["legacy"].get("sources", [])) + "._", "",
         "## 1. Executive summary", block("executive_summary", kept), "",
         "## 2. Scope", ""]
    kinds = Counter(t.get("kind", "table") for t in cat["tables"].values())
    schemas = Counter(t["schema"] for t in cat["tables"].values())
    rows_known = [t["row_count"] for t in cat["tables"].values() if t.get("row_count") is not None]
    L += [d.md_table(["metric", "value"], [
        ["schemas", ", ".join(f"{k} ({v})" for k, v in schemas.most_common())],
        ["tables / views", f"{kinds.get('table', 0)} / {kinds.get('view', 0) + kinds.get('matview', 0)}"],
        ["columns", s["columns"]],
        ["approx. rows (where known)", f"{sum(rows_known):,}" if rows_known else "not in export"],
        ["declared foreign keys", s["declared_relationships"]],
        ["inferred relationships", s["inferred_relationships"]],
        ["PII columns detected", s["pii_columns"]],
        ["average table health (0-100)", s["average_table_score"]]]), ""]

    L += ["## 3. Health scorecard", "",
          d.md_table(["severity", "findings"], [[k, v] for k, v in s["findings_by_severity"].items()]), "",
          "Rules triggered most often:", "",
          d.md_table(["rule", "count", "example message"],
                     [[r, n, next(f["message"] for f in fs if f["rule"] == r)] for r, n in list(s["findings_by_rule"].items())[:15]]), "",
          "Lowest-scoring tables:", "",
          d.md_table(["table", "score", "domain", "rows"],
                     [[k, v, dom_of.get(k, "-"), cat["tables"][k].get("row_count") or "-"] for k, v in list(F["table_scores"].items())[:15]]), ""]

    L += ["## 4. Domain map", "", block("domain_commentary", kept), ""]
    if doms.get("domains"):
        L += [d.md_table(["domain", "status", "tables", "shared/hub tables used", "retire candidates", "ERD"],
                         [[x["name"], x.get("status", "candidate"), len(x["tables"]),
                           ", ".join(x.get("shared_tables", [])) or "-", ", ".join(x.get("retire_candidates", [])) or "-",
                           f"[erd](erd/{x['name']}.md)"] for x in doms["domains"]]), "",
              "Hub tables (referenced from many places): " + (", ".join(doms.get("hubs", [])) or "none"), "",
              "Cross-domain dependencies:", "",
              d.md_table(["from", "to", "relationships"], [[e["from"], e["to"], e["count"]] for e in doms.get("cross_domain_edges", [])]) if doms.get("cross_domain_edges") else "_none_", ""]
        for x in doms["domains"]:
            L.append(f"- **{x['name']}**: " + ", ".join(t.split(".", 1)[1] for t in x["tables"]))
        L.append("")
    else:
        L += ["_Run discover_domains.py._", ""]

    L += ["## 5. Key findings", ""]
    bycat = defaultdict(list)
    for f in fs:
        bycat[f["category"]].append(f)
    order = ["security", "keys", "structure", "types", "naming", "documentation"]
    for c in sorted(bycat, key=lambda x: order.index(x) if x in order else 99):
        items = [f for f in bycat[c] if f["severity"] in ("critical", "high", "medium")][: a.top]
        if not items:
            continue
        L += [f"### {c.title()} ({len(bycat[c])})", "",
              d.md_table(["id", "sev", "object", "finding", "recommendation"],
                         [[f["id"], f["severity"], f["object"], f["message"] + (f" ({f['evidence']})" if f["evidence"] else ""),
                           f["recommendation"]] for f in items]), ""]
        if len(bycat[c]) > len(items):
            L += [f"_{len(bycat[c]) - len(items)} more in findings.csv._", ""]

    L += ["## 6. Data protection (UK GDPR / PCI)", ""]
    pii = F.get("pii_inventory", [])
    if pii:
        byc = defaultdict(list)
        for p in pii:
            byc[p["class"]].append(p["ref"])
        L += [d.md_table(["class", "columns", "examples"],
                         [[k, len(v), ", ".join(v[:8]) + (" ..." if len(v) > 8 else "")] for k, v in
                          sorted(byc.items(), key=lambda x: ["credential", "special_category", "financial", "personal"].index(x[0]))]), "",
              "Detection is name-based. Confirm with the data protection officer and add anything that is missed (e.g. free-text notes).", ""]
    else:
        L += ["_No PII detected by name heuristics; confirm manually._", ""]

    inf = [r for r in rels if r["source"] == "inferred"]
    L += ["## 7. Relationships to confirm", "",
          f"{len(inf)} relationships are inferred, not enforced. Each must be confirmed or rejected because they drive "
          "domain clustering, target FKs and orphan-row profiling.", ""]
    if inf:
        L += [d.md_table(["from", "to", "confidence", "reason", "status"],
                         [[f"{r['table']}.{r['columns'][0]}", f"{r['ref_table']}.{(r['ref_columns'] or ['?'])[0]}", r["confidence"], r["reason"], r["status"]]
                          for r in sorted(inf, key=lambda r: r["confidence"])]), ""]

    retire = {}
    for f in fs:
        if f["rule"] in ("LA-STR-007", "LA-STR-010", "LA-KEY-005"):
            retire.setdefault(f["object"], []).append(f["message"])
    L += ["## 8. Retirement candidates", "",
          d.md_table(["object", "why"], [[o, "; ".join(w)] for o, w in retire.items()]) if retire else "_none_", ""]

    unknown = {k: v for k, v in gl.items() if v["status"] == "unknown"}
    L += ["## 9. Legacy vocabulary", "",
          "Abbreviations found in legacy names and their proposed expansions. Target names use the expansions (see `glossary.json`).", "",
          d.md_table(["abbrev", "expansion", "uses", "status"],
                     [[k, v.get("expansion") or "?", v["uses"], v["status"]] for k, v in list(gl.items())[:40]]), ""]

    L += ["## 10. Modernization recommendations", block("recommendations", kept), ""]

    q = []
    for r in sorted(inf, key=lambda r: r["confidence"]):
        if r["confidence"] < 0.8:
            q.append(f"Does `{r['table']}.{r['columns'][0]}` reference `{r['ref_table']}`? ({r['reason']})")
    for f in fs:
        if f["rule"] == "LA-KEY-006":
            q.append(f"What does `{f['object']}` point to?")
        elif f["rule"] in ("LA-NAM-005", "LA-STR-002"):
            q.append(f"What is stored in `{f['object']}` ({f['evidence']})?")
        elif f["rule"] == "LA-STR-007":
            q.append(f"Is `{f['object']}` still needed, or can it be retired?")
        elif f["rule"] == "LA-STR-003":
            q.append(f"Which entity types can `{f['object']}` link to?")
    for k in unknown:
        q.append(f"What does the abbreviation `{k}` mean?")
    q.append("Which time zone are legacy `timestamp without time zone` values in?")
    L += ["## 11. Questions for subject-matter experts", block("sme_questions", kept), ""] + [f"- [ ] {x}" for x in q] + [""]

    L += ["## Appendix", "", "- Full findings: `findings.csv` (same folder)",
          "- Machine-readable: `.dbmod/legacy/findings.json`, `relationships.json`, `domains.json`, `glossary.json`", ""]
    d.write_text(rpath, "\n".join(L))
    with open(d.mem(root, "legacy", "assessment", "findings.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "severity", "rule", "category", "object", "domain", "message", "evidence", "recommendation", "status"])
        for f in F["findings"]:
            w.writerow([f["id"], f["severity"], f["rule"], f["category"], f["object"],
                        dom_of.get(".".join(f["object"].split(".")[:2]), ""), f["message"], f["evidence"], f["recommendation"], f["status"]])
    d.journal(root, "legacy-assessment", "report", f"report.md + findings.csv ({len(fs)} findings, {len(q)} SME questions)")
    pending = [k for k in NARRATIVE if k not in kept or "<!-- CLAUDE" in kept.get(k, "")]
    print(rpath)
    print("narrative sections still to write: " + (", ".join(pending) or "none"))


if __name__ == "__main__":
    main()
