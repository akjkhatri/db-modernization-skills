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
   5. `04_reconcile.sql`: source vs target row counts and money sums. Differences must be explained by filters.
3. Keep `migration_map` after go-live. It is the audit trail from legacy keys to new ids, and support teams use it.

**Re-running:** truncate the context's target tables (children first), drop or keep its id maps, then run 01–04 again.

**Large tables:** for tens of millions of rows, split `03_load.sql` per table and batch by legacy key ranges. Load with indexes and FKs in place unless timings say otherwise. Run `ANALYZE` after loading.

**Performance of postgres_fdw:**
- Set `fetch_size` on the server (e.g. `OPTIONS (fetch_size '10000')`) and `use_remote_estimate 'true'` for joins.
- For very large volumes, consider staging the legacy tables with `pg_dump --data-only -t` into a local `legacy_src` schema instead of reading through the FDW.

## Verified behaviour
`tests/run_pg_test.sh` in the package runs the whole chain against a real PostgreSQL server:
- seeds a dirty legacy database
- applies the generated Flyway files
- migrates customer, then billing
- shows the precheck catching an orphan, which makes the load fail
- applies a mapping decision and regenerates
- reconciles with matching counts and sums
