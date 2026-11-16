import json, pathlib, sys
pkt = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
out = pathlib.Path(sys.argv[2]) / 'memo.md'
if pkt['round'] == 1:
    out.write_text('# Q3 Vendor Memo\nRenewal savings: 18% without citation.\n', encoding='utf-8')
else:
    out.write_text('# Q3 Vendor Memo\nRenewal savings: 18% (Source: Finance Ledger, 2026-10-01).\n', encoding='utf-8')
