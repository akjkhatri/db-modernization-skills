#!/usr/bin/env python3
"""Print a compact briefing of the modernization memory. Run this FIRST in every session:
it is how the skills "remember" the legacy baseline and everything decided since.

Designed to stay small even for thousands of legacy tables (details via memory_query.py).

Usage: python memory_status.py [--root <repo>] [--json] [--journal 8]
"""
import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402


def gather(root, n_journal):
    man = d.load_manifest(root)
    cat = d.load_catalog(root, required=False)
    fnd = d.load_findings(root).get("findings", [])
    doms = d.load_domains(root)
    cov = d.compute_coverage(root)
    reqs = d.load_json(d.mem(root, "requirements", "index.json"), default={"items": []})["items"]
    adrs = d.load_json(d.mem(root, "decisions", "index.json"), default={"items": []})["items"]
    ctxs = []
    for c in d.list_contexts(root):
        m = d.load_model(root, c)
        ctxs.append({"context": c, "schema": m.get("schema"), "status": m.get("status"),
                     "version": m.get("version"), "accepted_version": m.get("accepted_version"),
                     "tables": len(m.get("tables", [])), "requirements": m.get("requirements", []),
                     "legacy_domains": m.get("legacy_domains", [])})
    jr = []
    jp = d.mem(root, "journal.jsonl")
    if os.path.exists(jp):
        with open(jp, encoding="utf-8") as fh:
            jr = [json.loads(x) for x in fh.read().splitlines() if x.strip()][-n_journal:]
    return man, cat, fnd, doms, cov, reqs, adrs, ctxs, jr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--journal", type=int, default=8)
    a = ap.parse_args()
    root = d.find_root(a.root)
    man, cat, fnd, doms, cov, reqs, adrs, ctxs, jr = gather(root, a.journal)

    if a.json:
        out = {"manifest": man, "contexts": ctxs, "coverage": {k: v for k, v in (cov or {}).items() if k != "columns"},
               "findings_by_severity": Counter(f["severity"] for f in fnd),
               "domains": [{"name": x["name"], "status": x.get("status"), "tables": len(x.get("tables", []))}
                           for x in doms.get("domains", [])],
               "open_requirements": [r for r in reqs if r["status"] not in ("implemented", "superseded", "rejected")],
               "adrs": adrs, "journal": jr}
        if os.path.exists(d.sme_path(root)):
            wv, fu = d.waiver_overview(root)
            out["waivers"], out["adr_followups"] = wv, fu
        print(json.dumps(out, indent=2, default=str))
        return

    p = print
    p(f"# Modernization memory: {man['project']}")
    p(f"phase: **{man['phase']}** · memory: {d.mem(root)} · updated {man.get('updated_at', '')[:16]}")
    std = d.load_standards(root)
    p(f"target: PostgreSQL · flyway: `{std['flyway']['location']}` · data migration: `{std['data_migration']['location']}`")
    p("")
    p("## Legacy baseline")
    if not cat:
        p("_Not ingested yet. Run legacy-assessment (Mode 1) before designing anything._")
    else:
        lg = man["legacy"]
        p(f"{lg['table_count']} tables · {lg['column_count']} columns · sources: "
          + ", ".join(s['file'] for s in lg.get('sources', [])) + f" · ingested {str(lg.get('ingested_at'))[:10]}"
          + (f" · assessed {str(lg.get('assessed_at'))[:10]}" if lg.get("assessed_at") else " · **not assessed yet**"))
        if fnd:
            sev = Counter(f["severity"] for f in fnd)
            p("findings: " + ", ".join(f"{k} {sev.get(k, 0)}" for k in ["critical", "high", "medium", "low", "info"]))
        if doms.get("domains"):
            p("")
            p(d.md_table(["legacy domain", "status", "tables", "shared/hub"],
                         [[x["name"], x.get("status", "candidate"), len(x.get("tables", [])),
                           len(x.get("shared_tables", []))] for x in doms["domains"]]))
    p("")
    p("## Target bounded contexts")
    if not ctxs:
        p("_None designed yet._")
    else:
        p(d.md_table(["context", "schema", "status", "version", "accepted", "tables", "legacy domains", "requirements"],
                     [[c["context"], c["schema"], c["status"], c["version"], c["accepted_version"] or "-",
                       c["tables"], ",".join(c["legacy_domains"]) or "-", ",".join(c["requirements"]) or "-"]
                      for c in ctxs]))
    if cov:
        t = cov["totals"]
        p("")
        p("## Legacy coverage (column disposition)")
        p(f"{cov['percent_disposed']}% disposed ({cov['percent_accepted']}% accepted) of {cov['total_columns']} legacy columns · "
          f"mapped-accepted {t['mapped_accepted']}, mapped-proposed {t['mapped_proposed']}, retired {t['retired']}, pending {t['pending']}")
        rows = []
        for dom, b in sorted(cov["per_domain"].items()):
            tot = sum(b.values()) or 1
            rows.append([dom, tot, b["mapped_accepted"] + b["mapped_proposed"], b["retired"], b["pending"],
                         f"{round(100 * (tot - b['pending']) / tot)}%"])
        p(d.md_table(["domain", "columns", "mapped", "retired", "pending", "disposed"], rows))
    p("")
    p("## Requirements")
    open_r = [r for r in reqs if r["status"] not in ("implemented", "superseded", "rejected")]
    p(f"{len(reqs)} total, {len(open_r)} open")
    if open_r:
        p(d.md_table(["id", "status", "contexts", "source", "title"],
                     [[r["id"], r["status"], ",".join(r["contexts"]) or "-", r.get("source") or "-", r["title"]] for r in open_r[-15:]]))
    if os.path.exists(d.sme_path(root)):
        qs = d.load_sme(root)["questions"]
        n_open = sum(1 for q in qs if q["status"] == "open")
        wv, fu = d.waiver_overview(root)
        p("")
        p("## Business gate: SME questions and waivers")
        p(f"{len(qs)} SME questions · {n_open} open · {len(wv)} waived · "
          f"{sum(1 for q in qs if q['status'] == 'answered')} answered by the business")
        went_through = sorted({c for w in wv for c in w["contexts"]})
        if wv:
            p(f"**{len(went_through)} accepted context(s) rely on a waiver:** {', '.join(went_through) or '-'}")
            today = d.today_iso()

            def due(w):
                if w["missing_review_date"]:
                    return "no review date"
                return f"**OVERDUE** since {w['review_by']}" if w["overdue"] else w["review_by"]
            p(d.md_table(["question", "ADR", "review by", "contexts", "waived by"],
                         [[w["id"], w["adr"], due(w), ",".join(w["contexts"]) or "(none accepted yet)", w["waived_by"] or "-"]
                          for w in sorted(wv, key=lambda w: w["review_by"] or "")]))
            if any(w["overdue"] for w in wv):
                p(f"_Overdue waivers fail memory_validate and block acceptance (today {today}). "
                  "Get the business's answer (review.py sme answer) or record a review (review.py sme review)._")
        if fu:
            p("")
            p("**ADR follow-ups:** the business has since answered, but the waiver's ADR is still in force:")
            for f in fu:
                p(f"- {f['adr']}: {f['id']} answered by {f['answered_by']} — supersede or reject the ADR")
    p("")
    p("## Decisions (ADRs)")
    if adrs:
        p(d.md_table(["id", "status", "title"], [[x["id"], x["status"], x["title"]] for x in adrs[-15:]]))
    else:
        p("_None yet._")
    if jr:
        p("")
        p("## Recent activity")
        for e in jr:
            p(f"- {e['at'][:16]} {e['skill']}/{e['action']}: {e['summary']}")


if __name__ == "__main__":
    main()
