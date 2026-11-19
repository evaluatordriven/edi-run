#!/usr/bin/env python3
"""Deterministic structural & pin Evaluator (E1) for Plumbline readings census."""
import json, pathlib, sys

target = pathlib.Path(sys.argv[1])
dim = sys.argv[2]
if target.is_dir():
    txt = "\n---\n".join(p.read_text(encoding="utf-8") for p in sorted(target.rglob("*")) if p.is_file())
else:
    txt = target.read_text(encoding="utf-8")

def find_quote(needles):
    for n in needles:
        if n in txt:
            return n
    return ""

if dim == "D1":
    req = ("python3 tests/run_all.py", "PLUMBLINE-TALLY pass=7 fail=0 skip=5", "EDI_CORPUS", "PLUMBLINE_REQUIRE_CORPUS", "CUSTODY_DIR")
    if all(k in txt for k in req):
        print(json.dumps({"outcome": "conforms", "quotation": "PLUMBLINE-TALLY pass=7 fail=0 skip=5"}))
    else:
        q = find_quote(["invoke pytest in your environment", "Quickstart"]) or txt.splitlines()[0]
        print(json.dumps({"outcome": "does_not_conform", "quotation": q}))
elif dim == "D2":
    req = ("hand-time", "--allow-hand-times", "C37–C40", "disclosed gaps, not covered rules")
    if all(k in txt for k in req):
        print(json.dumps({"outcome": "conforms", "quotation": "They are disclosed gaps, not covered rules."}))
    else:
        q = find_quote(["All Contracts v1.26.0 rules including C37 through C40 are 100% covered", "Coverage"]) or txt.splitlines()[0]
        print(json.dumps({"outcome": "does_not_conform", "quotation": q}))
elif dim == "D3":
    req = ("3e3ca58", "f8bfb793", "kit/plumbline_verify.py", "checker/check.py")
    if all(k in txt for k in req):
        print(json.dumps({"outcome": "conforms", "quotation": "kit/plumbline_verify.py"}))
    else:
        q = find_quote(["A single unified checker validates both standard custody fields", "Verification"]) or txt.splitlines()[0]
        print(json.dumps({"outcome": "does_not_conform", "quotation": q}))
else:
    print(json.dumps({"outcome": "unobserved", "reason": f"unknown dimension {dim}"}))
