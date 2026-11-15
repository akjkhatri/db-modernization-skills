#!/usr/bin/env python3
"""Ingest a legacy PostgreSQL table catalog (HTML page or CSV, any reasonable layout) into
the canonical .dbmod/legacy/catalog.json.

Layouts understood automatically:
  * one row per column with a table-name column   (information_schema-style CSV / HTML)
  * one HTML table per legacy table, the table name in the preceding heading/caption
    (SchemaSpy / DBeaver / dbdocs / Confluence-export style)
  * a table-summary table (table + description/row count) -> table comments & row counts
  * a foreign-key list (table, column, referenced table, referenced column)

Always run --inspect first on an unfamiliar export; if header detection is wrong, pass a
mapping file:  {"headers": {"Col Name": "column", "Fld Type": "type"}, "default_schema": "public",
                "skip_tables": [3, 4]}
Canonical fields: schema table column ordinal type length precision scale nullable not_null
default pk unique key fk_ref ref_table ref_column comment table_comment row_count kind

Usage:
  python ingest_catalog.py --root <repo> --input catalog.html [--input fks.csv] --inspect
  python ingest_catalog.py --root <repo> --input catalog.html [--mapping map.json]
  python ingest_catalog.py --root <repo> --url https://intranet/db/catalog.html
"""
import argparse
import csv
import hashlib
import io
import os
import re
import shutil
import sys
import urllib.request
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402

# ----------------------------------------------------------------------------- header aliases
ALIASES = {
    "schema": ["schema", "table_schema", "schema_name", "owner", "namespace", "nspname"],
    "table": ["table", "table_name", "tablename", "entity", "relation", "relname", "object", "object_name", "tbl"],
    "column": ["column", "column_name", "columnname", "field", "field_name", "attribute", "attname", "col", "col_name", "name"],
    "ordinal": ["ordinal", "ordinal_position", "position", "seq", "#", "no", "attnum", "column_id"],
    "type": ["type", "data_type", "datatype", "column_type", "udt_name", "pg_type", "format_type", "data type"],
    "length": ["length", "character_maximum_length", "max_length", "size", "char_length"],
    "precision": ["precision", "numeric_precision"],
    "scale": ["scale", "numeric_scale"],
    "nullable": ["nullable", "is_nullable", "null", "nulls", "null?", "allow_null", "allows_null", "optional"],
    "not_null": ["not_null", "not null", "mandatory", "required", "attnotnull"],
    "default": ["default", "column_default", "default_value", "defaults"],
    "pk": ["pk", "primary_key", "is_pk", "primary", "is_primary_key"],
    "unique": ["unique", "uq", "is_unique"],
    "key": ["key", "keys", "constraint", "constraints", "constraint_type", "column_key", "key_type", "index"],
    "fk_ref": ["references", "reference", "fk_ref", "foreign_key", "fk", "parent", "refers_to", "fk_reference", "foreign key"],
    "ref_table": ["ref_table", "foreign_table", "foreign_table_name", "referenced_table", "references_table", "parent_table", "ref_table_name"],
    "ref_column": ["ref_column", "foreign_column", "foreign_column_name", "referenced_column", "parent_column", "ref_column_name"],
    "ref_schema": ["ref_schema", "foreign_table_schema", "referenced_schema", "foreign_schema"],
    "comment": ["comment", "comments", "description", "desc", "remarks", "notes", "col_description", "column_comment", "meaning"],
    "table_comment": ["table_comment", "table_description", "obj_description"],
    "row_count": ["rows", "row_count", "rowcount", "reltuples", "records", "n_live_tup", "num_rows", "row count"],
    "kind": ["kind", "table_type", "object_type", "relkind"],
}
LOOKUP = {}
for canon, names in ALIASES.items():
    for n in names:
        LOOKUP[re.sub(r"[^a-z0-9#?]+", "_", n.lower()).strip("_")] = canon


def canon_header(h):
    k = re.sub(r"[^a-z0-9#?]+", "_", (h or "").lower()).strip("_")
    if k in LOOKUP:
        return LOOKUP[k]
    k2 = k.rstrip("?")
    return LOOKUP.get(k2)


