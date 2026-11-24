#!/bin/bash
# n8n's workflows back to n8n/workflows (versioned in git), so changes made in its editor don't
# live only in its database. Keeps what defines a workflow and drops what changes on every run
# or names the owner (staticData, dates, the owner's project). Run nightly by backup.sh.
set -euo pipefail
OUT=~/server/n8n/workflows
rm -rf "$OUT/.export"
# The extra ports keep clear of the running n8n's own.
/usr/local/bin/docker exec -e N8N_RUNNERS_BROKER_PORT=5690 -e N8N_PORT=5691 n8n \
  n8n export:workflow --all --separate --output=/workflows/.export/ >/dev/null 2>&1
/usr/bin/python3 -I - "$OUT" <<'PY'
import json, pathlib, sys
out = pathlib.Path(sys.argv[1])
KEEP = ("id", "name", "description", "active", "nodes", "connections", "settings", "pinData", "meta", "tags")
for f in sorted((out / ".export").glob("*.json")):
    wf = json.loads(f.read_text())
    wf = {k: wf[k] for k in KEEP if k in wf}
    wf["tags"] = [t["name"] if isinstance(t, dict) else t for t in wf.get("tags", [])]
    (out / f.name).write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n")
# A workflow deleted in n8n goes from git too (or importing this folder would bring it back),
# but only when the export clearly worked.
exported = {f.name for f in (out / ".export").glob("*.json")}
if exported:
    for f in out.glob("*.json"):
        if f.name not in exported:
            f.unlink()
PY
rm -rf "$OUT/.export"
