---
name: legacy-assessment
description: Mode 1 of legacy database modernization. Ingests a legacy PostgreSQL table catalog (an HTML catalog page by file or URL, or a CSV export, in any reasonable layout), analyses it for structural, typing, naming, integrity and GDPR/PCI problems, infers undeclared relationships, clusters tables into candidate business domains, draws ERDs, and writes an assessment report. The result is stored as the permanent legacy baseline in the project's .dbmod memory. Use it whenever the user wants to assess, analyse, audit, profile or understand a legacy or existing database schema, hands over a data dictionary or table catalog, asks what is wrong with the old schema, asks how the legacy data splits into domains or bounded contexts, or is starting a database modernization or rewrite. Also use it to re-ingest an updated catalog or to record review decisions on the assessment.
---

# Legacy assessment (Mode 1)

This mode builds the legacy baseline that every later design decision traces back to. The output is more than a report. It is structured memory (catalog, relationships, findings, domains, glossary) that **domain-modernization** (Mode 2) queries for each new requirement. Get the baseline accurate before moving on, because errors here end up in every target design.

The skill depends on **schema-memory** for the memory layout and the session protocol. Read that skill if you haven't in this conversation. Scripts are standard-library Python in `scripts/`, and every one takes `--root <app-repo>`. Below, `$MEM` is the schema-memory skill's `scripts/` folder and `$LA` is this skill's `scripts/` folder.

## Workflow

### 1. Load or initialise memory
```bash
python3 $MEM/memory_status.py --root <repo>
```
If there is no memory yet, run `$MEM/memory_init.py --root <repo> --project "<name>"`. The memory lives in the application's git repo.

If the legacy baseline already exists, decide which situation applies:
- A re-ingest of a newer export: go to step 2.
- Review work on the existing baseline: go to step 5.
- The user actually wants to design something: switch to domain-modernization.

### 2. Ingest the catalog
The user provides an HTML catalog page (a file, or a URL) or a CSV. Always inspect first:
```bash
python3 $LA/ingest_catalog.py --root <repo> --input <file.html|file.csv> --inspect
python3 $LA/ingest_catalog.py --root <repo> --url <https://...> --inspect
```
Check the summary: table count, column count, tables with a PK, declared FKs, and the sample tables. Show the user these numbers and confirm they match what they know about the database, for example "~300 tables". If headers were missed or misread, write a mapping file and re-inspect. Then ingest by running the same command without `--inspect`.

- URLs that need a login usually fail from the script. Ask the user to save the page as HTML, or fetch it with a browser or Atlassian tool if one is available.
- Exports without constraints or row counts weaken the analysis. Offer the DBA query in `references/ingestion-guide.md`.

Read that guide for any non-trivial input.

### 3. Analyse and cluster
```bash
python3 $LA/analyze_schema.py   --root <repo>     # relationships, findings, PII, glossary
python3 $LA/discover_domains.py --root <repo>     # candidate domains
python3 $LA/render_erd.py       --root <repo>     # Mermaid ERD per domain
python3 $LA/build_report.py     --root <repo>     # report.md + findings.csv
```
Rule meanings and severities are in `references/assessment-rules.md`.

The scripts handle detection. Your job is interpretation:
- **Domains:** look at each candidate's tables and ERD. Do the clusters reflect the business, or do they only reflect legacy naming? Propose renames, merges and moves. Hub tables such as customer often need an explicit owner decision.
- **Relationships:** low-confidence inferred relationships and `LA-KEY-006` columns are questions for subject-matter experts, not facts.
- **Findings:** group them into a few themes a CTO would care about: integrity, money and time, PII exposure, dead weight, and hidden structure (EAV, polymorphic, repeating groups).

For large schemas (hundreds of tables), work domain by domain:
- `memory_query.py domain <name>`
- `memory_query.py table <t>`

Read the aggregates in `findings.json`'s `summary` rather than loading every finding.

### 4. Review loop with the user
Present the first results before you write any narrative:
- the candidate domains (tables, hubs, retire candidates)
- the top risks
- the inferred relationships and SME questions that need an answer

