#!/usr/bin/env python3
"""Accept a context model after human approval. This is the only way a design enters Flyway.

Gates: model_lint, lineage_check and check_conflicts must report no errors.
Then it:
  1. writes <repo>/<flyway location>/V<utc-timestamp>__<ctx>_<description>.sql
  2. freezes the model as target/contexts/<ctx>/snapshots/vNNN.json (immutable)
  3. marks the model and its column mappings accepted, and moves cited REQs to 'accepted'
  4. generates the data-migration scripts (unless --no-data-migration)
  5. logs everything to the journal

Usage: python accept_model.py --context billing --description create_billing --approved-by "Jane Doe" [--root <repo>]
       [--allow-destructive] [--no-data-migration]
"""
import argparse
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _dbmod as d  # noqa: E402
import generate_migration as gm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def gate(script, root, ctx):
    args = [sys.executable, os.path.join(HERE, script), "--root", root]
    if ctx:
        args += ["--context", ctx]
    r = subprocess.run(args, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--context", required=True)
    ap.add_argument("--description", required=True, help="short snake_case summary for the Flyway file name")
    ap.add_argument("--approved-by", required=True)
    ap.add_argument("--allow-destructive", action="store_true")
    ap.add_argument("--no-data-migration", action="store_true")
    ap.add_argument("--adrs", help="comma-separated ADR ids the approver also accepted with this design")
    a = ap.parse_args()
    root = d.find_root(a.root)
    ctx = a.context
    m = d.load_model(root, ctx)
    if m["status"] == "accepted":
        d.die(f"{ctx} v{m['version']} is already accepted")
    if m["status"] != "proposed":
        d.die(f"{ctx} v{m['version']} is {m['status']}: run context_tool.py propose {ctx} and present it for review first")
    desc = re.sub(r"[^a-z0-9]+", "_", a.description.lower()).strip("_")

    failed = False
    for script, c in (("model_lint.py", ctx), ("lineage_check.py", ctx), ("check_conflicts.py", None)):
        rc, out = gate(script, root, c)
        errs = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
        if script == "check_conflicts.py":
            errs = [e for e in errs if re.search(rf"\b{re.escape(ctx)}\b", e)]
        if errs:
            failed = True
            print(f"--- {script}: blocked")
            print("\n".join(errs))
    if failed:
        d.die("acceptance gates failed; fix the errors above (nothing was written)")

    std = d.load_standards(root)
    base, av = gm.load_base(root, m)
    S, W, X = gm.build(std, base, m, a.allow_destructive)
    if X and not a.allow_destructive:
        d.die("destructive changes: " + "; ".join(X) + " (record an ADR, then --allow-destructive)")
    if not [s for s in S if s.strip() and not s.startswith("--")]:
        d.die("no schema changes to accept")
    fly_dir = os.path.join(root, std["flyway"]["location"])
    stamp = int(d.stamp())
    existing = os.listdir(fly_dir) if os.path.isdir(fly_dir) else []
    # Flyway versions must be unique and increasing: never reuse or go below an existing version
    latest = max([int(re.match(r"V(\d+)", f).group(1)) for f in existing if re.match(r"V\d+__", f)] + [0])
    stamp = str(max(stamp, latest + 1))
    fname = std["flyway"]["version_format"].format(timestamp=stamp, context=ctx, description=desc)
    fpath = os.path.join(fly_dir, fname)
    if os.path.exists(fpath):
        d.die(f"{fpath} exists")
    extra = f"-- approved by {a.approved_by} at {d.now_iso()}"
    d.write_text(fpath, "\n".join(gm.header(m, av, m.get("requirements", []), extra) + S) + "\n")

    m["status"] = "accepted"
    m["accepted_version"] = m["version"]
    m["accepted_at"] = d.now_iso()
    m["approved_by"] = a.approved_by
    m.setdefault("migrations", []).append({"version": m["version"], "file": os.path.relpath(fpath, root).replace(os.sep, "/")})
    m["updated_at"] = d.now_iso()
    snap = os.path.join(d.context_dir(root, ctx), "snapshots", f"v{m['version']:03d}.json")
    if os.path.exists(snap):
        d.die(f"snapshot {snap} already exists (immutable)")
    d.save_json(snap, m)
    d.save_json(os.path.join(d.context_dir(root, ctx), "model.json"), m)
    prev = os.path.join(d.context_dir(root, ctx), "pending", "preview.sql")
    if os.path.exists(prev):
        os.remove(prev)   # the Flyway file is now the only copy

    mp = d.load_mapping(root, ctx)
    for cm in mp.get("column_mappings", []):
        cm["status"] = "accepted"
    for tm in mp.get("table_mappings", []):
        tm["status"] = "accepted"
    d.save_json(d.mem(root, "mapping", f"{ctx}.json"), mp)

    idxp = d.mem(root, "requirements", "index.json")
    idx = d.load_json(idxp, default={"items": []})
    moved = []
    for it in idx["items"]:
        if it["id"] in m.get("requirements", []) and it["status"] in ("captured", "analysed", "designed"):
            it["status"] = "accepted"
            it.setdefault("history", []).append({"at": d.now_iso(), "status": "accepted", "note": f"{ctx} v{m['version']} accepted"})
            if ctx not in it["contexts"]:
                it["contexts"].append(ctx)
            p = d.mem(root, it["file"])
            if os.path.exists(p):
                lines = open(p, encoding="utf-8").read().splitlines()
                d.write_text(p, "\n".join("| Status | accepted |" if ln.startswith("| Status |") else ln for ln in lines) + "\n")
            moved.append(it["id"])
    d.save_json(idxp, idx)

    # ADRs approved together with the design
    adr_ok = []
    if a.adrs:
        ap_ = d.mem(root, "decisions", "index.json")
        aidx = d.load_json(ap_, default={"items": []})
        for it in aidx["items"]:
            if it["id"] in [x.strip() for x in a.adrs.split(",")] and it["status"] == "proposed":
                it["status"] = "accepted"
                it.setdefault("history", []).append({"at": d.now_iso(), "status": "accepted", "note": f"approved with {ctx} v{m['version']} by {a.approved_by}"})
                p = d.mem(root, it["file"])
                if os.path.exists(p):
                    lines = open(p, encoding="utf-8").read().splitlines()
                    d.write_text(p, "\n".join("| Status | accepted |" if ln.startswith("| Status |") else ln for ln in lines) + "\n")
                adr_ok.append(it["id"])
        d.save_json(ap_, aidx)
    # legacy findings this design resolves ("resolves": ["F-0012"] on table or column mappings)
    res_ids = {fid for e in mp.get("table_mappings", []) + mp.get("column_mappings", []) for fid in e.get("resolves", [])}
    resolved = []
    if res_ids:
        fp = d.mem(root, "legacy", "findings.json")
        fdata = d.load_findings(root)
        for f in fdata.get("findings", []):
            if f["id"] in res_ids and f["status"] == "open":
                f["status"] = "resolved-in-target"
                f["note"] = (f.get("note", "") + f" resolved by {ctx} v{m['version']}").strip()
                resolved.append(f["id"])
        d.save_json(fp, fdata)

    man = d.load_manifest(root)
    man["target"]["contexts"][ctx] = {"schema": m["schema"], "status": "accepted", "version": m["version"],
                                      "accepted_version": m["version"], "last_migration": m["migrations"][-1]["file"]}
    d.save_manifest(root, man)
    with open(os.path.join(d.context_dir(root, ctx), "design.md"), "a", encoding="utf-8") as fh:
        fh.write(f"| {m['version']} | accepted | {', '.join(m.get('requirements', []))} | approved by {a.approved_by}; {fname} |\n")
    d.journal(root, "domain-modernization", "accept", f"{ctx} v{m['version']} accepted by {a.approved_by} -> {fname}",
              requirements=m.get("requirements"), warnings=W)
    for w in W:
        print("WARN   " + w)
    print(f"accepted {ctx} v{m['version']}")
    print(f"flyway: {fpath}")
    print(f"snapshot: {snap}")
    if moved:
        print("requirements accepted: " + ", ".join(moved))
    if adr_ok:
        print("ADRs accepted: " + ", ".join(adr_ok))
    if resolved:
        print("legacy findings resolved-in-target: " + ", ".join(sorted(resolved)))
    subprocess.run([sys.executable, os.path.join(HERE, "render_erd.py"), "--root", root, "--target", ctx], capture_output=True)
    if not a.no_data_migration:
        r = subprocess.run([sys.executable, os.path.join(HERE, "generate_data_migration.py"), "--root", root, "--context", ctx],
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())


if __name__ == "__main__":
    main()
