#!/usr/bin/env python3
"""CLI entrypoint for edi-run (freeze, run, finalize, verify). Standard library only."""
import argparse, pathlib, sys
import gates, loop


def main():
    ap = argparse.ArgumentParser(description="E-DI minimal reference runtime")
    sub = ap.add_subparsers(dest="cmd", required=True)
    frz = sub.add_parser("freeze")
    frz.add_argument("job_dir", type=pathlib.Path)
    frz.add_argument("--dry-run", action="store_true",
                     help="run all freeze gates read-only; write nothing")
    for name in ("run", "verify", "resume"):
        sub.add_parser(name).add_argument("job_dir", type=pathlib.Path)
    sub.add_parser("init").add_argument("job_dir", type=pathlib.Path)
    fin = sub.add_parser("finalize")
    fin.add_argument("job_dir", type=pathlib.Path)
    fin.add_argument("--disposition", required=True, choices=("given", "not_yet_given", "declined"))
    fin.add_argument("--by", required=True)
    fin.add_argument("--ack-unobserved", default="")
    a = ap.parse_args()
    try:
        if a.cmd == "init":
            return loop.init_job(a.job_dir)
        if a.cmd == "finalize":
            ack = [x.strip() for x in a.ack_unobserved.split(",") if x.strip()] if a.ack_unobserved else None
            return loop.finalize_job(a.job_dir, state=a.disposition, by=a.by, ack_unobserved=ack)
        if a.cmd == "freeze":
            return loop.freeze_job(a.job_dir, dry_run=a.dry_run)
        return getattr(loop, f"{a.cmd}_job")(a.job_dir)
    except gates.GateError as e:
        # a gate refusal is an expected outcome, not a crash: one clean line,
        # exit 1 (argparse usage errors still exit 2 on their own)
        print(f"gate refusal: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
