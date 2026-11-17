"""Deterministic E-DI state machine (I-05 through I-07) wrapping evaluatordriven/custody tools."""
from collections import Counter
import json, math, pathlib, shutil, subprocess, sys, time
import adapters, gates

VENDOR = pathlib.Path(__file__).resolve().parent / "vendor"
sys.path.insert(0, str(VENDOR))
import check as custody_check, record as custody_record  # noqa: E402


def _append(job_dir, etype, actor, files, data):
    cwd = pathlib.Path.cwd().resolve()
    rel = {str(f.resolve().relative_to(cwd) if f.resolve().is_relative_to(cwd) else f.resolve()): gates.sha256_file(f) for f in files}
    return custody_record.append(str(job_dir / "job.jsonl"), {"type": etype, "at": custody_record.now(), "actor": actor, "source": "", "files": rel, "data": data})


def _subject_digest(art_dir, files, held_hashes):
    parts = [f"{f}:{gates.sha256_file(gates.safe_rel(f, art_dir))}" for f in sorted(files) if gates.safe_rel(f, art_dir).is_file()]
    if len(parts) != len(files):
        return None
    if any(p.split(":", 1)[1] in held_hashes for p in parts):
        raise gates.GateError("artifact matches held-out file digest (I-04)")
    return custody_record.digest_text("|".join(parts))


