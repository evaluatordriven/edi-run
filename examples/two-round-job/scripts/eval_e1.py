import json, pathlib, sys
target, dim = pathlib.Path(sys.argv[1]), sys.argv[2]
txt = (target / 'memo.md').read_text(encoding='utf-8') if target.is_dir() else target.read_text(encoding='utf-8')
if dim == 'D1':
    score = 5.0 if txt.startswith('# ') else 1.0
else:
    score = 5.0 if 'Source:' in txt else 1.0
print(json.dumps({'score': score, 'observation': txt.strip()[:80], 'remedy': 'Add Source: citation' if score < 4.0 else ''}))
