# Getting the legacy catalog in

## Contents
1. Supported inputs
2. URL inputs
3. Unusual layouts: inspect, then map
4. Best-quality export (give this to the DBA)
5. Re-ingesting

## 1. Supported inputs
`ingest_catalog.py` accepts any number of `--input` files (HTML or CSV) and `--url` pages, and merges them into one catalog. It recognises these layouts on its own:

| Layout | Typical source | How it's recognised |
|---|---|---|
| One row per column, with a table-name column | information_schema export, spreadsheet, wiki table | headers like `table_name`, `column_name`, `data_type` |
| One HTML table per legacy table, name in the heading above it | SchemaSpy, DBeaver docs, dbdocs, Confluence export | headers like `Column Name`, `Data Type`; table from the `<h1..h6>`/`<caption>` before it (`Table: public.cust_mst` works) |
| Table summary (one row per table) | catalog index page | `table` plus `rows`/`description` but no column header. Gives comments and row counts. |
| Foreign-key list | `pg_constraint` export | `table`, `column`, `foreign_table`, `foreign_column` |

Keys can be given in several forms:
- Boolean columns: `pk`, `unique`
- A combined `Key`/`Constraints` column: `PK`, `FK`, `UQ`, `PRI`, `UNIQUE`, `FK → cust_mst.cust_id`
- Separate `ref_table`/`ref_column` columns
- A `References` column: `public.cust_mst(cust_id)`, `cust_mst.cust_id`

Nullability can be `YES/NO`, `Y/N`, `true/false`, `NULL/NOT NULL`, or a ✓ in a "Not Null" column.

## 2. URL inputs
`--url` works when the page is reachable from where the script runs and needs no login. Intranet pages usually need sign-in or VPN. In that case, in order of preference:
1. Ask the user to save the page (browser → Save as → "Web page, HTML only") into the repo or a folder you can read, then use `--input`.
2. If a browser tool is available in the session and the user is signed in, open the page and save the HTML source, or extract its tables, to a file. Then use `--input`.
3. If the catalog page lives in Confluence, a connected Atlassian tool can fetch it. Save the body HTML to a file and use `--input`.

Every input is copied into `.dbmod/legacy/sources/` with its sha256, so the baseline can always be reproduced.

## 3. Unusual layouts: inspect, then map
Always run `--inspect` first on a new export. It lists every grid it found, how it classified the grid (`columns`, `tables`, `fks`, `unknown`), the header mapping, and sample parsed tables. Nothing is written.

If a header is not recognised or is mapped wrongly, write a mapping file and pass it with `--mapping`:
```json
{
  "headers": {"Fld": "column", "Fld Type": "type", "Mandatory?": "not_null", "Remarks": "comment", "Name": "table"},
  "default_schema": "public",
  "skip_tables": [0, 5]
}
```
Canonical fields are:
`schema table column ordinal type length precision scale nullable not_null default pk unique key fk_ref ref_table ref_column ref_schema comment table_comment row_count kind`.

`skip_tables` takes grid indexes from the `--inspect` output. Use it for legends, change logs and other non-catalog tables. The mapping is stored in `catalog.json`, so a re-ingest can reuse it.

Sanity-check the inspect summary before ingesting. If the table count, column count, number of tables with a PK, or declared FK count looks wrong, fix the mapping before going further.

## 4. Best-quality export (give this to the DBA)
HTML documentation pages often leave out constraints, defaults and row counts. If the DBA can run one read-only query, this CSV gives the richest catalog. Columns are named the way the ingester expects.

```sql
-- Run in psql:  \copy ( <query below> ) TO 'legacy_catalog.csv' WITH CSV HEADER
SELECT n.nspname                          AS table_schema,
       c.relname                          AS table_name,
       CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' ELSE 'table' END AS table_type,
       c.reltuples::bigint                AS row_count,
       obj_description(c.oid, 'pg_class') AS table_comment,
       a.attnum                           AS ordinal_position,
       a.attname                          AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable,
       pg_get_expr(ad.adbin, ad.adrelid)  AS column_default,
       (SELECT string_agg(CASE con.contype WHEN 'p' THEN 'PK' WHEN 'u' THEN 'UQ' WHEN 'f' THEN 'FK' END, ',')
          FROM pg_constraint con
         WHERE con.conrelid = c.oid AND a.attnum = ANY (con.conkey) AND con.contype IN ('p','u','f')) AS constraint_type,
       (SELECT fn.nspname FROM pg_constraint con JOIN pg_class fc ON fc.oid = con.confrelid
          JOIN pg_namespace fn ON fn.oid = fc.relnamespace
         WHERE con.conrelid = c.oid AND con.contype = 'f' AND con.conkey = ARRAY[a.attnum] LIMIT 1) AS ref_schema,
       (SELECT fc.relname FROM pg_constraint con JOIN pg_class fc ON fc.oid = con.confrelid
         WHERE con.conrelid = c.oid AND con.contype = 'f' AND con.conkey = ARRAY[a.attnum] LIMIT 1) AS foreign_table_name,
       (SELECT fa.attname FROM pg_constraint con
          JOIN pg_attribute fa ON fa.attrelid = con.confrelid AND fa.attnum = con.confkey[1]
         WHERE con.conrelid = c.oid AND con.contype = 'f' AND con.conkey = ARRAY[a.attnum] LIMIT 1) AS foreign_column_name,
       col_description(c.oid, a.attnum)   AS description
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef ad ON ad.adrelid = c.oid AND ad.adnum = a.attnum
WHERE c.relkind IN ('r','p','v','m')
  AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY 1, 2, a.attnum;
```
Multi-column foreign keys appear as `FK` without a reference. Export them separately if they matter:
```sql
SELECT n.nspname AS table_schema, c.relname AS table_name, a.attname AS column_name,
       fn.nspname AS ref_schema, fc.relname AS foreign_table_name, fa.attname AS foreign_column_name, con.conname AS key
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class fc ON fc.oid = con.confrelid JOIN pg_namespace fn ON fn.oid = fc.relnamespace
JOIN LATERAL unnest(con.conkey, con.confkey) AS k(col, fcol) ON true
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.col
JOIN pg_attribute fa ON fa.attrelid = fc.oid AND fa.attnum = k.fcol
WHERE con.contype = 'f';
```
Pass both files: `--input legacy_catalog.csv --input legacy_fks.csv`.

Stored procedures, functions and triggers often hold business rules the new app has to re-implement. If the DBA can export `pg_get_functiondef` output, keep it under `.dbmod/legacy/sources/` and read the relevant functions during domain modernization.

## 5. Re-ingesting
Re-run ingest when a better or newer export arrives. The previous catalog is kept as `catalog.<timestamp>.prev.json` and the diff is logged.

Removed tables and columns matter: if lineage already maps them, `memory_validate.py` will report errors. Resolve each one as either the export being incomplete or a real legacy change.

After re-ingesting, re-run `analyze_schema.py`, `discover_domains.py` and `build_report.py`. Review decisions are kept: confirmed relationships, finding statuses, confirmed domains and glossary terms.
