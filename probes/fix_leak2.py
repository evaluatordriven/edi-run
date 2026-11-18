import re
import pathlib
p = pathlib.Path("/Users/smoitozo/Code/edi-run/probes/run_probes.py")
s = p.read_text()

# 1) WRITE_MEMO appends a probe line from _tail
old_wm = (
    'WRITE_MEMO = (\n'
    '    "art.mkdir(parents=True, exist_ok=True)\\n"\n'
    '    "(art / \'memo.md\').write_text(\'# Header\\\\nFigure: 42% (Source: Q3, 2026-10-01)\\\\n\', encoding=\'utf-8\')\\n"\n'
    ')'
)
new_wm = (
    'WRITE_MEMO = (\n'
    '    "art.mkdir(parents=True, exist_ok=True)\\n"\n'
    '    "(art / \'memo.md\').write_text(\'# Header\\\\nFigure: 42% (Source: Q3, 2026-10-01)\\\\nPROBE \' + _tail + \'\\\\n\', encoding=\'utf-8\')\\n"\n'
    ')'
)
if old_wm in s:
    s = s.replace(old_wm, new_wm)
    print("WRITE_MEMO patched")
else:
    print("WRITE_MEMO already patched or shape differs:", "PROBE ' + _tail" in s)

# 2) PREAMBLE declares _tail
if "_tail = 'none'" not in s:
    old_pre = (
        'PREAMBLE = (\n'
        '    "import json, pathlib, sys, urllib.request, os\\n"\n'
        '    "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\\n"\n'
        '    "art = pathlib.Path(sys.argv[2])\\n"\n'
        ')'
    )
    new_pre = (
        'PREAMBLE = (\n'
        '    "import json, pathlib, sys, urllib.request, os\\n"\n'
        '    "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\\n"\n'
        '    "art = pathlib.Path(sys.argv[2])\\n"\n'
        '    "_tail = \'none\'\\n"\n'
        ')'
    )
    assert old_pre in s, "PREAMBLE shape"
    s = s.replace(old_pre, new_pre)
    print("PREAMBLE patched")

# 3) replace every sidecar leak.txt writer with a PROBE_TAIL assignment
n_before = s.count("(art / 'leak.txt')")
pat = re.compile(
    r"\+ WRITE_MEMO \+\n"
    r"(\s*)\"\(art / 'leak\.txt'\)\.write_text\('([A-Z_]+)=' \+ ([^,]+), encoding='utf-8'\)\\n\""
)
def repl(m):
    return '"_tail = \'%s=\' + %s\\n" + WRITE_MEMO' % (m.group(2), m.group(3))
s = pat.sub(repl, s)
# crude catch-all: any remaining leak.txt writer -> _tail = LEAK=unknown
leftover = re.compile(
    r"\+ WRITE_MEMO \+\n(\s*)\"\(art / 'leak\.txt'\)[^\"]*\\n\""
)
s = leftover.sub('"_tail = \'LEAK=unknown\'\\n" + WRITE_MEMO', s)
print("sidecar writers before:", n_before, "after:", s.count("'leak.txt'"))

# 4) agent_leak reads the PROBE line from memo.md
if "PROBE LEAK=" not in s:
    old_leak = (
        '    return leak_value(rs[-1] / "artifact", "LEAK")'
    )
    new_leak = (
        '    memo = rs[-1] / "artifact" / "memo.md"\n'
        '    if not memo.is_file():\n'
        '        return "(no memo)"\n'
        '    for ln in memo.read_text().splitlines():\n'
        '        if ln.startswith("PROBE LEAK="):\n'
        '            return ln[len("PROBE "):]\n'
        '    return "(no PROBE line)"'
    )
    assert old_leak in s, "agent_leak shape"
    s = s.replace(old_leak, new_leak)
    print("agent_leak patched")

p.write_text(s)
import ast; ast.parse(s); print("AST OK")
