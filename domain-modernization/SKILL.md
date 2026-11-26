---
name: domain-modernization
description: Mode 2 of legacy database modernization. It turns each new requirement (a Jira issue, Confluence page or pasted story) into a target PostgreSQL schema for a bounded context, grounded in the legacy baseline stored in .dbmod memory. It designs the DDD model, maps every column back to legacy (lineage), checks it against standards and the other contexts, and after approval generates Flyway migrations plus one-off legacy-to-new data migration scripts. Use it whenever the user brings a requirement, user story, epic or feature for the new application that needs data or tables, asks to design, extend or change the new schema or a bounded context, asks for a Flyway migration, asks how legacy data will move into the new tables, or wants to continue the modernization, even when they don't mention the legacy database.
---

# Domain modernization (Mode 2)

The new application is built gradually, one requirement at a time. Each requirement changes the target schema of one or more bounded contexts. This skill makes sure every change does three things:
1. **Fits what came before.** The contexts, ADRs and standards stored in memory.
2. **Stays anchored to the legacy schema.** Every target table and column says where its data comes from, or that it is new. Every legacy column eventually gets mapped or retired with a reason.
3. **Ships safely.** Lint and lineage gates, human approval, immutable Flyway migrations, and data-migration scripts with prechecks and reconciliation.

This skill depends on **schema-memory**: the session protocol and the `.dbmod/` layout. It also depends on the baseline produced by **legacy-assessment**. If `memory_status.py` reports that the legacy catalog is missing or not assessed, stop and run Mode 1 first. Designing without the baseline breaks the guarantee that legacy is always in view.

Below, `$MEM` is schema-memory's `scripts/` folder and `$DM` is this skill's `scripts/` folder. All scripts are standard-library Python and take `--root <app-repo>`.

## Workflow for one requirement

### 1. Load memory
```bash
python3 $MEM/memory_status.py --root <repo>
```
From the output, note:
- the existing contexts and their versions
- open requirements
- recent ADRs
- coverage

### 2. Capture the requirement
Read it from its source. Use a Jira or Confluence tool if one is connected; otherwise use pasted text or an export. See `references/requirements-intake.md`. Record it:
```bash
python3 $MEM/memory_record.py --root <repo> new REQ --title "..." --source PROJ-142 --body-file body.md
```
Leave `--contexts` off for now. Once step 4 has chosen the context(s), link them with `memory_record.py link REQ-n --contexts <ctx>`.
Write the REQ's **Data implications** section: entities, attributes, rules and volumes the requirement implies. Ask the product owner (the user) about real ambiguities now. A wrong guess here becomes a migration later.

### 3. Find the legacy evidence
For every implied entity or attribute, look in the legacy baseline:
```bash
python3 $MEM/memory_query.py --root <repo> search <term>          # try business words AND legacy abbreviations (cust, inv, amt)
python3 $MEM/memory_query.py --root <repo> table public.inv_hdr    # columns, findings, PII, current dispositions
python3 $MEM/memory_query.py --root <repo> domain <legacy-domain>
```
Fill in the REQ's **Legacy evidence** section with refs and finding ids. Note:
- which legacy data must be carried over
- which legacy quirks matter (text dates, flags, orphans)
- what is already owned by another context (`lineage` / `context` queries)

### 4. Decide where it lives
Choose which context the change belongs to:
- **Extend an existing context.** If it is accepted, run `context_tool.py bump <ctx> --requirement REQ-n`.
- **Create a new context:**
  ```bash
  python $DM/context_tool.py --root <repo> new <ctx> --legacy-domains <d1,d2> --description "..." --requirement REQ-n
  ```

Ownership follows the guidance in `references/modeling-guide.md` §3. A concept already owned elsewhere is referenced by id (`external_refs`), never re-modelled. If the choice is not obvious, explain the options and let the user decide, then record an ADR.

**Context names** are business names (`sales`, `billing`), not legacy ones. They must not be SQL reserved words, so `order` won't work. The legacy domain name stays in `legacy_domains`.

