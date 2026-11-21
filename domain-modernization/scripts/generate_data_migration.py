#!/usr/bin/env python3
"""Generate the one-off legacy -> new data migration for a context from its lineage
(mapping/<ctx>.json), model and the legacy catalog.

Output: <repo>/<data_migration.location>/<ctx>/v<NNN>/
  00_prerequisites.sql  postgres_fdw foreign schema for legacy tables + migration_map schema
  01_id_maps.sql        legacy key -> new id maps (pre-allocated from identity sequences)
  02_precheck.sql       duplicate legacy keys, unresolved lookups (orphans): run, review, fix/decide
  03_load.sql           INSERT ... SELECT per table in FK order, one transaction
  04_reconcile.sql      reconcile report, one row per check with a status (OK / MISMATCH / NOT RUN):
                        row counts; money totals raw legacy vs converted vs target, side by side; per-row
                        money compare to the penny (legacy key -> id map -> target row); totals per parent
                        (each foreign key resolved by lookup) and per month (first business date column)
  05_signoff.md         reconcile sign-off sheet for the data owner (results pasted in, decision recorded)
  README.md             run order, lineage summary, retired legacy columns

Conventions in mapping/<ctx>.json (see references/target-model-format.md):
  table_mappings[].sources[0] is the driving legacy table; extra sources need "joins":
     [{"table": "public.cust_contact", "type": "left", "on": "cust_contact.cust_id = cust_mst.cust_id"}]
  "filter": SQL on bare legacy table names; "legacy_key": [cols of driving table] (default its PK)
  "custom_select": full SELECT for unpivots/splits (returns custom_columns in order)
  column_mappings[].transform uses bare legacy table names (quote mixed-case ones)
  rule "lookup" + "lookup": "<schema.table>" resolves a legacy key through that table's id map

Reconcile settings: standards.json data_migration.reconcile (all optional; defaults shown)
  {"money_scale": 2, "per_row": true, "per_parent": true, "per_month": true, "examples": 5}

Usage: python generate_data_migration.py --context billing [--root <repo>]
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402
from generate_migration import qi  # noqa: E402

MONEY = {"amount", "price", "cost", "total", "balance", "fee", "charge", "tax", "vat"}
RECONCILE_DEFAULTS = {"money_scale": 2, "per_row": True, "per_parent": True, "per_month": True, "examples": 5}
NUMERIC_FAMILIES = ("int", "num", "float", "money")


def sql_str(x):
    return "'" + str(x).replace("'", "''") + "'"


def legacy_schema_alias(schema):
    return "legacy_src" if schema in ("public", "") else f"legacy_src_{schema}"


def self_grouped(src_g, tgt_g, src_v, tgt_v, src_from, where, target, S, NEX, label, show_g):
    """Totals per group (parent row or month), legacy side vs target side, rounded to S dp."""
    return (f"SELECT NULL::numeric, NULL::numeric, NULL::numeric, count(*) FILTER (WHERE a.s IS DISTINCT FROM b.t), "
            f"max(abs(coalesce(a.s, 0) - coalesce(b.t, 0))), "
            f"concat_ws('', count(*) || ' groups {label} at {S} dp', ': e.g. ' || nullif(array_to_string((array_agg({show_g} "
            f"ORDER BY abs(coalesce(a.s, 0) - coalesce(b.t, 0)) DESC) FILTER (WHERE a.s IS DISTINCT FROM b.t))[1:{NEX}], ', '), ''))"
            f" FROM (SELECT g, round(sum(v), {S}) AS s FROM (SELECT {src_g} AS g, coalesce(({src_v})::numeric, 0) AS v {src_from} {where}) x GROUP BY g) a"
            f" FULL JOIN (SELECT {tgt_g} AS g, round(sum(coalesce({tgt_v}, 0)), {S}) AS t FROM {target} GROUP BY 1) b USING (g)")


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
    rcfg = std["data_migration"].get("reconcile")
    if rcfg is None:
        print("NOTE: standards.json has no data_migration.reconcile section (project created before v1.3); "
              f"using defaults {RECONCILE_DEFAULTS}")
    rcfg = {**RECONCILE_DEFAULTS, **(rcfg or {})}
    S = int(rcfg["money_scale"])
    NEX = int(rcfg["examples"])

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
            REC.append(("row count", tn, f"SELECT NULL::numeric, NULL::numeric, (SELECT count(*) FROM {target})::numeric, NULL::bigint, NULL::numeric, "
                        f"'custom select: reconcile manually'::text", "CHECK MANUALLY"))
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
        REC.append(("row count", tn, f"SELECT NULL::numeric, (SELECT count(*) {base_from} {where})::numeric, "
                    f"(SELECT count(*) FROM {target})::numeric, NULL::bigint, NULL::numeric, "
                    + sql_str("filters applied: " + tm["filter"] if tm.get("filter") else "") + "::text", None))
        src_from = base_from + "".join(" " + x for x in lookups)
        fk_by_col = {fk["columns"][0]: fk for fk in t.get("foreign_keys", []) if len(fk["columns"]) == 1}
        month_col = next((c for c in t["columns"] if c["name"] not in audit and qi(c["name"]) in sel_cols
                          and d.type_family(c["type"]) in ("date", "ts", "tstz")), None)
        for c in t["columns"]:
            cm = cm_by[tn].get(c["name"])
            if not (cm and set(d.tokens(c["name"])) & MONEY and not set(d.tokens(c["name"])) & {"unit", "rate", "percent"}
                    and d.base_type(c["type"]) == "numeric" and cm.get("rule") in ("direct", "transform", "derived")):
                continue
            qc = qi(c["name"])
            conv = sel_exprs[sel_cols.index(qc)]
            # raw = the untouched legacy value: only when the column comes from ONE numeric legacy column
            raw, why_no_raw = None, "derived from several legacy columns: no single raw value"
            if len(cm.get("sources", [])) == 1:
                s_, t_, c_ = d.split_ref(cm["sources"][0])
                lt = cat["tables"].get(f"{s_}.{t_}")
                lc = next((x for x in (lt or {}).get("columns", []) if x["name"].lower() == c_), None)
                ltype = lc.get("data_type") or lc.get("type") or "" if lc else ""
                if lc and d.type_family(ltype) in NUMERIC_FAMILIES:
                    raw = f"{qi(lt['name'])}.{qi(lc['name'])}"
                elif lc:
                    why_no_raw = f"legacy {lt['name']}.{lc['name']} is {ltype}: compared after conversion only"
            lbl = f"{tn}.{c['name']}"
            REC.append(("money total", lbl,
                        f"SELECT {'(SELECT sum((' + raw + ')::numeric) ' + base_from + ' ' + where + ')' if raw else 'NULL::numeric'}, "
                        f"(SELECT sum(({conv})::numeric) {base_from} {where}), (SELECT sum({qc}) FROM {target}), NULL::bigint, NULL::numeric, "
                        f"{sql_str('raw legacy vs converted vs target' if raw else why_no_raw)}::text", None))
            cmp_src = raw or conv
            what = "raw legacy value" if raw else "converted value"
            if rcfg["per_row"]:
                if ipk and kx:
                    REC.append(("money per row", lbl,
                                f"SELECT NULL::numeric, NULL::numeric, NULL::numeric, count(*) FILTER (WHERE diff <> 0), max(abs(diff)), "
                                f"concat_ws('', count(*) || ' rows: {what} vs target at {S} dp', "
                                f"': e.g. legacy key ' || nullif(array_to_string((array_agg(k ORDER BY abs(diff) DESC, k) FILTER (WHERE diff <> 0))[1:{NEX}], ', '), ''))"
                                f" FROM (SELECT {kx} AS k, round(coalesce(({cmp_src})::numeric, 0), {S}) - round(coalesce(tgt.{qc}, 0), {S}) AS diff "
                                f"{base_from} LEFT JOIN {map_table(f'{sch}.{tn}')} AS m0 ON m0.legacy_key = {kx} "
                                f"LEFT JOIN {target} AS tgt ON tgt.{qi(ipk['name'])} = m0.new_id {where}) r", None))
                else:
                    REC.append(("money per row", lbl, "SELECT NULL::numeric, NULL::numeric, NULL::numeric, NULL::bigint, NULL::numeric, "
                                "'no legacy key / identity id map for this table: per-row compare not possible'::text", "NOT RUN"))
            if rcfg["per_parent"]:
                for col, fk in fk_by_col.items():
                    fcm = cm_by[tn].get(col)
                    if not (fcm and fcm.get("rule") == "lookup" and qi(col) in sel_cols):
                        continue
                    pmap = map_table(fcm["lookup"].lower())
                    REC.append(("money per parent", lbl, self_grouped(
                        f"{sel_exprs[sel_cols.index(qi(col))]}", qi(col), cmp_src, qc, src_from, where, target, S, NEX,
                        f"per {col} ({fcm['lookup'].lower()}), {what}", f"(SELECT legacy_key FROM {pmap} WHERE new_id = g)"), None))
            if rcfg["per_month"] and month_col:
                mexpr = sel_exprs[sel_cols.index(qi(month_col["name"]))]
                REC.append(("money per month", lbl, self_grouped(
                    f"date_trunc('month', {mexpr})::date", f"date_trunc('month', {qi(month_col['name'])})::date", cmp_src, qc,
                    src_from, where, target, S, NEX, f"per month of {month_col['name']}, {what}", "to_char(g, 'YYYY-MM')"), None))

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
    rblocks = []
    for i, (kind, lbl, q, fixed) in enumerate(REC, 1):
        ins = f"INSERT INTO reconcile_results (seq, check_type, target, source_raw, source_converted, target_value, diff_rows, max_abs_diff, note"
        if fixed:
            rblocks.append(f"{ins}, status) SELECT {i}, '{kind}', {sql_str(lbl)}, r.*, {sql_str(fixed)} FROM ({q}) r;")
            continue
        rblocks.append(f"""DO $rec$ BEGIN
  {ins}) SELECT {i}, '{kind}', {sql_str(lbl)}, r.* FROM ({q}) r;
