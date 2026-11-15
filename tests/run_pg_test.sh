#!/usr/bin/env bash
# Live test: run the generated Flyway migrations and data-migration scripts against a real
# PostgreSQL (legacy DB seeded with dirty data, new DB fed through postgres_fdw).
# Requires PostgreSQL server binaries (initdb/pg_ctl) and a 'postgres' OS user when run as root.
# Usage: tests/run_pg_test.sh   (runs run_e2e.sh first)
set -uo pipefail
PKG="$(cd "$(dirname "$0")/.." && pwd)"
W=/tmp/dbmod-e2e; PGBIN="${PGBIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | tail -1)}"; PORT=54329; DATA=/tmp/dbmod-pgdata
bash "$PKG/tests/run_e2e.sh" "$W" > /tmp/dbmod-e2e.log 2>&1 || { echo "e2e failed"; tail -30 /tmp/dbmod-e2e.log; exit 1; }
AS="su postgres -c"; [ "$(id -u)" != 0 ] && AS="bash -c"
$AS "$PGBIN/pg_ctl -D $DATA stop -m fast" >/dev/null 2>&1; rm -rf $DATA
$AS "$PGBIN/initdb -D $DATA -A trust -U postgres" >/dev/null
$AS "$PGBIN/pg_ctl -D $DATA -o '-p $PORT -k /tmp' -l /tmp/dbmod-pg.log start -w" >/dev/null
PSQL="$PGBIN/psql -h /tmp -p $PORT -U postgres -v ON_ERROR_STOP=1 -q"
$PSQL -c "CREATE DATABASE legacy" -c "CREATE DATABASE app"
python3 "$PKG/tests/fixtures/legacy_seed.py" | $PSQL -d legacy >/dev/null && echo "legacy db seeded"
echo "== Flyway migrations (applied in version order, as Flyway would)"
for f in $(ls "$W/app/db/migration"/V*.sql | sort); do $PSQL -d app -f "$f" && echo "  applied $(basename $f)" || { echo "  FAILED $f"; exit 1; }; done
$PSQL -d app -c "CREATE EXTENSION IF NOT EXISTS postgres_fdw" \
  -c "CREATE SERVER legacy_server FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host '/tmp', port '$PORT', dbname 'legacy')" \
  -c "CREATE USER MAPPING FOR CURRENT_USER SERVER legacy_server OPTIONS (user 'postgres')"
run_ctx(){ local dir="$1"; echo "== data migration $(basename $(dirname $dir))/$(basename $dir)"
  $PSQL -d app -f "$dir/00_prerequisites.sql" >/dev/null 2>&1 || $PSQL -d app -f "$dir/00_prerequisites.sql"
  $PSQL -d app -f "$dir/01_id_maps.sql" >/dev/null
  echo "-- precheck (rows = issues):"; $PSQL -d app -f "$dir/02_precheck.sql"
  if $PSQL -d app -f "$dir/03_load.sql" >/tmp/dbmod-load.log 2>&1; then echo "-- load OK"; else echo "-- load FAILED:"; cat /tmp/dbmod-load.log; fi
  echo "-- reconcile:"; $PSQL -d app -f "$dir/04_reconcile.sql"; }
run_ctx "$W/app/db/data-migration/customer/v001"
LATEST=$(ls -d $W/app/db/data-migration/billing/v* | sort | tail -1)
run_ctx "$LATEST"
echo "== DECISION (would be an ADR): invoices/payments of customers not migrated (soft-deleted) are archived, not loaded"
python3 - "$W/app/.dbmod/mapping/billing.json" <<'PY'
import json,sys; p=sys.argv[1]; m=json.load(open(p))
f={"billing.invoice":"EXISTS (SELECT 1 FROM migration_map.customer__customer c WHERE c.legacy_key = inv_hdr.cust_id::text)",
   "billing.invoice_line":"EXISTS (SELECT 1 FROM migration_map.billing__invoice i WHERE i.legacy_key = inv_dtl.inv_no::text)",
   "billing.payment":"EXISTS (SELECT 1 FROM migration_map.billing__invoice i WHERE i.legacy_key = pymt.inv_no::text)"}
for tm in m["table_mappings"]:
    if tm["target"] in f: tm["filter"]=f[tm["target"]]
json.dump(m,open(p,"w"),indent=1)
PY
python3 "$PKG/domain-modernization/scripts/generate_data_migration.py" --root "$W/app" --context billing
$PSQL -d app -c "TRUNCATE billing.payment, billing.invoice_line, billing.invoice; DROP TABLE migration_map.billing__invoice, migration_map.billing__invoice_line, migration_map.billing__payment"
run_ctx "$LATEST"
echo "== sales (depends on a context 'party' that is not designed yet): precheck must report NOT RUN, not crash"
$PSQL -d app -f "$W/app/db/data-migration/sales/v001/00_prerequisites.sql" >/dev/null 2>&1
$PSQL -d app -f "$W/app/db/data-migration/sales/v001/01_id_maps.sql" >/dev/null
$PSQL -d app -f "$W/app/db/data-migration/sales/v001/02_precheck.sql"
echo "== sample of migrated data"
$PSQL -d app -c "SELECT invoice_number, customer_id, status, net_amount, vat_amount, outstanding_amount, due_date FROM billing.invoice ORDER BY 1" \
  -c "SELECT customer_number, display_name, customer_type, email_address, credit_limit_amount FROM customer.customer ORDER BY 1" \
  -c "SELECT i.invoice_number, c.customer_number AS migrated_customer, l.cust_id AS legacy_customer, (c.customer_number = l.cust_id::text) AS lineage_ok FROM billing.invoice i JOIN customer.customer c ON c.customer_id = i.customer_id JOIN legacy_src.inv_hdr l ON 'INV-' || lpad(l.inv_no::text, 8, '0') = i.invoice_number ORDER BY 1" \
  -c "SELECT p.amount, p.payment_method_code, p.provider_token, p.received_at FROM billing.payment p ORDER BY 1"
$AS "$PGBIN/pg_ctl -D $DATA stop -m fast" >/dev/null
