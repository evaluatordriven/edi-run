#!/usr/bin/env python3
"""P4 record_summary: summarize a job before and after the Owner's finalize.

Two stages:
- pre-disposition (no `disposition` event in job.jsonl yet): chain-verify the
  custody log with vendor/record.py, report the run outcome and spend from
  state.json, and label the output `stage: pre-disposition`. Exit 0.
- post-finalize: run `edi.py verify <job>` and summarize record.json (plus, for
  readings jobs, the per-run outcome words and quotations from state.json).

All custody fields are read read-only. Reports the verify exit code verbatim;
verdict maps it (0→OK, 5→FAILED, 6→UNKNOWN, 1→REFUSED — the edi.py EXIT_CODES)
unless the record itself carries a verdict. Scores are reported per dimension
with EVERY evaluator/run observation plus min and max — a split between
evaluators is never collapsed to the best score (the Owner rules on the spread).
"""
import argparse
import json
import pathlib
import subprocess
import sys

from common import (EXIT_OK, EXIT_REFUSE, EXIT_USAGE, emit, load_json, refuse,
                    require_agent_id, repo_root, resolve_job, run_edi)

_VERDICT_BY_EXIT = {0: "OK", 5: "FAILED", 6: "UNKNOWN", 1: "REFUSED"}  # edi.py EXIT_CODES


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def scores_from_record(pred):
    """Per-dimension observations: every evaluator/run score, plus min and max.

    A dimension straddling its threshold (min below, max at/above) is flagged —
    the Owner reads the spread, never a single best score.
    """
    dims = {}
    for check in (pred or {}).get("checks") or []:
        dim, score = check.get("dimension"), check.get("score")
        if dim is None or not _num(score) or check.get("outcome") == "unobserved":
            continue
        entry = {"evaluator": check.get("evaluator"), "run": check.get("run"),
                 "score": score}
        if check.get("outcome"):
            entry["outcome"] = check.get("outcome")
        dims.setdefault(dim, []).append(entry)
    out = {}
    for dim, obs in dims.items():
        vals = [o["score"] for o in obs]
        out[dim] = {"observations": obs, "min": min(vals), "max": max(vals),
                    "straddles_threshold": None}
    return out


def _flag_thresholds(scores, job_cfg):
    dims = ((job_cfg or {}).get("bar") or {}).get("dimensions") or []
    for d in dims:
        entry = scores.get(d.get("id"))
        if not entry or entry["min"] is None:
            continue
        thr = (d.get("scale") or {}).get("threshold")
        if _num(thr):
            entry["straddles_threshold"] = entry["min"] < thr <= entry["max"]


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


def _load_events(job_dir):
    path = job_dir / "job.jsonl"
    if not path.is_file():
        return []
    try:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except json.JSONDecodeError:
        return []


def _chain_verify(job_dir):
    """Verify the job.jsonl chain with the vendored custody verifier (pre-finalize).

    Runs with cwd=job_dir: event file paths are job-relative.
    """
    return subprocess.run(
        [sys.executable, str(repo_root() / "vendor" / "record.py"), "verify",
         str(job_dir / "job.jsonl"), "--files"],
        capture_output=True, text=True, timeout=120, cwd=str(job_dir))


def _readings_report(state):
    """Per-dimension outcome word + quotation for every Evaluator and run."""
    dims = {}
    for rnd in sorted((state.get("rounds") or {}), key=lambda k: int(k) if k.isdigit() else 0):
        for check in ((state.get("rounds") or {}).get(rnd) or {}).get("checks") or []:
            if check.get("outcome") is None:
                continue
            dims.setdefault(check.get("dimension"), []).append({
                "evaluator": check.get("evaluator"), "run": check.get("run"),
                "outcome": check.get("outcome"), "quotation": check.get("quotation")})
    return dims


def main():
    ap = argparse.ArgumentParser(description="Summarize a job (P4: pre-disposition, then post-finalize)")
    ap.add_argument("job_dir", help="path to the job directory")
    try:
        a = ap.parse_args()
    except SystemExit:
        sys.exit(EXIT_USAGE)

    require_agent_id()
    job_dir = resolve_job(a.job_dir)  # run_edi switches cwd to the repo: relative paths must resolve HERE
    if not job_dir.is_dir():
        refuse(f"not a directory: {job_dir}")

    events = _load_events(job_dir)
    disp_evs = [e for e in events if e.get("type") == "disposition"]
    state = load_json(job_dir / "state.json") or {}

    if not disp_evs:
        # Pre-disposition stage: the job ran; the Owner has not ruled yet.
        chain = _chain_verify(job_dir)
        emit({
            "job_dir": str(job_dir),
            "stage": "pre-disposition",
            "finalized": False,
            "chain_verified": chain.returncode == 0,
            "chain_message": (chain.stderr or chain.stdout).strip() or None,
            "verified": None,
            "terminal_state": state.get("terminal_state"),
            "reason": state.get("reason"),
            "scores": state.get("incumbent_scores") or {},
            "spent_usd": state.get("cost_usd"),
            "next": "awaiting the Owner's ruling; finalize names the disposition",
        }, EXIT_OK)

    ddata = disp_evs[-1].get("data") or {}
    proc = run_edi(["verify", job_dir])
    verified = proc.returncode == 0

    pred = {}
    rec = load_json(job_dir / "record.json")
    if isinstance(rec, dict):
        pred = rec.get("predicate") if isinstance(rec.get("predicate"), dict) else rec

    disposition = pred.get("disposition") if isinstance(pred, dict) else None
    disp_state = (disposition or {}).get("state") if isinstance(disposition, dict) else disposition
    disp_by = (disposition or {}).get("by") if isinstance(disposition, dict) else None
    if not disp_state:
        disp_state = ddata.get("state")
        disp_by = disp_evs[-1].get("actor")
    verdict = pred["verdict"] if isinstance(pred, dict) and pred.get("verdict") else _VERDICT_BY_EXIT.get(proc.returncode)

    maker = pred.get("maker") if isinstance(pred, dict) else None
    maker_name = maker.get("name") if isinstance(maker, dict) else None
    if maker_name is None and isinstance(maker, dict):
        model = maker.get("model")
        maker_name = model.get("identifier") if isinstance(model, dict) else model

    scores = scores_from_record(pred) if isinstance(pred, dict) else {}
    _flag_thresholds(scores, load_json(job_dir / "job.json"))
    gaps = gaps_from_record(pred) if isinstance(pred, dict) else []

    payload = {
        "job_dir": str(job_dir),
        "stage": "finalized",
        "finalized": True,
        "verified": verified,
        "verify_exit_code": proc.returncode,  # verbatim
        "verify_message": (proc.stderr or proc.stdout).strip() or None,
        "verdict": verdict,
        "disposition": disp_state,
        "disposition_by": disp_by,
        "scores": scores,
        "maker_of_record": maker_name,
        "gaps": gaps,
    }

    # Readings jobs emit no record.json by design: report the disposition, the
    # per-run outcome words and quotations, and any disagreements in full.
    job_cfg = load_json(job_dir / "job.json") or {}
    if job_cfg.get("mode") == "readings":
        payload["mode"] = "readings"
        payload["readings"] = _readings_report(state)
        payload["disagreements"] = ddata.get("disagreements") or []
        if not maker_name:
            prods = [e for e in events if e.get("type") == "artifact-produced"]
            if prods:
                payload["maker_of_record"] = prods[-1].get("actor")

    emit(payload, EXIT_OK if verified else EXIT_REFUSE)


if __name__ == "__main__":
    main()
