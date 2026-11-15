# Requirements intake (Jira / Confluence)

Requirements for the new application arrive one at a time. Each becomes a `REQ-nnnn` record in memory. The record is the anchor that every schema change, mapping and ADR cites. It is how someone will later answer "why does this column exist?".

## Where requirements come from
| Source | How to read it |
|---|---|
| Jira issue (story/epic) | Use a connected Atlassian/Jira tool if the session has one: fetch the issue with its description, acceptance criteria, linked issues and attachments. Otherwise ask the user to paste the issue text, or export it (Jira → ⋯ → Export → Word/XML/HTML) and give you the file. |
| Confluence page (BRD, spec) | Use a connected Atlassian/Confluence tool if present. Otherwise ask for a page export (⋯ → Export → Word/PDF/HTML) or pasted text. Tables in specs often hold field lists, so read them carefully. |
| Pasted text, email, meeting notes | Fine. Record the person or meeting as the source. |

If the user names a Jira key or Confluence URL and no tool can read it, say so in one line and ask for the content. Don't guess what a ticket says.

## Recording
```bash
python $MEM/memory_record.py --root <repo> new REQ --title "<short title>" --source PROJ-142 --contexts billing --body-file req_body.md
```
- `--source` is the Jira key or Confluence URL, so the trail leads back to the original.
- Put the requirement text (or a faithful summary, if it is long) in the body.

Then fill in the REQ markdown sections. These are what the next session reads:
1. **Data implications.** Entities, attributes, rules, cardinalities, volumes, reporting needs, retention. Extract them from acceptance criteria, e.g. "an invoice can be partially paid" means a payment allocation concept and a NOT NULL outstanding amount.
2. **Legacy evidence.** For each implied entity or attribute, what the legacy schema already holds (via `memory_query.py search/table`), with references (`public.inv_hdr.paid_flg`) and the relevant findings.
3. **Schema changes.** Filled in after design: context, model version, tables and columns touched, migration file.
4. **Open questions.** Ambiguities for the product owner. Ask them now rather than guessing in the schema.

Update the status as work progresses:
- `analysed` after the data implications are written
- `designed` when the model change is proposed
- `accepted` (set by `accept_model.py`)
- `implemented` once the app ships it

## Splitting and grouping
- A requirement can touch several contexts. Link it to all of them (`memory_record.py link`). The schema change happens per context, each with its own migration.
- Several small requirements can be designed together in one model version. List them all in `model.requirements`.
- A requirement with no data impact (pure UI or behaviour) still gets recorded if it was brought to this skill. Mark it `rejected` with the note "no schema impact", so the next session doesn't redo the analysis.
