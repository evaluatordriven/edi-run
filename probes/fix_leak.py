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
assert old_wm in s, "WRITE_MEMO shape"
s = s.replace(old_wm, new_wm)

# 2) PREAMBLE declares _tail
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

# 3) replace every sidecar leak.txt writer with a PROBE_TAIL assignment
n_sidecars = s.count("(art / 'leak.txt')")
s = re.sub(
    r'\+ WRITE_MEMO \+\n(\s*)"\(art / \'leak\.txt\'\)\.write_text\(\'([A-Z_]+)=\' \+ ([^,]+), encoding=\'utf-8\'\)\\n"',
    lambda m: '"_tail = \'%s=\' + %s\\n" + WRITE_MEMO' % (m.group(2), m.group(3)),
    s)
s = re.sub(
    r'\+ WRITE_MEMO \+\n(\s*)"(art / \'leak\.txt\'\)\.write_text\(.*\)\\n"',
    lambda m: '"_tail = \'LEAK=unknown\\n" + WRITE_MEMO',
    s)
print("sidecar writers before:", n_sidecars, "after:", s.count("'leak.txt'"))

# 4) agent_leak reads the PROBE line from memo.md
old_leak = (
    'def agent_leak(job):\n'
    '    rounds = job / "rounds"\n'
    '    rs = sorted(rounds.glob("round-*")) if rounds.is_dir() else []\n'
    '    if not rs:\n'
    '        return "(no round artifact)"\n'
    '    return leak_value(rs[-1] / "artifact", "LEAK")'
)
new_leak = (
    'def agent_leak(job):\n'
    '    rounds = job / "rounds"\n'
    '    rs = sorted(rounds.glob("round-*")) if rounds.is_dir() else []\n'
    '    if not rs:\n'
    '        return "(no round artifact)"\n'
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

p.write_text(s)
import ast; ast.parse(s); print("AST OK")
