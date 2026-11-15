#!/usr/bin/env python3
"""Validate a target context model (model.json) against the format and .dbmod/standards.json.
Errors block acceptance; warnings need a reason (put it in design.md or an ADR).

Usage: python model_lint.py --context billing [--root <repo>] [--strict]
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402

KNOWN_TYPES = re.compile(r"^(smallint|integer|bigint|numeric(\(\d+,\s*\d+\))?|text|character varying\(\d+\)|varchar\(\d+\)|"
                         r"character\(\d+\)|char\(\d+\)|boolean|date|timestamp with time zone|timestamptz|time with time zone|"
                         r"interval|uuid|jsonb|bytea|citext|inet|cidr|tsvector|int4range|int8range|daterange|tstzrange|"
                         r"numrange|money|real|double precision|json|timestamp|timestamp without time zone|serial|bigserial|"
                         r"time|time without time zone)(\[\])?$", re.I)
MONEY = {"amount", "price", "cost", "total", "balance", "fee", "charge", "tax", "vat", "salary", "limit", "discount_amount"}
PII_HINT = re.compile(r"(email|phone|mobile|first_name|last_name|surname|full_name|date_of_birth|birth|address|postcode|"
                      r"post_code|national_insurance|passport|ip_address|salary|iban|account_number|sort_code|ethnic|health|religion)")


def lint(root, ctx):
    m = d.load_model(root, ctx)
    std = d.load_standards(root)
    gl = d.load_json(d.mem(root, "legacy", "glossary.json"), default={"terms": {}})["terms"]
    allowed_abbr = set(std["naming"].get("abbreviations_allowed", []))
    forbidden_prefix = tuple(std["naming"].get("forbidden_prefixes", []))
    maxlen = std["naming"].get("max_identifier_length", 63)
    E, W = [], []

    def name_ok(kind, n, where):
        if not d.is_snake(n):
            E.append(f"{where}: {kind} name '{n}' is not snake_case")
        if len(n) > maxlen:
            E.append(f"{where}: {kind} name '{n}' longer than {maxlen}")
        if n in d.PG_RESERVED:
            E.append(f"{where}: {kind} name '{n}' is a reserved word")
        if n.startswith(forbidden_prefix):
            E.append(f"{where}: {kind} name '{n}' uses a forbidden prefix")
        for tok in d.tokens(n):
            if tok in gl and tok not in allowed_abbr and gl[tok].get("expansion") and gl[tok]["expansion"] != tok:
                W.append(f"{where}: '{n}' uses legacy abbreviation '{tok}' (glossary: {gl[tok]['expansion']})")

    for k in ("context", "schema", "version", "status", "tables"):
        if k not in m:
            E.append(f"model missing '{k}'")
    if m.get("context") != ctx:
        E.append(f"model.context is {m.get('context')}, expected {ctx}")
    if not re.match(std["naming"].get("schema_name_pattern", r"^[a-z][a-z0-9_]*$"), m.get("schema", "")):
        E.append(f"schema name '{m.get('schema')}' does not match standards")
    if m.get("schema") in d.PG_RESERVED:
        E.append(f"schema name '{m.get('schema')}' is a reserved word")
    if not m.get("requirements"):
        W.append("model cites no requirements (every change should trace to a REQ)")
    if not m.get("legacy_domains"):
        W.append("model has no legacy_domains (is this context really green-field?)")
    enums = {e["name"]: e for e in m.get("enums", [])}
    for e in enums.values():
        name_ok("enum", e["name"], f"enum {e['name']}")
        if not e.get("values"):
            E.append(f"enum {e['name']} has no values")

    tables = {}
    for t in m.get("tables", []):
        if t["name"] in tables:
            E.append(f"duplicate table {t['name']}")
        tables[t["name"]] = t
    aud = std.get("audit_columns", {})
    lock = std.get("optimistic_locking", {})
    ts_std = std["types"]["timestamps"]
    forbidden = {"money", "real", "json", "serial", "bigserial", "timestamp without time zone", "timestamp"}
    for t in tables.values():
        w = f"{t['name']}"
        name_ok("table", t["name"], w)
        if std["naming"].get("table_names") == "singular" and d.singular(t["name"].split("_")[-1]) != t["name"].split("_")[-1]:
            W.append(f"{w}: table name should be singular")
        if not t.get("description"):
            (E if std["documentation"].get("table_comment_required") else W).append(f"{w}: missing description")
        if t.get("role") not in ("root", "entity", "value", "lookup", "link", "history"):
            E.append(f"{w}: role must be root|entity|value|lookup|link|history")
        cols = {}
        for c in t.get("columns", []):
            if c["name"] in cols:
                E.append(f"{w}: duplicate column {c['name']}")
            cols[c["name"]] = c
            cw = f"{w}.{c['name']}"
            name_ok("column", c["name"], cw)
            ty = c.get("type", "").strip().lower()
            if not ty:
                E.append(f"{cw}: no type")
                continue
            if not KNOWN_TYPES.match(ty) and ty not in enums and ty.split(".")[-1] not in enums:
                W.append(f"{cw}: unrecognised type '{ty}'")
            if d.base_type(ty) in forbidden or ty in forbidden:
                E.append(f"{cw}: type '{ty}' is forbidden by standards (timestamps: {ts_std}, money: {std['types']['money']})")
            if ty.startswith(("character(", "char(")) and int(re.findall(r"\d+", ty)[0]) > 3:
                W.append(f"{cw}: char(n) > 3; use text/varchar unless fixed width")
            if ty == "double precision" and set(d.tokens(c["name"])) & MONEY:
                E.append(f"{cw}: monetary value in floating point")
            if set(d.tokens(c["name"])) & MONEY and d.base_type(ty) == "numeric" and ty.replace(" ", "") != std["types"]["money"].replace(" ", "") \
                    and "percent" not in c["name"] and "rate" not in c["name"]:
                W.append(f"{cw}: monetary column type {ty}, standard is {std['types']['money']}")
            if std["gdpr"].get("require_pii_classification"):
                pii = c.get("pii")
                if pii is None:
                    E.append(f"{cw}: missing pii classification ({'/'.join(std['gdpr']['classes'])})")
                elif pii not in std["gdpr"]["classes"]:
                    E.append(f"{cw}: pii '{pii}' not one of {std['gdpr']['classes']}")
                elif pii == "none" and PII_HINT.search(c["name"]):
                    W.append(f"{cw}: name suggests personal data but pii=none")
                if pii in std["gdpr"].get("retention_note_required_for", []) and not (t.get("retention") or c.get("retention")):
                    W.append(f"{cw}: {pii} data but no retention rule on table or column")
            if c.get("identity") and d.base_type(ty) not in ("bigint", "integer", "smallint"):
                E.append(f"{cw}: identity on non-integer type")
            if d.base_type(ty) == "boolean" and c.get("nullable", True):
                W.append(f"{cw}: nullable boolean (three-valued); make NOT NULL with a default unless 'unknown' is meaningful")
        pk = t.get("primary_key") or []
        if not pk:
            E.append(f"{w}: no primary key")
        for col in pk:
            if col not in cols:
                E.append(f"{w}: primary key column {col} not defined")
            elif cols[col].get("nullable"):
                E.append(f"{w}: primary key column {col} is nullable")
        if len(pk) == 1 and pk[0] in cols and t.get("role") not in ("lookup", "link", "value"):
            exp = std["naming"]["primary_key_column"].format(table=t["name"])
            if pk[0] != exp:
                W.append(f"{w}: PK column '{pk[0]}' (standard: {exp})")
            if d.base_type(cols[pk[0]]["type"]) != std["keys"]["primary_key_type"] or not cols[pk[0]].get("identity"):
                W.append(f"{w}: PK should be {std['keys']['primary_key_type']} identity")
        if aud.get("required") and t.get("role") not in aud.get("exempt_roles", []):
            missing = [c["name"] for c in aud["columns"] if c["name"] not in cols]
            if missing:
                E.append(f"{w}: missing audit columns {missing} (run context_tool.py add-audit)")
        if t.get("role") in lock.get("required_for_roles", []) and lock["column"]["name"] not in cols:
            W.append(f"{w}: aggregate root without {lock['column']['name']} (optimistic locking)")
        for u in t.get("unique", []):
            for col in u["columns"]:
                if col not in cols:
                    E.append(f"{w}: unique column {col} not defined")
        for ix in t.get("indexes", []):
            for col in ix["columns"]:
                if col not in cols and "(" not in col:
                    E.append(f"{w}: index column {col} not defined")
        for ck in t.get("checks", []):
            if not ck.get("name") or not ck.get("expression"):
                E.append(f"{w}: check needs name and expression")
        for fk in t.get("foreign_keys", []):
            ref = fk.get("references", {})
            rt = ref.get("table", "")
            if "." in rt and not std["keys"].get("cross_schema_foreign_keys_allowed"):
                E.append(f"{w}: FK to another schema ({rt}); use an external_ref (id only) instead")
                continue
            if rt not in tables:
                E.append(f"{w}: FK references unknown table {rt}")
                continue
            rcols = {c["name"]: c for c in tables[rt]["columns"]}
            for lc, rc in zip(fk["columns"], ref.get("columns", [])):
                if lc not in cols:
                    E.append(f"{w}: FK column {lc} not defined")
                elif rc not in rcols:
                    E.append(f"{w}: FK target column {rt}.{rc} not defined")
                elif d.base_type(cols[lc]["type"]) != d.base_type(rcols[rc]["type"]):
                    E.append(f"{w}: FK {lc} type {cols[lc]['type']} != {rt}.{rc} {rcols[rc]['type']}")
            if len(fk["columns"]) != len(ref.get("columns", [])):
                E.append(f"{w}: FK column count mismatch")
            if fk.get("on_delete", "restrict") not in ("restrict", "cascade", "set null", "no action"):
                E.append(f"{w}: on_delete must be restrict|cascade|set null|no action")
            if fk.get("on_delete") == "set null" and any(cols.get(c, {}).get("nullable") is False for c in fk["columns"]):
                E.append(f"{w}: on_delete set null on NOT NULL column")
    contexts = set(d.list_contexts(root))
    for r in m.get("external_refs", []):
        try:
            tn, cn = r["column"].split(".")
        except ValueError:
            E.append(f"external_ref column '{r.get('column')}' must be table.column")
            continue
        if tn not in tables or cn not in {c["name"] for c in tables[tn]["columns"]}:
            E.append(f"external_ref {r['column']} not defined in model")
        if r["context"] == ctx:
            E.append(f"external_ref {r['column']} points at own context; use a foreign key")
        elif r["context"] not in contexts:
            W.append(f"external_ref {r['column']} -> context '{r['context']}' does not exist yet")
        else:
            other = d.load_model(root, r["context"])
            if r["entity"] not in {t["name"] for t in other["tables"]}:
                W.append(f"external_ref {r['column']} -> {r['context']}.{r['entity']} not in that context's model")
    if not tables:
        E.append("model has no tables")
    return m, E, W


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root")
    ap.add_argument("--context", required=True)
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    root = d.find_root(a.root)
    m, E, W = lint(root, a.context)
    for e in E:
        print("ERROR  " + e)
    for w in W:
        print("WARN   " + w)
    print(f"\n{a.context} v{m.get('version')}: {len(E)} error(s), {len(W)} warning(s)")
    sys.exit(1 if E or (a.strict and W) else 0)


if __name__ == "__main__":
    main()
