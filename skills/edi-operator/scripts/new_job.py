#!/usr/bin/env python3
"""P1 new_job: scaffold an edi-run job under $EDI_HOME via `edi.py init`.

Never writes bar/rubric/method/seed CONTENT; only fills the AUTHORSHIP.md
Operator line with EDI_AGENT_ID (plus an optional extra author). A --seed-dir
path is copied VERBATIM into the job's seed/ directory (transcription of an
existing seed, not authorship) and the copy is recorded in the output JSON.
"""
import argparse
import json
import pathlib
import shutil
import sys

from common import (EXIT_OK, EXIT_REFUSE, EXIT_USAGE, edi_home, emit, refuse,
                    require_agent_id, run_edi)


def main():
    ap = argparse.ArgumentParser(description="Scaffold a new edi-run job (P1)")
    ap.add_argument("--name", required=True, help="job name (created at $EDI_HOME/<name>)")
    ap.add_argument("--seed-dir", default=None, type=pathlib.Path,
                    help="existing seed directory; copied verbatim into the job's seed/")
    ap.add_argument("--author", default=None,
                    help="optional extra author recorded alongside the Operator identity")
    ap.add_argument("--mode", default=None, choices=("scored", "readings"),
                    help="optional runtime mode ('scored' or 'readings')")
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

    seed_copied = None
    if a.seed_dir is not None:
        if not a.seed_dir.is_dir():
            refuse(f"--seed-dir is not a directory: {a.seed_dir}")
        dest = job_dir / "seed"
        shutil.copytree(a.seed_dir, dest, dirs_exist_ok=True)
        seed_copied = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())

    authorship = job_dir / "AUTHORSHIP.md"
    lines = authorship.read_text(encoding="utf-8").splitlines()
    out = []
    operator = f"Operator: {agent}" + (f" (with {a.author})" if a.author else "")
    for line in lines:
        out.append(operator if line.startswith("Operator:") else line)
    if a.author and not any(l.startswith("Operator:") for l in lines):
        out.append(operator)
    authorship.write_text("\n".join(out) + "\n", encoding="utf-8")
    if a.mode:
        import json
        jpath = job_dir / "job.json"
        jcfg = json.loads(jpath.read_text(encoding="utf-8"))
        jcfg["mode"] = a.mode
        if a.mode == "readings":
            jcfg["bounds"]["max_rounds"] = 1
        jpath.write_text(json.dumps(jcfg, indent=2) + "\n", encoding="utf-8")

    emit({"job_dir": str(job_dir), "created": True,
          "authorship_path": str(authorship),
          "seed_dir": str(a.seed_dir) if a.seed_dir else None,
          "seed_files_copied": seed_copied,
          "operator": agent, "mode": a.mode})


if __name__ == "__main__":
    main()
