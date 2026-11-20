#!/usr/bin/env bash
# End-to-end test of the three skills on the synthetic Acme legacy catalog.
# Usage: tests/run_e2e.sh [workdir]   (default /tmp/dbmod-e2e)
set -euo pipefail
PKG="$(cd "$(dirname "$0")/.." && pwd)"
W="${1:-/tmp/dbmod-e2e}"; rm -rf "$W"; mkdir -p "$W/app"
MEM="$PKG/schema-memory/scripts"; LA="$PKG/legacy-assessment/scripts"; DM="$PKG/domain-modernization/scripts"
FX="$PKG/tests/fixtures"; R="--root $W/app"
python3 "$FX/make_fixture.py" "$W/in" >/dev/null
step(){ echo; echo "=== $*"; }

step "Mode 1: init + ingest + analyse"
python3 $MEM/memory_init.py $R --project "Acme ERP rewrite"
python3 $LA/ingest_catalog.py $R --input "$W/in/acme_catalog_flat.html"
python3 $LA/analyze_schema.py $R
python3 $LA/discover_domains.py $R
python3 $LA/review.py $R domain rename invoice billing_legacy >/dev/null
python3 $LA/review.py $R domain confirm all --by "Test Reviewer"
python3 $LA/review.py $R rel add public.ord_hdr.sales_rep_id public.emp.emp_id --note "SME" >/dev/null
python3 $LA/analyze_schema.py $R | grep "^findings"         # refresh after review: ids stable, answered finding -> not-detected
python3 $LA/discover_domains.py $R >/dev/null       # confirmed domains (incl. views) must not be duplicated
python3 $LA/render_erd.py $R >/dev/null
python3 $LA/build_report.py $R
python3 $MEM/memory_validate.py $R

step "Business gate: the business answers the SME questions (order domain left open on purpose)"
python3 $LA/review.py $R sme list --open | tail -1
for q in $(python3 -c "import json; print(' '.join(x['id'] for x in json.load(open('$W/app/.dbmod/legacy/sme_questions.json'))['questions'] if x['status']=='open' and x.get('domain')!='order'))"); do
  python3 $LA/review.py $R sme answer $q "Confirmed in SME workshop" --by "Dana Owner:data-owner" >/dev/null
done
python3 $LA/review.py $R sme list --open | tail -1
python3 $LA/review.py $R sme answer SQ-0001 "x" 2>&1 | tail -1 || true   # must refuse: --by required
python3 $LA/build_report.py $R >/dev/null
grep -c "^- \[x\] SQ-" "$W/app/.dbmod/legacy/assessment/report.md" | sed 's/^/answered or settled in report: /'

step "Mode 2 / REQ-0001: customer context"
python3 $MEM/memory_record.py $R new REQ --title "Customer master in new app" --source ACME-101 --contexts customer >/dev/null
python3 $MEM/memory_record.py $R new ADR --title "Do not migrate soft-deleted customers" --contexts customer --status accepted >/dev/null
python3 $DM/context_tool.py $R new customer --legacy-domains customer --description "Customer master data" --requirement REQ-0001
cp "$FX/customer_model.json" "$W/app/.dbmod/target/contexts/customer/model.json"
cp "$FX/customer_mapping.json" "$W/app/.dbmod/mapping/customer.json"
python3 $DM/model_lint.py $R --context customer
python3 $DM/lineage_check.py $R --context customer --show-pending 5 || true
python3 $DM/generate_migration.py $R --context customer --quiet
python3 $DM/context_tool.py $R propose customer
expect_blocked(){ if "$@" > /tmp/dbmod-blocked.log 2>&1; then echo "FAIL: expected the gate to block"; cat /tmp/dbmod-blocked.log; exit 1; fi; grep -E "^ERROR|^---" /tmp/dbmod-blocked.log | head -4; echo "  -> blocked as expected"; }
expect_blocked python3 $DM/accept_model.py $R --context customer --description create_customer --approved-by "Test Architect:architect"   # personal data: needs a data owner
expect_blocked python3 $DM/accept_model.py $R --context customer --description create_customer --approved-by "Test Architect"             # no role given
python3 $DM/accept_model.py $R --context customer --description create_customer --approved-by "Test Architect:architect" --approved-by "Dana Owner:data-owner"

