#!/usr/bin/env python3
"""Emit SQL that creates the synthetic Acme legacy database (same structure as the catalog
fixtures) with a little deliberately dirty data, for the live PostgreSQL test."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_fixture import T  # noqa: E402


def q(n):
    return n if n.islower() else f'"{n}"'


out = []
for tname, kind, rows, tc, cols in T:
    if kind == "view":
        continue
    defs, pk = [], []
    for n, ty, null, key, ref, cm, df in cols:
        ty2 = ty
        if df.startswith("nextval"):
            ty2, df = "serial", ""
        defs.append(f"{q(n)} {ty2}" + ("" if null else " NOT NULL") + (f" DEFAULT {df}" if df else ""))
        if "PK" in key:
            pk.append(q(n))
    if pk:
        defs.append(f"PRIMARY KEY ({', '.join(pk)})")
    out.append(f"CREATE TABLE {q(tname)} (\n  " + ",\n  ".join(defs) + "\n);")
out.append("ALTER TABLE ord_hdr ADD FOREIGN KEY (cust_id) REFERENCES cust_mst(cust_id);")
out.append("CREATE VIEW v_cust_orders AS SELECT c.cust_id, c.cust_nm, o.ord_no, o.total_amt FROM cust_mst c JOIN ord_hdr o USING (cust_id);")
out.append("""
INSERT INTO cust_mst (cust_id, cust_nm, cust_type, email_addr, credit_lmt, status, crt_dt, upd_dt, upd_usr, del_flg) VALUES
 (1, ' Alpha Ltd ', 'W', 'ALPHA@EXAMPLE.COM ', 5000.10, 'A', '2015-03-01 10:00', NULL, 'jsmith', 'N'),
 (2, 'Beta Stores', 'R', '', NULL, 'A', NULL, NULL, NULL, NULL),
 (3, 'Gamma Old', 'R', 'g@example.com', 100, 'X', '2010-01-01', '2012-01-01', 'admin', 'Y');
INSERT INTO ord_hdr (ord_no, cust_id, ord_dt, total_amt) VALUES (100, 1, '20230105', 120.00), (101, 2, '20230201', 80.50);
INSERT INTO inv_hdr (inv_no, ord_no, cust_id, inv_dt, due_dt, inv_amt, vat_amt, paid_flg, fax_no, currency) VALUES
 (5001, 100, 1, '2023-01-06', '2023-02-05', 100.00, 20.00, 'Y', '0123', 'gbp'),
 (5002, 101, 2, '2023-02-02', NULL, 67.08, 13.416, 'N', NULL, NULL),
 (5003, NULL, 3, '2012-05-05', '2012-06-05', 10.00, 2.00, 'Y', NULL, 'GBP');
INSERT INTO inv_dtl (inv_no, line_no, ord_no, description, amt) VALUES
 (5001, 1, 100, 'Widgets', 60.00), (5001, 2, 100, 'Gadgets', 40.00), (5002, 1, 101, NULL, 67.08), (5003, 1, NULL, 'Old stuff', 10.00);
INSERT INTO pymt (pymt_id, inv_no, pymt_dt, amt, pymt_method, card_no) VALUES
 (9001, 5001, '2023-01-20 09:30', 120.00, 'CARD', '4111111111111111'),
 (9002, 5002, '2023-02-10 12:00', 30.00, 'bacs', NULL),
 (9003, NULL, '2023-02-11 12:00', 5.00, 'CHQ', NULL),
 (9004, 5001, '2023-03-01 10:00', 20.00, 'REF', NULL);   -- a refund, stored positive with a type flag
""")
print("\n".join(out))
