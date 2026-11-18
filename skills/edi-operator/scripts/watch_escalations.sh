#!/bin/sh
# P7 watch_escalations: cron-safe, stateless scan for new escalations.
#
# Scans $EDI_HOME/*/escalation.json (default ~/edi-jobs) for files newer than
# the marker; emits one line per new escalation:
#   <job path>\t<reason>\t<recommendation>
# Dedupes via $EDI_HOME/.watched — the ONLY file this script writes. Refuses
# without EDI_AGENT_ID (exit 1), like the Python helpers.
set -u

EDI_AGENT_ID="${EDI_AGENT_ID:-}"
[ -n "$EDI_AGENT_ID" ] || { echo "refusing: EDI_AGENT_ID is required" >&2; exit 1; }

EDI_HOME="${EDI_HOME:-$HOME/edi-jobs}"
[ -d "$EDI_HOME" ] || exit 0   # nothing to watch yet; cron-safe no-op

MARKER="$EDI_HOME/.watched"
FIND_ARGS="-mindepth 2 -maxdepth 2 -name escalation.json -type f"
if [ -f "$MARKER" ]; then
    NEW=$(find "$EDI_HOME" $FIND_ARGS -newer "$MARKER" 2>/dev/null)
else
    NEW=$(find "$EDI_HOME" $FIND_ARGS 2>/dev/null)
fi

EXTRACT='
import json, sys
try:
    rec = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    rec = {}
reason = rec.get("reason") or "unknown"
reco = rec.get("recommendation")
if not reco:
    opts = rec.get("options") or []
    reco = opts[0].get("id") if opts and isinstance(opts[0], dict) else "inspect-state"
print(reason + "\t" + str(reco))
'

FOUND=0
for esc in $NEW; do
    job_dir=$(dirname "$esc")
    fields=$(python3 -c "$EXTRACT" "$esc" 2>/dev/null) || fields="unknown	inspect-state"
    printf '%s\t%s\n' "$job_dir" "$fields"
    FOUND=1
done

# advance the marker even when nothing new fired, so a stale marker never
# re-alerts old escalations after a first run
mkdir -p "$EDI_HOME" 2>/dev/null
: > "$MARKER" 2>/dev/null || true
exit 0