# ----------------------------------------------------------------------------- HTML parsing
class TableHTML(HTMLParser):
    """Collect every <table> with its rows and the nearest preceding heading/caption text."""
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6", "caption", "dt", "summary", "legend"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.stack = [], []
        self.last_heading = ""
        self.last_para = ""
        self._text_target = None
        self._buf = []
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.stack.append({"heading": self.last_heading, "para": self.last_para, "rows": [],
                               "id": attrs.get("id") or attrs.get("data-table") or ""})
        elif tag == "tr" and self.stack:
            self.stack[-1]["rows"].append([])
        elif tag in ("td", "th") and self.stack:
            self._cell = []
            self._cell_header = tag == "th"
        elif tag in self.HEADINGS or (tag == "p" and not self.stack):
            self._text_target = tag
            self._buf = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self.stack:
            txt = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            if not self.stack[-1]["rows"]:
                self.stack[-1]["rows"].append([])
            self.stack[-1]["rows"][-1].append(txt)
            self._cell = None
        elif tag == "table" and self.stack:
            t = self.stack.pop()
            t["rows"] = [r for r in t["rows"] if any(c for c in r)]
            self.tables.append(t)
        elif tag == self._text_target:
            txt = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if tag == "p":
                self.last_para = txt
            elif txt:
                if tag == "caption" and self.stack:
                    self.stack[-1]["heading"] = txt
                else:
                    self.last_heading = txt
                    self.last_para = ""
            self._text_target = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif self._text_target:
            self._buf.append(data)


def read_source(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw, raw.decode(enc)
        except UnicodeDecodeError:
            continue


def grids_from_file(path):
    """Return list of {'heading','para','rows','origin'} grids from an HTML or CSV file."""
    raw, text = read_source(path)
    low = text[:2000].lower()
    if path.lower().endswith((".html", ".htm", ".xhtml")) or "<table" in low or "<html" in low:
        p = TableHTML()
        p.feed(text)
        return [dict(t, origin=f"{os.path.basename(path)}#table{i}") for i, t in enumerate(p.tables)]
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    return [{"heading": "", "para": "", "rows": rows, "id": "", "origin": os.path.basename(path)}]


# ----------------------------------------------------------------------------- grid classification
def find_header(rows, overrides):
    """Find the header row (first row with >=2 recognised headers within the first 5 rows)."""
    best = (None, {}, 0)
    for i, r in enumerate(rows[:5]):
        m = {}
        for j, h in enumerate(r):
            c = overrides.get(h.strip()) or overrides.get(h.strip().lower()) or canon_header(h)
            if c and c not in m.values():
                m[j] = c
        if len(m) > best[2]:
            best = (i, m, len(m))
    return best[0], best[1]


def classify(fields):
    f = set(fields.values())
    if "column" in f and ("type" in f or "nullable" in f or "not_null" in f):
        return "columns"
    if "ref_table" in f and "table" in f and "column" in f:
        return "fks"
    if "table" in f and ("row_count" in f or "comment" in f or "table_comment" in f or "kind" in f) and "column" not in f:
        return "tables"
    if "column" in f and "table" in f:
        return "columns"
    return "unknown"


HEADING_STRIP = re.compile(r"^(table|view|entity|relation|object)\s*[:\-–]?\s*", re.I)


def table_from_heading(h):
    h = HEADING_STRIP.sub("", (h or "").strip())
    h = re.split(r"\s+[\-–—(]|\s{2,}", h)[0].strip()
    m = re.match(r'^"?([A-Za-z0-9_$]+)"?(?:\."?([A-Za-z0-9_$]+)"?)?$', h)
    if not m:
        return None, None
    return (m.group(1), m.group(2)) if m.group(2) else (None, m.group(1))


# ----------------------------------------------------------------------------- value parsing
TRUE = {"y", "yes", "true", "t", "1", "x", "✓", "✔", "on", "pk", "nullable", "null"}
FALSE = {"n", "no", "false", "f", "0", "", "-", "not null", "notnull", "off"}


def truthy(v):
    v = (v or "").strip().lower()
    if v in TRUE:
        return True
    if v in FALSE:
        return False
    return None


TYPE_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_ ]*?)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+))?\s*\))?\s*(\[\])?\s*$")


def parse_type(t, length=None, precision=None, scale=None):
    t = (t or "").strip()
    m = TYPE_RE.match(t)
    out = {"data_type": t.lower() or "unknown", "max_length": None, "precision": None, "scale": None}
    if m:
        base = m.group(1).lower().strip()
        a1, a2 = m.group(2), m.group(3)
        bt = d.base_type(base)
        if a1:
            if d.type_family(bt) == "num" or bt in ("numeric",):
                out["precision"], out["scale"] = int(a1), int(a2 or 0)
            else:
                out["max_length"] = int(a1)
        out["data_type"] = bt + ("[]" if m.group(4) else "")
    def toint(x):
        try:
            return int(float(x))
        except (TypeError, ValueError):
            return None
    out["max_length"] = out["max_length"] or toint(length)
    out["precision"] = out["precision"] or toint(precision)
    out["scale"] = out["scale"] if out["scale"] is not None else toint(scale)
    if out["data_type"] in ("character varying", "character") and out["max_length"]:
        out["display"] = f"{out['data_type']}({out['max_length']})"
    elif out["data_type"] == "numeric" and out["precision"]:
        out["display"] = f"numeric({out['precision']},{out['scale'] or 0})"
    return out





