# Assessment rule catalogue (rules_version 1.0)

Each rule is a heuristic based on names and catalog metadata, not on data. Treat findings as hypotheses to confirm. In the report, recommend profiling queries (section "Profiling queries" below) for the high-impact ones.

**Severity guide**
- **critical**: blocks a safe migration, or is a legal/security exposure.
- **high**: certain data-quality or design problems that the target model must address.
- **medium**: should be fixed in the target.
- **low** or **info**: hygiene.

Table health score = 100 minus the sum of the table's finding weights, where critical 25, high 10, medium 4, low 1.

| Rule | Sev | What it detects | Target-design implication |
|---|---|---|---|
| LA-KEY-001 | critical | Table without primary key | Find the natural key, dedupe; identity PK in target |
| LA-KEY-002 | high | Relationships not enforced (inferred only) | Expect orphans; profile; enforce FK in target |
| LA-KEY-003 | low | Business key used as PK on a large table | Surrogate PK + UNIQUE business key |
| LA-KEY-004 | low | serial / nextval default | GENERATED ALWAYS AS IDENTITY; reseed after load |
| LA-KEY-005 | medium | Isolated table (no relationships) | Retire, standalone context, or technical |
| LA-KEY-006 | medium | `*_id`/`*_no` column that resolves to nothing | Ask SME; add as manual relationship |
| LA-NAM-001 | medium | Not lower snake_case (quoted identifiers) | snake_case in target; quote in migration SQL |
| LA-NAM-002 | low | tbl_/t_/v_ prefixes | Drop prefixes |
| LA-NAM-004 | medium | Reserved word as column name | Rename |
| LA-NAM-005 | low | Generic names (col1, attr3, data) | Profile content before mapping |
| LA-TYP-001 | high | Dates/times stored as text | date/timestamptz; expect invalid values |
| LA-TYP-002 | high | `money` type, or float for monetary values | numeric(19,4) + currency |
| LA-TYP-003 | medium | timestamp without time zone | timestamptz; ADR on source time zone |
| LA-TYP-004 | medium | Boolean as Y/N, 0/1, char(1) | boolean / timestamptz |
| LA-TYP-005 | low | char(n) for variable text | text; trim |
| LA-TYP-006 | low | json not jsonb | jsonb or model it |
| LA-TYP-007 | medium | Coded values without lookup | Lookup table or enum + CHECK |
| LA-STR-001 | high | Repeating groups (phone1..3) | Child table / value-object collection |
| LA-STR-002 | high | Entity-Attribute-Value | Promote frequent attributes; jsonb for the tail |
| LA-STR-003 | high | Polymorphic association (x_type + x_id) | Explicit FKs or link table per owner |
| LA-STR-004 | medium | One-true-lookup-table | One lookup per code type, owned by its context |
| LA-STR-005 | medium | Wide table (> 40 cols by default) | Split by cohesion into aggregates/value objects |
| LA-STR-006 | medium | Attributes copied from a parent | Snapshot (rename, keep) or derived (drop) |
| LA-STR-007 | high | Backup/archive/temp/duplicate table | Retire, or migrate as history |
| LA-STR-008 | medium | Soft-delete flag | Migrate live rows only, or deleted_at; ADR |
| LA-STR-009 | low | Tables without audit columns (catalog-level) | Standard audit columns; backfill rule |
| LA-STR-010 | medium | Generic audit log with text old/new values | Archive; do not migrate into domain schema |
| LA-STR-011 | low | Key-value configuration table | Classify each key: config / flag / reference data |
| LA-STR-012 | info | View | Re-implement as query / read model |
| LA-STR-013 | medium/low | Most columns nullable; nullable relationship columns | Profile null rates; enforce NOT NULL |
| LA-SEC-001 | critical | Credentials (pwd, token) in app tables | Never migrate; identity provider |
| LA-SEC-002 | high | Card / bank data | PCI scope; tokenise |
| LA-SEC-003 | high | GDPR Art. 9 special category | DPIA, lawful basis, isolate + encrypt |
| LA-SEC-004 | medium | National identifiers (NINO, passport) | Restrict, encrypt |
| LA-DOC-001 | low | Undocumented tables (catalog-level) | Capture meaning in design.md |