**When the owning context does not exist yet** (common for early requirements, e.g. an order that references customer, product and employee):
- Still declare `external_refs` to the future context and entity (e.g. `customer.customer`), with matching `lookup` mappings.
- Do **not** create placeholder contexts to silence the warnings.
- The gates report this as a warning, not an error, so acceptance can proceed.
- Record an ADR saying which upstream contexts are pending.
- Tell the user that this context's **data migration** cannot run until those contexts are designed and loaded. Its precheck will report those checks as `NOT RUN`.
- Recommend designing the upstream contexts next.

### 5. Design the model and the lineage
Edit two files, using the formats in `references/target-model-format.md` and the complete examples in `references/examples/`:
- `.dbmod/target/contexts/<ctx>/model.json`
- `.dbmod/mapping/<ctx>.json`

Then:
- Apply the legacy-to-target patterns in `references/modeling-guide.md` §2 for every legacy finding on the source tables.
- Run `context_tool.py add-audit <ctx>` to add the standard audit and row_version columns.
- Write `design.md` for the context: purpose and boundary, ubiquitous language, aggregates and invariants, legacy sources and how quirks are fixed, decisions.
- Map every target column, or mark it `new` or `generated`.
- Retire the legacy columns in this domain that the new design deliberately drops. Give each a reason, and an ADR when the reason is a policy (PCI, GDPR minimisation, dead data).
- When the design fixes a legacy finding (e.g. a FK now enforced, a text date now `date`), add `"resolves": ["F-0012"]` to that table or column mapping. Acceptance then marks those findings `resolved-in-target`. For one-off cases, use `$MEM/memory_record.py finding F-0012 resolved-in-target --note "..."`.

### 6. Run the gates and iterate until clean
```bash
python3 $DM/model_lint.py      --root <repo> --context <ctx>
python3 $DM/lineage_check.py   --root <repo> --context <ctx>
python3 $DM/check_conflicts.py --root <repo>
python3 $DM/generate_migration.py --root <repo> --context <ctx>      # preview SQL
python3 $DM/render_erd.py --root <repo> --target <ctx>               # Mermaid ERD of the target
```
Fix every ERROR. For each WARN, either fix it or justify it in `design.md`. The lineage check also lists:
- open high and critical legacy findings on the source tables (the design should resolve each one)
- pending legacy columns (mention any left open to the user)

### 7. Propose and get approval
```bash
python3 $DM/context_tool.py --root <repo> propose <ctx>
python3 $MEM/memory_record.py --root <repo> status REQ-n designed
```
Present the proposal to the user, in this order:
1. What the requirement needed.
2. The model change, as a table or ERD.
3. Legacy lineage highlights: what is carried over, transformed, or retired, and why.
4. Decisions and ADRs.
5. Risks: the precheck areas, and any destructive or locking changes.
6. The preview SQL.

**Do not accept on your own initiative.** Acceptance writes immutable Flyway history, so it needs the user's explicit approval and the approvers' names and roles. List the proposed ADRs that the design relies on, so the approval covers them too.

Before presenting, check the two human gates so nobody is surprised at acceptance:
- **Approvals:** `standards.json → approvals` decides which roles must sign. By default an `architect` always signs, and a `data-owner` must also sign when the model holds money or personal/financial data (`security` for credentials and special-category data). Tell the user which roles this model needs and why, and ask who fills each role. Never invent a name or a role.
- **Business gate:** every SME question on the context's legacy tables must be answered by the business (`review.py sme list --open`), and its legacy domains confirmed. Open questions block acceptance. If the business cannot answer yet, the only way forward is an ADR explaining why it is safe to proceed, recorded with `review.py sme waive` and a review date. Acceptance is blocked while a waiver in scope is overdue. Tell the user when a context is being accepted on a waiver: it is listed in `memory_status.py` until the business answers.

