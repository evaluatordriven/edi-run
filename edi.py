#!/usr/bin/env python3
"""CLI entrypoint for edi-run (freeze, run, finalize, verify). Standard library only."""
import argparse, pathlib, sys
import loop


def main():
    ap = argparse.ArgumentParser(description="E-DI minimal reference runtime")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("freeze", "run", "verify", "resume"):
        sub.add_parser(name).add_argument("job_dir", type=pathlib.Path)
    fin = sub.add_parser("finalize")
    fin.add_argument("job_dir", type=pathlib.Path)
    fin.add_argument("--disposition", required=True, choices=("given", "not_yet_given", "declined"))
    fin.add_argument("--by", required=True)
    fin.add_argument("--ack-unobserved", default="")
    a = ap.parse_args()
    if a.cmd == "finalize":
        ack = [x.strip() for x in a.ack_unobserved.split(",") if x.strip()] if a.ack_unobserved else None
        return loop.finalize_job(a.job_dir, state=a.disposition, by=a.by, ack_unobserved=ack)
    return getattr(loop, f"{a.cmd}_job")(a.job_dir)


if __name__ == "__main__":
    sys.exit(main())
