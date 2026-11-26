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
  [ -f "$dir/03b_corrections.sql" ] && $PSQL -d app -f "$dir/03b_corrections.sql" >/dev/null
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
echo "== v1.3 DRIFT TEST: a conversion that rounds line amounts to 1 dp (67.08 -> 67.10)."
echo "   Converted and target totals agree (a converted-vs-target sum would pass); the raw-vs-target checks must flag it."
python3 - "$W/app/.dbmod/mapping/billing.json" <<'PY2'
import json,sys; p=sys.argv[1]; m=json.load(open(p))
for cm in m["column_mappings"]:
    if cm["target"] == "billing.invoice_line.line_amount": cm.update(rule="transform", transform="round(inv_dtl.amt::numeric, 1)")
json.dump(m,open(p,"w"),indent=1)
PY2
python3 "$PKG/domain-modernization/scripts/generate_data_migration.py" --root "$W/app" --context billing >/dev/null
$PSQL -d app -c "TRUNCATE billing.payment, billing.invoice_line, billing.invoice; DROP TABLE migration_map.billing__invoice, migration_map.billing__invoice_line, migration_map.billing__payment"
run_ctx "$LATEST" | tee /tmp/dbmod-drift.log | grep -E "check_type|---|line_amount"
for want in "MISMATCH \\(conversion changes the total\\)" "money per row +\\| invoice_line\\.line_amount +\\|.*\\| +1 \\| +0\\.02 \\| +0 \\| MISMATCH" "money per parent +\\| invoice_line\\.line_amount +\\|.*\\| +1 \\| +0\\.02 \\| +0 \\| MISMATCH"; do
  grep -qE "$want" /tmp/dbmod-drift.log || { echo "FAIL: drift not detected ($want)"; $AS "$PGBIN/pg_ctl -D $DATA stop -m fast" >/dev/null; exit 1; }; done
echo "  -> drift detected as expected: total, the row (legacy key 5002|1) and its invoice (5002)"

# ---------------------------------------------------------------- v1.5: money correctness and finance rulings
GEN="python3 $PKG/domain-modernization/scripts/generate_data_migration.py --root $W/app --context"
RUL="python3 $PKG/domain-modernization/scripts/reconcile_ruling.py --root $W/app"
MAP="$W/app/.dbmod/mapping/billing.json"
reload_billing(){ $GEN billing >/dev/null; $PSQL -d app -c "TRUNCATE billing.payment, billing.invoice_line, billing.invoice; DROP TABLE migration_map.billing__invoice, migration_map.billing__invoice_line, migration_map.billing__payment"; run_ctx "$LATEST" > "$1"; }
rec_only(){ $GEN "$1" >/dev/null; $PSQL -d app -f "$2/04_reconcile.sql" > "$3"; }
expect(){ grep -qE "$2" "$1" || { echo "FAIL: $3"; grep -E "check_type|$4" "$1"; $AS "$PGBIN/pg_ctl -D $DATA stop -m fast" >/dev/null; exit 1; }; echo "  -> $3"; }
refused(){ if "$@" > /tmp/dbmod-refused.log 2>&1; then echo "FAIL: expected a refusal"; cat /tmp/dbmod-refused.log; exit 1; fi; sed 's/^/     /' /tmp/dbmod-refused.log | head -2; }
python3 $PKG/schema-memory/scripts/memory_record.py --root $W/app new ADR --title "Reconcile rulings: finance decides which value is correct" --contexts billing,customer --status accepted | tail -1
ADR=$(python3 -c "import json;print(json.load(open('$W/app/.dbmod/decisions/index.json'))['items'][-1]['id'])")

echo "== v1.5 (1) finance rules the 67.10 row: the legacy 67.08 is right, so the migration must be fixed"
$RUL add --context billing --check "money per row" --target invoice_line.line_amount --key "5002|1" --decision legacy-correct \
  --period 2023-02 --period-status open --adr $ADR --by "Fiona Finance:data-owner" --note "Invoice was issued at 67.08"
rec_only billing "$LATEST" /tmp/v15-1.log
expect /tmp/v15-1.log "money per row +\| invoice_line\.line_amount .*MISMATCH .*1 ruled: fix the migration" "still MISMATCH, with the ruling to fix the migration" line_amount

echo "== v1.5 (2) unit change: legacy line amounts in pence, target in pounds"
$PSQL -d legacy -c "UPDATE inv_dtl SET amt = amt * 100"
python3 - "$MAP" <<'EOF'
import json,sys; p=sys.argv[1]; m=json.load(open(p))
for cm in m["column_mappings"]:
    if cm["target"] == "billing.invoice_line.line_amount": cm.update(rule="transform", transform="inv_dtl.amt / 100.0")