### 8. Accept (after approval)
```bash
python3 $DM/accept_model.py --root <repo> --context <ctx> --description <snake_summary> \
  --approved-by "<name>:architect" --approved-by "<name>:data-owner" --adrs ADR-0002,ADR-0003
```
`--approved-by` takes `"Name:role"` and is repeated once per approver; one person cannot fill two roles. `--adrs` lists the proposed ADRs the user approved along with the design (including any ADR behind an SME waiver). Leave out any they did not approve. Acceptance requires the model to be `proposed` (step 7).
This re-runs the gates (lint, lineage, conflicts, approvals, business gate) and then:
- writes `db/migration/V<ts>__<ctx>_<desc>.sql`
- freezes the snapshot
- marks the mappings and REQs accepted
- generates `db/data-migration/<ctx>/v<NNN>/` (run order and handling in `references/migration-guide.md`). Its `04_reconcile.sql` compares money raw legacy vs converted vs target, per row through the id maps, per parent and per month, at `data_migration.reconcile.money_scale` dp; `05_signoff.md` is the data owner's reconcile sign-off. Tell the user the migration is not done until every reconcile row is `OK` or explained, and the data owner has signed. Since v1.5 it also flags legacy NULLs that became values, compares totals per currency at each currency's decimal places, applies the rounding agreed with finance (`half-up`, `half-even`, `truncate`) and the business time zone, and runs the context's `reconcile_invariants` (e.g. invoice net = sum of lines). A deliberate unit or FX change is declared with `reconcile_raw` on the column mapping, so it is not reported as a mismatch.
- **A difference is not automatically a migration bug.** Sometimes legacy was wrong. Finance decides, never the migration team: record it with `reconcile_ruling.py add` (`new-correct` = legacy was wrong, the migrated value stands; `legacy-correct` = fix the mapping), for a stated reporting period. In a closed period the reported figure is not restated, so a disclosure is required. When finance says the parent is right (e.g. the invoice header) and the period is open, `--correct-with-line` adds a traced correcting line after the load (`03b_corrections.sql`), so the new system's invariant holds. When legacy stores refunds as positive amounts with a type flag, put the sign rule in the transform and in `reconcile_raw`. Regenerate the data migration afterwards; the rulings appear in `04_reconcile.sql` and `05_signoff.md`

Fill in the REQ's **Schema changes** section.

### 9. Close
```bash
python3 $MEM/memory_validate.py --root <repo>
python3 $MEM/memory_status.py --root <repo>
```
Report to the user:
- the files created (migration, data-migration folder)
- the coverage change
- open questions
- the next suggested step: a dependent context, or the precheck run against real legacy data

Remind them to commit `.dbmod/` and `db/` together in one PR.

## When the user asks for something smaller
- **"Add a column / small change":** still follow steps 4–8, just quickly. The bump, lineage, gates and approval are what keep the memory trustworthy.
- **"Just show me the SQL":** run steps 5–6 and show the preview. Explain that nothing is final until it is accepted.
- **"What would X look like?":** sketch the design in the conversation without writing memory, and offer to record it as a REQ.
- **Reconcile results from a real run:** for each row that is not `OK`, find out whether the migration or legacy is wrong. Fix the mapping yourself when the migration is wrong. When it may be legacy, ask the user to get finance's ruling and record it with `reconcile_ruling.py` (never rule yourself), then regenerate.
- **Precheck results from a real run:** treat each row as a data decision. Update the mapping filter or transform, or the model. Add an ADR, then run `generate_data_migration.py --context <ctx>` again. A mapping-only change doesn't need a model bump.

## Judgement notes
- **The requirement drives the design.** Legacy shows what data exists and what went wrong, not how the new schema should look. Don't copy legacy column lists.
- **Prefer additive evolution.** Use nullable columns or defaults, `renamed_from`, and new enum values. Destructive changes need an ADR and expand/contract planning.
- **Keep transforms honest.** If a transform guesses (e.g. "due date = issue + 30 when missing"), say so in `notes`, and in the REQ's open questions if the business hasn't confirmed it.
- **Large legacy estates:** work one legacy domain at a time with `memory_query.py`. Never load `catalog.json` whole.
