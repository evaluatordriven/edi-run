#!/usr/bin/env python3
"""P5 cost_report: spend figures from state.json ONLY (read-only).

Missing fields become null, never invented. No cross-checks against job.json.
"""
import argparse
import pathlib
import sys

from common import (EXIT_OK, EXIT_REFUSE, EXIT_USAGE, emit, load_json, refuse,
                    require_agent_id)


def per_model_tokens(state):
    usage = state.get("token_usage")
    if not isinstance(usage, dict):
        return None
    rows = []
    for model, u in usage.items():
        if not isinstance(u, dict):
            continue
        rows.append({
            "model": model,
            "tokens_in": u.get("prompt_tokens", u.get("input_tokens")),
            "tokens_out": u.get("completion_tokens", u.get("output_tokens")),
        })
    return rows or None


def main():
    ap = argparse.ArgumentParser(description="Cost report from state.json only (P5)")
    ap.add_argument("job_dir", help="path to the job directory")
    try:
        a = ap.parse_args()
    except SystemExit:
        sys.exit(EXIT_USAGE)

    require_agent_id()
    job_dir = pathlib.Path(a.job_dir)
    if not job_dir.is_dir():
        refuse(f"not a directory: {job_dir}")

    state = load_json(job_dir / "state.json")
    if state is None:
        refuse(f"no state.json in {job_dir}: nothing has been frozen yet")

    emit({
        "job_dir": str(job_dir),
        "spent_usd": state.get("cost_usd"),
        "cap_usd": state.get("cap_usd"),
        "per_model_tokens": per_model_tokens(state),
        "wall_seconds": state.get("wall_spent_seconds"),
    })


if __name__ == "__main__":
    main()
