"""Deterministic E-DI state machine (I-05 through I-07) wrapping evaluatordriven/custody tools."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import pathlib
import shutil
import subprocess
import sys

import adapters
import gates

VENDOR = pathlib.Path(__file__).resolve().parent / "vendor"
sys.path.insert(0, str(VENDOR))
import check as custody_check  # noqa: E402
import record as custody_record  # noqa: E402


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _subject_digest(art_dir, files):
    parts = [f"{f}:{gates.sha256_file(art_dir / f)}" for f in sorted(files) if (art_dir / f).is_file()]
    return custody_record.digest_text("|".join(parts)) if len(parts) == len(files) else None


def freeze_job(job_dir):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    gates.gate_bounds(cfg)
    gates.gate_seats(cfg, job_dir)
    gates.gate_instruments(cfg, job_dir)
    f_dt = gates.when(cfg["bar"]["frozenAt"])
    s_dt = f_dt - timedelta(seconds=10) if cfg["levelClaimed"] == "baseline" else max(datetime.now(timezone.utc), f_dt + timedelta(seconds=10))
    started_at = _iso(s_dt)
    gates.gate_freeze(cfg, job_dir, started_at=started_at)
    state = {"status": "frozen", "startedAt": started_at, "incumbent_round": None, "incumbent_digest": None,
             "incumbent_scores": None, "last_candidate_digest": None, "defects": [], "solutions": [],
             "rounds": {}, "terminal_state": None, "reason": None}
    (job_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return 0


def _eval_batch(cfg, job_dir, round_n, art_dir, clock):
    bid, dims = f"B{round_n}", cfg["bar"]["dimensions"]
    scales = {d["id"]: d.get("scale") for d in dims}
    ordered = sorted(cfg["evaluators"], key=lambda e: 0 if e["kind"] == "script" else 1)
    checks, controls, scores_rows, externals, dim_scores, defects = [], [], [], [], {}, []
    clean_p, planted_p = job_dir / cfg["controls"]["clean"], job_dir / cfg["controls"]["planted"]

    for ev in ordered:
        ev_id, ctl_failed = ev["id"], False
        for tag, path, exp in (("clean", clean_p, "conforms"), ("planted", planted_p, "does_not_conform")):
            cid, res = f"K-{bid}-{ev_id}-{tag}", adapters.run_evaluator(ev, path, dims[0]["id"], job_dir=job_dir)
            sc, thr = res.get("score"), (scales[dims[0]["id"]] or {}).get("threshold", 4.0)
            out = "conforms" if (sc is not None and float(sc) >= float(thr)) else "does_not_conform"
            ctl_failed = ctl_failed or (out != exp)
            controls.append({"id": cid, "batch": bid, "evaluator": ev_id, "case": {"sha256": gates.sha256_file(path)}, "expected": exp, "outcome": out})
            scores_rows.append({"id": cid, "batch": bid, "evaluator": ev_id, "expected": exp, "outcome": out})
        runs = int(ev.get("runs", 2 if cfg["levelClaimed"] in ("CL2", "CL3") else 1))
        for d in dims:
            did, sc_cfg = d["id"], scales[d["id"]]
            for run_i in range(1, runs + 1):
                clock += timedelta(milliseconds=10)
                har = job_dir / "evidence" / f"harness-{ev_id}-{bid}.json" if ev["kind"] == "model" and run_i == 1 else None
                res = adapters.run_evaluator(ev, art_dir, did, harness_out=har, job_dir=job_dir)
                if ctl_failed or "unobserved" in res:
                    u = res.get("unobserved") or {"channel": ev_id, "reason": "evaluator failed batch control (CL1-9)"}
                    checks.append({"dimension": did, "evaluator": ev_id, "run": run_i, "batch": bid, "at": _iso(clock),
                                   "outcome": "unobserved", "score": None, "failureCondition": f"score < {sc_cfg['threshold']}" if sc_cfg else d["criterion"],
                                   "evidence": None, "unobserved": u})
                else:
                    score = float(res["score"])
                    out = "conforms" if score >= float(sc_cfg["threshold"]) else "does_not_conform"
                    ev_name = f"reading-{did}-{ev_id}-r{round_n}-run{run_i}.json"
                    (job_dir / "evidence" / ev_name).write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
                    checks.append({"dimension": did, "evaluator": ev_id, "run": run_i, "batch": bid, "at": _iso(clock),
                                   "outcome": out, "score": score, "failureCondition": f"score < {sc_cfg['threshold']}",
                                   "evidence": {"name": ev_name, "sha256": gates.sha256_file(job_dir / "evidence" / ev_name)}, "unobserved": None})
                    dim_scores[did] = score
                    if out == "does_not_conform":
                        defects.append({"dimension": did, "score": score, "observation": res.get("observation", ""), "remedy": res.get("remedy", "")})
        if ev["kind"] == "model":
            h_name = f"harness-{ev_id}-{bid}.json"
            externals.append({"requirement": "CL1-15", "subject": ev_id,
                              "evidence": {"name": h_name, "sha256": gates.sha256_file(job_dir / "evidence" / h_name)}, "readBy": cfg["bar"]["author"]})
    return checks, controls, scores_rows, externals, dim_scores, defects, clock


def run_job(job_dir):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg, state = json.loads((job_dir / "job.json").read_text(encoding="utf-8")), json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    max_rounds, clock = int(cfg["bounds"]["max_rounds"]), gates.when(state["startedAt"])
    dims = [d["id"] for d in cfg["bar"]["dimensions"]]
    floors = {d["id"]: (d.get("scale") or {}).get("floor", 0.0) for d in cfg["bar"]["dimensions"]}

    for r in range(1, max_rounds + 1):
        r_dir = job_dir / "rounds" / f"round-{r:02d}"
        art_dir = r_dir / "artifact"
        r_dir.mkdir(parents=True, exist_ok=True)
        pkt = {"round": r, "mode": cfg.get("mode", "pass_r2"), "best_artifact_digest": state["incumbent_digest"],
               "defects": state["defects"], "workable_solutions": state["solutions"]}
        gates.gate_packet(pkt, job_dir, state["incumbent_digest"])
        (r_dir / "packet.json").write_text(json.dumps(pkt, indent=2) + "\n", encoding="utf-8")
        if state["incumbent_round"]:
            shutil.copytree(job_dir / "rounds" / f"round-{state['incumbent_round']:02d}" / "artifact", art_dir, dirs_exist_ok=True)
        adapters.run_maker(cfg["maker"], r_dir / "packet.json", art_dir)
        clock += timedelta(seconds=1)
        produced_at, cand_digest = _iso(clock), _subject_digest(art_dir, cfg["subjectFiles"])
        if cand_digest is None or state["last_candidate_digest"] == cand_digest:
            state.update(terminal_state="needs_owner", reason="no_artifact" if cand_digest is None else "dormant_channel")
            break
        state["last_candidate_digest"] = cand_digest
        checks, ctls, s_rows, exts, d_scores, defects, clock = _eval_batch(cfg, job_dir, r, art_dir, clock)
        state["rounds"][str(r)] = {"producedAt": produced_at, "digest": cand_digest, "checks": checks,
                                   "controls": ctls, "scores": s_rows, "external": exts, "dim_scores": d_scores}
        if any(c["outcome"] == "unobserved" for c in checks):
            if state["incumbent_round"] is None:
                state.update(incumbent_round=r, incumbent_digest=cand_digest, incumbent_scores=d_scores)
            state.update(terminal_state="needs_owner", reason="no_observation")
            break
        inc = state["incumbent_scores"]
        if inc is None or (any(d_scores[d] > inc[d] for d in dims) and not any(d_scores[d] < inc[d] for d in dims)):
            state.update(incumbent_round=r, incumbent_digest=cand_digest, incumbent_scores=d_scores)
        elif any(d_scores[d] > inc[d] for d in dims) and any(d_scores[d] < inc[d] for d in dims):
            state.update(terminal_state="needs_owner", reason="dimension_tradeoff")
            break
        state["defects"] = defects
        if any(d_scores[d] < floors[d] for d in dims):
            state.update(terminal_state="rejected", reason="below_floor")
            break
        if all(c["outcome"] == "conforms" for c in checks) or r == max_rounds:
            state.update(terminal_state="accepted" if all(c["outcome"] == "conforms" for c in checks) else "needs_owner",
                         reason="all_conform" if all(c["outcome"] == "conforms" for c in checks) else "cap_reached")
            break
    (job_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return {"accepted": 0, "rejected": 1, "needs_owner": 2}[state["terminal_state"]]


def finalize_job(job_dir, state="given", by="Owner", ack_unobserved=None):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg, st = json.loads((job_dir / "job.json").read_text(encoding="utf-8")), json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    win_r = str(st["incumbent_round"] or "1")
    win, art_dst, log_path = st["rounds"][win_r], job_dir / "artifact", job_dir / "job.jsonl"
    shutil.rmtree(art_dst, ignore_errors=True)
    shutil.copytree(job_dir / "rounds" / f"round-{int(win_r):02d}" / "artifact", art_dst)
    (job_dir / "scores.jsonl").write_text("\n".join(json.dumps(r) for r in win["scores"]) + "\n", encoding="utf-8")
    log_path.unlink(missing_ok=True)

    def _log(etype, at, actor, files, data):
        custody_record.append(str(log_path), {"type": etype, "at": at, "actor": actor, "source": "",
                                              "files": {str(f.relative_to(pathlib.Path.cwd()) if f.is_relative_to(pathlib.Path.cwd()) else f): gates.sha256_file(f) for f in files}, "data": data})

    f_at, bar_p = cfg["bar"]["frozenAt"], job_dir / cfg["bar"]["file"]
    _log("job-start", _iso(gates.when(f_at) - timedelta(seconds=300)), "edi-run", [], {"job": cfg["job"]})
    _log("bar-frozen", f_at, cfg["bar"]["author"], [bar_p], {})
    for i, a in enumerate(cfg["bar"]["approvals"], 1):
        _log("approval", _iso(gates.when(f_at) + timedelta(milliseconds=i)), a["by"], [bar_p],
             {"role": a["role"], "barDigest": a["barDigest"]["sha256"], "adopted": a.get("adopted")})
    _log("work-started", st["startedAt"], cfg["maker"]["name"], [], {})
    _log("artifact-produced", win["producedAt"], cfg["maker"]["name"], [art_dst / f for f in cfg["subjectFiles"]], {})
    for c in win["checks"]:
        _log("check", c["at"], c["evaluator"], [], c)
    t_ctl = _iso(gates.when(win["checks"][-1]["at"]) + timedelta(milliseconds=5))
    for k in win["controls"]:
        _log("control", t_ctl, k["evaluator"], [], k)
    for ex in win["external"]:
        _log("external", t_ctl, ex["readBy"], [], ex)
    t_disp = _iso(gates.when(t_ctl) + timedelta(seconds=1))
    ack = ack_unobserved if ack_unobserved is not None else cfg["disposition"].get("acknowledgesUnobserved", [])
    _log("disposition", t_disp, by, [], {"state": state, "acknowledgesUnobserved": ack})
    checks = win["checks"]
    violations = [{"dimension": d["id"], "description": f"{d['id']} failed conform threshold"} for d in cfg["bar"]["dimensions"]
                  if any(c["dimension"] == d["id"] and c["outcome"] == "does_not_conform" for c in checks)]
    disagreements = [{"dimension": d["id"], "checks": [i for i, c in enumerate(checks) if c["dimension"] == d["id"]], "note": "evaluators disagreed"}
                     for d in cfg["bar"]["dimensions"] if len({c["outcome"] for c in checks if c["dimension"] == d["id"]}) > 1]
    evals_clean = [{k: v for k, v in e.items() if k in ("id", "name", "kind", "model", "script", "sameModelAsMaker")} for e in cfg["evaluators"]]
    inputs = {"subject": {"files": [f"artifact/{f}" for f in cfg["subjectFiles"]]}, "specVersion": cfg.get("specVersion", "0.9.8"),
              "levelClaimed": cfg["levelClaimed"], "bar": {k: v for k, v in cfg["bar"].items() if k != "file"},
              "maker": {k: v for k, v in cfg["maker"].items() if k in ("name", "kind", "version", "method", "model")},
              "startedAt": st["startedAt"], "producedAt": win["producedAt"], "instrument": cfg["instrument"],
              "evaluators": evals_clean, "checks": checks, "controls": win["controls"], "disagreements": disagreements,
              "violations": violations, "separationGaps": cfg.get("separationGaps", []), "retention": cfg["retention"],
              "external": win["external"], "disposition": {"state": state, "by": by, "at": t_disp, "acknowledgesUnobserved": ack}}
    if cfg["levelClaimed"] in ("CL2", "CL3"):
        inputs["spread"] = [{"dimension": d["id"], "evaluator": ev["id"], "runs": len(outs), "agreement": Counter(outs).most_common(1)[0][1]}
                            for ev in evals_clean for d in cfg["bar"]["dimensions"]
                            if (outs := [c["outcome"] for c in checks if c["evaluator"] == ev["id"] and c["dimension"] == d["id"]])]
    if cfg["levelClaimed"] == "CL3":
        inputs["demonstrations"] = cfg.get("demonstrations", [])
    if cfg["levelClaimed"] == "baseline":
        inputs["readsBeforeFreeze"] = cfg.get("readsBeforeFreeze", [])
    in_p, rec_p = job_dir / "inputs.json", job_dir / "record.json"
    in_p.write_text(json.dumps(inputs, indent=2) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, str(VENDOR / "make_record.py"), str(in_p), str(rec_p), "--log", str(log_path)], check=True)
    rec = json.loads(rec_p.read_text(encoding="utf-8"))
    for s in rec["subject"]:
        s["name"] = s["name"].removeprefix("artifact/")
    rec_p.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return verify_job(job_dir)


def verify_job(job_dir):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    if custody_record.verify(str(job_dir / "job.jsonl"), files=True):
        return 1
    rec = json.loads((job_dir / "record.json").read_text(encoding="utf-8"))
    rep = custody_check.verify(rec, files=str(job_dir / "artifact"), evidence=str(job_dir / "evidence"), bar=str(job_dir / cfg["bar"]["file"]))
    return 0 if rep["verdict"] == "OK" else (1 if rep["verdict"] == "FAILED" else 2)