step "Mode 2 / REQ-0002: billing context"
python3 $MEM/memory_record.py $R new REQ --title "Invoicing and payments" --source "https://confluence.acme/x/BILL" --contexts billing >/dev/null
python3 $MEM/memory_record.py $R new ADR --title "No card or bank data in the new platform" --contexts billing --status accepted >/dev/null
python3 $DM/context_tool.py $R new billing --legacy-domains billing_legacy --description "Invoicing" --requirement REQ-0002
cp "$FX/billing_model.json" "$W/app/.dbmod/target/contexts/billing/model.json"
python3 - "$W/app/.dbmod/target/contexts/billing/model.json" <<'PY'
import json,sys; p=sys.argv[1]; m=json.load(open(p)); m["legacy_domains"]=["billing_legacy"]; json.dump(m,open(p,"w"),indent=1)
PY
cp "$FX/billing_mapping.json" "$W/app/.dbmod/mapping/billing.json"
python3 $DM/model_lint.py $R --context billing
python3 $DM/lineage_check.py $R --context billing --show-pending 8 || true
python3 $DM/check_conflicts.py $R
python3 $DM/context_tool.py $R propose billing
expect_blocked python3 $DM/accept_model.py $R --context billing --description create_billing --approved-by "Test Architect:architect" --approved-by "Test Architect:data-owner"   # one person, two roles
python3 $DM/accept_model.py $R --context billing --description create_billing --approved-by "Test Architect:architect" --approved-by "Fiona Finance:data-owner"

step "Mode 2 / REQ-0003: change to accepted billing (bump -> ALTER migration)"
python3 $MEM/memory_record.py $R new REQ --title "Track outstanding balance" --source ACME-140 --contexts billing >/dev/null
python3 $DM/context_tool.py $R bump billing --requirement REQ-0003 --reason "outstanding balance"
python3 - "$W/app/.dbmod/target/contexts/billing/model.json" <<'PY'
import json,sys; p=sys.argv[1]; m=json.load(open(p))
inv=[t for t in m["tables"] if t["name"]=="invoice"][0]
inv["columns"].insert(9,{"name":"outstanding_amount","type":"numeric(19,4)","nullable":False,"default":"0","pii":"none","description":"unpaid balance"})
inv["indexes"].append({"columns":["status","due_date"]})
ln=[t for t in m["tables"] if t["name"]=="invoice_line"][0]
[c for c in ln["columns"] if c["name"]=="description"][0].update({"name":"line_description","renamed_from":"description"})
m["enums"][0]["values"].append("written_off")
json.dump(m,open(p,"w"),indent=1)
PY
python3 - "$W/app/.dbmod/mapping/billing.json" <<'PY'
import json,sys; p=sys.argv[1]; m=json.load(open(p))
for c in m["column_mappings"]:
    if c["target"]=="billing.invoice_line.description": c["target"]="billing.invoice_line.line_description"
m["column_mappings"].append({"target":"billing.invoice.outstanding_amount","sources":["public.inv_hdr.inv_amt","public.inv_hdr.paid_flg"],"rule":"derived",
  "transform":"CASE WHEN inv_hdr.paid_flg='Y' THEN 0 ELSE inv_hdr.inv_amt END","requirements":["REQ-0003"]})
json.dump(m,open(p,"w"),indent=1)
PY
python3 $DM/context_tool.py $R diff billing
python3 $DM/generate_migration.py $R --context billing --quiet
python3 $DM/context_tool.py $R propose billing
python3 $DM/accept_model.py $R --context billing --description outstanding_balance --approved-by "Test Architect:architect" --approved-by "Fiona Finance:data-owner"

