#!/usr/bin/env python3
"""Manage target bounded contexts in .dbmod/target/contexts/<ctx>/.

  new <ctx> --legacy-domains billing[,x] [--schema billing] [--description ".."] [--requirement REQ-0001]
        scaffold model.json (draft v1), design.md, mapping/<ctx>.json, register in manifest
  bump <ctx> --requirement REQ-0007 [--reason ".."]
        start a change on an ACCEPTED context: version+1, status draft (snapshot stays immutable)
  propose <ctx>          mark the working model as proposed (ready for human review)
  add-audit <ctx> [--table t]
        add the standard audit / row_version columns from standards.json where missing
  diff <ctx>             show what changed since the last accepted snapshot (model level)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402

DESIGN_MD = """# Bounded context: {ctx}

## Purpose and boundary
{description}
<!-- What this context owns, what it explicitly does NOT own, upstream/downstream contexts. -->

## Ubiquitous language
<!-- term: meaning (legacy name if different) -->

## Aggregates and invariants
<!-- Aggregate root -> entities/value objects; rules the schema enforces (constraints) vs the app enforces. -->

## Legacy sources
Legacy domains: {domains}
<!-- Which legacy tables feed which aggregate; what legacy quirks are fixed (cite finding ids). -->

## Decisions
<!-- ADR ids and one-line summaries. -->

