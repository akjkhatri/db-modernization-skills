#!/usr/bin/env python3
"""Record human review decisions on the legacy assessment, without hand-editing JSON.

  rel confirm <schema.table.column> [--to schema.table]     confirm an inferred relationship
  rel reject  <schema.table.column> [--to schema.table] [--note why]
  rel add     <schema.table.column> <schema.table.column> [--note why]   manual relationship
  finding <F-0001> <open|accepted-risk|resolved-in-target|not-applicable> [--note why]
  domain rename <old> <new>
  domain describe <name> "<text>"
  domain move <schema.table> <domain>        (creates the domain if needed)
  domain share <schema.table> <domain>       (mark a hub table as used by a domain)
  domain merge <from> <into>
  domain confirm <name> [<name> ...] | domain confirm all
  term <abbrev> <expansion> [--confirm]
  sme list [--open] [--domain <name>]                       SME questions (built by build_report.py)
  sme answer <SQ-0001> "<answer>" --by "Name:role"          the business answers a question
  sme waive  <SQ-0001> --adr ADR-0004 --by "Name:role"      proceed without an answer, justified by an ADR

After changing relationships, re-run discover_domains.py (confirmed domains are kept) and
build_report.py.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402


def rel_cmd(root, a):
    p = d.mem(root, "legacy", "relationships.json")
    data = d.load_json(p, default={"relationships": []})
    rs = data["relationships"]
    s, t, c = d.split_ref(a.ref)
    tk = f"{s}.{t}"
    if a.action == "add":
        s2, t2, c2 = d.split_ref(a.target)
        rs.append({"table": tk, "columns": [c], "ref_table": f"{s2}.{t2}", "ref_columns": [c2] if c2 else [],
                   "source": "manual", "confidence": 1.0, "reason": a.note or "added by reviewer", "status": "confirmed"})
        msg = f"manual relationship {a.ref} -> {a.target}"
    else:
        hits = [r for r in rs if r["table"] == tk and r["columns"] == [c] and (not a.to or r["ref_table"] == a.to.lower())]
        if not hits:
            d.die(f"no relationship from {a.ref}" + (f" to {a.to}" if a.to else ""))
        if len(hits) > 1:
            d.die("several candidates, specify --to: " + ", ".join(r["ref_table"] for r in hits))
        hits[0]["status"] = "confirmed" if a.action == "confirm" else "rejected"
        if a.note:
            hits[0]["note"] = a.note
        if a.by:
            hits[0]["reviewed_by"] = a.by
        msg = f"{a.action} {a.ref} -> {hits[0]['ref_table']}"
    d.save_json(p, data)
    d.journal(root, "legacy-assessment", "review-rel", msg, note=a.note)
    print(msg)


def finding_cmd(root, a):
    p = d.mem(root, "legacy", "findings.json")
    data = d.load_findings(root)
    for f in data["findings"]:
        if f["id"] == a.id:
            f["status"] = a.status
            if a.note:
                f["note"] = a.note
            if a.by:
                f["reviewed_by"] = a.by
            d.save_json(p, data)
            d.journal(root, "legacy-assessment", "review-finding", f"{a.id} -> {a.status}", note=a.note)
            print(f"{a.id} {f['object']}: {a.status}")
            return
    d.die(f"{a.id} not found")


def domain_cmd(root, a):
    p = d.mem(root, "legacy", "domains.json")
    data = d.load_domains(root)
    doms = data["domains"]
    by = {x["name"]: x for x in doms}

    def get(name, create=False):
        if name not in by:
            if not create:
                d.die(f"domain {name} not found; known: {', '.join(by)}")
            by[name] = {"name": name, "status": "candidate", "description": "", "tables": [], "shared_tables": [],
                        "retire_candidates": [], "notes": ""}
            doms.append(by[name])
        return by[name]

    act, args = a.action, a.args
    if act == "rename":
        x = get(args[0])
        x["name"] = args[1]
        msg = f"renamed {args[0]} -> {args[1]}"
    elif act == "describe":
        get(args[0])["description"] = " ".join(args[1:])
        msg = f"described {args[0]}"
    elif act == "move":
        tk = args[0].lower()
        for x in doms:
            if tk in x["tables"]:
                x["tables"].remove(tk)
        tgt = get(args[1], create=True)
        tgt["tables"] = sorted(set(tgt["tables"]) | {tk})
        msg = f"moved {tk} -> {args[1]}"
    elif act == "share":
        tgt = get(args[1])
        tgt["shared_tables"] = sorted(set(tgt.get("shared_tables", [])) | {args[0].lower()})
        msg = f"{args[1]} shares {args[0]}"
    elif act == "merge":
        src, tgt = get(args[0]), get(args[1])
        tgt["tables"] = sorted(set(tgt["tables"]) | set(src["tables"]))
        tgt["shared_tables"] = sorted((set(tgt.get("shared_tables", [])) | set(src.get("shared_tables", []))) - set(tgt["tables"]))
        tgt["retire_candidates"] = sorted(set(tgt.get("retire_candidates", [])) | set(src.get("retire_candidates", [])))
        doms.remove(src)
        msg = f"merged {args[0]} into {args[1]}"
    elif act == "confirm":
        # 'all' confirms business domains only; technical/unassigned buckets must be named explicitly
        names = [x["name"] for x in doms if x["name"] not in ("technical", "unassigned")] if args == ["all"] else args
        for n in names:
            get(n)["status"] = "confirmed"
            if a.by:
                get(n)["confirmed_by"] = a.by
        msg = "confirmed " + ", ".join(names) + (f" (by {a.by})" if a.by else "")
        if args == ["all"] and any(x["name"] in ("technical", "unassigned") for x in doms):
            msg += " · technical/unassigned left as candidate: confirm them by name once reviewed"
    else:
        d.die(f"unknown domain action {act}")
    data["domains"] = [x for x in doms if x["tables"] or x.get("shared_tables")]
    d.save_json(p, data)
    d.journal(root, "legacy-assessment", "review-domain", msg, by=a.by)
    print(msg)


def term_cmd(root, a):
    p = d.mem(root, "legacy", "glossary.json")
    g = d.load_json(p, default={"terms": {}})
    g["terms"].setdefault(a.abbrev.lower(), {"uses": 0})
    g["terms"][a.abbrev.lower()].update({"expansion": a.expansion, "status": "confirmed" if a.confirm else "suggested"})
    d.save_json(p, g)
    d.journal(root, "legacy-assessment", "review-term", f"{a.abbrev} = {a.expansion}")
    print(f"{a.abbrev} = {a.expansion}")


def sme_cmd(root, a):
    sme = d.load_sme(root)
    qs = sme["questions"]
    if not qs:
        d.die("no SME questions yet: run build_report.py first")
    if a.action == "list":
        rows = [x for x in qs if (not a.open or x["status"] == "open") and (not a.domain or x.get("domain") == a.domain)]
        for x in rows:
            scope = x.get("domain") or "all contexts"
            extra = f" -> {x['answer']}" if x.get("answer") else (f" (waived: {x['adr']})" if x.get("adr") else "")
            print(f"{x['id']}  {x['status']:<18} [{scope}] {x['question']}{extra}")
        print(f"{len(rows)} question(s)")
        return
    if not a.id:
        d.die(f"sme {a.action} needs a question id (SQ-0001)")
    if not a.by:
        d.die('--by "Name:role" is required: the gate records who confirmed the legacy behaviour')
    name, role = d.parse_approver(a.by)
    x = next((q for q in qs if q["id"] == a.id.upper()), None)
    if x is None:
        d.die(f"{a.id} not found")
    if a.action == "answer":
        if not a.text:
            d.die("sme answer needs the answer text")
        x.update({"status": "answered", "answer": a.text, "answered_by": f"{name} ({role or 'role not given'})",
                  "answered_role": role, "answered_at": d.now_iso()})
        msg = f"{x['id']} answered by {name}"
    else:
        if not a.adr:
            d.die("sme waive needs --adr <ADR id> explaining why the design may proceed without an answer")
        aidx = d.load_json(d.mem(root, "decisions", "index.json"), default={"items": []})
        if not any(it["id"] == a.adr.upper() for it in aidx["items"]):
            d.die(f"{a.adr} not found: record it first with memory_record.py new ADR")
        x.update({"status": "waived", "adr": a.adr.upper(), "answered_by": f"{name} ({role or 'role not given'})",
                  "answered_role": role, "answered_at": d.now_iso()})
        msg = f"{x['id']} waived under {a.adr.upper()} by {name}"
    d.save_json(d.sme_path(root), sme)
    d.journal(root, "legacy-assessment", "review-sme", msg, question=x["question"], answer=x.get("answer"))
    print(msg)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rel")
    r.add_argument("action", choices=["confirm", "reject", "add"])
    r.add_argument("ref")
    r.add_argument("target", nargs="?")
    r.add_argument("--to")
    r.add_argument("--note")
    f = sub.add_parser("finding")
    f.add_argument("id")
    f.add_argument("status", choices=["open", "accepted-risk", "resolved-in-target", "not-applicable"])
    f.add_argument("--note")
    dm = sub.add_parser("domain")
    dm.add_argument("action", choices=["rename", "describe", "move", "share", "merge", "confirm"])
    dm.add_argument("args", nargs="+")
    t = sub.add_parser("term")
    t.add_argument("abbrev")
    t.add_argument("expansion")
    t.add_argument("--confirm", action="store_true")
    sm = sub.add_parser("sme")
    sm.add_argument("action", choices=["list", "answer", "waive"])
    sm.add_argument("id", nargs="?")
    sm.add_argument("text", nargs="?")
    sm.add_argument("--adr")
    sm.add_argument("--open", action="store_true")
    sm.add_argument("--domain")
    for p_ in (r, f, dm, t, sm):
        p_.add_argument("--by", help="reviewer name, stored with the decision")
    a = ap.parse_args()
    root = d.find_root(a.root)
    if a.cmd == "rel" and a.action == "add" and not a.target:
        d.die("rel add needs <from-ref> <to-ref>")
    {"rel": rel_cmd, "finding": finding_cmd, "domain": domain_cmd, "term": term_cmd, "sme": sme_cmd}[a.cmd](root, a)


if __name__ == "__main__":
    main()
