#!/usr/bin/env python3
"""Generate a deliberately messy synthetic legacy PostgreSQL catalog ("Acme Trading" ERP)
in three export layouts used by the tests:
  acme_catalog.csv            information_schema-style, one row per column
  acme_catalog_flat.html      one big HTML table + table summary table
  acme_catalog_sections.html  one HTML table per legacy table under <h2>Table: x</h2>
No real data: names and structures are invented.
"""
import csv
import html
import os
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))

# (table, kind, rows, comment, [ (col, type, nullable, key, ref, comment, default) ])
def c(name, typ, null=True, key="", ref="", comment="", default=""):
    return (name, typ, null, key, ref, comment, default)

T = [
 ("cust_mst", "table", 182000, "Customer master", [
   c("cust_id", "integer", False, "PK"), c("cust_nm", "varchar(100)", False, comment="customer name"),
   c("cust_type", "char(1)", comment="R=retail W=wholesale"), c("email_addr", "varchar(200)"),
   c("phone1", "varchar(20)"), c("phone2", "varchar(20)"), c("phone3", "varchar(20)"),
   c("addr1", "varchar(100)"), c("addr2", "varchar(100)"), c("addr3", "varchar(100)"),
   c("city", "varchar(50)"), c("post_cd", "varchar(10)"), c("country_cd", "char(2)"),
   c("region_cd", "char(3)"), c("dob", "varchar(10)", comment="dd/mm/yyyy"), c("nino", "varchar(9)", comment="national insurance no"),
   c("credit_lmt", "double precision"), c("status", "char(1)"), c("crt_dt", "timestamp"),
   c("upd_dt", "timestamp"), c("upd_usr", "varchar(20)"), c("del_flg", "char(1)", default="'N'")]),
 ("cust_contact", "table", 240000, "", [
   c("contact_id", "integer", False, "PK", default="nextval('cust_contact_contact_id_seq'::regclass)"),
   c("cust_id", "integer"), c("contact_name", "varchar(100)"), c("email", "varchar(200)"),
   c("phone", "varchar(20)"), c("is_primary", "smallint")]),
 ("cust_notes", "table", 51000, "", [
   c("note_id", "integer", False, "PK"), c("cust_id", "integer"), c("note_txt", "text"), c("created", "timestamp")]),
 ("cust_mst_bak", "table", 150000, "backup before 2017 migration", [
   c("cust_id", "integer"), c("cust_nm", "varchar(100)"), c("email_addr", "varchar(200)"), c("status", "char(1)")]),
 ("region", "table", 12, "", [c("region_cd", "char(3)", False, "PK"), c("region_desc", "varchar(50)")]),
 ("ord_hdr", "table", 2400000, "Sales order header", [
   c("ord_no", "integer", False, "PK"), c("cust_id", "integer", False, "FK", "public.cust_mst(cust_id)"),
   c("ord_dt", "varchar(8)", comment="yyyymmdd"), c("ord_status", "varchar(2)"),
   c("ship_addr1", "varchar(100)"), c("ship_addr2", "varchar(100)"), c("ship_city", "varchar(50)"),
   c("ship_post_cd", "varchar(10)"), c("total_amt", "money"), c("disc_pct", "real"),
   c("cust_nm", "varchar(100)", comment="copied from cust_mst at order time"), c("sales_rep_id", "integer"),
   c("crt_dt", "timestamp")]),
 ("ord_dtl", "table", 9100000, "", [
   c("ord_no", "integer", False, "PK, FK", "public.ord_hdr(ord_no)"), c("line_no", "integer", False, "PK"),
   c("prod_cd", "varchar(20)"), c("qty", "integer"), c("unit_price", "numeric(10,2)"), c("line_amt", "numeric(12,2)")]),
 ("ord_status_hist", "table", 7000000, "", [
   c("ord_no", "integer"), c("status", "varchar(2)"), c("chg_dt", "timestamp"), c("chg_by", "varchar(20)")]),
 ("tmp_ord_import", "table", 0, "", [
   c("col1", "text"), c("col2", "text"), c("col3", "text"), c("loaded", "timestamp")]),
 ("ord_hdr_2019", "table", 800000, "archive", [
   c("ord_no", "integer"), c("cust_id", "integer"), c("ord_dt", "varchar(8)"), c("total_amt", "money")]),
 ("prod_mst", "table", 14000, "Products", [
   c("prod_cd", "varchar(20)", False, "PK"), c("prod_desc", "varchar(200)"), c("cat_id", "integer"),
   c("unit_price", "numeric(10,2)"), c("uom", "varchar(5)"), c("active_yn", "char(1)"),
   c("attr1", "varchar(50)"), c("attr2", "varchar(50)"), c("attr3", "varchar(50)"), c("attr4", "varchar(50)"), c("attr5", "varchar(50)")]),
 ("prod_cat", "table", 140, "", [c("cat_id", "integer", False, "PK"), c("cat_name", "varchar(100)"), c("parent_cat_id", "integer")]),
 ("prod_attr", "table", 300000, "extra product attributes", [
   c("prod_cd", "varchar(20)", False), c("attr_name", "varchar(50)", False), c("attr_value", "varchar(500)")]),
 ("stock_lvl", "table", 40000, "", [
   c("prod_cd", "varchar(20)", False, "PK"), c("whse_cd", "varchar(5)", False, "PK"), c("qty_on_hand", "integer"), c("last_cnt_dt", "date")]),
 ("whse", "table", 6, "Warehouses", [c("whse_cd", "varchar(5)", False, "PK"), c("whse_name", "varchar(50)"), c("addr1", "varchar(100)"), c("post_cd", "varchar(10)")]),
 ("inv_hdr", "table", 2100000, "Invoice header", [
   c("inv_no", "integer", False, "PK"), c("ord_no", "integer"), c("cust_id", "integer"), c("inv_dt", "date"),
   c("due_dt", "date"), c("inv_amt", "numeric(12,2)"), c("vat_amt", "double precision"), c("paid_flg", "char(1)"),
   c("fax_no", "varchar(20)"), c("currency", "varchar(3)")]),
 ("inv_dtl", "table", 8000000, "", [
   c("inv_no", "integer", False, "PK, FK", "public.inv_hdr(inv_no)"), c("line_no", "integer", False, "PK"),
   c("ord_no", "integer"), c("description", "varchar(200)"), c("amt", "numeric(12,2)")]),
 ("pymt", "table", 1900000, "Payments", [
   c("pymt_id", "integer", False, "PK"), c("inv_no", "integer"), c("pymt_dt", "timestamp"), c("amt", "money"),
   c("pymt_method", "varchar(10)"), c("card_no", "varchar(20)"), c("bank_acct", "varchar(20)"), c("sort_cd", "varchar(8)")]),
 ("credit_note", "table", 30000, "", [
   c("cn_id", "integer", False, "PK"), c("inv_no", "integer"), c("cust_id", "integer"), c("amt", "numeric(12,2)"), c("reason", "varchar(200)")]),
 ("emp", "table", 450, "Employees", [
   c("emp_id", "integer", False, "PK"), c("first_nm", "varchar(50)"), c("last_nm", "varchar(50)"), c("email", "varchar(200)"),
   c("salary", "numeric(10,2)"), c("ni_number", "varchar(9)"), c("mgr_id", "integer"), c("dept_cd", "varchar(5)"),
   c("hire_dt", "date"), c("ethnicity", "varchar(30)"), c("health_notes", "text")]),
 ("dept", "table", 20, "", [c("dept_cd", "varchar(5)", False, "PK"), c("dept_name", "varchar(50)")]),
 ("usr", "table", 500, "Application users", [
   c("usr_id", "varchar(20)", False, "PK"), c("emp_id", "integer"), c("pwd", "varchar(50)"), c("last_login", "timestamp"), c("role_cd", "varchar(10)")]),
 ("audit_log", "table", 50000000, "", [
   c("log_id", "bigint", False, "PK"), c("tbl_name", "varchar(50)"), c("rec_id", "varchar(50)"), c("action", "char(1)"),
   c("old_val", "text"), c("new_val", "text"), c("chg_dt", "timestamp"), c("chg_usr", "varchar(20)")]),
 ("sys_param", "table", 80, "", [c("param_key", "varchar(50)", False, "PK"), c("param_val", "varchar(500)"), c("descr", "varchar(200)")]),
 ("entity_link", "table", 600000, "documents attached to anything", [
   c("link_id", "integer", False, "PK"), c("entity_type", "varchar(20)"), c("entity_id", "integer"), c("doc_path", "varchar(500)")]),
 ("code_lkp", "table", 900, "generic code lookup", [
   c("code_type", "varchar(20)", False, "PK"), c("code_val", "varchar(10)", False, "PK"), c("code_desc", "varchar(100)")]),
 ("OrderExport", "table", 100000, "", [
   c("ExportId", "integer", False, "PK"), c("OrderNo", "integer"), c("exportedAt", "timestamp"), c("Target", "varchar(20)")]),
 ("v_cust_orders", "view", None, "reporting view", [
   c("cust_id", "integer"), c("cust_nm", "varchar(100)"), c("ord_no", "integer"), c("total_amt", "money")]),
]


