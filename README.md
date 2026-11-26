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
- migrated money is **reconciled to the penny** against the raw legacy values, row by row, and the data owner signs it off
## What's new in v1.5.1

Two more points from readers.

- **Refunds and negative amounts.** The test data now has a refund stored the legacy way (positive, with a type flag) and half-pence lines, one of them negative. The tests show that a transform which forgets the sign is caught on the refund row, and that PostgreSQL's `round()` (12.35, -2.35 against finance's half-even 12.34, -2.34) keeps both the line total and the invoice total matching, so only the row-by-row check catches it.
- **Correcting lines.** When finance rules that the invoice header is right and its lines don't add up, `reconcile_ruling.py --correct-with-line` adds a correcting line after the load, tagged with the ruling and recorded in `migration_map.corrections`, so the new system's invoice = sum of lines rule holds. Open periods only: a closed period is disclosed, never corrected in place.

## What's new in v1.5

Money that matches isn't always right, and a difference isn't always the migration's fault. Two readers' comments shaped this release: totals can hide errors that cancel out, and sometimes the legacy number was the wrong one all along.

- **Finance rules on differences.** `reconcile_ruling.py` records which value is correct (legacy or migrated), who decided, the ADR, and the reporting period. In a closed period the reported figure isn't restated, so accepting a difference needs a disclosure. Accepted differences stop failing on re-runs; rulings that the migration is wrong keep the check red until it's fixed.
- **Deliberate conversions.** `reconcile_raw` on a column mapping says what the legacy value should become (pence to pounds, FX), so a known conversion isn't reported as a mismatch.
- **Checks across tables.** `reconcile_invariants`: invoice net = sum of lines, debits = credits, balance rolls forward. If every row matches but an invariant fails, legacy already had the inconsistency.
- **Per currency, rounding and time zone.** Totals per currency at each currency's decimal places (JPY 0, KWD 3), the rounding finance agreed (half-up, banker's half-even, truncate) and month-ends in the business's time zone.
- **NULLs that became values.** A legacy NULL loaded as 0 is flagged for review, not hidden.
- More money names are recognised (principal, interest, premium, limit …), and tables loaded by a custom query are reconciled too.

## What's new in v1.4

A reader asked how many waivers are still open when the first ones come due. That number shows how involved the business really is, so the skill now reports it.

- **Renewal limit.** Each waiver review counts as a renewal. After two (configurable), only a data owner can renew again, so a waiver can't be pushed back every 90 days without an answer.
- **Business involvement.** `memory_status.py` shows renewals per waiver, waivers due in the next 14 days, how many waivers were still open when the first one came due, and an answered-vs-open trend over time.

## What's new in v1.3

Reconciliation now checks the money to the penny, not just the totals. This came from a reader who has been through migrations where the totals matched but individual accounts were a few pence out.

- **Raw vs converted vs target.** Each money total is shown three ways: the raw legacy sum, the sum after transforms, and what landed in the target. Before, reconcile compared the converted value with the target, so a transform that rounded wrongly passed. Now it shows as `MISMATCH (conversion changes the total)`.
- **Row by row.** Every legacy row is followed through its id map to its new row, and each money value is compared at `money_scale` decimal places (default 2). The report shows how many rows differ, the largest difference and example legacy keys.
- **Per parent and per month.** Totals per invoice, per account (any foreign key resolved by lookup) and per calendar month, so drift that cancels out across a whole table still shows.
- **Data-owner sign-off.** Each data migration now includes `05_signoff.md`, and the migration guide has a parallel-run template for the calculations that move money.

Tested on a real PostgreSQL: a transform that rounds one line from 67.08 to 67.10 passes a converted-vs-target check, and v1.3 flags the total, the row and its invoice. Projects created before v1.3 use the defaults and print a note.

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
                     db/data-migration/<ctx>/vNNN/00..05       (fdw → id maps → precheck → load → reconcile → sign-off)
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

The run ends with approved Flyway migrations and a six-step data migration (prerequisites, id maps, precheck, load, reconcile, data-owner sign-off) for each context. ERDs are generated as Mermaid diagrams, so they render directly on GitHub.

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
| Reconcile | money compared to 2 dp (or per currency): raw vs converted vs target totals, per row via the id maps, per parent, month and currency, NULLs that became values, invariants across tables; data owner signs `05_signoff.md` | standards → data_migration.reconcile; mapping → reconcile_raw, reconcile_invariants |
| Finance rulings | finance decides which value is correct per difference, for a reporting period; closed periods need a disclosure; open periods can get a traced correcting line | `reconcile_ruling.py`, standards → reconcile.ruling_roles |
| Waiver renewals | each review is a renewal; past 2, only a data owner renews; involvement and trend in the status report | standards → business_gate.max_waiver_renewals |
| Data migration | one-off ETL via postgres_fdw, id-map tables kept as audit trail | standards → data_migration |

Edit `.dbmod/standards.json` in your repo to change these, and record the reason in an ADR.

## Package layout
```
schema-memory/          SKILL.md, scripts/ (init, status, query, record, validate), references/memory-spec.md, templates/
legacy-assessment/      SKILL.md, scripts/ (ingest, analyze, domains, erd, report, review), references/
domain-modernization/   SKILL.md, scripts/ (context_tool, model_lint, lineage_check, check_conflicts,
                        generate_migration, accept_model, generate_data_migration, reconcile_ruling, render_erd),
                        references/ (+ examples/)
tests/                  synthetic legacy fixtures, run_e2e.sh (all scripts), run_pg_test.sh (live PostgreSQL)
tools/sync_common.py    keeps the shared _dbmod.py identical in all three skills
```

## Why skills, not a single prompt

A database modernisation runs for months. A prompt forgets; these skills don't. The deterministic work (parsing catalogs, linting models, checking lineage, generating SQL) is done by tested Python scripts, while Claude does what it is good at: proposing domain models, explaining trade-offs and asking the right questions. Humans stay in charge of every decision that reaches a migration file, and the business, not just engineering, confirms what the legacy system does and signs off on data it owns.

## Author

**Anand Kumar**, Senior Java Tech Lead & Consultant: legacy modernisation, fintech and AI-augmented delivery.
[LinkedIn](https://www.linkedin.com/in/akjkhatri) · [GitHub](https://github.com/akjkhatri)
 