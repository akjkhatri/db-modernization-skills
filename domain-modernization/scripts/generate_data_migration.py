#!/usr/bin/env python3
"""Generate the one-off legacy -> new data migration for a context from its lineage
(mapping/<ctx>.json), model and the legacy catalog.

Output: <repo>/<data_migration.location>/<ctx>/v<NNN>/
  00_prerequisites.sql  postgres_fdw foreign schema for legacy tables + migration_map schema
  01_id_maps.sql        legacy key -> new id maps (pre-allocated from identity sequences)
  02_precheck.sql       duplicate legacy keys, unresolved lookups (orphans): run, review, fix/decide
  03_load.sql           INSERT ... SELECT per table in FK order, one transaction
  04_reconcile.sql      counts and money sums, source vs target
  README.md             run order, lineage summary, retired legacy columns

Conventions in mapping/<ctx>.json (see references/target-model-format.md):
  table_mappings[].sources[0] is the driving legacy table; extra sources need "joins":
     [{"table": "public.cust_contact", "type": "left", "on": "cust_contact.cust_id = cust_mst.cust_id"}]
  "filter": SQL on bare legacy table names; "legacy_key": [cols of driving table] (default its PK)
  "custom_select": full SELECT for unpivots/splits (returns custom_columns in order)
  column_mappings[].transform uses bare legacy table names (quote mixed-case ones)
  rule "lookup" + "lookup": "<schema.table>" resolves a legacy key through that table's id map

Usage: python generate_data_migration.py --context billing [--root <repo>]
"""
import argparse
import os
import sys
from collections import defaultdict

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402
from generate_migration import qi  # noqa: E402

MONEY = {"amount", "price", "cost", "total", "balance", "fee", "charge", "tax", "vat"}


def legacy_schema_alias(schema):
    return "legacy_src" if schema in ("public", "") else f"legacy_src_{schema}"


