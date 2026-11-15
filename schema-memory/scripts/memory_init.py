#!/usr/bin/env python3
"""Create the .dbmod/ modernization memory inside the application repository.

Idempotent: running it again never overwrites existing memory files; it only adds
missing folders/files (useful after upgrading the skills).

Usage:
  python memory_init.py --root <app-repo> --project "Policy Admin Rewrite" \
      [--flyway-location db/migration] [--data-migration-location db/data-migration]
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(os.path.dirname(HERE), "templates")

README = """# .dbmod — database modernization memory

This folder is the long-term memory of the legacy-to-new schema modernization. It is
maintained by the `schema-memory`, `legacy-assessment` and `domain-modernization`
Claude skills, and is meant to be committed and code-reviewed like source code.

| Path | What it holds | Who writes it |
|---|---|---|
| manifest.json | Phase, counters, context registry, settings | scripts |
| standards.json | Target design standards (edit + ADR to change) | humans |
| legacy/ | Canonical legacy catalog, findings, domains, assessment report, ERDs | legacy-assessment |
| target/contexts/<ctx>/ | Target model per bounded context, design notes, accepted snapshots | domain-modernization |
| mapping/<ctx>.json | Column-level lineage legacy -> target, retired legacy columns | domain-modernization |
| requirements/ | One file per requirement (REQ-nnnn) with its schema impact | domain-modernization |
| decisions/ | Architecture decision records (ADR-nnnn) | both |
| journal.jsonl / changelog.md | Append-only run log | scripts |

Rules: accepted snapshots and applied Flyway migrations are immutable; change the model and
generate a new migration instead. Never hand-edit catalog.json — re-ingest.
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="application repository root")
    ap.add_argument("--project", default="Legacy modernization")
    ap.add_argument("--flyway-location", default=None)
    ap.add_argument("--data-migration-location", default=None)
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        d.die(f"{root} is not a directory")
    created = []
    for sub in ["legacy/sources", "legacy/assessment/erd", "target/contexts", "mapping",
                "requirements", "decisions"]:
        p = d.mem(root, *sub.split("/"))
        if not os.path.isdir(p):
            os.makedirs(p)
            created.append(sub + "/")

    std_path = d.mem(root, "standards.json")
    if not os.path.exists(std_path):
        shutil.copy(os.path.join(TEMPLATES, "standards.default.json"), std_path)
        created.append("standards.json")
    std = d.load_json(std_path)
    if a.flyway_location:
        std["flyway"]["location"] = a.flyway_location
    if a.data_migration_location:
        std["data_migration"]["location"] = a.data_migration_location
    d.save_json(std_path, std)

    man_path = d.mem(root, "manifest.json")
    fresh = not os.path.exists(man_path)
    if fresh:
        manifest = {
            "schema_version": d.SCHEMA_VERSION,
            "project": a.project,
            "created_at": d.now_iso(),
            "updated_at": d.now_iso(),
            "phase": "initialised",
            "legacy": {"engine": "postgresql", "sources": [], "ingested_at": None,
                       "assessed_at": None, "table_count": 0, "column_count": 0},
            "target": {"engine": "postgresql", "contexts": {}},
            "counters": {"REQ": 0, "ADR": 0},
        }
        d.save_json(man_path, manifest)
        created.append("manifest.json")

    for name, text in [("README.md", README), ("changelog.md", "# Modernization changelog\n\n")]:
        p = d.mem(root, name)
        if not os.path.exists(p):
            d.write_text(p, text)
            created.append(name)
    for idx in ["requirements/index.json", "decisions/index.json"]:
        p = d.mem(root, *idx.split("/"))
        if not os.path.exists(p):
            d.save_json(p, {"items": []})
            created.append(idx)

    if fresh:
        d.journal(root, "schema-memory", "init", f"memory initialised for '{a.project}'",
                  flyway_location=std["flyway"]["location"])
    if not os.path.isdir(os.path.join(root, ".git")):
        print(f"NOTE: {root} is not a git repository root. The memory is meant to be versioned with the application; "
              "check the path, or run git init.")
    print(f"memory root: {d.mem(root)}")
    print("created: " + (", ".join(created) if created else "nothing (already initialised)"))
    print(f"flyway location (relative to repo): {std['flyway']['location']}")


if __name__ == "__main__":
    main()
