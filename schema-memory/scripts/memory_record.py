#!/usr/bin/env python3
"""Create or update requirement (REQ) and decision (ADR) records in .dbmod/.

  new REQ --title "..." [--source JIRA-123|URL] [--contexts billing,crm] [--body-file f.md|--body "..."]
  new ADR --title "..." [--contexts ..] [--related REQ-0003,ADR-0002] [--status proposed] [--body ...]
  status <ID> <new-status> [--note "..."]      e.g. status REQ-0003 implemented
  link <ID> --contexts billing                 add contexts to an existing record
  list [REQ|ADR] [--status open]
  finding <F-0001> <open|accepted-risk|resolved-in-target|not-applicable> [--note "sales v1 enforces FK"]
                                               record what happened to a legacy assessment finding

REQ statuses: captured, analysed, designed, accepted, implemented, superseded, rejected
ADR statuses: proposed, accepted, superseded, rejected
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
FOLDER = {"REQ": "requirements", "ADR": "decisions"}
STATUSES = {
    "REQ": ["captured", "analysed", "designed", "accepted", "implemented", "superseded", "rejected"],
    "ADR": ["proposed", "accepted", "superseded", "rejected"],
}


def slug(s):
    return "-".join(d.tokens(s))[:50] or "untitled"


def idx_path(root, kind):
    return d.mem(root, FOLDER[kind], "index.json")


def find_item(root, rid):
    kind = rid.split("-")[0].upper()
    if kind not in FOLDER:
        d.die(f"unknown id {rid}")
    idx = d.load_json(idx_path(root, kind))
    for it in idx["items"]:
        if it["id"] == rid:
            return kind, idx, it
    d.die(f"{rid} not found")


def cmd_new(root, a):
    kind = a.kind.upper()
    man = d.load_manifest(root)
    man["counters"][kind] = man["counters"].get(kind, 0) + 1
    rid = f"{kind}-{man['counters'][kind]:04d}"
    status = a.status or ("captured" if kind == "REQ" else "proposed")
    if status not in STATUSES[kind]:
        d.die(f"status must be one of {STATUSES[kind]}")
    body = a.body or ""
    if a.body_file:
        with open(a.body_file, encoding="utf-8") as fh:
            body = fh.read().strip()
    tmpl = open(os.path.join(TEMPLATES, "requirement.md" if kind == "REQ" else "adr.md"), encoding="utf-8").read()
    contexts = [c.strip() for c in (a.contexts or "").split(",") if c.strip()]
    text = tmpl.format(id=rid, title=a.title, status=status, source=a.source or "-",
                       contexts=", ".join(contexts) or "-", created_at=d.now_iso(),
                       related=a.related or "-", body=body or "<!-- fill in -->")
    fname = f"{rid}-{slug(a.title)}.md"
    path = d.mem(root, FOLDER[kind], fname)
    d.write_text(path, text)
    idx = d.load_json(idx_path(root, kind))
    idx["items"].append({"id": rid, "title": a.title, "status": status, "source": a.source,
                         "contexts": contexts, "related": [r for r in (a.related or "").split(",") if r],
                         "file": f"{FOLDER[kind]}/{fname}", "created_at": d.now_iso(),
                         "history": [{"at": d.now_iso(), "status": status}]})
    d.save_json(idx_path(root, kind), idx)
    d.save_manifest(root, man)
    d.journal(root, "schema-memory", f"new-{kind.lower()}", f"{rid} {a.title}", source=a.source)
    print(rid)
    print(path)


def cmd_status(root, a):
    kind, idx, it = find_item(root, a.id)
    if a.new_status not in STATUSES[kind]:
        d.die(f"status must be one of {STATUSES[kind]}")
    it["status"] = a.new_status
    it.setdefault("history", []).append({"at": d.now_iso(), "status": a.new_status, "note": a.note})
    d.save_json(idx_path(root, kind), idx)
    # keep the status row in the markdown in sync
    p = d.mem(root, it["file"])
    if os.path.exists(p):
        lines = open(p, encoding="utf-8").read().splitlines()
        lines = [f"| Status | {a.new_status} |" if ln.startswith("| Status |") else ln for ln in lines]
        d.write_text(p, "\n".join(lines) + "\n")
    d.journal(root, "schema-memory", "status", f"{a.id} -> {a.new_status}", note=a.note)
    print(f"{a.id}: {a.new_status}")


def cmd_link(root, a):
    kind, idx, it = find_item(root, a.id)
    for c in (a.contexts or "").split(","):
        c = c.strip()
        if c and c not in it["contexts"]:
            it["contexts"].append(c)
    d.save_json(idx_path(root, kind), idx)
    print(f"{a.id}: contexts={it['contexts']}")


def cmd_finding(root, a):
    p = d.mem(root, "legacy", "findings.json")
    data = d.load_findings(root)
    for f in data["findings"]:
        if f["id"] == a.id:
            f["status"] = a.status
            if a.note:
                f["note"] = a.note
            d.save_json(p, data)
            d.journal(root, "schema-memory", "finding", f"{a.id} -> {a.status}", note=a.note)
            print(f"{a.id} {f['object']}: {a.status}")
            return
    d.die(f"{a.id} not found")


def cmd_list(root, a):
    kinds = [a.kind.upper()] if a.kind else ["REQ", "ADR"]
    rows = []
    for k in kinds:
        for it in d.load_json(idx_path(root, k))["items"]:
            if a.status and it["status"] != a.status:
                continue
            rows.append([it["id"], it["status"], ",".join(it["contexts"]) or "-", it.get("source") or "-", it["title"]])
    print(d.md_table(["id", "status", "contexts", "source", "title"], rows) if rows else "(none)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("kind", choices=["REQ", "ADR", "req", "adr"])
    n.add_argument("--title", required=True)
    n.add_argument("--source")
    n.add_argument("--contexts")
    n.add_argument("--related")
    n.add_argument("--status")
    n.add_argument("--body")
    n.add_argument("--body-file")
    s = sub.add_parser("status")
    s.add_argument("id")
    s.add_argument("new_status")
    s.add_argument("--note")
    lk = sub.add_parser("link")
    lk.add_argument("id")
    lk.add_argument("--contexts", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("kind", nargs="?")
    ls.add_argument("--status")
    fd = sub.add_parser("finding")
    fd.add_argument("id")
    fd.add_argument("status", choices=["open", "accepted-risk", "resolved-in-target", "not-applicable"])
    fd.add_argument("--note")
    a = ap.parse_args()
    root = d.find_root(a.root)
    {"new": cmd_new, "status": cmd_status, "link": cmd_link, "list": cmd_list, "finding": cmd_finding}[a.cmd](root, a)


if __name__ == "__main__":
    main()