def parse_ref(s, default_schema):
    """'public.cust_mst(cust_id)' | 'cust_mst.cust_id' | 'public.cust_mst.cust_id' | 'FK -> cust_mst'."""
    s = (s or "").strip()
    s = re.sub(r"^(fk|foreign key)\s*[:\-]?\s*", "", s, flags=re.I).strip()
    if not s or s.lower() in ("-", "none", "n/a"):
        return None
    parts = re.findall(r'[A-Za-z0-9_$]+', s.replace("references", " "))
    if "(" in s:
        before, inside = s.split("(", 1)
        col = re.findall(r'[A-Za-z0-9_$]+', inside)
        tp = re.findall(r'[A-Za-z0-9_$]+', before)
        if not tp:
            return None
        schema, table = (tp[-2], tp[-1]) if len(tp) >= 2 else (default_schema, tp[-1])
        return d.table_key(schema, table), col[0].lower() if col else None
    if len(parts) >= 3:
        return d.table_key(parts[-3], parts[-2]), parts[-1].lower()
    if len(parts) == 2:
        return d.table_key(default_schema, parts[0]), parts[1].lower()
    if len(parts) == 1:
        return d.table_key(default_schema, parts[0]), None
    return None


# ----------------------------------------------------------------------------- build catalog
def new_table(schema, name):
    return {"schema": schema, "name": name, "kind": "table", "comment": None, "row_count": None,
            "columns": [], "primary_key": [], "unique_constraints": [], "foreign_keys": [], "indexes": []}