def topo(tables):
    deps = {t["name"]: {fk["references"]["table"] for fk in t.get("foreign_keys", []) if fk["references"]["table"] != t["name"]}
            for t in tables}
    out, seen = [], set()

    def visit(n, stack=()):
        if n in seen or n not in deps:
            return
        if n in stack:
            return
        for x in sorted(deps[n]):
            visit(x, stack + (n,))
        seen.add(n)
        out.append(n)
    for n in sorted(deps):
        visit(n)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--context", required=True)
    a = ap.parse_args()
    root = d.find_root(a.root)
    ctx = a.context
    m = d.load_model(root, ctx)
    mp = d.load_mapping(root, ctx)
    cat = d.load_catalog(root)
    std = d.load_standards(root)
    sch = m["schema"]
    mapsch = std["data_migration"].get("id_mapping_schema", "migration_map")
    audit = {c["name"]: c for c in std.get("audit_columns", {}).get("columns", [])}
    tm_by = {tm["target"].split(".", 1)[1]: tm for tm in mp.get("table_mappings", []) if tm["target"].startswith(sch + ".")}
    cm_by = defaultdict(dict)
    for cm in mp.get("column_mappings", []):
        s, t, c = d.split_ref(cm["target"])
        if s == sch:
            cm_by[t][c] = cm
    tables = {t["name"]: t for t in m["tables"]}
    order = topo(m["tables"])

    def leg(ref_table_key):
        t = cat["tables"][ref_table_key.lower()]
        return f"{legacy_schema_alias(t['schema'].lower())}.{qi(t['name'])}", qi(t["name"])

    def key_expr(tm):
        drv = cat["tables"][tm["sources"][0].lower()]
        cols = tm.get("legacy_key") or drv["primary_key"]
        if not cols:
            return None
        alias = qi(drv["name"])
        real = {c["name"].lower(): c["name"] for c in drv["columns"]}
        parts = [f"{alias}.{qi(real.get(c.lower(), c))}::text" for c in cols]
        return parts[0] if len(parts) == 1 else "concat_ws('|', " + ", ".join(parts) + ")"

    def map_table(target_table_key):
        s, t, _ = d.split_ref(target_table_key + ".x")
        return f"{mapsch}.{s}__{t}"

    def identity_pk(t):
        pk = t.get("primary_key", [])
        if len(pk) == 1:
            c = next(c for c in t["columns"] if c["name"] == pk[0])
            return c if c.get("identity") else None
        return None

    foreign_tables = defaultdict(set)
    for tm in mp.get("table_mappings", []):
        for s in tm.get("sources", []):
            if s.lower() in cat["tables"]:
                lt = cat["tables"][s.lower()]
                foreign_tables[lt["schema"]].add(lt["name"])
        for j in tm.get("joins", []):
            lt = cat["tables"].get(j["table"].lower())
            if lt:
                foreign_tables[lt["schema"]].add(lt["name"])

    P = ["-- 00 prerequisites: run once per migration database (needs superuser or the postgres_fdw owner)",
         "CREATE EXTENSION IF NOT EXISTS postgres_fdw;",
         "-- CREATE SERVER legacy_server FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host '<legacy-host>', dbname '<legacy-db>', port '5432');",
         "-- CREATE USER MAPPING FOR CURRENT_USER SERVER legacy_server OPTIONS (user '<readonly-user>', password '<from-vault>');",
         f"CREATE SCHEMA IF NOT EXISTS {mapsch};"]
    for s, ts in sorted(foreign_tables.items()):
        alias = legacy_schema_alias(s.lower())
        P += [f"CREATE SCHEMA IF NOT EXISTS {alias};"]
        P += [f"DROP FOREIGN TABLE IF EXISTS {alias}.{qi(t)};" for t in sorted(ts)]
        P += [f"IMPORT FOREIGN SCHEMA {qi(s)} LIMIT TO ({', '.join(qi(t) for t in sorted(ts))}) FROM SERVER legacy_server INTO {alias};"]

    IDM, PRE, LOAD, REC, notes = [], [], [], [], []
    deps_ext = set()
    for tn in order:
        t = tables[tn]
        tm = tm_by.get(tn)
        if not tm or not tm.get("sources"):
            notes.append(f"- `{sch}.{tn}`: no legacy source ({(tm or {}).get('notes', 'new table')}), nothing to load")
            continue
        drv_key = tm["sources"][0].lower()
        drv_full, drv_alias = leg(drv_key)
        joins = []
        for j in tm.get("joins", []):
            jf, ja = leg(j["table"])
            joins.append(f"{j.get('type', 'left').upper()} JOIN {jf} AS {ja} ON {j['on']}")
        where = f"WHERE {tm['filter']}" if tm.get("filter") else ""
        base_from = f"FROM {drv_full} AS {drv_alias}" + ("".join("\n    " + x for x in joins))
        ipk = identity_pk(t)
        kx = key_expr(tm)
        target = f"{qi(sch)}.{qi(tn)}"
        if tm.get("custom_select"):
            cols = tm.get("custom_columns") or []
            LOAD += [f"-- {sch}.{tn} <- custom select ({tm.get('grain', '')})",
                     f"INSERT INTO {target} ({', '.join(qi(c) for c in cols)})", tm["custom_select"].rstrip(";") + ";", ""]
            REC.append(f"SELECT '{tn}' AS target, NULL::bigint AS source_rows, (SELECT count(*) FROM {target}) AS target_rows, 'custom select: verify manually' AS check_note")
            continue
        if ipk and kx:
            mt = map_table(f"{sch}.{tn}")
            IDM += [f"-- {sch}.{tn}: legacy key {kx}",
                    f"CREATE TABLE IF NOT EXISTS {mt} (legacy_key text PRIMARY KEY, new_id bigint NOT NULL UNIQUE);",
                    f"INSERT INTO {mt} (legacy_key, new_id)",
                    f"SELECT k, nextval(pg_get_serial_sequence('{sch}.{tn}', '{ipk['name']}'))",
                    f"FROM (SELECT DISTINCT {kx} AS k {base_from} {where}) s",
                    "WHERE k IS NOT NULL",
                    "ON CONFLICT (legacy_key) DO NOTHING;", ""]
            PRE.append((f"{tn}: duplicate legacy key (rows merge into one)",
                        f"SELECT {kx} AS detail, count(*) AS n {base_from} {where} GROUP BY 1 HAVING count(*) > 1"))
        sel_cols, sel_exprs, lookups = [], [], []
        if ipk and kx:
            sel_cols.append(qi(ipk["name"]))
            sel_exprs.append("m0.new_id")
            lookups.append(f"JOIN {map_table(f'{sch}.{tn}')} AS m0 ON m0.legacy_key = {kx}")
        li = 0
        for c in t["columns"]:
            if ipk and c["name"] == ipk["name"]:
                continue
            cm = cm_by[tn].get(c["name"])
            expr = None
            if cm:
                rule = cm.get("rule", "direct")
                if rule == "lookup":
                    li += 1
                    tgt = cm["lookup"].lower()
                    if not tgt.startswith(sch + "."):
                        deps_ext.add(tgt)
                    src = cm.get("transform")
                    if not src:
                        s_, t_, c_ = d.split_ref(cm["sources"][0])
                        lt = cat["tables"][f"{s_}.{t_}"]
                        real = next(x["name"] for x in lt["columns"] if x["name"].lower() == c_)
                        src = f"{qi(lt['name'])}.{qi(real)}"
                    lookups.append(f"LEFT JOIN {map_table(tgt)} AS lk{li} ON lk{li}.legacy_key = ({src})::text")
                    expr = f"lk{li}.new_id"
                    PRE.append((f"{tn}.{c['name']}: no match in {tgt}" + (" (NOT NULL: load fails)" if not c.get("nullable") else " (loads as NULL)"),
                                f"SELECT ({src})::text AS detail, count(*) AS n {base_from} LEFT JOIN {map_table(tgt)} AS lk ON lk.legacy_key = ({src})::text "
                                f"{where + ' AND' if where else 'WHERE'} ({src}) IS NOT NULL AND lk.new_id IS NULL GROUP BY 1"))
                elif rule in ("new", "generated") and not cm.get("transform"):
                    expr = None
                elif cm.get("transform"):
                    expr = cm["transform"]
                elif cm.get("sources"):
                    s_, t_, c_ = d.split_ref(cm["sources"][0])
                    lt = cat["tables"][f"{s_}.{t_}"]
                    real = next(x["name"] for x in lt["columns"] if x["name"].lower() == c_)
                    expr = f"{qi(lt['name'])}.{qi(real)}"
            elif c["name"] in audit and c["name"].endswith("_by"):
                expr = "'legacy-migration'"
            elif c.get("identity") or c.get("default") is not None:
                expr = None
            else:
                expr = f"NULL /* TODO: no lineage for {c['name']} */"
            if expr is not None:
                sel_cols.append(qi(c["name"]))
                sel_exprs.append(expr)
        # ---- prechecks on the exact projection the load will insert, so an empty precheck really means "load will pass"
        proj = ("SELECT " + ", ".join(f"{e} AS {c}" for e, c in zip(sel_exprs, sel_cols)) + " " + base_from
                + "".join(" " + x for x in lookups) + (f" {where}" if where else ""))
        PRE.append((f"{tn}: transforms/casts fail (bad dates, enum values, numbers)",
                    f"SELECT 'all rows evaluated'::text AS detail, count(*) AS n FROM ({proj}) p WHERE (p.*)::text IS NULL HAVING count(*) > 0"))
        for c in t["columns"]:
            qc = qi(c["name"])
            if qc in sel_cols and not c.get("nullable", False):
                PRE.append((f"{tn}.{c['name']}: NULL into NOT NULL column",
                            f"SELECT 'rows'::text AS detail, count(*) AS n FROM ({proj}) p WHERE p.{qc} IS NULL HAVING count(*) > 0"))
        for ck in t.get("checks", []):
            PRE.append((f"{tn}: check '{ck['name']}' violated",
                        f"SELECT 'rows'::text AS detail, count(*) AS n FROM ({proj}) p WHERE NOT ({ck['expression']}) HAVING count(*) > 0"))
        for u in t.get("unique", []):
            if all(qi(x) in sel_cols for x in u["columns"]):
                cols_u = ", ".join(f"p.{qi(x)}::text" for x in u["columns"])
                PRE.append((f"{tn}: unique ({', '.join(u['columns'])}) violated",
                            f"SELECT concat_ws('|', {cols_u}) AS detail, count(*) AS n FROM ({proj}) p GROUP BY 1 HAVING count(*) > 1"))
        for fk in t.get("foreign_keys", []):
            col = fk["columns"][0]
            rt = fk["references"]["table"]
            if len(fk["columns"]) == 1 and qi(col) in sel_cols and rt in tables and not tm_by.get(rt, {}).get("sources"):
                PRE.append((f"{tn}.{col}: value missing from reference table {sch}.{rt}",
                            f"SELECT p.{qi(col)}::text AS detail, count(*) AS n FROM ({proj}) p WHERE p.{qi(col)} IS NOT NULL "
                            f"AND p.{qi(col)}::text NOT IN (SELECT {qi(fk['references']['columns'][0])}::text FROM {qi(sch)}.{qi(rt)}) GROUP BY 1"))
        LOAD += [f"-- {sch}.{tn} <- {', '.join(tm['sources'])}" + (f" · grain: {tm['grain']}" if tm.get("grain") else ""),
                 f"INSERT INTO {target} ({', '.join(sel_cols)})" + (" OVERRIDING SYSTEM VALUE" if ipk and kx else ""),
                 "SELECT " + ",\n       ".join(sel_exprs),
                 base_from + "".join("\n    " + x for x in lookups),
                 (where + ";") if where else ";", ""]
        REC.append(f"SELECT '{tn}' AS target, (SELECT count(*) {base_from} {where}) AS source_rows, "
                   f"(SELECT count(*) FROM {target}) AS target_rows, 'row count' AS check_note")
        for c in t["columns"]:
            cm = cm_by[tn].get(c["name"])
            if cm and set(d.tokens(c["name"])) & MONEY and not set(d.tokens(c["name"])) & {"unit", "rate", "percent"} \
                    and d.base_type(c["type"]) == "numeric" and cm.get("rule") in ("direct", "transform", "derived"):
                ex = cm.get("transform") or sel_exprs[sel_cols.index(qi(c["name"]))]
                REC.append(f"SELECT '{tn}.{c['name']}' AS target, (SELECT sum({ex})::numeric {base_from} {where}) AS source_rows, "
                           f"(SELECT sum({qi(c['name'])}) FROM {target}) AS target_rows, 'money sum' AS check_note")

    ver = m["version"]
    out = os.path.join(root, std["data_migration"]["location"], ctx, f"v{ver:03d}")
    hdr = f"-- data migration · context {ctx} · model v{ver} ({m['status']}) · generated {d.now_iso()} from .dbmod lineage\n"
    d.write_text(os.path.join(out, "00_prerequisites.sql"), hdr + "\n".join(P) + "\n")
    d.write_text(os.path.join(out, "01_id_maps.sql"), hdr + "BEGIN;\n" + "\n".join(IDM) + "COMMIT;\n")
    blocks = []
    for i, (lbl, q) in enumerate(PRE, 1):
        L = lbl.replace("'", "''")
        blocks.append(f"""DO $chk$ BEGIN
  INSERT INTO precheck_issues SELECT {i}, '{L}', detail, n FROM ({q} ORDER BY 2 DESC LIMIT 100) c;
EXCEPTION
  WHEN undefined_table THEN INSERT INTO precheck_issues VALUES ({i}, '{L}', 'NOT RUN: ' || SQLERRM || ' (run 01_id_maps.sql here and load the contexts this one depends on first)', NULL);
  WHEN OTHERS THEN INSERT INTO precheck_issues VALUES ({i}, '{L}', 'FAILS: ' || SQLERRM, NULL);
END $chk$;""")
    d.write_text(os.path.join(out, "02_precheck.sql"), hdr +
                 "-- One result row per data issue. An EMPTY result means 03_load.sql will pass: every check runs on the exact\n"
                 "-- projection the load inserts (casts, NOT NULL, CHECK, UNIQUE, lookups, reference data).\n"
                 "-- Every row is a data decision: fix at source, filter in the mapping, or default (record it in an ADR).\n"
                 "CREATE TEMP TABLE IF NOT EXISTS precheck_issues (seq int, issue text, detail text, n bigint);\nTRUNCATE precheck_issues;\n\n"
                 + "\n".join(blocks) + "\n\nSELECT issue, detail, n FROM precheck_issues ORDER BY seq, n DESC NULLS FIRST;\n")
    d.write_text(os.path.join(out, "03_load.sql"), hdr + "BEGIN;\nSET LOCAL statement_timeout = 0;\n\n" + "\n".join(LOAD) + "COMMIT;\n")
    d.write_text(os.path.join(out, "04_reconcile.sql"), hdr + ("\nUNION ALL\n".join(REC) + ";\n" if REC else "-- nothing to reconcile\n"))
    retired = mp.get("retired", [])
    readme = [f"# Data migration: {ctx} (model v{ver})", "",
              "Run against the NEW database, with legacy reachable via postgres_fdw:", "",
              "1. `00_prerequisites.sql`: fill in the server and user mapping first (credentials from your vault, never committed)",
              "2. `01_id_maps.sql`: pre-allocates new ids for every legacy key",
              "3. `02_precheck.sql`: must return no rows (it evaluates the exact load projection). Resolve every row first",
              "4. `03_load.sql`: one transaction, tables in foreign-key order",
              "5. `04_reconcile.sql`: source vs target counts and money sums must match (or differ only by documented filters)", "",
              "Re-runnable: the id maps are idempotent. Truncate the target tables before re-running 03.", ""]
    if deps_ext:
        readme += ["**Depends on other contexts being loaded first** (their id maps are used): " + ", ".join(sorted(deps_ext)), ""]
    if notes:
        readme += ["Tables with nothing to load:", ""] + notes + [""]
    readme += [f"Retired legacy data (deliberately NOT migrated, {len(retired)} entries):", ""]
    readme += [f"- `{r['source']}`: {r.get('reason', '')} {('(' + r['decision'] + ')') if r.get('decision') else ''}" for r in retired] or ["- none"]
    d.write_text(os.path.join(out, "README.md"), "\n".join(readme) + "\n")
    d.journal(root, "domain-modernization", "data-migration", f"{ctx} v{ver} scripts -> {os.path.relpath(out, root)}")
    print(f"data migration scripts: {out}")
    if deps_ext:
        print("depends on id maps of: " + ", ".join(sorted(deps_ext)))


if __name__ == "__main__":
    main()
