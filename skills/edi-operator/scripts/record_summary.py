#!/usr/bin/env python3
"""P4 record_summary: run `edi.py verify <job>` and summarize the record.

All custody fields (record.json) are read read-only. Reports the verify exit
code verbatim; verdict maps it (0→OK, 5→FAILED, 6→UNKNOWN, 1→REFUSED — the
edi.py EXIT_CODES) unless the record itself carries a verdict.
"""
import argparse
import pathlib
import sys

from common import (EXIT_OK, EXIT_REFUSE, EXIT_USAGE, emit, load_json, refuse,
                    require_agent_id, run_edi)

_VERDICT_BY_EXIT = {0: "OK", 5: "FAILED", 6: "UNKNOWN", 1: "REFUSED"}  # edi.py EXIT_CODES


def scores_from_record(pred):
    """Per-dimension scores: best observed score per dimension across checks."""
    scores = {}
    for check in pred.get("checks") or []:
        dim, score = check.get("dimension"), check.get("score")
        if dim is None or not isinstance(score, (int, float)) or isinstance(score, bool):
            continue
        if check.get("outcome") == "unobserved":
            continue
        prev = scores.get(dim)
        scores[dim] = score if prev is None else max(prev, score)
    return scores


def gaps_from_record(pred):
    gaps = []
    ack = ((pred.get("disposition") or {}).get("acknowledgesUnobserved")) or []
    for check in pred.get("checks") or []:
        if check.get("outcome") == "unobserved":
            gap = f"{check.get('evaluator')}:{check.get('dimension')}"
            if gap not in gaps:
                gaps.append(gap)
    for d in pred.get("disagreements") or []:
        if isinstance(d, dict):
            gaps.append(f"disagreement:{d.get('dimension')}:{d.get('evaluator')}")
        elif isinstance(d, str):
            gaps.append(f"disagreement:{d}")
    gaps.extend(a for a in ack if a not in gaps)
    return gaps


def main():
    ap = argparse.ArgumentParser(description="Verify + summarize a closed-out job (P4)")
    ap.add_argument("job_dir", help="path to the job directory")
    try:
        a = ap.parse_args()
    except SystemExit:
        sys.exit(EXIT_USAGE)

    require_agent_id()
    job_dir = pathlib.Path(a.job_dir)
    if not job_dir.is_dir():
        refuse(f"not a directory: {job_dir}")

    proc = run_edi(["verify", job_dir])
    verified = proc.returncode == 0

    pred = {}
    rec = load_json(job_dir / "record.json")
    if isinstance(rec, dict):
        pred = rec.get("predicate") if isinstance(rec.get("predicate"), dict) else rec

    disposition = pred.get("disposition") if isinstance(pred, dict) else None
    verdict = None
    if isinstance(pred, dict) and pred.get("verdict"):
        verdict = pred["verdict"]
    else:
        verdict = _VERDICT_BY_EXIT.get(proc.returncode)

    maker = pred.get("maker") if isinstance(pred, dict) else None
    maker_name = maker.get("name") if isinstance(maker, dict) else None
    if maker_name is None and isinstance(maker, dict):
        model = maker.get("model")
        maker_name = model.get("identifier") if isinstance(model, dict) else model

    emit({
        "job_dir": str(job_dir),
        "verified": verified,
        "verify_exit_code": proc.returncode,  # verbatim
        "verify_message": (proc.stderr or proc.stdout).strip() or None,
        "verdict": verdict,
        "disposition": (disposition or {}).get("state") if isinstance(disposition, dict) else disposition,
        "disposition_by": (disposition or {}).get("by") if isinstance(disposition, dict) else None,
        "scores": scores_from_record(pred) if isinstance(pred, dict) else {},
        "maker_of_record": maker_name,
        "gaps": gaps_from_record(pred) if isinstance(pred, dict) else [],
    }, EXIT_OK if verified else EXIT_REFUSE)


if __name__ == "__main__":
    main()