json.dump(m,open(p,"w"),indent=1)
EOF
reload_billing /tmp/v15-2a.log
expect /tmp/v15-2a.log "money per row +\| invoice_line\.line_amount .*\| +3 \| +6640\.92 \| +0 \| MISMATCH" "without reconcile_raw every row looks wrong (raw pence vs pounds)" line_amount
python3 - "$MAP" <<'EOF'
import json,sys; p=sys.argv[1]; m=json.load(open(p))
for cm in m["column_mappings"]:
    if cm["target"] == "billing.invoice_line.line_amount": cm["reconcile_raw"] = "inv_dtl.amt / 100.0"
json.dump(m,open(p,"w"),indent=1)
EOF
rec_only billing "$LATEST" /tmp/v15-2b.log
expect /tmp/v15-2b.log "money per row +\| invoice_line\.line_amount .*\| +0 \| +\| +0 \| OK .*expected legacy value \(reconcile_raw\)" "with reconcile_raw the known conversion passes; a real error would still show" line_amount
expect /tmp/v15-2b.log "money total +\| invoice_line\.line_amount +\| +167\.08" "raw column shows the expected 167.08 next to converted and target" line_amount

echo "== v1.5 (3) invariant: invoice net = sum of its lines; legacy itself disagrees on invoice 5002 (67.08 vs 67.00)"
$PSQL -d legacy -c "UPDATE inv_dtl SET amt = 6700 WHERE inv_no = 5002 AND line_no = 1"
python3 - "$MAP" <<'EOF'
import json,sys; p=sys.argv[1]; m=json.load(open(p))
m["reconcile_invariants"] = [{"kind": "sum_of_children", "name": "invoice net = sum of lines",
                              "parent": "invoice.net_amount", "child": "invoice_line.line_amount", "fk": "invoice_id"},
                             {"kind": "sql", "name": "net = lines (sql form)",
                              "left": "SELECT invoice_id::text, net_amount FROM billing.invoice",
                              "right": "SELECT invoice_id::text, sum(line_amount) FROM billing.invoice_line GROUP BY 1"}]
json.dump(m,open(p,"w"),indent=1)
EOF
reload_billing /tmp/v15-3a.log
expect /tmp/v15-3a.log "money per row +\| invoice_line\.line_amount .*\| +0 \| +\| +0 \| OK" "every row migrated faithfully" line_amount
expect /tmp/v15-3a.log "invariant +\| invoice net = sum of lines .*\| +1 \| +0\.08 \| +0 \| MISMATCH .*legacy already had this" "but the invariant fails: legacy was inconsistent" invariant
expect /tmp/v15-3a.log "invariant +\| net = lines \(sql form\) .*\| +1 \| +0\.08 \| +0 \| MISMATCH" "the same rule written as SQL agrees" invariant
K=$($PSQL -d app -At -c "SELECT new_id FROM migration_map.billing__invoice WHERE legacy_key = '5002'")
echo "   refused: closed period without disclosure, and a ruling by someone who is not a data owner:"
refused $RUL add --context billing --check invariant --target "invoice net = sum of lines" --key "$K" --decision new-correct \
  --period FY2023 --period-status closed --adr $ADR --by "Fiona Finance:data-owner" --note "Header is the invoiced amount"
refused $RUL add --context billing --check invariant --target "invoice net = sum of lines" --key "$K" --decision new-correct \
  --period FY2023 --period-status closed --disclosure "x" --adr $ADR --by "Dev Lead:architect" --note "Header is the invoiced amount"
$RUL add --context billing --check invariant --target "invoice net = sum of lines" --key "$K" --decision new-correct \
  --period FY2023 --period-status closed --disclosure "Line total disclosed in the migration report; no restatement of FY2023" \
  --adr $ADR --by "Fiona Finance:data-owner" --note "Header is what the customer was invoiced"
rec_only billing "$LATEST" /tmp/v15-3b.log
expect /tmp/v15-3b.log "invariant +\| invoice net = sum of lines .*\| +0 \| +\| +1 \| OK \(1 ruled by finance\)" "after finance's ruling (FY2023, closed, disclosed): OK, 1 ruled" invariant

