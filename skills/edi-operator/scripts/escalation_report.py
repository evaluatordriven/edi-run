#!/usr/bin/env python3
"""P5/P6 escalation_report: read-only JSON evidence pack for the §6 review rubric.

Reads escalation.json, state.json, job.jsonl (tail), record.json and
AUTHORSHIP.md — all custody artifacts — read-only. Exits 1 with a clear
message when there is no escalation.json AND no terminal failure state.
"""
import argparse
import json
import pathlib
import sys

from common import (EXIT_OK, EXIT_REFUSE, EXIT_USAGE, emit, load_json, refuse,
                    require_agent_id)

_FAILURE_STATES = {"needs_owner"}


def parse_authorship(job_dir):
    path = job_dir / "AUTHORSHIP.md"
    if not path.is_file():
        return None
    fields = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" in line and line.strip():
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def artifact_digests(job_dir, log_events):
    """Artifact digests: record.json, else state.json (incumbent + tried rounds),
    else artifact-produced/supplied events' top-level `files` in the log."""
    digests = {}
    rec = load_json(job_dir / "record.json")
    if isinstance(rec, dict):
        pred = rec.get("predicate") if isinstance(rec.get("predicate"), dict) else rec
        for subj in (rec.get("subject") or []):
            if isinstance(subj, dict) and isinstance(subj.get("digest"), dict):
                digests[subj.get("name", "artifact")] = subj["digest"].get("sha256")
        bar = (pred.get("bar") or {}).get("digest", {}).get("sha256") \
            if isinstance(pred, dict) else None
        if bar:
            digests["bar.md"] = bar
    if not digests:  # no record yet (escalated/unfinalized): state.json is authoritative
        state = load_json(job_dir / "state.json") or {}
        if state.get("incumbent_digest"):
            digests["incumbent"] = state["incumbent_digest"]
        for rnd, data in sorted((state.get("rounds") or {}).items()):
            if isinstance(data, dict) and data.get("digest"):
                digests[f"round-{rnd}"] = data["digest"]
    if not digests:  # last resort: artifact-produced / artifact-supplied events in the log
        for ev in log_events:
            data = ev.get("data") or {}
            if ev.get("type") in ("artifact-produced", "artifact-supplied"):
                for name, d in (ev.get("files") or {}).items():
                    digests[name] = d.get("sha256") if isinstance(d, dict) else d
            if ev.get("type") == "bar-frozen" and isinstance(data.get("sha256"), str):
                digests["bar.md"] = data["sha256"]
    return digests


def main():
    ap = argparse.ArgumentParser(description="Escalation evidence pack for a job (P5/P6)")
    ap.add_argument("job_dir", help="path to the job directory")
    try:
        a = ap.parse_args()
    except SystemExit:
        sys.exit(EXIT_USAGE)

    require_agent_id()
    job_dir = pathlib.Path(a.job_dir).resolve()  # run_edi switches cwd to the repo: relative paths must resolve HERE
    if not job_dir.is_dir():
        refuse(f"not a directory: {job_dir}")

    escalation = load_json(job_dir / "escalation.json")
    state = load_json(job_dir / "state.json") or {}
    terminal = state.get("terminal_state")

    if escalation is None and terminal not in _FAILURE_STATES:
        refuse(f"no escalation.json and no terminal failure state in {job_dir} "
               f"(terminal_state={terminal!r}): nothing to escalate")

    log_events = []
    log_path = job_dir / "job.jsonl"
    if log_path.is_file():
        with open(log_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        all_events = []
        for line in lines:
            try: all_events.append(json.loads(line))
            except json.JSONDecodeError: pass
        for line in lines[-10:]:
            try:
                log_events.append(json.loads(line))
            except json.JSONDecodeError:
                log_events.append({"unparsed": line[:200]})

    error_fields = {k: state[k] for k in
                    ("integrity_error", "maker_error", "runtime_error") if state.get(k)}

    emit({
        "job_dir": str(job_dir),
        "escalation": escalation,
        "terminal_state": terminal,
        "rounds_completed": len(state.get("rounds") or {}),
        "last_10_log_events": log_events,
        "error_fields": error_fields,
        "artifact_digests": artifact_digests(job_dir, all_events if log_path.is_file() else []),
        "authorship": parse_authorship(job_dir),
    })


if __name__ == "__main__":
    main()