## Relationship inference
Rules are tried in order:
1. Column name equals the single-column PK name of another table (confidence 0.9).
2. A `<stem>_id|_cd|_no|_code` column where the stem matches a table name, or that name with `s`, `_mst` or `tbl_` (0.75).
3. A self-reference prefix (`parent_`, `mgr_`, `manager_`, `prev_` …) pointing at the table's own PK (0.6–0.7).

Adjustments:
- Type-family mismatch: −0.4.
- Ambiguous (several candidate tables): −0.2.
- Anything below `--min-confidence` (default 0.5) is dropped.

Reviewers confirm or reject relationships with `review.py rel ...`. Their decisions survive re-runs.

## PII detection
Detection is based on column-name patterns for these classes:
- `credential`
- `financial`: card, bank, salary
- `special_category`: health, ethnicity, religion, sexuality, union, biometric
- `personal`: names, contact details, DOB, address, national identifiers, IP

It does not see free-text columns that contain PII (notes, comments). Raise those as SME questions.

## Data rules (LA-DAT, from profile_data.py)
These come from profiling the data, not the catalog. Findings are `DP-0001`… in `.dbmod/legacy/profile.json`.

| rule | severity | what | SME question |
|---|---|---|---|
| LA-DAT-001 | high | orphan rows: child key values with no parent (declared or inferred relationship) | fix at source, placeholder parent, or archive? |
| LA-DAT-002 | high / medium | duplicate values of a key other tables rely on; exact duplicate rows in a table with no primary key | which row is real, or genuine repeats? |
| LA-DAT-003 | medium | placeholder dates (`profiling.placeholder_dates`, also as text like 19000101) | unknown, not applicable, or open-ended? |
| LA-DAT-004 | medium | code values that differ only by case or spaces (gbp / GBP) | the same code? |
| LA-DAT-005 | low | leading or trailing spaces | none: trim in the transform |
| LA-DAT-006 | medium | text dates that don't parse (columns named *_dt, *date*) | what should they become? |
| LA-DAT-007 | medium / low | mostly NULL (≥ `profiling.mostly_null_percent`) / always NULL | one question per table: still used? |
| LA-DAT-008 | medium | amounts with more decimals than `profiling.money_decimals` | which rounding rule does finance use? |
| LA-DAT-009 | low | both NULL and empty string used | do they mean the same? |
| LA-DAT-010 | low | empty table | none: listed as a retirement candidate |

## Profiling queries
`profile_data.py plan` generates all of these. The hand-written versions below are for a quick look or another database. They are read-only and run against the legacy database.
```sql
-- orphan rows behind an inferred relationship
SELECT count(*) FROM legacy.child c LEFT JOIN legacy.parent p ON p.pk = c.fk WHERE c.fk IS NOT NULL AND p.pk IS NULL;
-- duplicate natural keys in a table without PK
SELECT k1, k2, count(*) FROM legacy.t GROUP BY 1,2 HAVING count(*) > 1 ORDER BY 3 DESC LIMIT 50;
-- invalid text dates
SELECT count(*) FILTER (WHERE col !~ '^\d{8}$') AS bad, count(*) FROM legacy.t;
-- distinct codes behind a code column
SELECT col, count(*) FROM legacy.t GROUP BY 1 ORDER BY 2 DESC;
-- real null rate per column
SELECT count(*) FILTER (WHERE col IS NULL)::numeric / count(*) FROM legacy.t;
-- EAV attribute distribution
SELECT attr_name, count(*), count(DISTINCT entity_id) FROM legacy.eav GROUP BY 1 ORDER BY 2 DESC;
```
