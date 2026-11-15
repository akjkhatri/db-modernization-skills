#!/usr/bin/env python3
"""Keep the shared helper identical in all three skills.
  python tools/sync_common.py          copy schema-memory/scripts/_dbmod.py to the other skills
  python tools/sync_common.py --check  exit 1 if the copies differ (use in CI)
"""
import filecmp
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "schema-memory", "scripts", "_dbmod.py")
DST = [os.path.join(ROOT, s, "scripts", "_dbmod.py") for s in ("legacy-assessment", "domain-modernization")]
bad = [p for p in DST if not os.path.exists(p) or not filecmp.cmp(SRC, p, shallow=False)]
if "--check" in sys.argv:
    print("in sync" if not bad else "OUT OF SYNC: " + ", ".join(bad))
    sys.exit(1 if bad else 0)
for p in DST:
    shutil.copy(SRC, p)
print("synced " + ", ".join(DST))
