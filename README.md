# db-modernization skills

[![Tests](https://github.com/akjkhatri/db-modernization-skills/actions/workflows/tests.yml/badge.svg)](https://github.com/akjkhatri/db-modernization-skills/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-13%2B-4169E1?logo=postgresql&logoColor=white)
![Claude Skills](https://img.shields.io/badge/Claude-Skills-D97757)

**Three Claude skills that turn a legacy PostgreSQL database into a clean, domain-driven schema, one requirement at a time, with lineage, GDPR/PCI checks, business confirmation of legacy behaviour and role-based approval before any migration is written.**

## The problem

Legacy databases rarely get rewritten in one go. Teams modernise them requirement by requirement, and along the way they lose track of which legacy columns have been mapped, which were deliberately retired, and why. Floats hold money, card numbers sit in plain tables, relationships exist only in application code, and nobody can say how far the migration has actually got.

These skills give Claude (and the team) a **persistent memory of the modernisation programme** and a set of **gates** that every new design has to pass:

- every target column is traced back to its legacy source (**lineage**)
- every legacy column is classified for **GDPR/PCI**, and PII can never silently become "non-PII"
- the **business confirms what the legacy system actually does** (SME questions answered) before a context is redesigned
- nothing is written to `db/migration` until the **right people approve**: an architect, plus the data owner when the model holds money or personal data
- **coverage** (the share of legacy columns mapped or deliberately retired) shows real progress
## What's new in v1.2

Waivers can no longer quietly become permanent. Another good question from the same discussion: an exception written in week three shouldn't still be the rule two years later.

- **Every waiver has a review date** (`sme waive … --review-by`, default 90 days). `sme review` records who re-checked it, what they found, and the next date.
- **Visible:** `memory_status` shows how many accepted contexts rely on a waiver, with each review date.
- **Enforced:** an overdue waiver fails `memory_validate` (and so CI) and blocks acceptance until the business answers or someone records a review.
- **Closed loop:** when the business answers a waived question, the waiver's ADR is flagged until it is superseded or rejected.

## What's new in v1.1

Two gates that put the business, not just engineering, in the loop. Both came out of a community discussion about who should really sign off on a legacy migration.

- **Role-based approval.** `--approved-by` now takes `"Name:role"` and can be repeated. The roles a model needs come from `standards.json`: an architect always signs, and a data owner must also sign when the model holds money or personal/financial data. One person can't fill two roles. Changing money from a float to `numeric(19,4)` changes totals in the last cent, so finance has to agree the new numbers are right.
- **Business gate.** SME questions are now tracked with stable ids in `.dbmod/legacy/sme_questions.json`. A context can't be accepted while questions on its legacy tables are unanswered, because half the "bugs" in a legacy system turn out to be rules someone relies on. If the business can't answer yet, the only way forward is a waiver backed by an accepted ADR.

Projects created with v1.0 keep working: without the new `standards.json` sections, acceptance behaves as before and prints a note.

## The three skills

| Skill | Mode | What it does |
|---|---|---|
| `schema-memory` | (base) | Persistent project memory in `.dbmod/` inside your app repo: legacy baseline, target contexts, lineage, requirements, ADRs, coverage, journal. Session protocol, queries and integrity validation. |
| `legacy-assessment` | Mode 1 | Ingests the legacy catalog (HTML page or CSV, any layout), finds structural, typing, naming, integrity and GDPR/PCI issues, infers undeclared relationships, clusters tables into domains, draws ERDs, and writes the assessment report. |
| `domain-modernization` | Mode 2 | For each Jira/Confluence requirement: designs the bounded-context model, maps every column to legacy (lineage), runs the lint, lineage, conflict and business gates, and after role-based approval writes Flyway migrations plus data-migration ETL with prechecks and reconciliation. |

## How it fits together
```
legacy catalog (HTML/CSV) ──► Mode 1 ──► .dbmod/legacy/   (baseline: catalog, findings, domains, glossary, report)
                                              │
Jira / Confluence requirement ──► Mode 2 ─────┤ reads baseline + previous contexts + ADRs
                                              ▼
                     .dbmod/target/contexts/<ctx>/model.json  +  .dbmod/mapping/<ctx>.json (lineage)
                                              │  gates: model_lint · lineage_check · check_conflicts
                                              │  business gate: SME questions answered by the business
                                              │  role-based approval (accept_model --approved-by "Name:role")
                                              ▼
                     db/migration/V<ts>__<ctx>_<desc>.sql      (Flyway, immutable)
                     db/data-migration/<ctx>/vNNN/00..04       (fdw → id maps → precheck → load → reconcile)
```

## Example: what it finds in a typical legacy schema

Run against the synthetic **Acme ERP** catalog in `tests/fixtures` (28 tables, 166 columns), Mode 1 reports:

```
ingested 28 tables, 166 columns from acme_catalog_flat.html
declared FKs: 3 · tables without PK: 6
findings: critical 6, high 41, medium 48, low 21, info 1
```

When Mode 2 designs the new `customer` and `billing` contexts, it reminds you which legacy problems the design must resolve:

```
F-0019 high public.cust_mst.credit_lmt: Monetary value in floating point
F-0018 high public.cust_mst.dob: Date/time stored as text
F-0024 high public.cust_mst: Repeating group 'phone*' (3 columns)
F-0093 high public.pymt.amt: PostgreSQL money type (locale-dependent)
F-0097 high public.pymt.card_no: Payment card / bank data
```

and tracks progress per context:

```
customer: 8/12 target columns mapped explicitly · 25 legacy columns pending · 0 error(s)
billing: 20/38 target columns mapped explicitly · 5 legacy columns pending · 0 error(s)
```

The run ends with approved Flyway migrations and a five-step data migration (prerequisites, id maps, precheck, load, reconcile) for each context. ERDs are generated as Mermaid diagrams, so they render directly on GitHub.

## Quick start

Requirements: Python 3.9+ (standard library only). Generated SQL targets PostgreSQL 13+ (tested on 16); data migration uses `postgres_fdw`.

**Claude Code:** copy the three folders into `~/.claude/skills/` (personal) or `<repo>/.claude/skills/` (shared with the team via git). Install all three: the two modes rely on `schema-memory`.

**Claude app (claude.ai / desktop):** download the three zips from the [latest release](../../releases/latest) and upload them under Settings → Capabilities → Skills.

Then ask Claude:

1. *"Assess our legacy schema. Catalog: `<file or URL>`, app repo: `<path>`"* runs Mode 1, which creates `.dbmod/`, the report and the domain map.
2. Review: confirm domains, relationships and glossary terms. The business answers the SME questions (`review.py sme answer SQ-0004 "..." --by "Name:data-owner"`); a context can't be accepted while questions on its legacy tables are open.
3. *"Here's story PROJ-142 …"* (or a Confluence link or pasted text) runs Mode 2: a proposal for your approval, then *"approved by `<name>` (architect) and `<name>` (data owner)"* produces the migration files.
4. Commit `.dbmod/` and `db/` together in one PR per requirement. In CI, run:
   `python3 schema-memory/scripts/memory_validate.py --root . --strict`
   The best catalog comes from the DBA query in `legacy-assessment/references/ingestion-guide.md`, which captures constraints, defaults and row counts. HTML catalog pages (SchemaSpy, DBeaver, dbdocs, Confluence exports) work too.

## Testing
```bash
bash tests/run_e2e.sh        # Mode 1 + three requirements through Mode 2, memory validation
bash tests/run_pg_test.sh    # same, then applies migrations and runs the data migration on a real PostgreSQL
```
Both run in GitHub Actions on every push. All fixtures are synthetic.

## Design decisions (defaults you can change)
| Topic | Default | Where |
|---|---|---|
| Schema layout | one schema per bounded context; no cross-schema FKs (id references instead) | `standards.json` → keys |
| Keys | `bigint` identity `<table>_id`, optional `public_id uuid` | standards |
| Types | `timestamptz`, `numeric(19,4)` money, `text` + CHECK, no `money`/`float`/`json`/`serial` | standards → types |
| Audit | created/updated at/by on all non-lookup tables; `row_version` on aggregate roots | standards |
| GDPR | every column classified (`pii`); legacy PII cannot become `pii: none`; retention notes | standards → gdpr; lineage gate |
| Migrations | Flyway `V<UTC timestamp>__<ctx>_<desc>.sql`, written only on approval | standards → flyway |
| Approvals | architect always; data owner for money and personal/financial data; security for credentials and special-category data; one person per role | standards → approvals |
| Business gate | legacy domains confirmed and SME questions answered (or waived under an accepted ADR) before acceptance | standards → business_gate |
| Waivers | review date on every waiver (default 90 days); overdue waivers fail validation and block acceptance | standards → business_gate.waiver_review_days |
| Data migration | one-off ETL via postgres_fdw, id-map tables kept as audit trail | standards → data_migration |

Edit `.dbmod/standards.json` in your repo to change these, and record the reason in an ADR.

## Package layout
```
schema-memory/          SKILL.md, scripts/ (init, status, query, record, validate), references/memory-spec.md, templates/
legacy-assessment/      SKILL.md, scripts/ (ingest, analyze, domains, erd, report, review), references/
domain-modernization/   SKILL.md, scripts/ (context_tool, model_lint, lineage_check, check_conflicts,
                        generate_migration, accept_model, generate_data_migration, render_erd), references/ (+ examples/)
tests/                  synthetic legacy fixtures, run_e2e.sh (all scripts), run_pg_test.sh (live PostgreSQL)
tools/sync_common.py    keeps the shared _dbmod.py identical in all three skills
```

## Why skills, not a single prompt

A database modernisation runs for months. A prompt forgets; these skills don't. The deterministic work (parsing catalogs, linting models, checking lineage, generating SQL) is done by tested Python scripts, while Claude does what it is good at: proposing domain models, explaining trade-offs and asking the right questions. Humans stay in charge of every decision that reaches a migration file, and the business, not just engineering, confirms what the legacy system does and signs off on data it owns.

## Author

**Anand Kumar**, Senior Java Tech Lead & Consultant: legacy modernisation, fintech and AI-augmented delivery.
[LinkedIn](https://www.linkedin.com/in/akjkhatri) · [GitHub](https://github.com/akjkhatri)
 