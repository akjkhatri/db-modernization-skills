# Flyway and data-migration guide

## Schema migrations (Flyway)
- Only `accept_model.py` writes versioned files, into `<repo>/<standards.flyway.location>` (default `db/migration`). The name is `V<UTC timestamp>__<context>_<description>.sql`. The timestamp is forced above the latest existing version, so versions stay unique and increasing even when several contexts are accepted on the same day.
- Each file covers one context (`one_context_per_migration`). Never edit an applied file; bump the model and generate a new one.
- `pending/preview.sql` is a scratch preview for review. It is never applied.
- Flyway config notes for the team:
  - `flyway.locations=filesystem:db/migration`
  - `flyway.schemas` should list every context schema, or leave it unset and let migrations create their own schemas.
  - With feature branches merging out of order, enable `outOfOrder=true` in dev, or rebase so the newer timestamp lands last.
- PostgreSQL can't run `ALTER TYPE … ADD VALUE` in the same transaction that then uses the new value. If a migration both adds and uses an enum value, split it into two migrations (accept twice).

### Changes that need care
| Change | Generated as | Watch out |
|---|---|---|
| Add NOT NULL column without default | ADD COLUMN … NOT NULL | Fails on populated tables. Add a default or do it in two steps. |
| Type change | ALTER COLUMN TYPE … USING col::type | Rewrites the table and locks it. Check the cast. |
| SET NOT NULL | ALTER COLUMN … SET NOT NULL | Fails if NULLs exist; scans the table |
| Rename (`renamed_from`) | RENAME | The app must deploy in step (expand/contract preferred) |
| Drop column/table | only with `--allow-destructive` | Needs an ADR and a backup; do it after the app stops using it |

## Data migration (one-off legacy → new)
The new application is a different system, so there is no long-running sync. Instead, each context gets re-runnable ETL scripts generated from its lineage: `<repo>/db/data-migration/<ctx>/v<NNN>/`.

**Run order per environment**
1. Apply all Flyway migrations, so the new schemas exist.
2. For each context, in dependency order (`check_conflicts.py` prints it, e.g. customer before billing):
   1. `00_prerequisites.sql`: once per database. Set up `postgres_fdw` server and user mapping, with credentials from a vault and never committed. It imports only the legacy tables this context needs, into `legacy_src`.
   2. `01_id_maps.sql`: pre-allocates new ids for every legacy key, in `migration_map.<schema>__<table>`. Idempotent.
   3. `02_precheck.sql`: one result row per data issue. Every check runs on the exact projection that `03_load.sql` inserts:
      - transform and cast failures (bad dates, invalid enum values)
      - NULLs into NOT NULL columns, including unresolved lookups
      - CHECK and UNIQUE violations
      - codes missing from seeded reference tables
      - duplicate legacy keys and orphans (shown with the legacy value)

      Each check runs on its own. A check that depends on a context not loaded yet shows as `NOT RUN` instead of failing the whole file. **An empty result means the load can run.** Every row is a decision: fix at source, add a `filter`, change a transform to default, or relax the model. Record the decision in an ADR, update the mapping, and regenerate with `generate_data_migration.py`.
   4. `03_load.sql`: a single transaction with INSERT … SELECT in FK order, `OVERRIDING SYSTEM VALUE` with the pre-allocated ids.
   5. `04_reconcile.sql`: the reconcile report. One row per check, each with a status (`OK`, `MISMATCH`, `NOT RUN`, `FAILS`). Money is compared at `data_migration.reconcile.money_scale` decimal places (default 2, to the penny):
      - **row count** per table, with any mapping filter shown in the note
      - **money total**: the raw legacy sum, the converted sum (after transforms) and the target sum, side by side. If a transform changes the total (rounding, truncation, a wrong sign), the status is `MISMATCH (conversion changes the total)`, even though converted and target agree. Derived columns (built from several legacy columns) have no raw value, so they are compared after conversion and the note says so
      - **money per row**: every legacy row is followed through its id map to its target row, and each money value is compared. The report gives the number of differing rows, the largest difference and example legacy keys. Tables with no legacy key or identity id map show `NOT RUN`
      - **money per parent**: totals per foreign-key parent resolved by lookup (e.g. invoice lines per invoice, payments per invoice)
      - **money per month**: totals per calendar month of the table's first business date column. Drift that cancels out across the whole table still shows here

      Turn checks off or change the scale in `standards.json` (`per_row`, `per_parent`, `per_month`, `money_scale`, `examples`). Projects created before v1.3 use the defaults and print a NOTE.
   6. `05_signoff.md`: paste the reconcile output in. Every row that is not `OK` is either fixed (mapping change, regenerate, re-run) or explained (a documented filter, or an ADR) and accepted. **The data owner signs it before go-live**, and the decision is recorded in `.dbmod/decisions/`.
3. Keep `migration_map` after go-live. It is the audit trail from legacy keys to new ids, and support teams use it.

**Re-running:** truncate the context's target tables (children first), drop or keep its id maps, then run 01–04 again and update the sign-off.

**Large tables:** for tens of millions of rows, split `03_load.sql` per table and batch by legacy key ranges. Load with indexes and FKs in place unless timings say otherwise. Run `ANALYZE` after loading.

**Performance of postgres_fdw:**
- Set `fetch_size` on the server (e.g. `OPTIONS (fetch_size '10000')`) and `use_remote_estimate 'true'` for joins.
- For very large volumes, consider staging the legacy tables with `pg_dump --data-only -t` into a local `legacy_src` schema instead of reading through the FDW.

## Parallel run (recommended for money)
Reconcile proves the migrated data matches legacy at the moment of migration. A parallel run proves the new application calculates the same way. Template:

| Item | What to fill in |
|---|---|
| Period | e.g. one full billing cycle or month end, chosen with the data owner |
| Scope | the calculations that move money: invoice totals, VAT, balances, interest, fees |
| Inputs | the same real transactions entered or replayed in both systems |
| Compare | totals per account (parent) and per period, at the same `money_scale` as reconcile |
| Tolerance | normally zero. Any non-zero tolerance is a data-owner decision recorded in an ADR |
| Differences | each one explained: legacy bug (ADR), new bug (fix), or accepted rule change (ADR) |
| Exit | data-owner sign-off, recorded in the `Parallel run` section of `05_signoff.md` |

## Verified behaviour
`tests/run_pg_test.sh` in the package runs the whole chain against a real PostgreSQL server:
- seeds a dirty legacy database
- applies the generated Flyway files
- migrates customer, then billing
- shows the precheck catching an orphan, which makes the load fail
- applies a mapping decision and regenerates
- reconciles with every check `OK`: counts, raw vs converted vs target totals, per row, per invoice and per month
- **drift test:** changes a transform to round line amounts to 1 dp (67.08 becomes 67.10). Converted and target totals still agree, so a converted-vs-target sum alone would pass. The reconcile report flags the total, the one row (legacy key `5002|1`) and its invoice (`5002`)
