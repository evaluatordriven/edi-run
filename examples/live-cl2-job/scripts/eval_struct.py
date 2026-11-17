import json, pathlib, re, sys
target, dim = pathlib.Path(sys.argv[1]), sys.argv[2]
txt = (target / 'summary.md').read_text(encoding='utf-8') if target.is_dir() else target.read_text(encoding='utf-8')
words = len(txt.split())
if dim == 'D1':
    ok = txt.startswith('# Incident Summary:') and 40 <= words <= 180
    rem = '' if ok else 'Must start with "# Incident Summary:" and be 40-180 words.'
else:
    has_sec = '## Root Cause & Prevention' in txt
    has_sha = bool(re.search(r'`[0-9a-f]{7}`', txt))
    has_dt = bool(re.search(r'\b\d{4}-\d{2}-\d{2}\b', txt))
    ok = has_sec and has_sha and has_dt
    rem = '' if ok else 'Add section "## Root Cause & Prevention" citing the 7-char hex commit SHA in backticks (e.g. `7f3a91c`) and date as YYYY-MM-DD (e.g. 2026-10-14).'
print(json.dumps({'score': 5.0 if ok else 1.0, 'observation': txt.strip()[:100], 'remedy': rem}))