EXCEPTION
  WHEN undefined_table THEN INSERT INTO reconcile_results (seq, check_type, target, status, note) VALUES ({i}, '{kind}', {sql_str(lbl)}, 'NOT RUN', SQLERRM || ' (run 01_id_maps.sql and 03_load.sql first)');
  WHEN OTHERS THEN INSERT INTO reconcile_results (seq, check_type, target, status, note) VALUES ({i}, '{kind}', {sql_str(lbl)}, 'FAILS', SQLERRM);
END $rec$;""")
    d.write_text(os.path.join(out, "04_reconcile.sql"), hdr +
                 f"-- Reconcile report: one row per check. Money is compared at {S} decimal places (standards.json data_migration.reconcile.money_scale).\n"
                 "-- money total   : raw legacy sum | converted sum (after transforms) | target sum, side by side. A conversion that\n"
                 "--                 changes the total (rounding, truncation, sign) shows as raw <> target even when converted = target.\n"
                 "-- money per row : every legacy row followed through its id map to its target row; diff_rows must be 0.\n"
                 "-- per parent / per month: totals per foreign-key parent and per calendar month; drift that cancels out overall shows here.\n"
                 "-- Every row that is not OK is explained (documented filter, ADR) or fixed, then the data owner signs 05_signoff.md.\n"
                 "CREATE TEMP TABLE IF NOT EXISTS reconcile_results (seq int, check_type text, target text, source_raw numeric, source_converted numeric,\n"
                 "  target_value numeric, diff_rows bigint, max_abs_diff numeric, note text, status text);\nTRUNCATE reconcile_results;\n\n"
                 + "\n".join(rblocks) +
                 f"""