def ingest(grids, mapping, report):
    overrides = {k: v for k, v in mapping.get("headers", {}).items()}
    overrides.update({k.lower(): v for k, v in mapping.get("headers", {}).items()})
    dschema = mapping.get("default_schema", "public")
    skip = set(mapping.get("skip_tables", []))
    tables, fk_rows, table_meta = {}, [], []

    for gi, g in enumerate(grids):
        rows = g["rows"]
        if gi in skip or len(rows) < 2:
            report.append((gi, g["origin"], g["heading"][:40], "skipped" if gi in skip else "too small", {}))
            continue
        hi, fields = find_header(rows, overrides)
        kind = classify(fields) if hi is not None else "unknown"
        report.append((gi, g["origin"], g["heading"][:40], kind, {rows[hi][j] if j < len(rows[hi]) else j: c for j, c in fields.items()} if hi is not None else {}))
        if kind == "unknown":
            continue
        hs, ht = table_from_heading(g.get("heading") or g.get("id"))
        for r in rows[hi + 1:]:
            rec = {c: (r[j].strip() if j < len(r) else "") for j, c in fields.items()}
            if kind == "tables":
                table_meta.append((rec.get("schema") or hs or dschema, rec.get("table"), rec))
                continue
            if kind == "fks":
                fk_rows.append(rec | {"schema": rec.get("schema") or dschema})
                continue
            tname = rec.get("table") or ht
            if not tname or not rec.get("column"):
                continue
            if rec["column"].lower() in ("column", "column name", "field"):
                continue  # repeated header row
            schema = rec.get("schema") or hs or dschema
            key = d.table_key(schema, tname)
            t = tables.setdefault(key, new_table(d.norm_ident(schema), d.norm_ident(tname)))
            if g.get("para") and not t["comment"] and not rec.get("table"):
                t["comment"] = g["para"][:500]
            ty = parse_type(rec.get("type"), rec.get("length"), rec.get("precision"), rec.get("scale"))
            nullable = True
            if "nullable" in rec:
                v = truthy(rec["nullable"])
                nullable = True if v is None else v
                if rec["nullable"].strip().lower() in ("not null", "no", "n"):
                    nullable = False
            if "not_null" in rec:
                v = truthy(rec["not_null"])
                if v is not None:
                    nullable = not v
            keytxt = (rec.get("key") or "").upper()
            is_pk = bool(truthy(rec.get("pk"))) or bool(re.search(r"\b(PK|PRI|PRIMARY)\b", keytxt))
            is_uq = bool(truthy(rec.get("unique"))) or bool(re.search(r"\b(UQ|UNI|UNIQUE|AK)\b", keytxt))
            colname = d.norm_ident(rec["column"])
            col = {"name": colname, "ordinal": int(rec["ordinal"]) if (rec.get("ordinal") or "").isdigit() else len(t["columns"]) + 1,
                   "data_type": ty.get("display") or ty["data_type"], "nullable": nullable and not is_pk,
                   "default": rec.get("default") or None, "is_pk": is_pk, "is_unique": is_uq,
                   "comment": rec.get("comment") or None, "max_length": ty["max_length"],
                   "precision": ty["precision"], "scale": ty["scale"]}
            if any(c["name"].lower() == colname.lower() for c in t["columns"]):
                continue
            t["columns"].append(col)
            if is_pk:
                t["primary_key"].append(colname.lower())
            if is_uq and not is_pk:
                t["unique_constraints"].append([colname.lower()])
            ref = None
            if rec.get("ref_table"):
                ref = (d.table_key(rec.get("ref_schema") or dschema, rec["ref_table"]), (rec.get("ref_column") or "").lower() or None)
            elif rec.get("fk_ref"):
                ref = parse_ref(rec["fk_ref"], dschema)
            elif re.search(r"\bFK\b", keytxt) and re.search(r"(->|→|REFERENCES|\()", keytxt):
                ref = parse_ref(re.split(r"FK", rec.get("key"), flags=re.I)[-1], dschema)
            if ref:
                t["foreign_keys"].append({"name": None, "columns": [colname.lower()], "ref_table": ref[0],
                                          "ref_columns": [ref[1]] if ref[1] else []})
            if rec.get("row_count") and t["row_count"] is None:
                rc = re.sub(r"[^\d.\-]", "", rec["row_count"])
                try:
                    t["row_count"] = int(float(rc)) if float(rc) >= 0 else None   # reltuples = -1: never analysed
                except ValueError:
                    pass
            if rec.get("table_comment") and not t["comment"]:
                t["comment"] = rec["table_comment"]
            if not rec.get("kind") and re.match(r"^\s*(materiali[sz]ed\s+)?view\b", g.get("heading") or "", re.I):
                t["kind"] = "view"
            if rec.get("kind"):
                k = rec["kind"].lower()
                t["kind"] = "view" if "view" in k or k == "v" else ("matview" if k == "m" else "table")

    for schema, tname, rec in table_meta:
        if not tname:
            continue
        key = d.table_key(schema, tname)
        if key in tables:
            if rec.get("comment") or rec.get("table_comment"):
                tables[key]["comment"] = tables[key]["comment"] or rec.get("comment") or rec.get("table_comment")
            rc = re.sub(r"[^\d.]", "", rec.get("row_count") or "")
            if rc and not (rec.get("row_count") or "").strip().startswith("-"):
                tables[key]["row_count"] = int(float(rc))
            k = (rec.get("kind") or rec.get("type") or "").lower()
            if "view" in k:
                tables[key]["kind"] = "matview" if "material" in k else "view"
    for rec in fk_rows:
        key = d.table_key(rec["schema"], rec["table"])
        if key not in tables:
            continue
        ref_t = d.table_key(rec.get("ref_schema") or rec["schema"], rec["ref_table"])
        fk = {"name": rec.get("key") or None, "columns": [rec["column"].lower()], "ref_table": ref_t,
              "ref_columns": [rec.get("ref_column", "").lower()] if rec.get("ref_column") else []}
        if fk not in tables[key]["foreign_keys"]:
            tables[key]["foreign_keys"].append(fk)

    # resolve FK targets without a column -> referenced table's single PK
    for t in tables.values():
        for fk in t["foreign_keys"]:
            if not fk["ref_columns"] and fk["ref_table"] in tables and len(tables[fk["ref_table"]]["primary_key"]) == 1:
                fk["ref_columns"] = list(tables[fk["ref_table"]]["primary_key"])
        t["columns"].sort(key=lambda c: c["ordinal"])
    return tables


