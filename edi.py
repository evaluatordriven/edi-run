#!/usr/bin/env python3
"""CLI entrypoint for edi-run (freeze, run, read, finalize, verify). Standard library only."""
import argparse, pathlib, sys
import gates, loop

# G:exit-codes 1/2 reserved for gate refusal and usage; run/record outcomes use 3..6
VERSION = "0.1.3"
EXIT_CODES = {"ok": 0, "refused": 1, "usage": 2, "rejected": 3, "needs_owner": 4, "record_failed": 5, "record_undecided": 6}
_RUN = {0: 0, 1: EXIT_CODES["rejected"], 2: EXIT_CODES["needs_owner"]}
_RECORD = {0: 0, 1: EXIT_CODES["record_failed"], 2: EXIT_CODES["record_undecided"]}


def main():
    ap = argparse.ArgumentParser(description="E-DI minimal reference runtime")
    ap.add_argument("--version", action="version", version=f"edi-run {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    frz = sub.add_parser("freeze")
    frz.add_argument("job_dir", type=pathlib.Path)
    frz.add_argument("--dry-run", action="store_true", help="run all freeze gates read-only; write nothing")
    for name in ("run", "read", "verify", "resume", "init"):
        sub.add_parser(name).add_argument("job_dir", type=pathlib.Path)
    fin = sub.add_parser("finalize")
    fin.add_argument("job_dir", type=pathlib.Path)
    fin.add_argument("--disposition", required=True, choices=("given", "not_yet_given", "declined"))
    fin.add_argument("--by", required=True)
    fin.add_argument("--ack-unobserved", default="")
    a = ap.parse_args()
    try:
        if a.cmd == "init": return loop.init_job(a.job_dir)
        if a.cmd == "finalize":
            ack = [x.strip() for x in a.ack_unobserved.split(",") if x.strip()] if a.ack_unobserved else None
            return _RECORD.get(loop.finalize_job(a.job_dir, state=a.disposition, by=a.by, ack_unobserved=ack), EXIT_CODES["record_undecided"])
        if a.cmd == "freeze": return loop.freeze_job(a.job_dir, dry_run=a.dry_run)
        rc = getattr(loop, f"{a.cmd}_job")(a.job_dir)
        return _RUN.get(rc, EXIT_CODES["needs_owner"]) if a.cmd in ("run", "read", "resume") else _RECORD.get(rc, EXIT_CODES["record_undecided"])
    except gates.GateError as e:
        print(f"gate refusal: {e}", file=sys.stderr); return 1


if __name__ == "__main__":
    sys.exit(main())
