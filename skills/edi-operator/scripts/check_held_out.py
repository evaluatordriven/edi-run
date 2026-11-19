#!/usr/bin/env python3
"""Scan a file for held-out material before anything leaves a job (P5/§6).

Usage: check_held_out.py <job_dir> <file> [<file> ...]

Runs edi-run's held-out filter (the same needles the Maker packet gate uses)
over the given files. Prints per-file match counts and exits non-zero if ANY
match is found. On a match, return the file to its author — never edit it
yourself (an operator edit could itself leak or corrupt evidence).

Disclosed limit: paraphrases and fragments shorter than the needle length are
not caught. This check is necessary, not sufficient.
"""
import sys
from pathlib import Path

_p = Path(__file__).resolve()
while _p != _p.parent and not (_p / "gates.py").is_file():
    _p = _p.parent
sys.path.insert(0, str(_p))
import gates  # noqa: E402
from common import refuse, require_agent_id, resolve_job  # noqa: E402


def _resolve(p):
    r = resolve_job(p)
    if r is None:
        refuse(f"cannot resolve path: {p}")
    return r


def main():
    try:
        import argparse
        ap = argparse.ArgumentParser(description="Check files for held-out material before delivery (P5)")
        ap.add_argument("job_dir")
        ap.add_argument("files", nargs="+")
        a = ap.parse_args()
    except SystemExit:
        sys.exit(2)

    require_agent_id()
    job_dir = _resolve(a.job_dir)
    if not job_dir.is_dir():
        refuse(f"not a directory: {job_dir}")
    windows, digests, lines = gates.held_out_needles(job_dir)
    needle_set = (windows, digests, lines)
    any_match = False
    for f in a.files:
        p = _resolve(f)
        if not p.is_file():
            refuse(f"not a file: {p}")
        text = gates._canon(p.read_text(encoding="utf-8", errors="ignore"))
        hits = sum(1 for w in windows if w in text)
        hits += sum(1 for d in digests if d in text)
        hits += sum(1 for ln in lines if ln in text)
        print(f"{p.name}: {hits} match(es)")
        any_match = any_match or hits > 0
    sys.exit(1 if any_match else 0)


if __name__ == "__main__":
    main()
