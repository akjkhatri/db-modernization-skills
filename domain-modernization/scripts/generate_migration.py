#!/usr/bin/env python3
"""Generate a Flyway SQL migration for a context by diffing its working model.json against
the last ACCEPTED snapshot (or nothing, for a brand-new context).

Preview (default): writes .dbmod/target/contexts/<ctx>/pending/preview.sql and prints it.
The real versioned file is written by accept_model.py, so Flyway history only ever
contains approved changes.

Supports: create schema/enum/table, add enum values, add/alter/rename columns
("renamed_from" on a column or table), nullability/default/type changes, PK/unique/check/FK/
index changes, comments (with [PII:class] tags), automatic indexes on FK columns.
Destructive changes (drop table/column/enum value) fail unless --allow-destructive.

Usage: python generate_migration.py --context billing [--root <repo>] [--allow-destructive] [--quiet]
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402

TYPE_NORMAL = {"timestamptz": "timestamp with time zone", "varchar": "character varying", "int": "integer",
               "int8": "bigint", "int4": "integer", "bool": "boolean"}


def qi(n):
    return n if d.is_snake(n) and n not in d.PG_RESERVED else '"' + n.replace('"', '""') + '"'


def lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def cname(std, kind, table, **kw):
    tmpl = std["naming"]["constraint_names"][kind]
    n = tmpl.format(table=table, **kw)
    if len(n) > 63:
        n = n[:54] + "_" + hashlib.md5(n.encode()).hexdigest()[:8]
    return n


def coltype(m, c):
    t = c["type"].strip()
    if t in {e["name"] for e in m.get("enums", [])}:
        return f"{qi(m['schema'])}.{qi(t)}"
    return TYPE_NORMAL.get(t.lower(), t)


def coldef(m, c):
    parts = [qi(c["name"]), coltype(m, c)]
    if c.get("identity"):
        parts.append("GENERATED ALWAYS AS IDENTITY")
    if not c.get("nullable", False):
        parts.append("NOT NULL")
    if c.get("default") is not None and not c.get("identity"):
        parts.append(f"DEFAULT {c['default']}")
    return " ".join(parts)


def tref(m, t):
    return f"{qi(m['schema'])}.{qi(t)}"


def constraints(std, m, t):
    """name -> SQL fragment for every named constraint of table t (excluding FKs)."""
    out = {}
    if t.get("primary_key"):
        out[cname(std, "primary_key", t["name"])] = "PRIMARY KEY (" + ", ".join(qi(c) for c in t["primary_key"]) + ")"
    for u in t.get("unique", []):
        n = u.get("name") or cname(std, "unique", t["name"], columns="_".join(u["columns"]))
        out[n] = "UNIQUE " + ("NULLS NOT DISTINCT " if u.get("nulls_not_distinct") else "") + "(" + ", ".join(qi(c) for c in u["columns"]) + ")"
    for ck in t.get("checks", []):
        out[cname(std, "check", t["name"], name=ck["name"])] = f"CHECK ({ck['expression']})"
    return out


def fks(std, m, t):
    out = {}
    for fk in t.get("foreign_keys", []):
        rt = fk["references"]["table"]
        n = fk.get("name") or cname(std, "foreign_key", t["name"], ref_table=rt)
        if n in out:
            n = cname(std, "foreign_key", t["name"], ref_table=rt + "_" + "_".join(fk["columns"]))
        out[n] = (f"FOREIGN KEY ({', '.join(qi(c) for c in fk['columns'])}) REFERENCES {tref(m, rt)} "
                  f"({', '.join(qi(c) for c in fk['references']['columns'])}) ON DELETE {fk.get('on_delete', 'restrict').upper()}")
    return out


def indexes(std, m, t):
    out = {}
    covered = [t.get("primary_key", [])] + [u["columns"] for u in t.get("unique", [])]
    for ix in t.get("indexes", []):
        n = ix.get("name") or cname(std, "index", t["name"], columns="_".join(c.split("(")[0].strip() for c in ix["columns"]))
        cols = ", ".join(c if "(" in c else qi(c) for c in ix["columns"])
        out[n] = (f"CREATE {'UNIQUE ' if ix.get('unique') else ''}INDEX {qi(n)} ON {tref(m, t['name'])} "
                  f"{'USING ' + ix['method'] + ' ' if ix.get('method') else ''}({cols})" + (f" WHERE {ix['where']}" if ix.get("where") else ""))
        covered.append(ix["columns"])
    if std["constraints"].get("index_foreign_keys"):
        for fk in t.get("foreign_keys", []):
            if not any(cv[: len(fk["columns"])] == fk["columns"] for cv in covered):
                n = cname(std, "index", t["name"], columns="_".join(fk["columns"]))
                out[n] = f"CREATE INDEX {qi(n)} ON {tref(m, t['name'])} ({', '.join(qi(c) for c in fk['columns'])})"
                covered.append(fk["columns"])
    return out


def comment_sql(std, m, t, c=None):
    tag = std["gdpr"].get("comment_tag", "[PII:{class}]")
    if c is None:
        txt = t.get("description") or ""
        if t.get("retention"):
            txt += f" Retention: {t['retention']}"
        return f"COMMENT ON TABLE {tref(m, t['name'])} IS {lit(txt.strip())};"
    txt = c.get("description") or ""
    if c.get("pii") and c["pii"] != "none":
        txt = tag.format(**{"class": c["pii"]}) + " " + txt
    return f"COMMENT ON COLUMN {tref(m, t['name'])}.{qi(c['name'])} IS {lit(txt.strip())};"


def build(std, old, new, allow_destructive=False):
    """Return (sql_lines, warnings, destructive_list)."""
    S, W, X = [], [], []
    m = new
    first = old is None
    old = old or {"tables": [], "enums": []}
    if first:
        S += [f"CREATE SCHEMA IF NOT EXISTS {qi(m['schema'])};",
              f"COMMENT ON SCHEMA {qi(m['schema'])} IS {lit('Bounded context: ' + m['context'] + '. ' + (m.get('description') or ''))};", ""]
    # enums
    oe = {e["name"]: e for e in old.get("enums", [])}
    for e in m.get("enums", []):
        if e["name"] not in oe:
            S.append(f"CREATE TYPE {qi(m['schema'])}.{qi(e['name'])} AS ENUM ({', '.join(lit(v) for v in e['values'])});")
        else:
            for v in e["values"]:
                if v not in oe[e["name"]]["values"]:
                    S.append(f"ALTER TYPE {qi(m['schema'])}.{qi(e['name'])} ADD VALUE IF NOT EXISTS {lit(v)};")
            for v in oe[e["name"]]["values"]:
                if v not in e["values"]:
                    X.append(f"enum value {e['name']}.{v} removed (PostgreSQL cannot drop enum values; needs a manual type swap)")
    for n in oe:
        if n not in {e["name"] for e in m.get("enums", [])}:
            X.append(f"enum {n} removed")
            if allow_destructive:
                S.append(f"DROP TYPE {qi(m['schema'])}.{qi(n)};")
    ot = {t["name"]: t for t in old.get("tables", [])}
    nt = {t["name"]: t for t in m.get("tables", [])}
    # table renames
    for t in nt.values():
        rf = t.get("renamed_from")
        if rf and rf in ot and t["name"] not in ot:
            S.append(f"ALTER TABLE {tref(m, rf)} RENAME TO {qi(t['name'])};")
            ot[t["name"]] = dict(ot.pop(rf), name=t["name"])
    created = [n for n in nt if n not in ot]
    post = []
    for n in created:
        t = nt[n]
        body = [coldef(m, c) for c in t["columns"]]
        body += [f"CONSTRAINT {qi(k)} {v}" for k, v in constraints(std, m, t).items()]
        S += [f"CREATE TABLE {tref(m, n)} (", "    " + ",\n    ".join(body), ");"]
        post += [f"ALTER TABLE {tref(m, n)} ADD CONSTRAINT {qi(k)} {v};" for k, v in fks(std, m, t).items()]
        post += [v + ";" for v in indexes(std, m, t).values()]
        post.append(comment_sql(std, m, t))
        post += [comment_sql(std, m, t, c) for c in t["columns"]]
        S.append("")
    for n in nt:
        if n in created:
            continue
        t, o = nt[n], ot[n]
        A = []
        oc = {c["name"]: c for c in o["columns"]}
        for c in t["columns"]:
            rf = c.get("renamed_from")
            if rf and rf in oc and c["name"] not in oc:
                A.append(f"ALTER TABLE {tref(m, n)} RENAME COLUMN {qi(rf)} TO {qi(c['name'])};")
                oc[c["name"]] = dict(oc.pop(rf), name=c["name"])
        for c in t["columns"]:
            if c["name"] not in oc:
                if not c.get("nullable", False) and c.get("default") is None and not c.get("identity"):
                    W.append(f"{n}.{c['name']}: NOT NULL column added without default; fails if the table has rows (add a default or backfill step)")
                A.append(f"ALTER TABLE {tref(m, n)} ADD COLUMN {coldef(m, c)};")
                post.append(comment_sql(std, m, t, c))
                continue
            p = oc[c["name"]]
            if coltype(m, c).lower() != coltype(m, p).lower():
                A.append(f"ALTER TABLE {tref(m, n)} ALTER COLUMN {qi(c['name'])} TYPE {coltype(m, c)} USING {qi(c['name'])}::{coltype(m, c)};")
                W.append(f"{n}.{c['name']}: type change {p['type']} -> {c['type']} (check the USING cast and table rewrite cost)")
            if bool(c.get("nullable", False)) != bool(p.get("nullable", False)):
                A.append(f"ALTER TABLE {tref(m, n)} ALTER COLUMN {qi(c['name'])} {'DROP' if c.get('nullable') else 'SET'} NOT NULL;")
                if not c.get("nullable"):
                    W.append(f"{n}.{c['name']}: SET NOT NULL fails if NULLs exist")
            if c.get("default") != p.get("default") and not c.get("identity"):
                A.append(f"ALTER TABLE {tref(m, n)} ALTER COLUMN {qi(c['name'])} " + (f"SET DEFAULT {c['default']};" if c.get("default") is not None else "DROP DEFAULT;"))
            if bool(c.get("identity")) != bool(p.get("identity")):
                A.append(f"ALTER TABLE {tref(m, n)} ALTER COLUMN {qi(c['name'])} " + ("ADD GENERATED ALWAYS AS IDENTITY;" if c.get("identity") else "DROP IDENTITY;"))
            if (c.get("description"), c.get("pii")) != (p.get("description"), p.get("pii")):
                post.append(comment_sql(std, m, t, c))
        for cn in oc:
            if cn not in {c["name"] for c in t["columns"]}:
                X.append(f"column {n}.{cn} dropped")
                if allow_destructive:
                    A.append(f"ALTER TABLE {tref(m, n)} DROP COLUMN {qi(cn)};")
        for kind, fn in (("constraint", constraints), ("fk", fks)):
            oldc, newc = fn(std, m, o), fn(std, m, t)
            for k, v in oldc.items():
                if k not in newc or newc[k] != v:
                    A.append(f"ALTER TABLE {tref(m, n)} DROP CONSTRAINT {qi(k)};")
            for k, v in newc.items():
                if k not in oldc or oldc[k] != v:
                    (A if kind == "constraint" else post).append(f"ALTER TABLE {tref(m, n)} ADD CONSTRAINT {qi(k)} {v};")
        oi, ni = indexes(std, m, o), indexes(std, m, t)
        for k, v in oi.items():
            if k not in ni or ni[k] != v:
                A.append(f"DROP INDEX {qi(m['schema'])}.{qi(k)};")
        for k, v in ni.items():
            if k not in oi or oi[k] != v:
                post.append(v + ";")
        if (t.get("description"), t.get("retention")) != (o.get("description"), o.get("retention")):
            post.append(comment_sql(std, m, t))
        if A:
            S += [f"-- {n}"] + A + [""]
    # reference data seeds (model table "seed": [ {col: value, ...} ]); idempotent inserts by PK
    for n, t in nt.items():
        if not t.get("seed"):
            continue
        pk = t.get("primary_key", [])
        old_keys = {tuple(str(r.get(k)) for k in pk) for r in ot.get(n, {}).get("seed", [])} if n in ot else set()
        new_rows = [r for r in t["seed"] if tuple(str(r.get(k)) for k in pk) not in old_keys]
        if new_rows:
            cols = list(new_rows[0].keys())
            vals = ",\n    ".join("(" + ", ".join("NULL" if r.get(c) is None else (str(r[c]).lower() if isinstance(r[c], bool) else (str(r[c]) if isinstance(r[c], (int, float)) else lit(r[c]))) for c in cols) + ")" for r in new_rows)
            post.append(f"INSERT INTO {tref(m, n)} ({', '.join(qi(c) for c in cols)}) VALUES\n    {vals}\nON CONFLICT DO NOTHING;")
    for n in ot:
        if n not in nt:
            X.append(f"table {n} dropped")
            if allow_destructive:
                S.append(f"DROP TABLE {tref(m, n)};")
    if post:
        S += ["-- constraints, indexes and comments"] + post
    return S, W, X


def header(m, base_version, reqs, extra=""):
    return [f"-- Flyway migration · context: {m['context']} · schema: {m['schema']}",
            f"-- model version {m['version']} (from {'v' + str(base_version) if base_version else 'nothing'})",
            f"-- requirements: {', '.join(reqs) or '-'}",
            "-- generated by domain-modernization/generate_migration.py from .dbmod; do not edit after it has been applied",
            extra, ""]


def load_base(root, m):
    av = m.get("accepted_version")
    if not av:
        return None, None
    return d.load_json(os.path.join(d.context_dir(root, m["context"]), "snapshots", f"v{av:03d}.json")), av


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--context", required=True)
    ap.add_argument("--allow-destructive", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    root = d.find_root(a.root)
    m = d.load_model(root, a.context)
    std = d.load_standards(root)
    base, av = load_base(root, m)
    if base and json.dumps(base.get("tables"), sort_keys=True) == json.dumps(m.get("tables"), sort_keys=True) \
            and json.dumps(base.get("enums"), sort_keys=True) == json.dumps(m.get("enums"), sort_keys=True):
        print("no changes since accepted v%s" % av)
        return
    if m["status"] == "accepted":
        d.die("model is accepted but differs from its snapshot: run context_tool.py bump first")
    S, W, X = build(std, base, m, a.allow_destructive)
    if X and not a.allow_destructive:
        for x in X:
            print("DESTRUCTIVE  " + x)
        d.die("destructive changes found. Prefer additive change (expand/contract); re-run with --allow-destructive after an ADR.")
    text = "\n".join(header(m, av, m.get("requirements", [])) + S) + "\n"
    p = os.path.join(d.context_dir(root, a.context), "pending", "preview.sql")
    d.write_text(p, text)
    for w in W:
        print("WARN   " + w)
    if not a.quiet:
        print(text)
    print(f"preview: {p}")


if __name__ == "__main__":
    main()