Record what the user decides, passing `--by "<name>"` so the decision is attributed:
```bash
python3 $LA/review.py --root <repo> domain rename department people --by "A. Khan"
python3 $LA/review.py --root <repo> domain move public.usr identity
python3 $LA/review.py --root <repo> domain describe billing "Invoicing, payments and credit notes"
python3 $LA/review.py --root <repo> domain confirm billing customer --by "A. Khan"   # or: domain confirm all
python3 $LA/review.py --root <repo> rel confirm public.emp.mgr_id
python3 $LA/review.py --root <repo> rel add public.ord_hdr.sales_rep_id public.emp.emp_id --note "SME: rep is an employee"
python3 $LA/review.py --root <repo> finding F-0042 accepted-risk --note "table frozen since 2019"
python3 $LA/review.py --root <repo> term whse warehouse --confirm
python3 $LA/review.py --root <repo> sme list --open                     # SME questions still waiting for the business
python3 $LA/review.py --root <repo> sme answer SQ-0004 "Retired in 2019, no readers" --by "J. Patel:data-owner"
python3 $LA/review.py --root <repo> sme waive SQ-0007 --adr ADR-0004 --by "J. Patel:data-owner" --review-by 2027-03-01
python3 $LA/review.py --root <repo> sme review SQ-0007 --review-by 2027-06-01 --by "J. Patel:data-owner" --note "finance still checking"
```
- `domain confirm all` confirms the business domains only. Review the `technical` and `unassigned` buckets separately, and confirm them by name.
- **SME questions are a gate, not a to-do list.** `build_report.py` saves them with stable ids in `.dbmod/legacy/sme_questions.json`. Domain modernization cannot accept a context while questions on its legacy tables are open, because the business must confirm what the old system actually does before it is redesigned. Record the business's own answer and who gave it; never answer on their behalf. A waiver is a temporary exception, not an answer: it always has a review date (default `business_gate.waiver_review_days`, 90 days). An overdue waiver fails `memory_validate.py` and blocks acceptance until someone records the business's answer (`sme answer`) or a review with a new date (`sme review`). When the business answers a waived question, the waiver's ADR is listed as a follow-up in `memory_status.py` until it is superseded or rejected, so the exception does not outlive the answer. Questions settled through other review commands (a confirmed relationship, a confirmed glossary term, a finding marked accepted-risk) close automatically on the next `build_report.py`.
- After relationship changes, refresh by re-running `analyze_schema.py`, `discover_domains.py`, `render_erd.py` and `build_report.py`. Analysis keeps finding ids stable. A finding that no longer applies, such as an unenforced-relationship finding the reviewer answered, is kept with status `not-detected` rather than deleted. Confirmed domains, reviewer-given names, review statuses and glossary terms survive the refresh.
- Cross-cutting decisions (time zone, money, keys, PII, soft delete) go in ADRs via `$MEM/memory_record.py new ADR ...`.

If the user isn't available to review, say which proposals you are leaving as candidates. Then write the narrative on that basis and flag it as provisional.

### 5. Write the narrative
Write the narrative after the review, so the report reflects the agreed domains. Open `.dbmod/legacy/assessment/report.md` and replace each `<!-- CLAUDE: ... -->` marker. Keep the surrounding `BEGIN/END` markers so that re-running `build_report.py` preserves your text.
- **Executive summary:** health, top risks, proposed decomposition, first decisions.
- **Domain commentary:** one line per domain with a verdict (become a context, split, merge, retire, or technical).
- **Recommendations:** a prioritised roadmap. Name the first context to design and why. Prefer upstream master data (e.g. customer, product) before contexts that reference it, because data migration loads in dependency order. List the cross-cutting ADRs needed: time zone of legacy timestamps, money representation, key strategy, PII handling, soft delete and retention. List the data profiling to commission (use the queries in the rules reference).
- **SME questions:** curate the generated list into a short list grouped by owner.

Ground every statement in memory. Cite finding ids, table names and counts. Don't speculate about data you haven't seen. Without data profiling, quality issues are risks, not facts. If later review changes the numbers, re-run `build_report.py` and adjust the narrative.

### 6. Close the session
```bash
python3 $MEM/memory_validate.py --root <repo>
```
Then tell the user:
- the headline numbers
- where the report is
- the decisions still open
- that the baseline is ready for domain modernization, and which domain you suggest starting with

The report is a document the user will share. Offer to turn it into a shareable doc, and send them the `report.md` file.

## Judgement notes
- **Legacy names are evidence, not design.** Record what a column means (glossary, comments) so Mode 2 can name things properly. Never carry legacy abbreviations into target names.
- **Backup, archive and temp tables still count.** They need an explicit "retire" disposition later, so they stay in the catalog and in their domain's `retire_candidates`.
- **The technical domain is real.** Audit logs, config tables and generic lookups are usually replaced by platform features rather than migrated. Say so, so nobody designs a context around them.
- **Don't hide uncertainty.** If the catalog lacks FKs, comments or row counts, state how that limits the assessment and what export would fix it.