echo "== v1.5 (4) legacy NULL credit limit became 0 in the customer context"
CUST="$W/app/db/data-migration/customer/v001"
rec_only customer "$CUST" /tmp/v15-4a.log
expect /tmp/v15-4a.log "money nulls +\| customer\.credit_limit_amount .*\| +1 \| .*REVIEW" "flagged for review (legacy key 2)" credit_limit
$RUL add --context customer --check "money nulls" --target customer.credit_limit_amount --key 2 --decision new-correct \
  --period 2026-11 --period-status open --adr $ADR --by "Fiona Finance:data-owner" --note "No limit recorded means no credit: 0 is right"
rec_only customer "$CUST" /tmp/v15-4b.log
expect /tmp/v15-4b.log "money nulls +\| customer\.credit_limit_amount .*OK \(1 ruled by finance\)" "after the ruling: OK, 1 ruled" credit_limit
grep -A6 "## Finance rulings" "$CUST/05_signoff.md" | tail -1 | sed 's/^/   05_signoff.md: /'

echo "== v1.5 (5) rounding agreed with finance: half-even (banker's) vs half-up"
python3 - "$W/app/.dbmod/standards.json" <<'EOF'
import json,sys; p=sys.argv[1]; s=json.load(open(p)); s["data_migration"]["reconcile"]["rounding"] = "half-even"; json.dump(s,open(p,"w"),indent=2)
EOF
$GEN billing >/dev/null; $PSQL -d app -f "$LATEST/00_prerequisites.sql" >/dev/null 2>&1
$PSQL -d app -At -c "SELECT 'half-even: 2.345->' || migration_map.round_money(2.345, 2) || '  2.355->' || migration_map.round_money(2.355, 2) || '  -2.345->' || migration_map.round_money(-2.345, 2) || '   half-up (round): 2.345->' || round(2.345, 2)" | tee /tmp/v15-5.log
expect /tmp/v15-5.log "half-even: 2\.345->2\.34 .*2\.355->2\.36 .*-2\.345->-2\.34 .*half-up \(round\): 2\.345->2\.35" "banker's rounding available for transforms and used by reconcile" x
grep -m1 "SET TIME ZONE" "$LATEST/04_reconcile.sql" | sed 's/^/   04_reconcile.sql: /'
echo "== v1.5 all checks passed"

# ---------------------------------------------------------------- v1.5.1: refunds, negative amounts, correcting lines
setmap(){ python3 - "$MAP" "$1" <<'EOF'
import json,sys; p, code = sys.argv[1:]; m=json.load(open(p)); exec(code); json.dump(m,open(p,"w"),indent=1)
EOF
}
col(){ echo "c = next(x for x in m['column_mappings'] if x['target'] == '$1')"; }
echo "== v1.5.1 reset: legacy line amounts back in pounds, lines rounded with finance's rule (half-even)"
$PSQL -d legacy -c "UPDATE inv_dtl SET amt = amt / 100; UPDATE inv_dtl SET amt = 67.08 WHERE inv_no = 5002 AND line_no = 1"
setmap "$(col billing.invoice_line.line_amount); c.update(rule='transform', transform='migration_map.round_money(inv_dtl.amt, 2)'); c.pop('reconcile_raw', None)"
reload_billing /tmp/v151-0.log
expect /tmp/v151-0.log "money total +\| payment\.amount +\| +130\.00 +\| +130\.00 .*\| OK" "refund (stored +20 with type REF) loads as -20: expected 130.00 = converted = target" payment
expect /tmp/v151-0.log "money per row +\| payment\.amount .*\| +0 \| +\| +0 \| OK" "every payment, refund included, matches per row" payment

echo "== v1.5.1 (1) sign bug: the transform forgets to negate refunds; the declared sign rule catches it"
PAYOK="$(col billing.payment.amount); c['transform'] = \"CASE WHEN upper(pymt.pymt_method) = 'REF' THEN -(pymt.amt::numeric) ELSE pymt.amt::numeric END\"; c['reconcile_raw'] = c['transform']"
setmap "$(col billing.payment.amount); c['transform'] = 'pymt.amt::numeric'"
reload_billing /tmp/v151-1.log
expect /tmp/v151-1.log "money per row +\| payment\.amount .*\| +1 \| +40\.00 \| +0 \| MISMATCH .*9004" "refund 9004 flagged: +20 loaded, -20 expected" payment
echo "== v1.5.1 (2) correct transform, but no sign rule declared: the sign change shows, it is not hidden"
setmap "$PAYOK; c.pop('reconcile_raw')"
reload_billing /tmp/v151-2.log
expect /tmp/v151-2.log "money total +\| payment\.amount +\| +170\.00 +\| +130\.00 .*MISMATCH \(conversion changes the total\)" "raw 170.00 vs 130.00: declare the sign rule (reconcile_raw) or fix it" payment
setmap "$PAYOK"

