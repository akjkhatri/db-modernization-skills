# Target model and lineage formats

## Contents
1. model.json (one per bounded context)
2. mapping/<ctx>.json (lineage)
3. Worked example
4. Evolving an accepted model

## 1. model.json
Path: `.dbmod/target/contexts/<ctx>/model.json`. Create it with `context_tool.py new`, then edit it.

```json
{
  "context": "billing", "schema": "billing",
  "description": "Invoicing, payments and credit notes.",
  "version": 1, "status": "draft", "accepted_version": null,
  "legacy_domains": ["invoice"],
  "requirements": ["REQ-0002"],
  "tables": [ { ...table... } ],
  "enums": [ {"name": "invoice_status", "values": ["issued", "paid", "cancelled"]} ],
  "external_refs": [ {"column": "invoice.customer_id", "context": "customer", "entity": "customer", "mode": "id"} ]
}
```
| Field | Meaning |
|---|---|
| `status` | `draft` → `proposed` (context_tool propose) → `accepted` (accept_model only) |
| `legacy_domains` | legacy domains this context draws from. Drives coverage and ownership warnings. |
| `requirements` | every REQ that shaped this version |
| `external_refs` | columns holding ids owned by another context. No FK is created. Use these instead of cross-schema foreign keys. |

**Table**
```json
{
  "name": "invoice", "role": "root", "aggregate": "Invoice",
  "description": "Invoice issued to a customer for an order.",
  "retention": "6 years (HMRC)",
  "renamed_from": null,
  "columns": [
    {"name": "invoice_id", "type": "bigint", "identity": true, "nullable": false, "pii": "none", "description": "surrogate key"},
    {"name": "status", "type": "invoice_status", "nullable": false, "pii": "none", "description": "lifecycle"},
    {"name": "net_amount", "type": "numeric(19,4)", "nullable": false, "pii": "none", "description": "net total"},
    {"name": "currency_code", "type": "character(3)", "nullable": false, "default": "'GBP'", "pii": "none", "description": "ISO 4217"}
  ],
  "primary_key": ["invoice_id"],
  "unique": [{"columns": ["invoice_number"]}],
  "checks": [{"name": "due_after_issue", "expression": "due_date >= issue_date"}],
  "foreign_keys": [{"columns": ["sales_channel_code"], "references": {"table": "sales_channel", "columns": ["code"]}, "on_delete": "restrict"}],
  "indexes": [{"columns": ["status", "due_date"], "where": "status = 'issued'"}],
  "seed": []
}
```
- `role` is one of `root`, `entity`, `value`, `lookup`, `link`, `history`. Lookup and link tables are exempt from audit columns. Roots need `row_version`.
- Column `type` is a PostgreSQL type, or the name of an enum in this model. `default` is raw SQL: `"now()"`, `"'GBP'"`, `"0"`.
- `pii` is one of `none`, `personal`, `special_category`, `financial`, `credential`. Personal, special-category and financial data need a `retention` on the table or column.
- `renamed_from` (on a table or column) makes the generator emit `RENAME` instead of a drop plus add.
- `seed` (normally on `lookup` tables, e.g. `[{"code": "card", "label": "Card", "is_active": true}]`) holds reference rows inserted by the Flyway migration (`ON CONFLICT DO NOTHING`). Rows added in later versions are inserted by later migrations.
- FK `references.table` is a table in the **same** model. Indexes on FK columns are created automatically.
- Constraint and index names come from `standards.json` (`pk_`, `fk_`, `uq_`, `ck_`, `ix_`).

## 2. mapping/<ctx>.json
```json
{
  "context": "billing",
  "table_mappings": [
    {"target": "billing.invoice", "sources": ["public.inv_hdr"], "grain": "one row per inv_hdr",
     "legacy_key": ["inv_no"], "filter": "inv_hdr.inv_dt >= DATE '2015-01-01'"},
    {"target": "billing.invoice_line", "sources": ["public.inv_dtl", "public.prod_mst"],
     "joins": [{"table": "public.prod_mst", "type": "left", "on": "prod_mst.prod_cd = inv_dtl.prod_cd"}]},
    {"target": "billing.payment_method", "sources": [], "notes": "reference data seeded via model seed rows"}
  ],
  "column_mappings": [
    {"target": "billing.invoice.invoice_number", "sources": ["public.inv_hdr.inv_no"], "rule": "transform",
     "transform": "'INV-' || lpad(inv_hdr.inv_no::text, 8, '0')", "requirements": ["REQ-0002"], "notes": ""},
    {"target": "billing.invoice.vat_amount", "sources": ["public.inv_hdr.vat_amt"], "rule": "transform",
     "transform": "round(coalesce(inv_hdr.vat_amt,0)::numeric, 4)", "resolves": ["F-0049"]},
    {"target": "billing.invoice.customer_id", "sources": ["public.inv_hdr.cust_id"], "rule": "lookup", "lookup": "customer.customer"},
    {"target": "billing.invoice.issue_date", "sources": ["public.inv_hdr.inv_dt"], "rule": "direct"},
    {"target": "billing.payment.provider_token", "sources": [], "rule": "new", "notes": "PSP token, no legacy equivalent"}
  ],
  "retired": [
    {"source": "public.pymt.card_no", "reason": "PCI: never migrate PAN", "decision": "ADR-0002"},
    {"source": "public.inv_hdr_bak.*", "reason": "backup copy of inv_hdr"}
  ]
}
```

