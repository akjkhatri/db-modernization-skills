"""Shared helpers for the db-modernization skills (schema-memory, legacy-assessment,
domain-modernization).

An identical copy of this file ships in each skill's scripts/ folder so that every skill
stays standalone. If you change it, copy it to the other two skills
(tools/sync_common.py in the package root does that and checks they match).

Standard library only: these scripts must run on locked-down enterprise machines.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
import sys
import tempfile

sys.dont_write_bytecode = True   # never litter installed skill folders with __pycache__

MEMORY_DIR = ".dbmod"
SCHEMA_VERSION = "1.0"

# PostgreSQL reserved key words (SQL:2016 + PG specific) that cannot be used unquoted
# as identifiers, plus a few that are legal but cause pain in ORMs.
PG_RESERVED = set("""
all analyse analyze and any array as asc asymmetric authorization binary both case cast check
collate collation column concurrently constraint create cross current_catalog current_date
current_role current_schema current_time current_timestamp current_user default deferrable desc
distinct do else end except false fetch for foreign freeze from full grant group having ilike in
initially inner intersect into is isnull join lateral leading left like limit localtime
localtimestamp natural not notnull null offset on only or order outer overlaps placing primary
references returning right select session_user similar some symmetric system_user table
tablesample then to trailing true union unique user using variadic verbose when where window with
""".split())
PAINFUL_NAMES = {"type", "value", "key", "name", "data", "date", "time", "timestamp", "level",
                 "comment", "position", "order", "status"}

SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# --------------------------------------------------------------------------- basics
def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def stamp() -> str:
    """UTC timestamp usable in a Flyway version: 20261112145501."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")


def die(msg: str, code: int = 2):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_json(path, default=None):
    if not os.path.exists(path):
        if default is not None:
            return default
        die(f"missing file: {path}")
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            die(f"invalid JSON in {path}: {exc}")


def save_json(path, data):
    """Atomic write so an interrupted run never leaves half a memory file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o644)   # mkstemp creates 0600; memory files are shared through git


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# --------------------------------------------------------------------------- memory root
def find_root(explicit: str | None = None) -> str:
    """Return the directory that CONTAINS .dbmod/ (normally the application repo root).

    Order: --root argument, DBMOD_ROOT env var, then walk up from the cwd.
    """
    cands = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("DBMOD_ROOT"):
        cands.append(os.environ["DBMOD_ROOT"])
    for c in cands:
        c = os.path.abspath(c)
        if os.path.basename(c) == MEMORY_DIR:
            c = os.path.dirname(c)
        if os.path.exists(os.path.join(c, MEMORY_DIR, "manifest.json")):
            return c
        die(f"no {MEMORY_DIR}/manifest.json under {c}. Run memory_init.py first.")
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, MEMORY_DIR, "manifest.json")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            die("could not find .dbmod/ memory (walked up from cwd). "
                "Pass --root <repo> or run schema-memory/scripts/memory_init.py.")
        cur = parent


def mem(root: str, *parts) -> str:
    return os.path.join(root, MEMORY_DIR, *parts)


def load_manifest(root):
    return load_json(mem(root, "manifest.json"))


def save_manifest(root, manifest):
    manifest["updated_at"] = now_iso()
    save_json(mem(root, "manifest.json"), manifest)


def load_standards(root):
    return load_json(mem(root, "standards.json"))


def load_catalog(root, required=True):
    p = mem(root, "legacy", "catalog.json")
    if not os.path.exists(p):
        if required:
            die("legacy catalog not found. Run legacy-assessment first (ingest_catalog.py).")
        return None
    return load_json(p)


def load_domains(root):
    return load_json(mem(root, "legacy", "domains.json"), default={"domains": []})


def load_findings(root):
    return load_json(mem(root, "legacy", "findings.json"), default={"findings": []})


def context_dir(root, ctx):
    return mem(root, "target", "contexts", ctx)


def list_contexts(root):
    base = mem(root, "target", "contexts")
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base) if os.path.exists(os.path.join(base, d, "model.json")))


def load_model(root, ctx):
    return load_json(os.path.join(context_dir(root, ctx), "model.json"))


def load_mapping(root, ctx):
    return load_json(mem(root, "mapping", f"{ctx}.json"),
                     default={"context": ctx, "table_mappings": [], "column_mappings": [], "retired": []})


def load_all_mappings(root):
    out = {}
    for p in sorted(glob.glob(mem(root, "mapping", "*.json"))):
        name = os.path.splitext(os.path.basename(p))[0]
        out[name] = load_json(p)
    return out


# --------------------------------------------------------------------------- journal
def journal(root, skill, action, summary, **details):
    """Append-only run log. Every script that changes memory calls this."""
    entry = {"at": now_iso(), "skill": skill, "action": action, "summary": summary}
    if details:
        entry["details"] = details
    os.makedirs(mem(root), exist_ok=True)
    with open(mem(root, "journal.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    with open(mem(root, "changelog.md"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"- {entry['at'][:16].replace('T', ' ')} UTC · **{skill}/{action}** · {summary}\n")


# --------------------------------------------------------------------------- refs & names
def norm_ident(s: str) -> str:
    return (s or "").strip().strip('"').strip("`").strip("[]").strip()


def table_key(schema: str, table: str) -> str:
    return f"{norm_ident(schema) or 'public'}.{norm_ident(table)}".lower()


def split_ref(ref: str):
    """'schema.table.column' -> (schema, table, column). 2-part refs -> (schema, table, None)."""
    parts = [norm_ident(p) for p in ref.split(".")]
    if len(parts) == 3:
        return parts[0].lower(), parts[1].lower(), parts[2].lower()
    if len(parts) == 2:
        return parts[0].lower(), parts[1].lower(), None
    raise ValueError(f"bad reference '{ref}', expected schema.table[.column]")


def is_snake(name: str) -> bool:
    return bool(SNAKE_RE.match(name or ""))


def tokens(name: str):
    """Split identifiers like custOrderHdr / cust_order_hdr / CUST-ORDER into tokens."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name or "")
    return [t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if t]


def singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("ses", "xes", "ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def base_type(pg_type: str) -> str:
    t = (pg_type or "").lower().strip()
    t = re.sub(r"\(.*\)", "", t).strip()
    aliases = {
        "int": "integer", "int4": "integer", "int8": "bigint", "int2": "smallint",
        "serial": "integer", "serial4": "integer", "bigserial": "bigint", "serial8": "bigint",
        "smallserial": "smallint", "float8": "double precision", "float4": "real",
        "float": "double precision", "bool": "boolean", "varchar": "character varying",
        "char": "character", "bpchar": "character", "decimal": "numeric",
        "timestamptz": "timestamp with time zone", "timestamp": "timestamp without time zone",
        "timetz": "time with time zone", "time": "time without time zone",
    }
    t = aliases.get(t, t)
    if t.startswith("timestamp") and "with time zone" not in t:
        t = "timestamp without time zone" if "without" in t or t == "timestamp" else t
    return t


TYPE_FAMILY = {
    "smallint": "int", "integer": "int", "bigint": "int",
    "numeric": "num", "real": "float", "double precision": "float", "money": "money",
    "character varying": "str", "character": "str", "text": "str", "citext": "str",
    "uuid": "uuid", "boolean": "bool", "date": "date",
    "timestamp without time zone": "ts", "timestamp with time zone": "tstz",
    "json": "json", "jsonb": "json", "bytea": "bin",
}


def type_family(pg_type: str) -> str:
    return TYPE_FAMILY.get(base_type(pg_type), "other")


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |")
    return "\n".join(out)


# --------------------------------------------------------------------------- coverage
def legacy_table_domains(root):
    """legacy table key -> domain name (from legacy/domains.json)."""
    out = {}
    for d in load_domains(root).get("domains", []):
        for t in d.get("tables", []):
            out[t.lower()] = d["name"]
        for t in d.get("shared_tables", []):
            out.setdefault(t.lower(), d["name"])
    return out


def compute_coverage(root):
    """Disposition of every legacy column: mapped (accepted/proposed), retired, or pending.

    A legacy column is 'mapped' when any context's column_mappings lists it as a source,
    'retired' when a retired entry names it (or its whole table with '*'), else 'pending'.
    """
    cat = load_catalog(root, required=False)
    if not cat:
        return None
    mapped, retired = {}, {}
    for ctx, m in load_all_mappings(root).items():
        for cm in m.get("column_mappings", []):
            for s in cm.get("sources", []):
                mapped.setdefault(s.lower(), []).append((ctx, cm.get("status", "proposed")))
        for r in m.get("retired", []):
            retired[r["source"].lower()] = (ctx, r.get("reason", ""))
    dom = legacy_table_domains(root)
    per_dom, rows = {}, []
    totals = {"mapped_accepted": 0, "mapped_proposed": 0, "retired": 0, "pending": 0}
    for tkey, t in cat["tables"].items():
        d = dom.get(tkey, "(unassigned)")
        bucket = per_dom.setdefault(d, dict(totals, **{k: 0 for k in totals}))
        table_retired = f"{tkey}.*" in retired
        for c in t["columns"]:
            ref = f"{tkey}.{c['name'].lower()}"
            if ref in mapped:
                st = "mapped_accepted" if any(s == "accepted" for _, s in mapped[ref]) else "mapped_proposed"
            elif ref in retired or table_retired:
                st = "retired"
            else:
                st = "pending"
            bucket[st] += 1
            totals[st] += 1
            rows.append((ref, d, st))
    n = sum(totals.values()) or 1
    return {
        "totals": totals,
        "total_columns": sum(totals.values()),
        "percent_disposed": round(100.0 * (n - totals["pending"]) / n, 1),
        "percent_accepted": round(100.0 * (totals["mapped_accepted"] + totals["retired"]) / n, 1),
        "per_domain": per_dom,
        "columns": rows,
    }