echo "== v1.5.1 (3) half-pence lines, one negative: 12.345 and -2.345 on invoice 5004 (net 10.00)"
echo "   (legacy line amounts widened to 3 dp, as line-level amounts often are)"
$PSQL -d legacy -c "ALTER TABLE inv_dtl ALTER COLUMN amt TYPE numeric(12,3)" && $PSQL -d app -c "ALTER FOREIGN TABLE legacy_src.inv_dtl ALTER COLUMN amt TYPE numeric(12,3)"
$PSQL -d legacy -c "INSERT INTO inv_hdr (inv_no, ord_no, cust_id, inv_dt, due_dt, inv_amt, vat_amt, paid_flg, fax_no, currency) VALUES (5004, NULL, 1, '2023-03-03', NULL, 10.00, 0, 'N', NULL, 'GBP');
  INSERT INTO inv_dtl (inv_no, line_no, ord_no, description, amt) VALUES (5004, 1, NULL, 'Service', 12.345), (5004, 2, NULL, 'Overcharge refunded', -2.345)"
echo "   transform uses PostgreSQL round() (halves away from zero: 12.35, -2.35); finance's rule is half-even (12.34, -2.34)"
setmap "$(col billing.invoice_line.line_amount); c['transform'] = 'round(inv_dtl.amt, 2)'"
reload_billing /tmp/v151-3.log
expect /tmp/v151-3.log "money total +\| invoice_line\.line_amount +\| +177\.080? .*\| OK" "the total still matches (177.08): the drift cancels out" line_amount
expect /tmp/v151-3.log "money per parent +\| invoice_line\.line_amount .*\| +0 \| .*\| OK" "so does the invoice total (10.00)" line_amount
expect /tmp/v151-3.log "money per row +\| invoice_line\.line_amount .*\| +2 \| +0\.01 \| +0 \| MISMATCH .*5004\|1, 5004\|2" "only the per-row check catches both lines, the negative one included" line_amount
setmap "$(col billing.invoice_line.line_amount); c['transform'] = 'migration_map.round_money(inv_dtl.amt, 2)'"
reload_billing /tmp/v151-3b.log
expect /tmp/v151-3b.log "money per row +\| invoice_line\.line_amount .*\| +0 \| +\| +0 \| OK" "with finance's rounding: OK" line_amount

echo "== v1.5.1 (4) correcting line: invoice 5005 (Nov 2026, open period) header 25.00, its line 24.99; finance: the header is right"
$PSQL -d legacy -c "INSERT INTO inv_hdr (inv_no, ord_no, cust_id, inv_dt, due_dt, inv_amt, vat_amt, paid_flg, fax_no, currency) VALUES (5005, NULL, 1, '2026-11-10', NULL, 25.00, 5.00, 'N', NULL, 'GBP');
  INSERT INTO inv_dtl (inv_no, line_no, ord_no, description, amt) VALUES (5005, 1, NULL, 'Consulting', 24.99)"
setmap "i = m['reconcile_invariants'][0]; i['correction_columns'] = {'line_number': '(SELECT coalesce(max(l.line_number), 0) + 1 FROM billing.invoice_line l WHERE l.invoice_id = d.k::bigint)', 'line_description': \"'Migration correction ({ruling})'\"}"
reload_billing /tmp/v151-4a.log
expect /tmp/v151-4a.log "invariant +\| invoice net = sum of lines .*\| +1 \| +0\.01 \| +0 \| MISMATCH" "invariant fails on 5005 by 0.01" invariant
K5=$($PSQL -d app -At -c "SELECT new_id FROM migration_map.billing__invoice WHERE legacy_key = '5005'")
echo "   refused: a correcting line in a closed period"
refused $RUL add --context billing --check invariant --target "invoice net = sum of lines" --key "$K5" --decision new-correct --correct-with-line \
  --period FY2025 --period-status closed --disclosure "x" --adr $ADR --by "Fiona Finance:data-owner" --note "Header is what was billed"
$RUL add --context billing --check invariant --target "invoice net = sum of lines" --key "$K5" --decision new-correct --correct-with-line \
  --period 2026-11 --period-status open --adr $ADR --by "Fiona Finance:data-owner" --note "Header is what the customer was billed and paid"