step "Memory after three requirements"
python3 $MEM/memory_status.py $R
python3 $MEM/memory_query.py $R table public.inv_hdr
python3 $MEM/memory_query.py $R lineage public.pymt.card_no
python3 $MEM/memory_query.py $R lineage public.cust_mst_bak.cust_nm
python3 $MEM/memory_query.py $R search invoice --limit 5
python3 $MEM/memory_query.py $R domain billing_legacy
python3 $MEM/memory_query.py $R unmapped --domain billing_legacy
python3 $MEM/memory_query.py $R context billing
python3 $MEM/memory_query.py $R relationships public.ord_hdr
python3 $MEM/memory_record.py $R list
python3 -c "import json,sys; f=json.load(open('$W/app/.dbmod/legacy/findings.json'))['findings']; nd=[x['id'] for x in f if x['status']=='not-detected']; ids=[x['id'] for x in f]; assert len(ids)==len(set(ids)); print('not-detected findings kept:', nd)"
step "Upstream-not-designed case: sales context referencing a product context that does not exist"
python3 $MEM/memory_record.py $R new REQ --title "Order capture" --source ACME-201 >/dev/null
python3 $DM/context_tool.py $R new order --legacy-domains order 2>&1 | tail -1 || true   # must refuse: reserved word
python3 $DM/context_tool.py $R new sales --legacy-domains order --requirement REQ-0004 --description "Order capture"
python3 - "$W/app/.dbmod" <<'PY'
import json,sys; mem=sys.argv[1]
m=json.load(open(f"{mem}/target/contexts/sales/model.json"))
aud=[{"name":n,"type":t,"nullable":False,**({"default":"now()"} if t!="text" else {}),"pii":"none","description":"audit"} for n,t in
     [("created_at","timestamp with time zone"),("created_by","text"),("updated_at","timestamp with time zone"),("updated_by","text")]]
m["tables"]=[{"name":"sales_order","role":"root","aggregate":"SalesOrder","description":"Captured order","columns":[
  {"name":"sales_order_id","type":"bigint","identity":True,"nullable":False,"pii":"none","description":"key"},
  {"name":"customer_id","type":"bigint","nullable":False,"pii":"none","description":"customer ref"},
  {"name":"order_number","type":"text","nullable":False,"pii":"none","description":"business number"}]+aud+[
  {"name":"row_version","type":"integer","nullable":False,"default":"1","pii":"none","description":"lock"}],
  "primary_key":["sales_order_id"],"unique":[{"columns":["order_number"]}],"checks":[],"foreign_keys":[],"indexes":[]}]
m["external_refs"]=[{"column":"sales_order.customer_id","context":"party","entity":"party","mode":"id"}]
json.dump(m,open(f"{mem}/target/contexts/sales/model.json","w"),indent=1)
mp={"context":"sales","table_mappings":[{"target":"sales.sales_order","sources":["public.ord_hdr"],"legacy_key":["ord_no"]}],
    "column_mappings":[{"target":"sales.sales_order.customer_id","sources":["public.ord_hdr.cust_id"],"rule":"lookup","lookup":"party.party"},
                       {"target":"sales.sales_order.order_number","sources":["public.ord_hdr.ord_no"],"rule":"transform","transform":"ord_hdr.ord_no::text","resolves":["F-0001"]}],
    "retired":[]}
json.dump(mp,open(f"{mem}/mapping/sales.json","w"),indent=1)
PY
python3 $DM/model_lint.py $R --context sales | tail -1
python3 $DM/lineage_check.py $R --context sales --show-pending 3 | tail -2
python3 $DM/accept_model.py $R --context sales --description order_capture --approved-by "T" 2>&1 | tail -1 || true   # must refuse: not proposed
python3 $DM/context_tool.py $R propose sales
expect_blocked python3 $DM/accept_model.py $R --context sales --description order_capture --approved-by "Test Architect:architect"   # order-domain SME questions still open
python3 $MEM/memory_record.py $R new ADR --title "Order legacy quirks handled in the sales context" --contexts sales >/dev/null   # ADR-0003, proposed
FIRST=$(python3 -c "import json; print([x['id'] for x in json.load(open('$W/app/.dbmod/legacy/sme_questions.json'))['questions'] if x['status']=='open'][0])")
python3 $LA/review.py $R sme waive $FIRST --adr ADR-0003 --by "Olu Orders:product-owner"
for q in $(python3 -c "import json; print(' '.join(x['id'] for x in json.load(open('$W/app/.dbmod/legacy/sme_questions.json'))['questions'] if x['status']=='open'))"); do
  python3 $LA/review.py $R sme answer $q "Confirmed with order desk" --by "Olu Orders:product-owner" >/dev/null
done
expect_blocked python3 $DM/accept_model.py $R --context sales --description order_capture --approved-by "Test Architect:architect"   # waiver ADR not accepted yet
python3 $DM/accept_model.py $R --context sales --description order_capture --approved-by "Test Architect:architect" --adrs ADR-0003 | grep -E "accepted|resolved"
python3 $MEM/memory_validate.py $R
echo; echo "Flyway:"; ls "$W/app/db/migration"; echo "Data migration:"; find "$W/app/db/data-migration" -type f | sort