def freeze_job(job_dir, started_at=None):
    job_dir, cfg = pathlib.Path(job_dir).resolve(), json.loads((pathlib.Path(job_dir) / "job.json").read_text(encoding="utf-8"))
    gates.gate_bounds(cfg)
    gates.gate_seats(cfg, job_dir)
    pins = gates.gate_instruments(cfg, job_dir)
    if (job_dir / "job.jsonl").exists():
        raise gates.GateError("job.jsonl already exists; freeze runs once")
    _append(job_dir, "job-start", "edi-run", [], {"job": cfg["job"]})
    bar_p = gates.safe_rel(cfg["bar"].get("file", "bar.md"), job_dir)
    if cfg["levelClaimed"] == "baseline":
        s_at = started_at or _append(job_dir, "work-started", cfg["maker"]["name"], [], {})["at"]
        time.sleep(0.002)
        ev_frz = _append(job_dir, "bar-frozen", cfg["bar"]["author"], [bar_p], {})
        cfg["bar"]["frozenAt"] = ev_frz["at"]
    else:
        ev_frz = _append(job_dir, "bar-frozen", cfg["bar"]["author"], [bar_p], {})
        for a in cfg["bar"]["approvals"]:
            _append(job_dir, "approval", a["by"], [bar_p], {"role": a["role"], "barDigest": a["barDigest"]["sha256"], "adopted": a.get("adopted")})
        time.sleep(0.002)
        s_at = started_at or _append(job_dir, "work-started", cfg["maker"]["name"], [], {})["at"]
    gates.gate_freeze(cfg, job_dir, started_at=s_at)
    demos, rub_txt = [], gates.safe_rel(cfg["instrument"]["rubric"]["file"], job_dir).read_text(encoding="utf-8")
    if cfg["levelClaimed"] == "CL3":
        p_case = gates.safe_rel(cfg["controls"]["planted"], job_dir)
        for ev in cfg["evaluators"]:
            for d in cfg["bar"]["dimensions"]:
                res = adapters.run_evaluator(ev, p_case, d["id"], job_dir=job_dir, dim_cfg=d, rubric_txt=rub_txt)
                if res.get("score") is not None and float(res["score"]) >= float((d.get("scale") or {}).get("threshold", 4.0)):
                    raise gates.GateError(f"CL3 planted case passed on {ev['id']}/{d['id']} (CL3-1)")
                demos.append({"dimension": d["id"], "evaluator": ev["id"], "planted": {"sha256": gates.sha256_file(p_case)}, "outcome": "does_not_conform", "at": custody_record.now()})
    state = {"status": "frozen", "pins": pins, "startedAt": s_at, "frozenAt": ev_frz["at"], "demonstrations": demos, "incumbent_round": None, "incumbent_digest": None, "incumbent_scores": None, "last_candidate_digest": None, "defects": [], "solutions": [], "rounds": {}, "terminal_state": None, "reason": None, "cost_usd": 0.0}
    (job_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return 0


def _eval_batch(cfg, job_dir, round_n, art_dir, rem_s):
    bid, dims = f"B{round_n}", cfg["bar"]["dimensions"]
    scales, ordered = {d["id"]: d.get("scale") for d in dims}, sorted(cfg["evaluators"], key=lambda e: 0 if e["kind"] == "script" else 1)
    checks, controls, scores_rows, externals, per_dim, defects, solutions = [], [], [], [], {d["id"]: [] for d in dims}, [], []
    clean_p, planted_p = gates.safe_rel(cfg["controls"]["clean"], job_dir), gates.safe_rel(cfg["controls"]["planted"], job_dir)
    rub_txt = gates.safe_rel(cfg["instrument"]["rubric"]["file"], job_dir).read_text(encoding="utf-8")
    for ev in ordered:
        ev_id, bad_dims = ev["id"], set()
        for d in dims:
            did, thr = d["id"], float((scales[d["id"]] or {}).get("threshold", 4.0))
            for tag, path, exp in (("clean", clean_p, "conforms"), ("planted", planted_p, "does_not_conform")):
                cid, res = f"K-{bid}-{ev_id}-{did}-{tag}", adapters.run_evaluator(ev, path, did, job_dir=job_dir, timeout=rem_s, dim_cfg=d, rubric_txt=rub_txt)
                sc = res.get("score")
                if sc is None or not math.isfinite(float(sc)):
                    bad_dims.add(did)
                    out, sc_val = ("conforms" if exp == "does_not_conform" else "does_not_conform"), (thr if exp == "does_not_conform" else thr - 1.0)
                else:
                    sc_val, out = float(sc), ("conforms" if float(sc) >= thr else "does_not_conform")
                    bad_dims.update([did] if out != exp else [])
                ctl = {"id": cid, "batch": bid, "evaluator": ev_id, "dimension": did, "case": {"sha256": gates.sha256_file(path)}, "expected": exp, "outcome": out}
                controls.append(ctl)
                scores_rows.append({"id": cid, "batch": bid, "evaluator": ev_id, "expected": exp, "score": sc_val, "threshold": thr})
                _append(job_dir, "control", ev_id, [], ctl)
        for d in dims:
            did, sc_cfg = d["id"], scales[d["id"]]
            for run_i in range(1, int(ev.get("runs", 2 if cfg["levelClaimed"] in ("CL2", "CL3") else 1)) + 1):
                har = job_dir / "evidence" / f"harness-{ev_id}-{bid}.json" if ev["kind"] == "model" and run_i == 1 else None
                res = adapters.run_evaluator(ev, art_dir, did, job_dir=job_dir, harness_out=har, timeout=rem_s, dim_cfg=d, rubric_txt=rub_txt)
                if did in bad_dims or "unobserved" in res or res.get("score") is None:
                    u = res.get("unobserved") or {"channel": ev_id, "reason": "evaluator failed batch control (CL1-9)"}
                    chk = {"dimension": did, "evaluator": ev_id, "run": run_i, "batch": bid, "at": custody_record.now(), "outcome": "unobserved", "score": None, "failureCondition": f"score < {sc_cfg['threshold']}" if sc_cfg else d["criterion"], "evidence": None, "unobserved": u}
                else:
                    score, out = float(res["score"]), ("conforms" if float(res["score"]) >= float(sc_cfg["threshold"]) else "does_not_conform")
                    ev_name = f"reading-{did}-{ev_id}-r{round_n}-run{run_i}.json"
                    (job_dir / "evidence" / ev_name).write_text(json.dumps({"dimension": did, "evaluator": ev_id, "round": round_n, "run": run_i, "score": score, "observation": res.get("observation", ""), "remedy": res.get("remedy", "")}, indent=2) + "\n", encoding="utf-8")
                    chk = {"dimension": did, "evaluator": ev_id, "run": run_i, "batch": bid, "at": custody_record.now(), "outcome": out, "score": score, "failureCondition": f"score < {sc_cfg['threshold']}", "evidence": {"name": ev_name, "sha256": gates.sha256_file(job_dir / "evidence" / ev_name)}, "unobserved": None, **({"endpoint": res["_endpoint"]} if res.get("_endpoint") else {})}
                    per_dim[did].append(score)
                    if out == "does_not_conform":
                        defects.append({"dimension": did, "evaluator": ev_id, "score": score, "observation": res.get("observation", ""), "remedy": res.get("remedy", "")})
                    else:
                        solutions.append({"dimension": did, "evaluator": ev_id, "score": score, "round": round_n})
                chk["at"] = _append(job_dir, "check", ev_id, [], chk)["at"]
                checks.append(chk)
        if ev["kind"] == "model":
            h_name = f"harness-{ev_id}-{bid}.json"
            ex = {"requirement": "CL1-15", "subject": ev_id, "evidence": {"name": h_name, "sha256": gates.sha256_file(job_dir / "evidence" / h_name)}, "readBy": cfg["bar"]["author"]}
            externals.append(ex)
            _append(job_dir, "external", ex["readBy"], [], ex)
    return checks, controls, scores_rows, externals, {k: min(v) if v else 0.0 for k, v in per_dim.items()}, defects, solutions


def run_job(job_dir):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg, state = json.loads((job_dir / "job.json").read_text(encoding="utf-8")), json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    if gates.gate_instruments(cfg, job_dir) != state["pins"]:
        raise gates.GateError("frozen instruments modified after freeze (I-01/I-02)")
    max_rounds, max_wall, max_cost, t0 = int(cfg["bounds"]["max_rounds"]), float(cfg["bounds"]["max_wall_seconds"]), float(cfg["bounds"]["max_cost_usd"]), time.monotonic()
    dims, floors = [d["id"] for d in cfg["bar"]["dimensions"]], {d["id"]: float(cfg.get("rejectFloor", (d.get("scale") or {}).get("floor", 0.0))) for d in cfg["bar"]["dimensions"]}
    held_hashes = {gates.sha256_file(p) for p in (job_dir / "held_out").rglob("*") if p.is_file()} if (job_dir / "held_out").is_dir() else set()
    for r in range(1, max_rounds + 1):
        rem_s = max_wall - (time.monotonic() - t0)
        if rem_s <= 0 or state.get("cost_usd", 0.0) >= max_cost:
            state.update(terminal_state="needs_owner", reason="cap_reached")
            break
        r_dir, art_dir = job_dir / "rounds" / f"round-{r:02d}", job_dir / "rounds" / f"round-{r:02d}" / "artifact"
        r_dir.mkdir(parents=True, exist_ok=True)
        pkt = {"round": r, "mode": cfg.get("mode", "pass_r2"), "best_artifact_digest": state["incumbent_digest"], "defects": state["defects"], "workable_solutions": state["solutions"]}
        gates.gate_packet(pkt, job_dir, state["incumbent_digest"])
        (r_dir / "packet.json").write_text(json.dumps(pkt, indent=2) + "\n", encoding="utf-8")
        if state["incumbent_round"]:
            shutil.copytree(job_dir / "rounds" / f"round-{state['incumbent_round']:02d}" / "artifact", art_dir, dirs_exist_ok=True)
        try:
            adapters.run_maker(cfg["maker"], r_dir / "packet.json", art_dir, cfg["subjectFiles"], job_dir, timeout=rem_s)
        except TimeoutError:
            state.update(terminal_state="needs_owner", reason="cap_reached")
            break
        cand_digest = _subject_digest(art_dir, cfg["subjectFiles"], held_hashes)
        if cand_digest is None or cand_digest in (state["last_candidate_digest"], state["incumbent_digest"]):
            state.update(terminal_state="needs_owner", reason="no_artifact" if cand_digest is None else "dormant_channel")
            break
        state["last_candidate_digest"] = cand_digest
        ev_prod = _append(job_dir, "artifact-produced", cfg["maker"]["name"], [art_dir / f for f in cfg["subjectFiles"]], {})
        checks, ctls, s_rows, exts, d_scores, defects, solutions = _eval_batch(cfg, job_dir, r, art_dir, max_wall - (time.monotonic() - t0))
        state["rounds"][str(r)] = {"producedAt": ev_prod["at"], "digest": cand_digest, "checks": checks, "controls": ctls, "scores": s_rows, "external": exts, "dim_scores": d_scores}
        if any(c["outcome"] == "unobserved" for c in checks):
            if state["incumbent_round"] is None:
                state.update(incumbent_round=r, incumbent_digest=cand_digest, incumbent_scores=d_scores)
            state.update(terminal_state="needs_owner", reason="no_observation")
            break
        inc = state["incumbent_scores"]
        if inc is None or (any(d_scores[d] > inc[d] for d in dims) and not any(d_scores[d] < inc[d] for d in dims)):
            state.update(incumbent_round=r, incumbent_digest=cand_digest, incumbent_scores=d_scores, defects=defects, solutions=solutions)
        elif any(d_scores[d] > inc[d] for d in dims) and any(d_scores[d] < inc[d] for d in dims):
            state.update(terminal_state="needs_owner", reason="dimension_tradeoff")
            break
        if any(state["incumbent_scores"][d] < floors[d] for d in dims):
            state.update(terminal_state="rejected", reason="below_floor")
            break
        win_checks = state["rounds"][str(state["incumbent_round"])]["checks"]
        if all(c["outcome"] == "conforms" for c in win_checks) or r == max_rounds:
            state.update(terminal_state="accepted" if all(c["outcome"] == "conforms" for c in win_checks) else "needs_owner", reason="all_conform" if all(c["outcome"] == "conforms" for c in win_checks) else "cap_reached")
            break
    (job_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return {"accepted": 0, "rejected": 1, "needs_owner": 2}[state["terminal_state"]]


def finalize_job(job_dir, state="given", by="Owner", ack_unobserved=None):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg, st = json.loads((job_dir / "job.json").read_text(encoding="utf-8")), json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    if gates.gate_instruments(cfg, job_dir) != st["pins"]:
        raise gates.GateError("frozen instruments modified after freeze (I-01/I-02)")
    gates.gate_seats(cfg, job_dir, approver_by=by)
    if not st.get("incumbent_round"):
        raise gates.GateError("cannot finalize a job that produced no artifact")
    win_r, max_r, art_dst, log_path = str(st["incumbent_round"]), str(max(int(k) for k in st["rounds"])), job_dir / "artifact", job_dir / "job.jsonl"
    win = st["rounds"][win_r]
    shutil.rmtree(art_dst, ignore_errors=True)
    shutil.copytree(job_dir / "rounds" / f"round-{int(win_r):02d}" / "artifact", art_dst)
    (job_dir / "scores.jsonl").write_text("\n".join(json.dumps(r) for r in win["scores"]) + "\n", encoding="utf-8")
    if win_r != max_r:
        ev_prod = _append(job_dir, "artifact-produced", cfg["maker"]["name"], [art_dst / f for f in cfg["subjectFiles"]], {})
        checks = [dict(c, at=_append(job_dir, "check", c["evaluator"], [], c)["at"]) for c in win["checks"]]
        for k in win["controls"]:
            _append(job_dir, "control", k["evaluator"], [], {ki: vi for ki, vi in k.items() if ki not in ("expected", "outcome")})
        for ex in win["external"]:
            _append(job_dir, "external", ex["readBy"], [], ex)
        prod_at = ev_prod["at"]
    else:
        checks, prod_at = win["checks"], win["producedAt"]
    ack = ack_unobserved if ack_unobserved is not None else cfg["disposition"].get("acknowledgesUnobserved", [])
    ev_disp = _append(job_dir, "disposition", by, [], {"state": state, "acknowledgesUnobserved": ack})
    violations = [{"dimension": d["id"], "description": f"{d['id']} failed conform threshold"} for d in cfg["bar"]["dimensions"] if any(c["dimension"] == d["id"] and c["outcome"] == "does_not_conform" for c in checks)]
    disagreements = [{"dimension": d["id"], "checks": [i for i, c in enumerate(checks) if c["dimension"] == d["id"]], "note": "evaluators disagreed"} for d in cfg["bar"]["dimensions"] if len({c["outcome"] for c in checks if c["dimension"] == d["id"]}) > 1]
    evals_clean = [{k: v for k, v in e.items() if k in ("id", "name", "kind", "model", "script", "sameModelAsMaker")} for e in cfg["evaluators"]]
    inputs = {"subject": {"dir": "artifact"}, "specVersion": cfg.get("specVersion", "0.9.8"), "levelClaimed": cfg["levelClaimed"], "bar": dict({k: v for k, v in cfg["bar"].items() if k != "file"}, frozenAt=st["frozenAt"]),
              "maker": {k: v for k, v in cfg["maker"].items() if k in ("name", "kind", "version", "method", "model")}, "startedAt": st["startedAt"], "producedAt": prod_at, "instrument": cfg["instrument"],
              "evaluators": evals_clean, "checks": checks, "controls": win["controls"], "disagreements": disagreements, "violations": violations, "separationGaps": cfg.get("separationGaps", []),
              "retention": cfg["retention"], "external": win["external"], "disposition": {"state": state, "by": by, "at": ev_disp["at"], "acknowledgesUnobserved": ack}}
    if cfg["levelClaimed"] in ("CL2", "CL3"):
        inputs["spread"] = [{"dimension": d["id"], "evaluator": ev["id"], "runs": len(outs), "agreement": Counter(outs).most_common(1)[0][1]} for ev in evals_clean for d in cfg["bar"]["dimensions"] if (outs := [c["outcome"] for c in checks if c["evaluator"] == ev["id"] and c["dimension"] == d["id"]])]
    if cfg["levelClaimed"] == "CL3":
        inputs["demonstrations"] = st.get("demonstrations", [])
    if cfg["levelClaimed"] == "baseline":
        inputs["readsBeforeFreeze"] = cfg.get("readsBeforeFreeze", [])
    (job_dir / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, str(VENDOR / "make_record.py"), str(job_dir / "inputs.json"), str(job_dir / "record.json"), "--log", str(log_path)], cwd=str(job_dir), check=True)
    return verify_job(job_dir)


def verify_job(job_dir):
    job_dir, cfg = pathlib.Path(job_dir).resolve(), json.loads((pathlib.Path(job_dir) / "job.json").read_text(encoding="utf-8"))
    if custody_record.verify(str(job_dir / "job.jsonl"), files=True):
        return 1
    rep = custody_check.verify(json.loads((job_dir / "record.json").read_text(encoding="utf-8")), files=str(job_dir / "artifact"), evidence=str(job_dir / "evidence"), bar=str(job_dir / cfg["bar"]["file"]))
    return 0 if rep["verdict"] == "OK" else (1 if rep["verdict"] == "FAILED" else 2)
