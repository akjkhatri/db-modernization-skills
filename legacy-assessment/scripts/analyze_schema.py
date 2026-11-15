#!/usr/bin/env python3
"""Rule-based assessment of the legacy catalog.

Produces in .dbmod/legacy/:
  relationships.json  declared FKs + inferred relationships (naming/type heuristics) with confidence
  findings.json       findings (rule ids LA-*), table health scores, PII inventory
  glossary.json       legacy abbreviation glossary (expansions + unknown tokens to confirm)

Human review status of inferred relationships and findings is preserved across re-runs.
Rule catalogue: ../references/assessment-rules.md

Usage: python analyze_schema.py [--root <repo>] [--wide-table 40] [--min-confidence 0.5]
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402

RULES_VERSION = "1.0"
SEV_WEIGHT = {"critical": 25, "high": 10, "medium": 4, "low": 1, "info": 0}

ABBREV = {
    "cust": "customer", "nm": "name", "dt": "date", "cd": "code", "amt": "amount", "qty": "quantity",
    "ord": "order", "inv": "invoice", "dtl": "detail", "hdr": "header", "mst": "master", "pymt": "payment",
    "pmt": "payment", "lmt": "limit", "flg": "flag", "usr": "user", "pwd": "password", "desc": "description",
    "descr": "description", "txt": "text", "addr": "address", "whse": "warehouse", "emp": "employee",
    "dept": "department", "crt": "created", "upd": "updated", "chg": "changed", "lvl": "level", "cnt": "count",
    "lkp": "lookup", "uom": "unit_of_measure", "cat": "category", "prod": "product", "no": "number",
    "num": "number", "nbr": "number", "acct": "account", "bal": "balance", "cn": "credit_note", "mgr": "manager",
    "yn": "yes_no", "ind": "indicator", "pct": "percent", "disc": "discount", "val": "value", "tbl": "table",
    "rec": "record", "seq": "sequence", "stat": "status", "sts": "status", "typ": "type", "grp": "group",
    "loc": "location", "org": "organisation", "co": "company", "tel": "telephone", "ph": "phone",
    "cnty": "county", "ctry": "country", "post": "postal", "ref": "reference", "src": "source", "tgt": "target",
    "trx": "transaction", "txn": "transaction", "dob": "date_of_birth", "nino": "national_insurance_number",
    "ni": "national_insurance", "vat": "vat", "param": "parameter", "cfg": "config", "lst": "last", "prv": "previous",
}
DATE_TOKENS = {"dt", "date", "dob", "time", "ts", "timestamp", "day", "dte", "datetime", "birth"}
MONEY_TOKENS = {"amt", "amount", "price", "cost", "total", "lmt", "limit", "salary", "balance", "bal", "vat",
                "tax", "fee", "charge", "value", "credit", "debit", "wage", "pay"}
BOOL_TOKENS = {"flag", "flg", "yn", "ind", "active", "paid", "deleted", "del", "enabled", "primary", "valid", "closed"}
CODE_TOKENS = {"status", "type", "cd", "code", "kind", "category", "class", "method", "role", "action", "reason"}
SELF_REF_HINTS = {"parent", "mgr", "manager", "supervisor", "master", "superior", "prev", "previous", "next", "orig", "original", "replaced"}
COPY_RE = re.compile(r"(_bak|_backup|_old|_copy|_tmp|_temp|_archive|_arch|_save|_\d{4}(_\d{2})?|_v\d+|_new)$|^(tmp_|temp_|bak_|old_|copy_)", re.I)

PII_RULES = [
    ("credential", r"(^|_)(pwd|password|passwd|pass_hash|pin|secret|token|api_key)($|_)"),
    ("financial", r"(^|_)(card_no|card_num|card_number|pan|cvv|iban|bank_acct|bank_account|account_no|acct_no|sort_cd|sort_code|swift|bic)($|_)"),
    ("special_category", r"(^|_)(ethnic|ethnicity|race|religion|faith|health|medical|disability|sexual|orientation|union|biometric|genetic|diagnosis)"),
    ("personal", r"(^|_)(nino|ni_number|ni_no|ssn|passport|driving_licen[cs]e|national_id|tax_id|utr)($|_)"),
    ("personal", r"(^|_)(email|e_mail|email_addr|phone\d*|mobile|tel|fax_no|fax)($|_)"),
    ("personal", r"(^|_)(first_nm|last_nm|first_name|last_name|surname|forename|full_name|contact_name|cust_nm|customer_name|emp_name)($|_)"),
    ("personal", r"(^|_)(dob|date_of_birth|birth_dt|birth_date)($|_)"),
    ("personal", r"(^|_)(addr\d*|address\d*|post_cd|postcode|zip|street|ship_addr\d*)($|_)"),
    ("personal", r"(^|_)(ip_addr|ip_address|last_login)($|_)"),
    ("financial", r"(^|_)(salary|wage|income)($|_)"),
]


def toks(name):
    return d.tokens(name)


def build_relationships(cat, old_rels, min_conf):
    tables = {k: t for k, t in cat["tables"].items() if t.get("kind", "table") == "table"}
    rels, seen = [], set()
    for k, t in cat["tables"].items():
        for fk in t["foreign_keys"]:
            seen.add((k, fk["columns"][0]))
            rels.append({"table": k, "columns": fk["columns"], "ref_table": fk["ref_table"],
                         "ref_columns": fk["ref_columns"], "source": "declared", "confidence": 1.0,
                         "reason": "declared foreign key", "status": "confirmed"})
    # index single-column PKs
    pk_by_col = defaultdict(list)
    pk_tables = {}
    for k, t in tables.items():
        if len(t["primary_key"]) == 1:
            pk_by_col[t["primary_key"][0]].append(k)
            pk_tables[k] = t["primary_key"][0]
    coltype = {(k, c["name"].lower()): c["data_type"] for k, t in cat["tables"].items() for c in t["columns"]}

    def compatible(a, b):
        fa, fb = d.type_family(a), d.type_family(b)
        return fa == fb or {fa, fb} <= {"int", "num"}

    def table_stem_tokens(k):
        return set(toks(k.split(".", 1)[1])) - {"mst", "master", "tbl", "t", "hdr", "header"}

    inferred = []
    for k, t in tables.items():
        single_pk = t["primary_key"][0] if len(t["primary_key"]) == 1 else None
        for c in t["columns"]:
            cn = c["name"].lower()
            if (k, cn) in seen or cn == single_pk:
                continue
            ct = toks(cn)
            cands = []
            # (a) exact PK column-name match in another table
            for rk in pk_by_col.get(cn, []):
                if rk != k:
                    cands.append((rk, 0.9, f"column name equals primary key of {rk}"))
            # (b) <x>_id / <x>_cd / <x>_no where table x / x_mst / xs exists
            if not cands and len(ct) >= 2 and ct[-1] in ("id", "cd", "code", "no", "num", "key"):
                stem = "_".join(ct[:-1])
                for rk in pk_tables:
                    if rk == k:
                        continue
                    rname = rk.split(".", 1)[1]
                    if rname in (stem, stem + "s", stem + "_mst", stem + "_master", "tbl_" + stem) or d.singular(rname) == stem:
                        cands.append((rk, 0.75, f"'{stem}' matches table {rname}"))
            # (c) self reference: parent_cat_id, mgr_id ...
            if single_pk and ct and ct[0] in SELF_REF_HINTS and (cn.endswith(single_pk) or ct[-1] in ("id", "cd", "no")):
                cands = [(k, 0.7 if cn.endswith(single_pk) else 0.6, "self-reference naming (" + ct[0] + ")")]
            for rk, conf, why in cands:
                rcol = pk_tables[rk]
                if not compatible(c["data_type"], coltype[(rk, rcol)]):
                    conf -= 0.4
                    why += "; type mismatch"
                if len(cands) > 1 and rk != k:
                    conf -= 0.2
                    why += f"; ambiguous ({len(cands)} candidates)"
                if conf >= min_conf:
                    inferred.append({"table": k, "columns": [cn], "ref_table": rk, "ref_columns": [rcol],
                                     "source": "inferred", "confidence": round(conf, 2), "reason": why, "status": "inferred"})
    old = {(r["table"], tuple(r["columns"]), r["ref_table"]): r for r in old_rels if r["source"] == "inferred"}
    for r in inferred:
        o = old.get((r["table"], tuple(r["columns"]), r["ref_table"]))
        if o and o.get("status") in ("confirmed", "rejected"):
            r["status"] = o["status"]
            if o.get("note"):
                r["note"] = o["note"]
    # human-added relationships (source manual) survive re-runs
    rels += inferred + [r for r in old_rels if r["source"] == "manual"]
    return rels


class Findings:
    def __init__(self):
        self.items = []

    def add(self, rule, sev, cat, obj, msg, evidence="", rec=""):
        self.items.append({"rule": rule, "severity": sev, "category": cat, "object": obj, "message": msg,
                           "evidence": evidence, "recommendation": rec, "status": "open"})


def analyze(cat, rels, wide):
    F = Findings()
    pii = []
    tables = cat["tables"]
    live_rels = [r for r in rels if r["status"] != "rejected"]
    touched = Counter()
    for r in live_rels:
        touched[r["table"]] += 1
        touched[r["ref_table"]] += 1
    rel_from = defaultdict(list)
    for r in live_rels:
        rel_from[r["table"]].append(r)
    colsets = {k: frozenset(c["name"].lower() for c in t["columns"]) for k, t in tables.items()}
    abbrev_use = Counter()
    unknown_tokens = Counter()
    undocumented = 0
    no_audit = []

    for k, t in tables.items():
        name = t["name"]
        is_view = t.get("kind", "table") != "table"
        cols = t["columns"]
        cnames = [c["name"].lower() for c in cols]
        if not t.get("comment"):
            undocumented += 1
        if is_view:
            F.add("LA-STR-012", "info", "structure", k, "View: review for embedded business/reporting logic",
                  rec="Re-implement as a read model/query in the new app or a reporting view; do not migrate blindly.")
        # ---- keys
        if not is_view and not t["primary_key"]:
            F.add("LA-KEY-001", "critical", "keys", k, "Table has no primary key",
                  f"{len(cols)} columns" + (f", rows≈{t['row_count']:,}" if t.get("row_count") is not None else ""),
                  "Identify the natural key / dedupe before migration; target table gets an identity PK.")
        inf = [r for r in rel_from[k] if r["source"] != "declared"]
        if inf:
            F.add("LA-KEY-002", "high", "keys", k,
                  f"{len(inf)} relationship(s) not enforced by the database",
                  "; ".join(f"{r['columns'][0]}->{r['ref_table']} ({r['confidence']})" for r in inf[:6]),
                  "Expect orphan rows; profile referential integrity before migration and enforce FKs in target.")
        if not is_view and len(t["primary_key"]) == 1:
            pkc = next((c for c in cols if c["name"].lower() == t["primary_key"][0]), None)
            if pkc and d.type_family(pkc["data_type"]) == "str" and not any(x in ("cd", "code") for x in toks(pkc["name"])) and (t.get("row_count") or 0) > 1000:
                F.add("LA-KEY-003", "low", "keys", f"{k}.{pkc['name']}", "Business/natural key used as primary key",
                      pkc["data_type"], "Use a surrogate identity PK in target and keep the business key as a UNIQUE column.")
        for c in cols:
            if (c.get("default") or "").lower().startswith("nextval("):
                F.add("LA-KEY-004", "low", "keys", f"{k}.{c['name']}", "Sequence/serial default", c["default"],
                      "Use GENERATED ALWAYS AS IDENTITY in target; reseed after data load.")
        if not is_view and touched[k] == 0 and not COPY_RE.search(name):
            F.add("LA-KEY-005", "medium", "keys", k, "Isolated table: no declared or inferred relationships",
                  rec="Confirm business use; candidate for retirement or a standalone context.")
        # ---- naming
        if not d.is_snake(name):
            F.add("LA-NAM-001", "medium", "naming", k, "Table name is not lower snake_case (needs quoting)", name,
                  "Use snake_case in target.")
        bad_cols = [c["name"] for c in cols if not d.is_snake(c["name"])]
        if bad_cols:
            F.add("LA-NAM-001", "medium", "naming", k, f"{len(bad_cols)} column name(s) not lower snake_case", ", ".join(bad_cols[:8]),
                  "Use snake_case in target; quote these identifiers in migration SQL.")
        if re.match(r"^(tbl_|t_|tb_)", name, re.I) or (re.match(r"^v_|^vw_", name, re.I) and not is_view):
            F.add("LA-NAM-002", "low", "naming", k, "Technical prefix in table name", name)
        res = [c["name"] for c in cols if c["name"].lower() in d.PG_RESERVED]
        if res:
            F.add("LA-NAM-004", "medium", "naming", k, "Reserved word used as column name", ", ".join(res))
        generic = [c["name"] for c in cols if re.match(r"^(col|field|attr|data|value|misc|extra|spare|filler|user_def)_?\d*$", c["name"].lower())]
        if generic:
            F.add("LA-NAM-005", "low", "naming", k, "Generic/meaningless column names", ", ".join(generic[:8]),
                  "Find out what is actually stored (profile the data, ask users) before mapping.")
        for tok in toks(name) + [x for c in cols for x in toks(c["name"])]:
            if tok in ABBREV:
                abbrev_use[tok] += 1
            elif len(tok) <= 4 and not tok.isdigit() and tok not in ("id", "is", "of", "to", "at", "by", "on", "the", "and", "log", "key", "name", "city", "type", "code", "date", "note", "line", "role", "path", "link", "last", "rate", "unit", "from", "item", "qty", "sku"):
                unknown_tokens[tok] += 1
        # ---- types
        for c in cols:
            ref = f"{k}.{c['name'].lower()}"
            ct = set(toks(c["name"]))
            fam = d.type_family(c["data_type"])
            bt = d.base_type(c["data_type"])
            cm = (c.get("comment") or "").lower()
            if fam == "str" and (ct & DATE_TOKENS or re.search(r"yyyy|dd/mm|mm/dd|yymmdd", cm)) and not ct & {"by", "usr", "user"}:
                F.add("LA-TYP-001", "high", "types", ref, "Date/time stored as text", f"{c['data_type']} {cm}".strip(),
                      "Convert to date/timestamptz; profile for invalid values (expect some).")
            if bt == "money":
                F.add("LA-TYP-002", "high", "types", ref, "PostgreSQL money type (locale-dependent)", bt, "Use numeric(19,4) + currency code.")
            elif fam == "float" and ct & MONEY_TOKENS:
                F.add("LA-TYP-002", "high", "types", ref, "Monetary value in floating point", bt, "Use numeric(19,4); reconcile rounding during migration.")
            if bt == "timestamp without time zone":
                F.add("LA-TYP-003", "medium", "types", ref, "timestamp without time zone", rec="Use timestamptz; decide the legacy source time zone (ADR).")
            if fam in ("str", "int") and bt != "boolean" and (ct & BOOL_TOKENS or c["name"].lower().startswith(("is_", "has_"))) \
                    and ((c.get("max_length") or 99) <= 3 or bt in ("smallint", "integer", "character")):
                F.add("LA-TYP-004", "medium", "types", ref, "Boolean encoded as code/number", c["data_type"],
                      "Use boolean (or timestamptz for 'deleted'/'closed' style flags); map Y/N/1/0 values explicitly.")
            if bt == "json":
                F.add("LA-TYP-006", "low", "types", ref, "json instead of jsonb", rec="Use jsonb, or better, model the attributes.")
            if bt == "character" and (c.get("max_length") or 1) > 3 and not ct & CODE_TOKENS:
                F.add("LA-TYP-005", "low", "types", ref, "char(n) padding for variable text", c["data_type"], "Use text/varchar; trim on migration.")
            is_fk = any(c["name"].lower() in r["columns"] for r in rel_from[k])
            if fam == "str" and ct & CODE_TOKENS and not ct & {"post", "postal", "zip", "sort", "swift", "bank"} \
                    and (c.get("max_length") or 99) <= 10 and not is_fk and not c.get("is_pk"):
                F.add("LA-TYP-007", "medium", "types", ref, "Coded value without a lookup/constraint", c["data_type"],
                      "Profile distinct values; model as lookup table or enum with CHECK.")
        # ---- structure
        groups = defaultdict(list)
        for c in cols:
            m = re.match(r"^(.*?)_?(\d+)$", c["name"].lower())
            if m and m.group(1) and not m.group(1).endswith(("v", "ipv")):
                groups[m.group(1)].append(c["name"])
        for stem, members in groups.items():
            if len(members) >= 2 and stem not in ("col", "column", "field", "fld"):
                F.add("LA-STR-001", "high", "structure", k, f"Repeating group '{stem}*' ({len(members)} columns)", ", ".join(members),
                      "Model as a child table or value object collection.")
        cs = set(cnames)
        if any(x in cs for x in ("attr_name", "attribute", "attr_key", "prop_name", "key", "name", "param_name")) and \
                any(x in cs for x in ("attr_value", "value", "attr_val", "prop_value", "val")) and len(cols) <= 6 and \
                not any(x in cs for x in ("param_key",)):
            F.add("LA-STR-002", "high", "structure", k, "Entity-Attribute-Value pattern", ", ".join(cnames),
                  "Profile attribute names; promote frequent attributes to columns, keep the long tail in jsonb.")
        rel_cols = {r["columns"][0] for r in rel_from[k]}
        for c in cnames:
            if c.endswith("_type"):
                stem = c[:-5]
                if f"{stem}_id" in cs and f"{stem}_id" not in t["primary_key"] and f"{stem}_id" not in rel_cols:
                    F.add("LA-STR-003", "high", "structure", k, f"Polymorphic association ({stem}_type + {stem}_id)",
                          rec="Replace with explicit FKs per owner type or one link table per owner.")
        if len(cols) <= 5 and sum(1 for c in cnames if re.search(r"(code|cd)_?(type|grp|group|cat)|^(type|category)$", c)) and \
                sum(1 for c in cnames if re.search(r"(code|cd)_?(val|value)?$", c)) and any("desc" in c for c in cnames):
            F.add("LA-STR-004", "medium", "structure", k, "One-true-lookup-table (many code lists in one table)",
                  rec="Split into one lookup table/enum per code type, owned by the context that uses it.")
        if len(cols) > wide:
            F.add("LA-STR-005", "medium", "structure", k, f"Wide table ({len(cols)} columns)", rec="Likely several aggregates/value objects; split by cohesion.")
        all_rel_cols = {r["columns"][0] for r in live_rels if r["table"] == k} | {r["ref_columns"][0] for r in live_rels if r["ref_table"] == k and r["ref_columns"]}
        for r in rel_from[k]:
            parent = tables.get(r["ref_table"])
            if not parent or r["ref_table"] == k or COPY_RE.search(name):
                continue
            parent_rel_cols = {x["columns"][0] for x in live_rels if x["table"] == r["ref_table"]}
            pcols = {c["name"].lower() for c in parent["columns"]} - set(parent["primary_key"]) - parent_rel_cols
            dup = sorted((cs & pcols) - set(r["columns"]) - all_rel_cols - {"status", "crt_dt", "upd_dt", "created", "upd_usr", "description", "amt", "del_flg"})
            if dup:
                F.add("LA-STR-006", "medium", "structure", k, f"Attributes copied from {r['ref_table']}", ", ".join(dup),
                      "Decide per column: snapshot-at-time (keep, rename e.g. customer_name_at_order) or derived (drop, join).")
        if COPY_RE.search(name):
            twin = [o for o in tables if o != k and colsets[k] and colsets[k] <= colsets[o]]
            F.add("LA-STR-007", "high", "structure", k, "Backup/archive/temporary table" + (f" (columns subset of {twin[0]})" if twin else ""),
                  rec="Confirm with data owner; usually retire, or migrate as history of the main table.")
        elif not is_view:
            twin = [o for o in tables if o != k and len(colsets[k]) >= 3 and colsets[k] == colsets[o]]
            if twin:
                F.add("LA-STR-007", "high", "structure", k, f"Same column set as {twin[0]}", rec="Duplicate structure: merge or retire.")
        flags = [c for c in cnames if re.match(r"^(del_flg|deleted|is_deleted|del_ind|delete_flag|del_yn|archived)$", c)]
        if flags:
            F.add("LA-STR-008", "medium", "structure", k, "Soft-delete flag", ", ".join(flags),
                  "Decide per entity: migrate only live rows, or deleted_at timestamptz; ADR on retention.")
        if not is_view and not any(re.search(r"(crt|creat|insert|ins)_?(dt|date|ts|at|on|time)?$|^created$", c) for c in cnames):
            no_audit.append(k)
        # reference-looking columns that resolve to nothing
        if not is_view:
            for c in cols:
                ct = toks(c["name"])
                cn = c["name"].lower()
                if len(ct) >= 2 and ct[-1] in ("id", "no", "num", "nbr") and not set(ct) & {"fax", "phone", "tel", "card", "acct", "account", "sort", "post", "ni", "passport", "vat"} \
                        and cn not in rel_cols and cn not in t["primary_key"] \
                        and not cn.endswith("_type") and not (c.get("is_pk")) and f"{'_'.join(ct[:-1])}_type" not in cs:
                    F.add("LA-KEY-006", "medium", "keys", f"{k}.{cn}", "Looks like a reference but no target table identified",
                          c["data_type"], "Ask SMEs what it points to (another system? a renamed table?); record in relationships.json as manual.")
        if {"old_val", "new_val"} <= cs or ({"tbl_name", "table_name"} & cs and ({"old_value", "new_value", "old_val", "new_val"} & cs)):
            F.add("LA-STR-010", "medium", "structure", k, "Generic audit log table (stringly-typed old/new values)",
                  rec="Do not migrate into the domain schema; archive, and use proper auditing/CDC in the new app.")
        if any(c in cs for c in ("param_key", "config_key", "setting_name", "param_name")) and len(cols) <= 5:
            F.add("LA-STR-011", "low", "structure", k, "Key-value configuration table",
                  rec="Classify each parameter: app config, feature flag, or real reference data.")
        if not is_view and len(cols) > 5:
            nullable_share = sum(1 for c in cols if c["nullable"]) / len(cols)
            if nullable_share > 0.8:
                F.add("LA-STR-013", "medium", "structure", k, f"{round(nullable_share * 100)}% of columns nullable",
                      rec="Profile actual null rates; make mandatory attributes NOT NULL in target.")
        nullable_fks = [r["columns"][0] for r in rel_from[k] if any(c["name"].lower() == r["columns"][0] and c["nullable"] for c in cols)]
        if nullable_fks and not is_view:
            F.add("LA-STR-013", "low", "structure", k, "Nullable relationship columns", ", ".join(nullable_fks),
                  "Confirm optionality of each relationship.")
        # ---- PII / security
        for c in cols:
            ref = f"{k}.{c['name'].lower()}"
            cn = c["name"].lower()
            for cls, rx in PII_RULES:
                if re.search(rx, cn):
                    pii.append({"ref": ref, "class": cls, "reason": f"name matches {cls} pattern"})
                    if cls == "credential":
                        F.add("LA-SEC-001", "critical", "security", ref, "Credential stored in application table", c["data_type"],
                              "Never migrate; use the identity provider. If kept, only a modern salted hash (argon2/bcrypt).")
                    elif cls == "financial" and cn not in ("salary", "wage", "income"):
                        F.add("LA-SEC-002", "high", "security", ref, "Payment card / bank data", c["data_type"],
                              "PCI-DSS scope: tokenise via payment provider; do not migrate raw PAN.")
                    elif cls == "special_category":
                        F.add("LA-SEC-003", "high", "security", ref, "GDPR special category data (Art. 9)", rec="Needs lawful basis + DPIA; isolate, encrypt, restrict.")
                    elif re.search(r"nino|ni_number|ni_no|ssn|passport|licen|national_id|tax_id|utr", cn):
                        F.add("LA-SEC-004", "medium", "security", ref, "National identifier", rec="Restrict access; consider column encryption.")
                    break
    if no_audit:
        F.add("LA-STR-009", "low", "structure", "(catalog)", f"{len(no_audit)} tables have no creation timestamp/audit columns",
              ", ".join(no_audit[:20]) + (" ..." if len(no_audit) > 20 else ""),
              "Target standard audit columns; backfill created_at from the best legacy proxy (e.g. first status-history row).")
    if undocumented:
        F.add("LA-DOC-001", "low", "documentation", "(catalog)", f"{undocumented} of {len(tables)} tables have no description",
              rec="Capture meaning from SMEs during domain modernization; record in design.md.")
    return F.items, pii, abbrev_use, unknown_tokens


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--wide-table", type=int, default=40)
    ap.add_argument("--min-confidence", type=float, default=0.5)
    a = ap.parse_args()
    root = d.find_root(a.root)
    cat = d.load_catalog(root)
    rpath = d.mem(root, "legacy", "relationships.json")
    old_rels = d.load_json(rpath, default={"relationships": []})["relationships"]
    rels = build_relationships(cat, old_rels, a.min_confidence)
    d.save_json(rpath, {"generated_at": d.now_iso(), "relationships": rels})

    items, pii, abbrev_use, unknown = analyze(cat, rels, a.wide_table)
    fpath = d.mem(root, "legacy", "findings.json")
    old = d.load_json(fpath, default={"findings": []})["findings"]
    # stable identity: rule + object + occurrence (messages carry counts and may change between runs)
    def keyed(fs):
        seen, out = {}, {}
        for f in fs:
            k = (f["rule"], f["object"])
            seen[k] = seen.get(k, 0) + 1
            out[k + (seen[k],)] = f
        return out
    old_by = keyed([f for f in old if f.get("status") != "not-detected"])
    nxt = max([int(f["id"].split("-")[1]) for f in old] + [0]) + 1
    new_keyed = keyed(items)
    for key, f in new_keyed.items():
        o = old_by.get(key)
        if o:
            f["id"] = o["id"]
            f["status"] = o.get("status", "open")
            if o.get("note"):
                f["note"] = o["note"]
        else:
            f["id"] = f"F-{nxt:04d}"
            nxt += 1
    # findings no longer detected (e.g. a reviewer added the missing relationship) keep their id for traceability
    for key, o in old_by.items():
        if key not in new_keyed:
            items.append(dict(o, status="not-detected", note=(o.get("note", "") + f" no longer detected {d.now_iso()[:10]}").strip()))
    items.extend(f for f in old if f.get("status") == "not-detected" and f["id"] not in {x["id"] for x in items})
    items.sort(key=lambda f: (list(SEV_WEIGHT).index(f["severity"]), f["object"]))
    scores = {}
    for k in cat["tables"]:
        pen = sum(SEV_WEIGHT[f["severity"]] for f in items if f["status"] != "not-detected" and (f["object"] == k or f["object"].startswith(k + ".")))
        scores[k] = max(0, 100 - pen)
    live = [f for f in items if f["status"] != "not-detected"]
    sev = Counter(f["severity"] for f in live)
    summary = {"tables": len(cat["tables"]), "columns": sum(len(t["columns"]) for t in cat["tables"].values()),
               "declared_relationships": sum(1 for r in rels if r["source"] == "declared"),
               "inferred_relationships": sum(1 for r in rels if r["source"] == "inferred"),
               "findings_by_severity": {k: sev.get(k, 0) for k in SEV_WEIGHT},
               "findings_by_rule": dict(Counter(f["rule"] for f in live).most_common()),
               "pii_columns": len(pii), "pii_by_class": dict(Counter(p["class"] for p in pii)),
               "average_table_score": round(sum(scores.values()) / max(1, len(scores)), 1)}
    d.save_json(fpath, {"generated_at": d.now_iso(), "rules_version": RULES_VERSION, "summary": summary,
                        "findings": items, "table_scores": dict(sorted(scores.items(), key=lambda x: x[1])),
                        "pii_inventory": pii})
    gpath = d.mem(root, "legacy", "glossary.json")
    oldg = d.load_json(gpath, default={"terms": {}})["terms"]
    terms = {}
    for tok, n in abbrev_use.most_common():
        terms[tok] = {"expansion": oldg.get(tok, {}).get("expansion", ABBREV[tok]), "uses": n,
                      "status": oldg.get(tok, {}).get("status", "suggested")}
    for tok, n in unknown.most_common():
        if n >= 2:
            terms[tok] = {"expansion": oldg.get(tok, {}).get("expansion"), "uses": n,
                          "status": oldg.get(tok, {}).get("status", "unknown")}
    d.save_json(gpath, {"generated_at": d.now_iso(), "terms": terms})
    man = d.load_manifest(root)
    man["legacy"]["assessed_at"] = d.now_iso()
    if man["phase"] in ("initialised", "ingested"):
        man["phase"] = "assessed"
    d.save_manifest(root, man)
    d.journal(root, "legacy-assessment", "analyze",
              f"{len(live)} findings ({', '.join(f'{k} {v}' for k, v in summary['findings_by_severity'].items() if v)}), "
              f"{summary['inferred_relationships']} inferred relationships, {len(pii)} PII columns")
    print(f"findings: {summary['findings_by_severity']}")
    print(f"relationships: {summary['declared_relationships']} declared, {summary['inferred_relationships']} inferred")
    print(f"PII columns: {summary['pii_by_class']}")
    print(f"avg table health: {summary['average_table_score']}/100 · glossary terms: {len(terms)} "
          f"({sum(1 for t in terms.values() if t['status'] == 'unknown')} unknown)")
    print("top rules: " + ", ".join(f"{k}×{v}" for k, v in list(summary["findings_by_rule"].items())[:8]))


if __name__ == "__main__":
    main()
