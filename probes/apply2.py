import re
import pathlib
p = pathlib.Path("/Users/smoitozo/Code/edi-run/probes/run_probes.py")
s = p.read_text()

# 1) P1/C1/C2/C3 skip on a sandboxless host (deep finding P1-1)
for fn in ("check_p1", "check_c1", "check_c2", "check_c3"):
    pat = re.compile(r"(def %s\(\):\n)(    reset_upstream\(\)\n|    job = agent_job)" % fn)
    guard = "    if SANDBOX_TYPE is None:\n        return \"SKIP\", \"no sandbox on host\"\n"
    m = pat.search(s)
    assert m, fn
    s = s[:m.end(1)] + guard + s[m.end(1):]

# 2) exceptions are never laundered into KNOWN-FAIL (deep P1-2 / adv #5)
old_main = '''            try:
                status, detail = fn()
            except Exception as e:
                status = "FAIL" if known is None else "KNOWN-FAIL"
                detail = "exception: %s: %s" % (type(e).__name__, str(e)[:160])'''
new_main = '''            try:
                status, detail = fn()
            except Exception as e:
                # an exception is never laundered into KNOWN-FAIL: a probe bug or
                # environment failure presents as ERROR and closes the gate
                status, detail = "ERROR", "exception: %s: %s" % (type(e).__name__, str(e)[:160])'''
assert old_main in s
s = s.replace(old_main, new_main)

# 3) a masked check that PASSES is a stale manifest entry: warn, and fail --strict
old_sum = '''    n_fail = sum(1 for r in results if r[2] == "FAIL")
    n_known = sum(1 for r in results if r[2] == "KNOWN-FAIL")'''
new_sum = '''    n_fail = sum(1 for r in results if r[2] == "FAIL") + sum(1 for r in results if r[2] == "ERROR")
    n_known = sum(1 for r in results if r[2] == "KNOWN-FAIL")'''
assert old_sum in s
s = s.replace(old_sum, new_sum)
old_gate = '''    if n_fail:
        print("UNEXPECTED FAILURES - gate closed")
        return 1'''
new_gate = '''    stale = [r[0] for r in results if r[2] == "PASS" and any(k[0] == r[0] for k in CHECKS if k[3])]
    for sid in stale:
        print("STALE KNOWN-FAIL: %s now passes - remove its KNOWN_FAILS entry in this commit" % sid)
    if n_fail:
        print("UNEXPECTED FAILURES - gate closed")
        return 1'''
assert old_gate in s
s = s.replace(old_gate, new_gate)
s = s.replace('''    if strict and n_known:
        print("--strict: %d KNOWN-FAIL remain (S1/C1-C7/R1 backlog)" % n_known)
        return 2''',
'''    if strict and (n_known or stale):
        print("--strict: %d KNOWN-FAIL remain, %d stale entries (S1/C1-C3/R1 backlog)" % (n_known, len(stale)))
        return 2''')

# 4) dedupe the wrong_model entry (first definition is dead)
wms = [m.start() for m in re.finditer(re.escape('"wrong_model": PREAMBLE + ('), s)]
print("wrong_model defs:", len(wms))
if len(wms) == 2:
    # delete the FIRST one: locate its block (ends with '    ),\n')
    first = wms[0]
    end = s.index("    ),\n", first) + len("    ),\n")
    s = s[:first] + s[end:]

# 5) read_proc dead except clause: 'except Exception' with no body pairing
old_rp = '''        "    pids = -1\\n"
        "except Exception:\\n"
        "    val = 'DENIED'\\n"
'''
# find actual shape first
m = re.search(r'"read_proc": PREAMBLE \+ \(\n(.*?)\n    \),\n', s, re.S)
print("read_proc block:")
print(m.group(1))
p.write_text(s)
import ast; ast.parse(s); print("AST OK")
