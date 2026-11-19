#!/usr/bin/env python3
"""P2 preflight: thin read-only wrapper around `edi.py freeze <job> --dry-run`.

Never freezes. Maps the runtime's exit code to a JSON verdict and additionally
warns when job.json pricing contains a 0-price model seat (read read-only).
"""
import argparse
import pathlib
import sys

from common import (EXIT_OK, EXIT_REFUSE, EXIT_USAGE, emit, load_json, refuse,
                    require_agent_id, run_edi)


def zero_price_warnings(cfg):
    """Warning for every pricing entry that contains a 0 rate (a `$0` seat
    passes some runtimes but lies to the cost report — spec §3.6)."""
    warnings = []
    pricing = cfg.get("pricing") if isinstance(cfg, dict) else None
    if isinstance(pricing, dict):
        for key, entry in pricing.items():
            if not isinstance(entry, dict):
                continue
            for field in ("input_usd_per_mtok", "output_usd_per_mtok"):
                rate = entry.get(field)
                if isinstance(rate, (int, float)) and not isinstance(rate, bool) \
                        and float(rate) == 0.0:
                    warnings.append(
                        f"pricing['{key}'].{field} is 0: a $0-placeholder seat "
                        "lies to the cost report; confirm with the Owner before freezing")
    return warnings


def main():
    ap = argparse.ArgumentParser(description="Read-only preflight of an edi-run job (P2)")
    ap.add_argument("job_dir", help="path to the job directory")
    try:
        a = ap.parse_args()
    except SystemExit:
        sys.exit(EXIT_USAGE)

    require_agent_id()
    job_dir = pathlib.Path(a.job_dir).resolve()  # run_edi switches cwd to the repo: relative paths must resolve HERE
    if not job_dir.is_dir():
        refuse(f"not a directory: {job_dir}")

    proc = run_edi(["freeze", job_dir, "--dry-run"])
    if proc.returncode not in (0, 1):
        print((proc.stderr or proc.stdout).strip(), file=sys.stderr)
        sys.exit(EXIT_USAGE if proc.returncode == 2 else EXIT_REFUSE)

    violations = []
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip() or "freeze --dry-run refused"
        violations.append(msg)

    cfg = load_json(job_dir / "job.json") or {}
    warnings = zero_price_warnings(cfg)

    emit({"pass": proc.returncode == 0, "violations": violations,
          "warnings": warnings},
         EXIT_OK if proc.returncode == 0 else EXIT_REFUSE)


if __name__ == "__main__":
    main()