UPDATE reconcile_results SET status = CASE
    WHEN check_type = 'row count' THEN CASE WHEN source_converted = target_value THEN 'OK' ELSE 'MISMATCH' END
    WHEN check_type = 'money total' THEN CASE
        WHEN round(source_converted, {S}) IS DISTINCT FROM round(target_value, {S}) THEN 'MISMATCH'
        WHEN source_raw IS NOT NULL AND round(source_raw, {S}) IS DISTINCT FROM round(target_value, {S}) THEN 'MISMATCH (conversion changes the total)'
        ELSE 'OK' END
    ELSE CASE WHEN diff_rows = 0 THEN 'OK' ELSE 'MISMATCH' END END
WHERE status IS NULL;

SELECT check_type, target, source_raw, source_converted, target_value, diff_rows, max_abs_diff, status, note
FROM reconcile_results ORDER BY seq;
""" if REC else hdr + "-- nothing to reconcile\n")
    signoff = [f"# Reconcile sign-off: {ctx} (model v{m['version']})", "",
               "Run `04_reconcile.sql` against the loaded target and paste its full output below. Every row must be `OK`, or explained",
               "(a documented filter, or an ADR in `.dbmod/decisions/`) and fixed or accepted. The data owner signs; the migration does",
               "not go live without this sheet.", "",
               f"- Money compared at: {S} decimal places (`standards.json` data_migration.reconcile.money_scale)",
               f"- Checks generated: {len(REC)} (" + ", ".join(f"{n} {k}" for k, n in Counter(r[0] for r in REC).items()) + ")",
               "- Run on: <date>  against: <environment / snapshot of legacy>", "",
               "## Output of 04_reconcile.sql", "", "```", "<paste here>", "```", "",
               "## Rows that are not OK", "", "| Check | Target | Status | Explanation / ADR | Fixed or accepted |", "|---|---|---|---|---|", "| | | | | |", "",
               "## Parallel run (if done)", "", "Period, scope and result of comparing legacy and new outputs side by side (see migration-guide.md).", "",
               "## Decision", "", "- [ ] Reconcile accepted. Data owner: <Name>, date: <date>",
               "- [ ] Recorded in `.dbmod/decisions/` (ADR-____), with any accepted differences listed", ""]
    d.write_text(os.path.join(out, "05_signoff.md"), "\n".join(signoff) + "\n")
    retired = mp.get("retired", [])
    readme = [f"# Data migration: {ctx} (model v{ver})", "",
              "Run against the NEW database, with legacy reachable via postgres_fdw:", "",
              "1. `00_prerequisites.sql`: fill in the server and user mapping first (credentials from your vault, never committed)",
              "2. `01_id_maps.sql`: pre-allocates new ids for every legacy key",
              "3. `02_precheck.sql`: must return no rows (it evaluates the exact load projection). Resolve every row first",
              "4. `03_load.sql`: one transaction, tables in foreign-key order",
              "5. `04_reconcile.sql`: reconcile report, every row must be `OK`: row counts; money totals raw legacy vs converted vs target;"
              f" every money value per row to {S} dp; totals per parent and per month. Explain or fix anything else",
              "6. `05_signoff.md`: the data owner reviews the reconcile output and signs it off before go-live", "",
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