**Rules**
| rule | meaning | needs |
|---|---|---|
| direct | same value, compatible type | 1 source |
| transform | conversion or cleansing of one source | sources and `transform` |
| derived | computed from several sources | sources and `transform` |
| split / merge | one legacy column into several target columns, or several into one | sources and `transform` |
| lookup | legacy key resolved to a new id through `migration_map` | sources and `lookup: "<schema.table>"` (`transform` optional, as the key expression) |
| new | no legacy equivalent (new requirement data) | no sources |
| generated / constant | identity, uuid, audit, or a fixed value | `transform` optional |

**Writing SQL fragments** (`transform`, `filter`, `join.on`)
- Refer to legacy tables by their bare name, e.g. `inv_hdr.inv_no`. The generator aliases `legacy_src.<table> AS <table>`. Quote mixed-case names: `"OrderExport"."OrderNo"`.
- Fragments run inside the new database, so new-schema objects (`migration_map.*`, enum casts like `::billing.invoice_status`) are usable.

**Other conventions**
- Target columns with no mapping are fine if they are an identity, have a default, or are audit columns. Otherwise `lineage_check` errors.
- `legacy_key` defaults to the driving table's PK. It is needed when the legacy table has no PK, or when the grain differs.
- Every legacy column in the context's legacy domains must end up either mapped or `retired` with a reason. That is what coverage measures.

**Reconcile extras** (v1.5)
- `"reconcile_raw"` on a column mapping: SQL over legacy columns for the value the target SHOULD hold, when the conversion is deliberate (pence to pounds: `"inv_dtl.amt_pence / 100.0"`; FX: `"pymt.amt * pymt.fx_rate"`). Reconcile compares that, not the raw column, so a known conversion is not reported as a mismatch while a real error still is.
- `"reconcile_invariants"` at the top of the mapping: totals that must agree inside the target.
  ```json
  "reconcile_invariants": [
    {"kind": "sum_of_children", "name": "invoice net = sum of lines", "parent": "invoice.net_amount", "child": "invoice_line.line_amount", "fk": "invoice_id"},
    {"kind": "sql", "name": "journal balances", "left": "SELECT journal_id::text, sum(amount) FILTER (WHERE side = 'D') FROM ledger.entry GROUP BY 1",
     "right": "SELECT journal_id::text, sum(amount) FILTER (WHERE side = 'C') FROM ledger.entry GROUP BY 1"},
    {"kind": "sql", "name": "balance rolls forward", "left": "SELECT account_id::text, closing_balance FROM ledger.account",
     "right": "SELECT a.account_id::text, a.opening_balance + coalesce(sum(t.amount), 0) FROM ledger.account a LEFT JOIN ledger.txn t USING (account_id) GROUP BY a.account_id, a.opening_balance"}
  ]
  ```
  Each `sql` side returns `(key, value)`; keys on one side only count as differences. Optional `"scale"` (decimal places).
- `"money": true|false` on a model column overrides money detection by name (names like amount, principal, interest, premium, limit, fee, balance are detected).

**Repeating groups / unpivots** (in the customer context's own mapping): `custom_select` replaces the generated SELECT and must return `custom_columns` in order:
```json
{"target": "customer.customer_phone", "sources": ["public.cust_mst"], "grain": "one row per non-empty phone1..3",
 "custom_select": "SELECT mm.new_id, x.phone, x.pos FROM legacy_src.cust_mst m JOIN migration_map.customer__customer mm ON mm.legacy_key = m.cust_id::text CROSS JOIN LATERAL (VALUES (m.phone1,1),(m.phone2,2),(m.phone3,3)) x(phone,pos) WHERE nullif(trim(x.phone),'') IS NOT NULL",
 "custom_columns": ["customer_id", "phone_number", "position"]}
```
Prechecks are not generated for `custom_select` tables. Validate them with a dry run.

## 3. Worked example
`references/examples/` contains complete model and mapping pairs for a `customer` and a `billing` context (validated end-to-end against PostgreSQL, including FDW load and reconciliation). Read them when unsure of a shape.

## 4. Evolving an accepted model
1. `context_tool.py bump <ctx> --requirement REQ-nnnn`. The model goes to version+1, status draft. The snapshot stays frozen.
2. Edit `model.json` and `mapping/<ctx>.json`. Prefer additive changes:
   - New nullable columns, or NOT NULL columns with a default.
   - Renames via `renamed_from`.
   - Additive enum values.
3. `context_tool.py diff <ctx>` shows the model diff. `generate_migration.py` shows the SQL.
4. Destructive changes (drop table, drop column, drop enum value) need an ADR, then `--allow-destructive`. The recommended pattern is expand/contract: add the new structure in one version, migrate the app, and drop the old structure in a later version.