def split_type(t):
    if "(" in t:
        base, args = t.split("(", 1)
        args = args.rstrip(")").split(",")
        return base, args
    return t, []


def write_csv(path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["table_schema", "table_name", "column_name", "ordinal_position", "data_type", "character_maximum_length",
                    "numeric_precision", "numeric_scale", "is_nullable", "column_default", "constraint_type",
                    "foreign_table_name", "foreign_column_name", "description"])
        for tname, kind, rows, tc, cols in T:
            for i, (n, ty, null, key, ref, cm, df) in enumerate(cols, 1):
                base, args = split_type(ty)
                base = {"varchar": "character varying", "char": "character"}.get(base, base)
                ln = args[0] if base in ("character varying", "character") and args else ""
                pr = args[0] if base == "numeric" and args else ""
                sc = args[1] if base == "numeric" and len(args) > 1 else ""
                rt = rc = ""
                if ref:
                    rt = ref.split("(")[0].split(".")[-1]
                    rc = ref.split("(")[1].rstrip(")")
                ctype = ",".join(x.strip().replace("PK", "PRIMARY KEY").replace("FK", "FOREIGN KEY") for x in key.split(",") if x.strip())
                w.writerow(["public", tname, n, i, base, ln, pr, sc, "YES" if null else "NO", df, ctype, rt, rc, cm])