## Version history
| version | status | requirements | summary |
|---|---|---|---|
| 1 | draft | {req} | initial design |
"""


def model_path(root, ctx):
    return os.path.join(d.context_dir(root, ctx), "model.json")


def cmd_new(root, a):
    ctx = a.ctx.lower()
    if not d.is_snake(ctx):
        d.die("context name must be snake_case")
    if os.path.exists(model_path(root, ctx)):
        d.die(f"context {ctx} already exists")
    doms = [x.strip() for x in (a.legacy_domains or "").split(",") if x.strip()]
    known = {x["name"] for x in d.load_domains(root).get("domains", [])}
    for x in doms:
        if x not in known:
            d.die(f"legacy domain '{x}' not in domains.json (known: {', '.join(sorted(known))})")
    schema = (a.schema or ctx).lower()
    if schema in d.PG_RESERVED or ctx in d.PG_RESERVED:
        d.die(f"'{schema}' is a PostgreSQL reserved word; pick another context/schema name (e.g. 'sales' instead of 'order')")
    for c in d.list_contexts(root):
        if d.load_model(root, c).get("schema") == schema:
            d.die(f"schema {schema} already used by context {c}")
    reqs = [a.requirement] if a.requirement else []
    model = {"context": ctx, "schema": schema, "description": a.description or "", "version": 1, "status": "draft",
             "accepted_version": None, "legacy_domains": doms, "requirements": reqs, "tables": [], "enums": [],
             "external_refs": [], "created_at": d.now_iso(), "updated_at": d.now_iso()}
    os.makedirs(os.path.join(d.context_dir(root, ctx), "snapshots"), exist_ok=True)
    d.save_json(model_path(root, ctx), model)
    d.write_text(os.path.join(d.context_dir(root, ctx), "design.md"),
                 DESIGN_MD.format(ctx=ctx, description=a.description or "", domains=", ".join(doms) or "-", req=", ".join(reqs) or "-"))
    mp = d.mem(root, "mapping", f"{ctx}.json")
    if not os.path.exists(mp):
        d.save_json(mp, {"context": ctx, "table_mappings": [], "column_mappings": [], "retired": []})
    man = d.load_manifest(root)
    man["target"]["contexts"][ctx] = {"schema": schema, "status": "draft", "version": 1, "accepted_version": None}
    if man["phase"] in ("assessed", "ingested", "initialised"):
        man["phase"] = "modernizing"
    d.save_manifest(root, man)
    d.journal(root, "domain-modernization", "context-new", f"{ctx} (schema {schema}) from legacy {doms}", requirement=a.requirement)
    print(f"created context {ctx}: {d.context_dir(root, ctx)}")


def _sync_registry(root, m):
    man = d.load_manifest(root)
    man["target"]["contexts"][m["context"]] = {"schema": m["schema"], "status": m["status"], "version": m["version"],
                                               "accepted_version": m.get("accepted_version")}
    d.save_manifest(root, man)


def cmd_bump(root, a):
    m = d.load_model(root, a.ctx)
    if m["status"] != "accepted":
        d.die(f"{a.ctx} is {m['status']} v{m['version']}: keep editing the working version, no bump needed")
    m["version"] += 1
    m["status"] = "draft"
    if a.requirement and a.requirement not in m["requirements"]:
        m["requirements"].append(a.requirement)
    m["updated_at"] = d.now_iso()
    d.save_json(model_path(root, a.ctx), m)
    _sync_registry(root, m)
    with open(os.path.join(d.context_dir(root, a.ctx), "design.md"), "a", encoding="utf-8") as fh:
        fh.write(f"| {m['version']} | draft | {a.requirement or '-'} | {a.reason or ''} |\n")
    d.journal(root, "domain-modernization", "context-bump", f"{a.ctx} -> v{m['version']} draft", requirement=a.requirement, reason=a.reason)
    print(f"{a.ctx}: v{m['version']} draft (last accepted v{m['accepted_version']})")


def cmd_propose(root, a):
    m = d.load_model(root, a.ctx)
    if m["status"] == "accepted":
        d.die("already accepted; bump first")
    m["status"] = "proposed"
    m["updated_at"] = d.now_iso()
    d.save_json(model_path(root, a.ctx), m)
    _sync_registry(root, m)
    d.journal(root, "domain-modernization", "context-propose", f"{a.ctx} v{m['version']} proposed for review")
    print(f"{a.ctx} v{m['version']}: proposed")


def cmd_add_audit(root, a):
    m = d.load_model(root, a.ctx)
    if m["status"] == "accepted":
        d.die("accepted model is frozen; bump first")
    std = d.load_standards(root)
    aud = std.get("audit_columns", {})
    lock = std.get("optimistic_locking", {})
    added = []
    for t in m["tables"]:
        if a.table and t["name"] != a.table:
            continue
        names = {c["name"] for c in t["columns"]}
        if aud.get("required") and t.get("role") not in aud.get("exempt_roles", []):
            for c in aud["columns"]:
                if c["name"] not in names:
                    t["columns"].append(dict(c, description="audit column", pii="none"))
                    added.append(f"{t['name']}.{c['name']}")
        if t.get("role") in lock.get("required_for_roles", []) and lock["column"]["name"] not in names:
            t["columns"].append(dict(lock["column"], description="optimistic locking version", pii="none"))
            added.append(f"{t['name']}.{lock['column']['name']}")
    m["updated_at"] = d.now_iso()
    d.save_json(model_path(root, a.ctx), m)
    print("added: " + (", ".join(added) or "nothing"))


def cmd_diff(root, a):
    m = d.load_model(root, a.ctx)
    av = m.get("accepted_version")
    old = d.load_json(os.path.join(d.context_dir(root, a.ctx), "snapshots", f"v{av:03d}.json")) if av else {"tables": []}
    ot = {t["name"]: t for t in old.get("tables", [])}
    nt = {t["name"]: t for t in m.get("tables", [])}
    for n in sorted(set(nt) - set(ot)):
        print(f"+ table {n}")
    for n in sorted(set(ot) - set(nt)):
        print(f"- table {n}")
    for n in sorted(set(ot) & set(nt)):
        oc = {c["name"]: c for c in ot[n]["columns"]}
        nc = {c["name"]: c for c in nt[n]["columns"]}
        for c in sorted(set(nc) - set(oc)):
            print(f"+ {n}.{c} {nc[c]['type']}")
        for c in sorted(set(oc) - set(nc)):
            print(f"- {n}.{c}")
        for c in sorted(set(oc) & set(nc)):
            if json.dumps(oc[c], sort_keys=True) != json.dumps(nc[c], sort_keys=True):
                print(f"~ {n}.{c}")
        for k in ("primary_key", "unique", "checks", "foreign_keys", "indexes"):
            if json.dumps(ot[n].get(k), sort_keys=True) != json.dumps(nt[n].get(k), sort_keys=True):
                print(f"~ {n} {k}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new"); n.add_argument("ctx"); n.add_argument("--legacy-domains"); n.add_argument("--schema")
    n.add_argument("--description"); n.add_argument("--requirement")
    b = sub.add_parser("bump"); b.add_argument("ctx"); b.add_argument("--requirement"); b.add_argument("--reason")
    p = sub.add_parser("propose"); p.add_argument("ctx")
    au = sub.add_parser("add-audit"); au.add_argument("ctx"); au.add_argument("--table")
    df = sub.add_parser("diff"); df.add_argument("ctx")
    a = ap.parse_args()
    root = d.find_root(a.root)
    {"new": cmd_new, "bump": cmd_bump, "propose": cmd_propose, "add-audit": cmd_add_audit, "diff": cmd_diff}[a.cmd](root, a)


if __name__ == "__main__":
    main()