$GEN billing >/dev/null
$PSQL -d app -f "$LATEST/03b_corrections.sql" >/dev/null; $PSQL -d app -f "$LATEST/03b_corrections.sql" >/dev/null   # twice: must not duplicate
$PSQL -d app -f "$LATEST/04_reconcile.sql" > /tmp/v151-4b.log
expect /tmp/v151-4b.log "invariant +\| invoice net = sum of lines .*\| +0 \| +\| +0 \| OK " "invariant now plainly OK: the new system's rule holds" invariant
expect /tmp/v151-4b.log "row count +\| invoice_line .*\| OK" "row count still legacy vs target (the correcting line is left out)" "row count"
expect /tmp/v151-4b.log "money total +\| invoice_line\.line_amount +\| +202\.070? .*\| OK" "line totals still compare with legacy (202.07)" line_amount
expect /tmp/v151-4b.log "correcting lines +\| invoice_line\.line_amount .*0\.0100 .*\| +1 \| OK \(1 ruled by finance\) .*RR-[0-9]+ on $K5" "the correcting line is listed with its ruling" correcting
$PSQL -d app -c "SELECT c.ruling_id, c.parent_key AS invoice_id, l.line_number, l.line_description, l.line_amount, l.created_by FROM migration_map.corrections c JOIN billing.invoice_line l ON l.invoice_line_id = c.new_id"
echo "== v1.5.1 all checks passed"

# ---------------------------------------------------------------- v1.6: data profiling of the legacy database
LA="python3 $PKG/legacy-assessment/scripts"
echo "== v1.6 dirty legacy data: an orphan payment, a 2019 archive with a placeholder date, an orphan and a duplicated row"
$PSQL -d legacy -c "INSERT INTO pymt (pymt_id, inv_no, pymt_dt, amt, pymt_method) VALUES (9005, 5999, '2023-04-01 09:00', 9.99, 'CARD');
  INSERT INTO ord_hdr_2019 (ord_no, cust_id, ord_dt, total_amt) VALUES (100, 1, '19000101', 50), (100, 1, '19000101', 50), (77, 9, '2019013', 12)"
$LA/profile_data.py --root $W/app plan | head -1
$PSQL -d legacy -q -f $W/app/.dbmod/legacy/profiling/profile.sql > $W/app/.dbmod/legacy/profiling/profile_result.csv
$LA/profile_data.py --root $W/app ingest --file $W/app/.dbmod/legacy/profiling/profile_result.csv | tee /tmp/v16.log
P=$W/app/.dbmod/legacy/profile.json
pj(){ python3 -c "import json,sys; f=[x for x in json.load(open('$P'))['findings'] if x['rule']=='$1' and x['object']=='$2' and x['status']=='open']; print(f[0]['id'] + ' ' + f[0]['message'] + ' | ' + f[0]['evidence'] if f else 'MISSING')"; }
for want in "LA-DAT-001 public.pymt.inv_no" "LA-DAT-001 public.ord_hdr_2019.cust_id" "LA-DAT-002 public.ord_hdr_2019" "LA-DAT-003 public.ord_hdr_2019.ord_dt" \
            "LA-DAT-006 public.ord_hdr_2019.ord_dt" "LA-DAT-004 public.inv_hdr.currency" "LA-DAT-005 public.cust_mst.cust_nm" "LA-DAT-008 public.inv_dtl.amt"; do
  r=$(pj $want); echo "  -> $want: $r"; [ "$r" != "MISSING" ] || { echo "FAIL: $want not found"; exit 1; }; done
$LA/build_report.py --root $W/app >/dev/null
grep -q "## 12. Data profile" $W/app/.dbmod/legacy/assessment/report.md && echo "  -> report has the Data profile section"
$LA/review.py --root $W/app sme list --open | grep -E "placeholder parent|same code|rounding rule|real one" | sed 's/^/   /'
ORPH=$(pj LA-DAT-001 public.pymt.inv_no | cut -d' ' -f1)
$LA/review.py --root $W/app finding $ORPH accepted-risk --note "Payment to a purged invoice: archived, not migrated (ADR)" --by "Fiona Finance:data-owner"
$LA/build_report.py --root $W/app >/dev/null
$LA/review.py --root $W/app sme list | grep -q "resolved-by-review.*public.pymt.inv_no" && echo "  -> settling $ORPH closed its SME question" || { echo "FAIL: question not settled"; exit 1; }
cp $W/app/.dbmod/legacy/profiling/profile_result.csv /tmp/dbmod-profile_result.csv
echo "== v1.6 all checks passed"
$AS "$PGBIN/pg_ctl -D $DATA stop -m fast" >/dev/null