def write_flat_html(path):
    out = ["<html><head><title>Acme DB catalog</title></head><body><h1>Acme Trading database catalog</h1>",
           "<h2>Tables</h2><table><tr><th>Table</th><th>Type</th><th>Rows</th><th>Description</th></tr>"]
    for tname, kind, rows, tc, cols in T:
        out.append(f"<tr><td>{tname}</td><td>{kind}</td><td>{'' if rows is None else f'{rows:,}'}</td><td>{html.escape(tc)}</td></tr>")
    out.append("</table><h2>Columns</h2><table><thead><tr><th>Schema</th><th>Table</th><th>Column</th><th>Type</th>"
               "<th>Nullable</th><th>Key</th><th>References</th><th>Description</th></tr></thead><tbody>")
    for tname, kind, rows, tc, cols in T:
        for n, ty, null, key, ref, cm, df in cols:
            out.append(f"<tr><td>public</td><td>{tname}</td><td>{n}</td><td>{ty}</td><td>{'Y' if null else 'N'}</td>"
                       f"<td>{key}</td><td>{ref}</td><td>{html.escape(cm)}</td></tr>")
    out.append("</tbody></table></body></html>")
    open(path, "w", encoding="utf-8").write("\n".join(out))


def write_sections_html(path):
    out = ["<html><body><h1>Schema: public</h1>"]
    for tname, kind, rows, tc, cols in T:
        out.append(f"<h2>{'View' if kind == 'view' else 'Table'}: public.{tname}</h2>")
        if tc:
            out.append(f"<p>{html.escape(tc)}</p>")
        out.append("<table border=1><tr><th>#</th><th>Column Name</th><th>Data Type</th><th>Not Null</th>"
                   "<th>Default</th><th>Constraints</th><th>Comment</th></tr>")
        for i, (n, ty, null, key, ref, cm, df) in enumerate(cols, 1):
            cons = []
            if "PK" in key:
                cons.append("PK")
            if ref:
                cons.append("FK &rarr; " + ref.replace("public.", "").replace("(", ".").rstrip(")"))
            out.append(f"<tr><td>{i}</td><td>{n}</td><td>{ty}</td><td>{'' if null else '&#10003;'}</td>"
                       f"<td>{html.escape(df)}</td><td>{'<br>'.join(cons)}</td><td>{html.escape(cm)}</td></tr>")
        out.append("</table>")
    out.append("</body></html>")
    open(path, "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    write_csv(os.path.join(OUT, "acme_catalog.csv"))
    write_flat_html(os.path.join(OUT, "acme_catalog_flat.html"))
    write_sections_html(os.path.join(OUT, "acme_catalog_sections.html"))
    print(f"{len(T)} tables, {sum(len(t[4]) for t in T)} columns -> {OUT}")
