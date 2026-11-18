#!/usr/bin/env python3
"""P1 new_job: scaffold an edi-run job under $EDI_HOME via `edi.py init`.

Never writes bar/rubric/method/seed content; only fills the AUTHORSHIP.md
Operator line with EDI_AGENT_ID (plus an optional extra author). A --seed-dir
path is RECORDED in the output JSON only — control content is never copied.
"""
import argparse
import pathlib
import sys

from common import (EXIT_OK, EXIT_REFUSE, EXIT_USAGE, edi_home, emit, refuse,
                    require_agent_id, run_edi)


def main():
    ap = argparse.ArgumentParser(description="Scaffold a new edi-run job (P1)")
    ap.add_argument("--name", required=True, help="job name (created at $EDI_HOME/<name>)")
    ap.add_argument("--seed-dir", default=None,
                    help="existing seed directory; its PATH is recorded, never copied")
    ap.add_argument("--author", default=None,
                    help="optional extra author recorded alongside the Operator identity")
    try:
        a = ap.parse_args()
    except SystemExit:
        sys.exit(EXIT_USAGE)

    agent = require_agent_id()
    if "/" in a.name or a.name in (".", "..") or a.name.startswith("."):
        refuse(f"invalid job name {a.name!r}: must be a plain directory name")
    job_dir = edi_home() / a.name
    if job_dir.exists():
        refuse(f"job already exists: {job_dir}")

    proc = run_edi(["init", job_dir])
    if proc.returncode != 0:
        print((proc.stderr or proc.stdout).strip(), file=sys.stderr)
        sys.exit(EXIT_REFUSE)

    authorship = job_dir / "AUTHORSHIP.md"
    lines = authorship.read_text(encoding="utf-8").splitlines()
    out = []
    operator = f"Operator: {agent}" + (f" (with {a.author})" if a.author else "")
    for line in lines:
        out.append(operator if line.startswith("Operator:") else line)
    if a.author and not any(l.startswith("Operator:") for l in lines):
        out.append(operator)
    authorship.write_text("\n".join(out) + "\n", encoding="utf-8")

    emit({"job_dir": str(job_dir), "created": True,
          "authorship_path": str(authorship),
          "seed_dir": a.seed_dir, "operator": agent})


if __name__ == "__main__":
    main()
