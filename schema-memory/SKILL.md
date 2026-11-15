---
name: schema-memory
description: Persistent project memory for legacy database modernization. It stores the legacy schema baseline, findings, domains, target bounded-context models, legacy-to-target lineage, requirements and ADRs in a .dbmod/ folder in the application repo. Use it at the start and end of every legacy-assessment or domain-modernization session, and whenever the user asks what has been decided so far, the modernization status, legacy coverage, where a legacy table or column went, which requirement caused a schema change, or to log a decision. Also use it to initialise modernization memory in a new repo.
---

# Schema modernization memory

Skills keep nothing between conversations, but this project runs for months: requirements arrive one by one, and each new schema has to fit the legacy baseline and everything decided before. This skill gives the project that memory. It is a version-controlled `.dbmod/` folder in the application repo, plus scripts that read and write it consistently.

The other two skills, **legacy-assessment** (Mode 1) and **domain-modernization** (Mode 2), depend on it. The full file contract is in `references/memory-spec.md`. Read it the first time you work in a repo, or whenever you are about to hand-edit a memory file.

Scripts are standard-library Python 3.9+, run from this skill's `scripts/` folder. All of them accept `--root <app-repo>`. Without it they walk up from the current directory looking for `.dbmod/`.

## Session protocol (every time)

**Start:** load the memory before reasoning about any schema.
```bash
python3 scripts/memory_status.py --root <repo>
```
This prints a compact briefing: phase, legacy stats, domains, target contexts and versions, coverage, open requirements, ADRs and recent activity. Treat it as ground truth. If something in the conversation contradicts it, say so and ask; do not silently overwrite. Then pull only the detail you need:
```bash
python3 scripts/memory_query.py --root <repo> search invoice
python3 scripts/memory_query.py --root <repo> table public.inv_hdr       # columns, keys, findings, PII, where each column went
python3 scripts/memory_query.py --root <repo> domain billing
python3 scripts/memory_query.py --root <repo> unmapped --domain billing
python3 scripts/memory_query.py --root <repo> lineage public.inv_hdr.inv_no
python3 scripts/memory_query.py --root <repo> context billing
```
Legacy catalogs can have thousands of tables, so use these queries instead of opening `catalog.json` directly.

**End:** validate before telling the user you are done.
```bash
python3 scripts/memory_validate.py --root <repo>
```
Fix every ERROR. Report any WARN items to the user that need a human decision.

## No memory yet?
If `memory_status.py` reports that no `.dbmod/` exists, ask where the application repo is (the user wants memory in the app's git repo), then run:
```bash
python3 scripts/memory_init.py --root <repo> --project "<name>" [--flyway-location db/migration]
```
Init is idempotent and never overwrites existing memory. Next, tell the user to run the legacy assessment (Mode 1). Designs made without the legacy baseline cannot be traced back to legacy, so lineage would be lost.

## Recording requirements and decisions
Use the script so ids, the index, the journal and the changelog stay consistent:
```bash
python3 scripts/memory_record.py --root <repo> new REQ --title "Monthly invoicing" --source PROJ-142 --contexts billing --body-file req.md
python3 scripts/memory_record.py --root <repo> new ADR --title "Money as numeric(19,4)" --contexts billing --related REQ-0003
python3 scripts/memory_record.py --root <repo> status REQ-0003 designed --note "model v2 proposed"
python3 scripts/memory_record.py --root <repo> list REQ --status captured
```
After creating a record, open the generated markdown file and fill in its sections (data implications, legacy evidence, decision, consequences). A record with an empty body is not useful to the next session.

Write an ADR whenever a choice would surprise someone later. Examples:
- splitting or merging legacy entities
- retiring legacy data
- deviating from `standards.json`
- choosing which context owns a shared concept
- any cross-context dependency

## What you may edit by hand
Edit these files by hand, following `references/memory-spec.md`:
- `model.json` and `design.md`
- `mapping/<ctx>.json`
- the name, description, status and table assignment fields in `legacy/domains.json`
- relationship and finding `status` fields
- REQ and ADR bodies

Everything else is written by scripts. Never edit:
- `catalog.json` (re-ingest instead)
- snapshots
- applied Flyway migrations
- `journal.jsonl`

When you change standards, edit `standards.json` and record the reason in an ADR.

## Answering "where are we?" questions
For status, coverage, "what happened to legacy column X" or "why does table Y exist", answer from `memory_status.py` and `memory_query.py` output. Cite REQ, ADR and context versions so the user can follow the trail. Coverage percentages show how much of the legacy estate has a decided fate. That is the main progress measure for the modernization.
