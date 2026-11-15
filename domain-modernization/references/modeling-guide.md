# Modeling guide: legacy evidence → target design (PostgreSQL)

## Contents
1. Principles
2. Legacy pattern → target pattern
3. Bounded context and aggregate decisions
4. PostgreSQL design defaults
5. GDPR and security
6. Checklist before proposing

## 1. Principles
- **Requirement first, legacy as evidence.** The new application is a different system. Model what the requirement needs, in the business's language. Use legacy for three things: the data you must carry over, the business rules hidden in its structure, and the traps to avoid. Do not re-create legacy tables with nicer names.
- **Design for the data you will migrate.** Every target column is either backed by legacy data (with a known transform and known quality issues) or is new. Know which before you choose NOT NULL, unique and check constraints. Legacy data will violate optimistic constraints, and `02_precheck.sql` will tell you where.
- **One owner per concept.** A concept such as customer, product or order belongs to one context. Others hold its id (`external_refs`) plus, where history matters, a snapshot of the attributes they need, named as a snapshot (for example `customer_name_at_order`).
- **Make decisions explicit.** Anything a future reader would question goes in an ADR: a split, a retirement, a deviation from standards, or a time-zone assumption.

## 2. Legacy pattern → target pattern
| Legacy finding | Target design | Migration note |
|---|---|---|
| No PK (LA-KEY-001) | identity PK; business key as UNIQUE if one exists | `legacy_key` in table mapping; precheck shows duplicates |
| Unenforced relationship (LA-KEY-002) | real FK inside a context, or `external_ref` across contexts | precheck orphans; decide: filter, default, or "unknown" row |
| Business key as PK (LA-KEY-003) | surrogate `<table>_id` plus a UNIQUE business number | keep the number visible (e.g. `customer_number`) |
| Text dates (LA-TYP-001) | `date` / `timestamptz` | `transform` with `to_date(x,'YYYYMMDD')`; profile bad values first; NULLIF on blanks |
| money / float amounts (LA-TYP-002) | `numeric(19,4)` plus currency | `x::numeric`, `round(x::numeric, 4)`; reconcile sums |
| timestamp without tz (LA-TYP-003) | `timestamptz` | `x AT TIME ZONE 'Europe/London'`, per ADR |
| Y/N, 0/1 flags (LA-TYP-004) | `boolean NOT NULL DEFAULT`, or `<event>_at timestamptz` | explicit CASE mapping; unknown values → precheck |
| Coded values (LA-TYP-007) | lookup table with `seed`, or enum + CHECK | profile distinct values; translate in `transform` |
| Repeating groups (LA-STR-001) | child table (`customer_phone`) or a value-object column set | `custom_select` with `CROSS JOIN LATERAL (VALUES ...)` |
| EAV (LA-STR-002) | promote frequent attributes to columns; `jsonb` for the long tail | profile attribute frequency; pivot with `max(...) FILTER (WHERE attr_name = ..)` |
| Polymorphic link (LA-STR-003) | one link table per owner type, or nullable FKs + CHECK exactly-one | split by `entity_type` in `filter` |
| One-true-lookup (LA-STR-004) | one lookup per code type, owned by the context that uses it | `filter code_type = '...'` |
| Wide table (LA-STR-005) | split by cohesion into aggregate + value objects | several table mappings with the same driving table |
| Copied parent attributes (LA-STR-006) | drop (derive via join), or keep as an explicit snapshot | document the choice per column |
| Backup/archive/temp (LA-STR-007) | nothing, or a `history` table if needed | `retired` with `<table>.*` |
| Soft-delete flag (LA-STR-008) | migrate live rows only, or `deleted_at timestamptz` | `filter`; ADR on retention |
| Generic audit log (LA-STR-010) | platform audit or CDC; archive the legacy log | retire |
| Config table (LA-STR-011) | app config / feature flags; only real reference data in the DB | retire most keys with a reason |
| Credentials (LA-SEC-001) | identity provider; never migrate | retire with ADR |
| Card/bank data (LA-SEC-002) | PSP token only | retire with ADR |

## 3. Bounded context and aggregate decisions
- Start from the confirmed legacy domain. The legacy domain is a hint, not the answer. One legacy domain can become two contexts (e.g. order capture vs fulfilment), and two legacy domains can merge.
- **Aggregate** = what must be consistent in one transaction. The root gets `row_version`. Children reference the root with `ON DELETE CASCADE` only when they cannot exist alone.
- **Cross-context data:** use an id via `external_refs`, plus a snapshot of the attributes needed for history. No cross-schema FK by default. Integrity across contexts is enforced by the application or by events. The data migration still resolves those ids through the other context's `migration_map`, which is why contexts are loaded in dependency order.
- **Upstream context not designed yet:** declare the `external_ref` and the `lookup` mapping anyway. The gates warn but don't block. Record an ADR. The context's data migration waits until the upstream context exists and is loaded, and its precheck reports those checks as `NOT RUN`. Never create placeholder contexts. They pollute memory and fail their own lint.
- **Shared/hub legacy tables** (e.g. `cust_mst` used everywhere): exactly one context owns them. The others map only the key, as a `lookup`.
- **Shared kernel:** when several contexts need the same small code list, either one context owns it and publishes it, or each keeps its own copy. Choose and record an ADR.

## 4. PostgreSQL design defaults
These are the defaults from `standards.json`. `model_lint.py` enforces them.
- `bigint GENERATED ALWAYS AS IDENTITY` PKs named `<table>_id`. Add a `public_id uuid DEFAULT gen_random_uuid()` when the entity is exposed outside its context.
- `timestamptz` everywhere; `numeric(19,4)` for money with a `character(3)` currency; `text` with a CHECK instead of arbitrary `varchar(n)`.
- NOT NULL by default. Nullable means "genuinely optional", not "legacy had nulls". If legacy had nulls, decide on a default, a filter or an "unknown" value, and note it.
- Audit columns `created_at/by`, `updated_at/by` on every non-lookup table, plus `row_version` on roots.
- Name CHECK constraints for business rules (`due_after_issue`). Use partial indexes for common filtered queries.
- Use `jsonb` only for opaque or long-tail payloads.
- Enums only for small, code-owned state machines. Use lookup tables with `seed` for business-managed lists.

## 5. GDPR and security
- Classify every column (`pii`). Legacy PII cannot be mapped into a `pii: none` column; `lineage_check` blocks it.
- **Data minimisation:** a new design should not carry over personal data with no current purpose. Retire it with a reason and ask the user to confirm with their DPO.
- Special-category data (health, ethnicity, …) needs a lawful basis and a DPIA. Isolate it in its own table with restricted grants and say so in `design.md`.
- Add a `retention` note on tables holding personal or financial data. It is written into the table comment.

## 6. Checklist before proposing
- [ ] Every table has a description, a role and an aggregate. The PK and audit columns follow the standards.
- [ ] `model_lint.py` shows 0 errors, and each warning is explained in `design.md`.
- [ ] `lineage_check.py` shows 0 errors. Pending legacy columns in this context's domains are either handled or listed as open questions.
- [ ] `check_conflicts.py` shows 0 errors. Duplicated concepts across contexts are deliberate and documented.
- [ ] High and critical legacy findings on the source tables are addressed in the design or explicitly accepted.
- [ ] REQ markdown updated (data implications, legacy evidence, schema changes). ADRs written.
- [ ] Preview migration reviewed (`generate_migration.py`).