def diff_catalogs(old, new):
    ot, nt = set(old["tables"]), set(new["tables"])
    changes = {"tables_added": sorted(nt - ot), "tables_removed": sorted(ot - nt), "columns_changed": []}
    for k in sorted(ot & nt):
        oc = {c["name"].lower(): (c["data_type"], c["nullable"]) for c in old["tables"][k]["columns"]}
        nc = {c["name"].lower(): (c["data_type"], c["nullable"]) for c in new["tables"][k]["columns"]}
        for c in sorted(set(oc) | set(nc)):
            if oc.get(c) != nc.get(c):
                changes["columns_changed"].append(f"{k}.{c}: {oc.get(c)} -> {nc.get(c)}")
    return changes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--input", action="append", default=[])
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--mapping")
    ap.add_argument("--inspect", action="store_true", help="show detected grids & header mapping, write nothing")
    ap.add_argument("--min-tables", type=int, default=1)
    a = ap.parse_args()
    root = d.find_root(a.root)
    mapping = d.load_json(a.mapping) if a.mapping else {}

    inputs = list(a.input)
    for u in a.url:
        dest = d.mem(root, "legacy", "sources", f"url-{d.stamp()}-{re.sub(r'[^A-Za-z0-9]+', '_', u)[-60:]}.html")
        try:
            with urllib.request.urlopen(u, timeout=60) as resp:
                data = resp.read()
        except Exception as exc:  # noqa: BLE001
            d.die(f"could not fetch {u}: {exc}. Save the page (Ctrl+S, 'HTML only') and pass it with --input.")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        inputs.append(dest)
    if not inputs:
        d.die("give --input <file> and/or --url <url>")

    grids = []
    for p in inputs:
        if not os.path.exists(p):
            d.die(f"input not found: {p}")
        grids.extend(grids_from_file(p))
    report = []
    tables = ingest(grids, mapping, report)

    if a.inspect:
        print(f"{len(grids)} grid(s) found")
        for gi, origin, heading, kind, fields in report:
            print(f"[{gi}] {origin} heading='{heading}' -> {kind} {fields}")
        print(f"\nwould ingest {len(tables)} tables, {sum(len(t['columns']) for t in tables.values())} columns, "
              f"{sum(len(t['foreign_keys']) for t in tables.values())} declared FKs, "
              f"{sum(1 for t in tables.values() if t['primary_key'])} tables with PK, "
              f"{sum(1 for t in tables.values() if t.get('row_count') is not None)} with row counts, "
              f"{sum(1 for t in tables.values() if t.get('comment'))} with table comments, "
              f"{sum(1 for t in tables.values() for c in t['columns'] if c.get('comment'))} column comments, "
              f"{sum(1 for t in tables.values() if t.get('kind') != 'table')} views")
        for k in list(tables)[:8]:
            t = tables[k]
            print(f"  {k}: " + ", ".join(f"{c['name']} {c['data_type']}{'' if c['nullable'] else ' NN'}{' PK' if c['is_pk'] else ''}" for c in t["columns"][:6]) + (" ..." if len(t["columns"]) > 6 else ""))
        return
    if len(tables) < a.min_tables:
        d.die("no tables recognised. Run with --inspect and supply --mapping.")

    sources = []
    for p in inputs:
        raw = open(p, "rb").read()
        sha = hashlib.sha256(raw).hexdigest()
        dest = d.mem(root, "legacy", "sources", os.path.basename(p))
        if os.path.abspath(p) != os.path.abspath(dest):
            shutil.copy(p, dest)
        sources.append({"file": os.path.basename(p), "sha256": sha, "ingested_at": d.now_iso()})

    cat = {"schema_version": d.SCHEMA_VERSION, "engine": "postgresql", "generated_at": d.now_iso(),
           "sources": sources, "mapping": mapping or None, "tables": dict(sorted(tables.items()))}
    cpath = d.mem(root, "legacy", "catalog.json")
    changes = None
    if os.path.exists(cpath):
        old = d.load_json(cpath)
        changes = diff_catalogs(old, cat)
        if any(changes.values()):
            shutil.copy(cpath, d.mem(root, "legacy", f"catalog.{d.stamp()}.prev.json"))
    d.save_json(cpath, cat)
    man = d.load_manifest(root)
    ncols = sum(len(t["columns"]) for t in tables.values())
    man["legacy"].update({"sources": sources, "ingested_at": d.now_iso(), "table_count": len(tables), "column_count": ncols})
    if man["phase"] == "initialised":
        man["phase"] = "ingested"
    d.save_manifest(root, man)
    summary = f"{len(tables)} tables, {ncols} columns from {', '.join(s['file'] for s in sources)}"
    d.journal(root, "legacy-assessment", "ingest", summary, changes=changes)
    print("ingested " + summary)
    no_pk = sum(1 for t in tables.values() if not t["primary_key"])
    print(f"declared FKs: {sum(len(t['foreign_keys']) for t in tables.values())} · tables without PK: {no_pk}")
    if changes:
        print(f"re-ingest diff: +{len(changes['tables_added'])} tables, -{len(changes['tables_removed'])} tables, "
              f"{len(changes['columns_changed'])} column changes")
        if changes["tables_removed"]:
            print("  removed tables (check lineage!): " + ", ".join(changes["tables_removed"][:20]))


if __name__ == "__main__":
    main()
