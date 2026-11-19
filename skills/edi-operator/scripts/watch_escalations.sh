#!/bin/sh
# P7 watch_escalations: cron-safe scan for new escalations.
#
# Scans $EDI_HOME/*/escalation.json (default ~/edi-jobs) and emits one line per
# NEW or CHANGED escalation:
#   <job path>\t<reason>\t<recommendation>
# Dedup via $EDI_HOME/.watched — the ONLY file this script writes. Refuses
# without EDI_AGENT_ID (exit 1), like the Python helpers.
#
# Dedup is per-job by the sha256 of escalation.json's CONTENT, NOT a single
# `-newer $MARKER` timestamp and NOT (mtime_ns, size): a global marker misses
# escalations whose mtime ties with the marker update, and a same-size,
# mtime-restored replacement used to slip past an (mtime,size) signature
# (round-3 external review #15). A changed escalation is always flagged.
set -u

EDI_AGENT_ID="${EDI_AGENT_ID:-}"
[ -n "$EDI_AGENT_ID" ] || { echo "refusing: EDI_AGENT_ID is required" >&2; exit 1; }

EDI_HOME="${EDI_HOME:-$HOME/edi-jobs}"
[ -d "$EDI_HOME" ] || exit 0   # nothing to watch yet; cron-safe no-op

exec python3 - "$EDI_HOME" "$EDI_AGENT_ID" <<'PY'
import hashlib, json, pathlib, re, sys

home = pathlib.Path(sys.argv[1])
marker = home / (".watched-" + re.sub(r"[^A-Za-z0-9_.-]", "_", sys.argv[2]))
seen = {}
if marker.is_file():
    for ln in marker.read_text(encoding="utf-8").splitlines():
        if "\t" in ln:
            job, sig = ln.split("\t", 1)
            seen[job] = sig

for esc in sorted(home.glob("*/escalation.json")):
    if not esc.is_file():
        continue
    job = str(esc.parent)
    sig = hashlib.sha256(esc.read_bytes()).hexdigest()
    if seen.get(job) == sig:
        continue  # already alerted, unchanged since
    seen[job] = sig
    try:
        rec = json.loads(esc.read_text(encoding="utf-8"))
    except Exception:
        rec = {}
    reason = rec.get("reason") or "unknown"
    reco = rec.get("recommendation")
    if not reco:
        opts = rec.get("options") or []
        reco = opts[0].get("id") if opts and isinstance(opts[0], dict) else "inspect-state"
    print(f"{job}\t{reason}\t{reco}")

marker.write_text("".join(f"{j}\t{s}\n" for j, s in sorted(seen.items())), encoding="utf-8")
PY
