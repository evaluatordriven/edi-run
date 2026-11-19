"""Deterministic E-DI state machine (I-05 through I-07) wrapping evaluatordriven/custody tools."""
from collections import Counter
from contextlib import contextmanager
import fcntl, hashlib, json, math, os, pathlib, shutil, signal, subprocess, sys, threading, time
import adapters, gates, metering
from metering import Meter, CapExceeded

VENDOR = pathlib.Path(__file__).resolve().parent / "vendor"
sys.path.insert(0, str(VENDOR))
import check as custody_check, record as custody_record  # noqa: E402


def _load_job_json(job_dir):
    # G:parse-boundary strict JSON parse refuses duplicate properties
    try: return gates.strict_json_loads((pathlib.Path(job_dir) / "job.json").read_text(encoding="utf-8"))
    except ValueError as e: raise gates.GateError(f"job.json refused at the parse boundary: {e}")


_JOB_LOCKS = {}
_JOB_LOCKS_GUARD = threading.Lock()


class _JobLifecycleLock:
    def __init__(self, path):
        self._path, self._rl, self._fd, self._depth = path, threading.RLock(), None, 0

    @contextmanager
    def hold(self):
        # G:P6 reentrant job-scoped flock; exception-safe depth tracking
        with self._rl:
            if self._depth == 0:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd = open(self._path, "a")
                try: fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
                except BaseException: fd.close(); raise
                self._fd = fd
            self._depth += 1
            try: yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN); self._fd.close(); self._fd = None


def job_lock(job_dir):
    jd = pathlib.Path(job_dir).resolve()
    with _JOB_LOCKS_GUARD:
        return _JOB_LOCKS.setdefault(str(jd), _JobLifecycleLock(jd / "job.lock")).hold()


def _has_disposition(job_dir):
    log = pathlib.Path(job_dir) / "job.jsonl"
    return log.is_file() and any(e.get("type") == "disposition" for e in custody_record.read(str(log)))


def _append(job_dir, etype, actor, files, data):
    # G:P5/P6 job-relative file keys; disposition is a lifecycle barrier under job_lock
    jd = pathlib.Path(job_dir).resolve()
    with job_lock(jd):
        if _has_disposition(jd):
            raise gates.GateError("job already has a disposition; no custody event may follow it — the job is closed (P6 lifecycle barrier)")
        rel = {(f.resolve().relative_to(jd).as_posix() if f.resolve().is_relative_to(jd) else str(f.resolve())): gates.sha256_file(f) for f in files}
        return custody_record.append(str(job_dir / "job.jsonl"), {"type": etype, "at": custody_record.now(), "actor": actor, "source": "", "files": rel, "data": data})


def custody_verify(job_dir):
    # G:P5 chain + content verification of job.jsonl with job-relative file re-hash checks
    log_path = pathlib.Path(job_dir).resolve()
    if log_path.is_dir():
        log_path = log_path / "job.jsonl"
    problems = custody_record.verify(str(log_path), files=False)
    events = custody_record.read(str(log_path))
    legacy = not any(e.get("type") == "job-pin" for e in events)
    for i, e in enumerate(events, 1):
        for path, h in (e.get("files") or {}).items():
            p = pathlib.Path(path)
            if not p.is_absolute():
                jd_cand, cw = log_path.parent / p, pathlib.Path.cwd() / p
                jd_file, cw_file = jd_cand.is_file(), cw.is_file()
                if jd_file and (not legacy or not cw_file or custody_record.digest_file(jd_cand) == custody_record.digest_file(cw)):
                    p = jd_cand
                elif legacy and cw_file:
                    if jd_file:
                        problems.append(f"event {i}: ambiguous custody file for {path}: job-relative candidate {jd_cand} and caller-relative {cw} disagree")
                        continue
                    p = cw
                else:
                    p = jd_cand
            if not p.is_file():
                problems.append(f"event {i}: file not found: {path}")
            elif custody_record.digest_file(p) != h:
                problems.append(f"event {i}: file changed since recorded: {path}")
    return problems


def _subject_digest(art_dir, files, held_hashes):
    parts = [f"{f}:{gates.sha256_file(gates.safe_rel(f, art_dir))}" for f in sorted(files) if gates.safe_rel(f, art_dir).is_file()]
    if len(parts) != len(files):
        return None
    if any(p.split(":", 1)[1] in held_hashes for p in parts):
        raise gates.GateError("artifact matches held-out file digest (I-04)")
    return custody_record.digest_text("|".join(parts))


GAP_RULES = (
    ("I-01", lambda st: not st.get("frozenAt"), "bar freeze never recorded"),
    ("I-05", lambda st: not st.get("rounds"), "no evaluation batch completed (no checks observed)"),
    ("I-06", lambda st: not st.get("incumbent_round"), "no artifact survived as Pareto-best incumbent"),
    ("I-07", lambda st: not st.get("terminal_state"), "loop has no terminal state"),
)


def _gap_list(st, extra=()):
    return [{"invariant": inv, "missing": msg} for inv, test, msg in GAP_RULES if test(st)] + list(extra)


def _emit_gap_record(job_dir, st, cfg, extra=()):
    # G:§6 incomplete or declined runs emit a gap-listed record (invalid by design)
    lines = (job_dir / "job.jsonl").read_text(encoding="utf-8").splitlines()
    log_last = json.loads(lines[-1]) if lines else {}
    rec = {"recordType": "edi-run/gap-record",
           "x-edi-run": {"gaps": _gap_list(st, extra), "terminalState": st.get("terminal_state"),
                          "reason": st.get("reason"), "startedAt": st.get("startedAt"), "lastEventHash": log_last.get("hash")}}
    (job_dir / "record.json").write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    if custody_check.verify(rec, files=str(job_dir / "artifact"), evidence=str(job_dir / "evidence"), bar=str(job_dir / cfg["bar"]["file"]))["verdict"] == "OK":
        raise gates.GateError("gap-listed record unexpectedly verified OK (emitter defect)")
    return 1


def _preflight_freeze_cfg(cfg):
    bar, maker, seed = cfg.get("bar") or {}, cfg.get("maker") or {}, cfg.get("seed")
    why = "the real append path records it; refusing before any log write"
    checks = [("bar.author", bar.get("author")), ("maker.name", maker.get("name"))]
    for i, a in enumerate(bar.get("approvals") or []):
        checks += [(f"bar.approvals[{i}].by", a.get("by") if isinstance(a, dict) else None),
                   (f"bar.approvals[{i}].barDigest.sha256", ((a.get("barDigest") or {}).get("sha256") if isinstance(a, dict) and isinstance(a.get("barDigest"), dict) else None))]
    if seed is not None:
        checks.append(("seed.author", seed.get("author") if isinstance(seed, dict) else None))
    for label, val in checks:
        if not isinstance(val, str) or not val.strip():
            raise gates.GateError(f"freeze pre-flight: {label} must be a non-empty string ({why})")


def init_job(job_dir):
    # G:N14 scaffold structure only; never pre-create rounds/ (freeze refuses existing rounds/)
    job_dir = pathlib.Path(job_dir)
    for bad, msg in ((job_dir.is_symlink(), f"{job_dir} is a symlink; init never writes through a link"),
                     (job_dir.exists() and not job_dir.is_dir(), f"{job_dir} exists and is not a directory"),
                     (job_dir.is_dir() and any(job_dir.iterdir()), f"{job_dir} exists and is not empty")):
        if bad:
            print(f"refusing to init: {msg}", file=sys.stderr); return 1
    job_dir.mkdir(parents=True, exist_ok=True)
    for d in ("scripts", "seed"):
        (job_dir / d).mkdir(exist_ok=True)
    for name in ("bar.md", "rubric.md", "method.md"):
        (job_dir / name).write_text("", encoding="utf-8")
    (job_dir / "AUTHORSHIP.md").write_text("Operator:\nBar author:\nRubric author:\nMethod author:\nSeed author:\nSponsor:\n", encoding="utf-8")
    job = {"job": None, "levelClaimed": None, "specVersion": None, "bounds": {"max_rounds": None, "max_cost_usd": None, "max_wall_seconds": None},
           "mode": None, "subjectFiles": [], "bar": {"file": "bar.md", "digest": {"sha256": None}, "frozenAt": None, "author": None, "dimensions": [], "approvals": []},
           "maker": {"name": None, "kind": None, "version": None, "method": {"file": "method.md"}, "model": None, "sandbox": None, "command": None},
           "instrument": {"rubric": {"file": "rubric.md"}, "runner": {"file": None}}, "evaluators": [], "controls": {"clean": None, "planted": None},
           "retention": {"location": None, "public": None}, "disposition": {"state": None, "by": None, "acknowledgesUnobserved": []},
           "pricing": {}, "seed": None, "barDigest": None}
    (job_dir / "job.json").write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
    return 0


def _job_snapshot(job_dir):
    return {str(p.relative_to(job_dir)) + ("/" if p.is_dir() else ""):
            ((p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_mode) if p.is_file() else ("dir", p.stat().st_mtime_ns))
            for p in sorted(job_dir.rglob("*"))}


def _ok_rate(v):
    try:
        return math.isfinite(float(v)) and float(v) > 0.0
    except (TypeError, ValueError):
        return False


def freeze_job(job_dir, started_at=None, dry_run=False):
    # G:I-01/I-02/G2/F5 validate all freeze gates before any disk write; hold job_lock on real freeze
    if dry_run:
        return _freeze_job_inner(job_dir, started_at=started_at, dry_run=True)
    with job_lock(job_dir):
        return _freeze_job_inner(job_dir, started_at=started_at, dry_run=False)


def _freeze_job_inner(job_dir, started_at=None, dry_run=False):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg = _load_job_json(job_dir)
    _preflight_freeze_cfg(cfg)
    pre_snap = _job_snapshot(job_dir) if dry_run else None
    gates.gate_bounds(cfg)
    gates.gate_seats(cfg, job_dir)
    model_ids = []
    if cfg["maker"].get("kind") in ("model", "agent"):
        model_ids.append((cfg["maker"].get("model") or {}).get("identifier") if isinstance(cfg["maker"].get("model"), dict) else cfg["maker"].get("model"))
    model_ids += [(e.get("model") or {}).get("identifier") if isinstance(e.get("model"), dict) else e.get("model") for e in cfg["evaluators"] if e.get("kind") == "model"]
    pricing_map = cfg.get("pricing", {})
    if (missing := [m for m in model_ids if m and m not in pricing_map and "default" not in pricing_map]):
        raise gates.GateError(f"missing pricing for model seat(s) {missing}; add a pricing map to job.json so the cost cap is enforceable")
    if [m for m in model_ids if not m]:
        raise gates.GateError("every model/agent seat needs a non-empty model.identifier; an empty identifier skips pricing entirely (recheck G2)")
    if (zero := [key for key, p in pricing_map.items() if not isinstance(p, dict) or not all(_ok_rate(p.get(k)) for k in ("input_usd_per_mtok", "output_usd_per_mtok"))]):
        raise gates.GateError(f"pricing entries {zero} contain a zero, negative, non-numeric or non-finite rate; unpriced spend must be refused, never silently $0 (recheck G2)")
    if (unbounded := [m for m in model_ids if m and metering.output_bound(cfg.get("bounds"), pricing_map, m) is None]):
        raise gates.GateError(f"model seat(s) {unbounded} have no max_output_tokens (set bounds.max_output_tokens or pricing[<model>].max_output_tokens): output spend that cannot be bounded is refused at freeze, not at the first paid call (I-07)")
    gates.gate_commands(cfg, job_dir)
    _bar = cfg.get("bar") or {}
    if (_bar.get("digest") or {}).get("sha256") is None:
        _bar["digest"] = {"sha256": gates.sha256_file(gates.safe_rel(_bar.get("file", "bar.md"), job_dir))}
    if (job_dir / "rounds").exists():
        raise gates.GateError("rounds/ already exists; it is runtime custody space, never present at freeze")
    if (job_dir / "job.jsonl").exists():
        raise gates.GateError("job.jsonl already exists; freeze runs once")
    pins = gates.gate_instruments(cfg, job_dir)
    gates.gate_freeze_pre(cfg, job_dir)
    seed, seed_pins = cfg.get("seed"), None
    if seed:
        sdir = gates.safe_rel(seed["dir"], job_dir)
        if not sdir.is_dir():
            raise gates.GateError(f"seed dir {seed['dir']} does not exist")
        seed_files = sorted(str(f.relative_to(sdir)) for f in sdir.rglob("*") if f.is_file())
        if seed_files != sorted(cfg["subjectFiles"]):
            raise gates.GateError(f"seed must supply exactly subjectFiles {sorted(cfg['subjectFiles'])}; found {seed_files}")
        if any(str(e["name"]).casefold() == str(seed["author"]).casefold() for e in cfg["evaluators"]):
            raise gates.GateError("seed author cannot serve as Evaluator (CL1-6)")
        if any(str(a["by"]).casefold() == str(seed["author"]).casefold() for a in cfg["bar"]["approvals"]):
            raise gates.GateError("seed author cannot hold the Buyer or Specialist seat (the approver of the bar would be the maker of record)")
        seed_pins = {f: gates.sha256_file(sdir / f) for f in cfg["subjectFiles"]}
    demos, demo_meter = [], None
    rub_txt = gates.safe_rel(cfg["instrument"]["rubric"]["file"], job_dir).read_text(encoding="utf-8")
    if cfg["levelClaimed"] == "CL3":
        p_case = gates.safe_rel(cfg["controls"]["planted"], job_dir)
        demo_meter = None if dry_run else Meter(job_dir, pricing=pricing_map, bounds=cfg.get("bounds"))
        if demo_meter is not None:
            demo_meter._round = 0
        for ev in cfg["evaluators"]:
            if dry_run and ev.get("kind") == "model":
                print(f"freeze --dry-run: skipped the paid CL3 planted-case demonstration for evaluator {ev.get('id')}", file=sys.stderr)
                continue
            for d in cfg["bar"]["dimensions"]:
                try:
                    res = adapters.run_evaluator(ev, p_case, d["id"], job_dir=job_dir, dim_cfg=d, rubric_txt=rub_txt, meter=demo_meter, mode=cfg.get("mode") or "scored")
                except CapExceeded as e:
                    raise gates.GateError(f"cost cap reached during the CL3 planted-case demonstration: {e}")
                if res.get("score") is None or float(res["score"]) >= float((d.get("scale") or {}).get("threshold", 4.0)):
                    raise gates.GateError(f"CL3 planted case did not fail on {ev['id']}/{d['id']} (CL3-1)")
                demos.append({"dimension": d["id"], "evaluator": ev["id"], "planted": {"sha256": gates.sha256_file(p_case)}, "outcome": "does_not_conform", "at": custody_record.now()})
    if dry_run:
        if cfg["levelClaimed"] == "baseline":
            s_at = started_at or (custody_record.now() if hasattr(custody_record, "now") else None)
            time.sleep(0.002)
            cfg["bar"]["frozenAt"] = custody_record.now()
        else:
            s_at = started_at or custody_record.now()
        gates.gate_freeze(cfg, job_dir, started_at=s_at)
        if (post_snap := _job_snapshot(job_dir)) != pre_snap:
            changed = sorted(k for k in set(post_snap) | set(pre_snap or {}) if post_snap.get(k) != (pre_snap or {}).get(k))
            raise RuntimeError(f"freeze --dry-run wrote to the host (defect): {changed[:5]}")
        return 0
    _tmpj = job_dir / "job.json.tmp-freeze"
    _tmpj.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(_tmpj, job_dir / "job.json")
    _append(job_dir, "job-start", "edi-run", [], {"job": cfg["job"]})
    if seed:
        _append(job_dir, "artifact-supplied", seed["author"], [gates.safe_rel(seed["dir"], job_dir) / f for f in cfg["subjectFiles"]], {"origin": seed.get("origin", ""), "note": seed.get("note", "")})
    bar_p = gates.safe_rel(cfg["bar"].get("file", "bar.md"), job_dir)
    if cfg["levelClaimed"] == "baseline":
        s_at = started_at or _append(job_dir, "work-started", cfg["maker"]["name"], [], {})["at"]
        time.sleep(0.002)
    ev_frz = _append(job_dir, "bar-frozen", cfg["bar"]["author"], [bar_p], {})
    if cfg["levelClaimed"] == "baseline":
        cfg["bar"]["frozenAt"] = ev_frz["at"]
    for a in cfg["bar"]["approvals"]:
        _append(job_dir, "approval", a["by"], [bar_p], {"role": a["role"], "barDigest": a["barDigest"]["sha256"], "adopted": a.get("adopted")})
    if cfg["levelClaimed"] != "baseline":
        time.sleep(0.002)
        s_at = started_at or _append(job_dir, "work-started", cfg["maker"]["name"], [], {})["at"]
    _append(job_dir, "job-pin", "edi-run", [], {"pins": pins})
    gates.gate_freeze(cfg, job_dir, started_at=s_at)
    if (ho := job_dir / "held_out").is_dir():
        for f in ho.rglob("*"):
            os.chmod(f, 0o700 if f.is_dir() else 0o600)
        os.chmod(ho, 0o700)
    state = {"status": "frozen", "pins": pins, "startedAt": s_at, "frozenAt": ev_frz["at"], "demonstrations": demos,
             "incumbent_round": None, "incumbent_digest": None, "incumbent_scores": None, "last_candidate_digest": None,
             "defects": [], "solutions": [], "rounds": {}, "terminal_state": None, "reason": None,
             "cost_usd": round(demo_meter.accrued(), 6) if demo_meter else 0.0,
             "seed": ({"dir": seed["dir"], "author": seed["author"], "digests": seed_pins} if seed else None)}
    _write_state(job_dir, state, demo_meter)
    return 0


def _parse_reading(res, target_txt=""):
    # G:C29/D4.6/RD2 validate readings-mode payload: outcome + non-empty quotation grounded in judged text
    if not isinstance(res, dict) or "unobserved" in res:
        return "unobserved", "", (res.get("unobserved") if isinstance(res, dict) else None)
    if "score" in res and res["score"] is not None:
        return None, "", {"reason": "readings mode forbids numeric score in reading slot (C29)"}
    out = res.get("outcome", res.get("verdict"))
    if not isinstance(out, str) or out not in ("conforms", "does_not_conform", "unobserved"):
        return None, "", {"reason": f"invalid reading outcome {out!r} (C29)"}
    q = str(res.get("quotation", res.get("observation", "")) or "").strip()
    if out == "unobserved":
        return "unobserved", q, {"reason": str(res.get("reason") or q or "unobserved"), "failed": False}
    if not q or (target_txt and " ".join(q.split()) not in " ".join(target_txt.split())):
        return "unobserved", "", {"reason": "reading quotation is blank or not grounded in judged text (C29/RD2)", "failed": True}
    return out, q, None


def _eval_batch(cfg, job_dir, round_n, art_dir, deadline, meter=None, att_tag=""):
    # G:I-05/CL1-9/CL1-15/C29 run controls first (script before model), then candidate checks
    if meter is not None:
        meter._round = round_n
    (job_dir / "evidence").mkdir(parents=True, exist_ok=True)
    mode, bid, dims = cfg.get("mode") or "scored", f"B{round_n}{att_tag}", cfg["bar"]["dimensions"]
    scales, ordered = {d["id"]: d.get("scale") for d in dims}, sorted(cfg["evaluators"], key=lambda e: 0 if e["kind"] == "script" else 1)
    checks, controls, scores_rows, externals, per_dim, defects, solutions, usages = [], [], [], [], {d["id"]: [] for d in dims}, [], [], []
    clean_p, planted_p = gates.safe_rel(cfg["controls"]["clean"], job_dir), gates.safe_rel(cfg["controls"]["planted"], job_dir)
    rub_txt = gates.safe_rel(cfg["instrument"]["rubric"]["file"], job_dir).read_text(encoding="utf-8")
    art_txt = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in sorted(art_dir.rglob("*")) if p.is_file()) if mode == "readings" else ""
    for ev in ordered:
        ev_id, bad_dims = ev["id"], set()
        for d in dims:
            did, thr = d["id"], float((scales[d["id"]] or {}).get("threshold", 4.0))
            for tag, path, exp in (("clean", clean_p, "conforms"), ("planted", planted_p, "does_not_conform")):
                cid = f"K-{bid}-{ev_id}-{did}-{tag}"
                res = adapters.run_evaluator(ev, path, did, job_dir=job_dir, timeout=max(1.0, deadline - time.time()), dim_cfg=d, rubric_txt=rub_txt, meter=meter, mode=mode)
                if mode == "readings":
                    r_out, _, _ = _parse_reading(res, path.read_text(encoding="utf-8", errors="ignore"))
                    out = r_out if r_out in ("conforms", "does_not_conform") else ("conforms" if exp == "does_not_conform" else "does_not_conform")
                    sc_val = 1.0 if out == "conforms" else 0.0
                    if out != exp: bad_dims.add(did)
                else:
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
            for run_i in range(1, int(ev.get("runs", 2)) + 1):
                har = job_dir / "evidence" / f"harness-{ev_id}-{bid}.json" if ev["kind"] == "model" and run_i == 1 else None
                res = adapters.run_evaluator(ev, art_dir, did, job_dir=job_dir, harness_out=har, timeout=max(1.0, deadline - time.time()), dim_cfg=d, rubric_txt=rub_txt, meter=meter, mode=mode)
                if mode == "readings":
                    r_out, quote, u_info = _parse_reading(res, art_txt)
                    if did in bad_dims or r_out is None or r_out == "unobserved":
                        u = ({"channel": ev_id, "reason": "evaluator failed batch control (CL1-9)", "failed": True} if did in bad_dims else
                             {"channel": ev_id, "reason": (u_info or {}).get("reason", "unobserved"), "failed": bool(r_out is None or (u_info or {}).get("failed", True))})
                        chk = {"dimension": did, "evaluator": ev_id, "run": run_i, "batch": bid, "at": custody_record.now(), "outcome": "unobserved", "score": None, "quotation": None, "failureCondition": d["criterion"], "evidence": None, "unobserved": u}
                    else:
                        ev_name = f"reading-{did}-{ev_id}-r{round_n}{att_tag}-run{run_i}.json"
                        (job_dir / "evidence" / ev_name).write_text(json.dumps({"dimension": did, "evaluator": ev_id, "round": round_n, "run": run_i, "outcome": r_out, "quotation": quote}, indent=2) + "\n", encoding="utf-8")
                        chk = {"dimension": did, "evaluator": ev_id, "run": run_i, "batch": bid, "at": custody_record.now(), "outcome": r_out, "score": None, "quotation": quote, "failureCondition": d["criterion"], "evidence": {"name": ev_name, "sha256": gates.sha256_file(job_dir / "evidence" / ev_name)}, "unobserved": None, **({"endpoint": res["_endpoint"]} if res.get("_endpoint") else {})}
                        if r_out == "does_not_conform":
                            defects.append({"round": round_n, "dimension": did, "evaluator": ev_id, "quotation": quote, "observation": quote})
                        else:
                            solutions.append({"dimension": did, "evaluator": ev_id, "quotation": quote, "round": round_n})
                elif did in bad_dims or "unobserved" in res or res.get("score") is None:
                    u = res.get("unobserved") or {"channel": ev_id, "reason": "evaluator failed batch control (CL1-9)"}
                    chk = {"dimension": did, "evaluator": ev_id, "run": run_i, "batch": bid, "at": custody_record.now(), "outcome": "unobserved", "score": None, "failureCondition": f"score < {sc_cfg['threshold']}" if sc_cfg else d["criterion"], "evidence": None, "unobserved": u}
                else:
                    score, out = float(res["score"]), ("conforms" if float(res["score"]) >= float(sc_cfg["threshold"]) else "does_not_conform")
                    ev_name = f"reading-{did}-{ev_id}-r{round_n}{att_tag}-run{run_i}.json"
                    (job_dir / "evidence" / ev_name).write_text(json.dumps({"dimension": did, "evaluator": ev_id, "round": round_n, "run": run_i, "score": score, "observation": res.get("observation", ""), "remedy": res.get("remedy", "")}, indent=2) + "\n", encoding="utf-8")
                    chk = {"dimension": did, "evaluator": ev_id, "run": run_i, "batch": bid, "at": custody_record.now(), "outcome": out, "score": score, "failureCondition": f"score < {sc_cfg['threshold']}", "evidence": {"name": ev_name, "sha256": gates.sha256_file(job_dir / "evidence" / ev_name)}, "unobserved": None, **({"endpoint": res["_endpoint"]} if res.get("_endpoint") else {})}
                    per_dim[did].append(score)
                    if out == "does_not_conform":
                        defects.append({"round": round_n, "dimension": did, "evaluator": ev_id, "score": score, "observation": res.get("observation", ""), "remedy": res.get("remedy", "")})
                    else:
                        solutions.append({"dimension": did, "evaluator": ev_id, "score": score, "round": round_n})
                chk["at"] = _append(job_dir, "check", ev_id, [], chk)["at"]
                checks.append(chk)
        if ev["kind"] == "model":
            h_name = f"harness-{ev_id}-{bid}.json"
            ex = {"requirement": "CL1-15", "subject": ev_id, "evidence": {"name": h_name, "sha256": gates.sha256_file(job_dir / "evidence" / h_name)}, "readBy": "edi-run"}
            externals.append(ex)
            _append(job_dir, "external", ex["readBy"], [], ex)
    return checks, controls, scores_rows, externals, {k: min(v) if v else 0.0 for k, v in per_dim.items()}, defects, solutions, usages


def _write_state(job_dir, state, meter=None):
    # G:P9 anchor usage.jsonl on every state write
    if meter is not None:
        state["usage_anchor"] = list(meter.anchor())
        state.pop("usage_unanchored", None)
        if (tu := meter.token_usage()):
            state["token_usage"] = tu
    tmp = job_dir / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, job_dir / "state.json")


def _write_escalation(job_dir, state, cfg):
    reason, abandon = state.get("reason"), ("abandon", "Finalize with --disposition declined")
    opts = {"cap_reached": [("raise-bounds", "Edit max_rounds/max_cost_usd/max_wall_seconds in job.json, then re-freeze a successor job carrying the incumbent forward"), ("accept-incumbent", "Finalize with --disposition given to accept the current best artifact"), abandon],
            "no_observation": [("inspect-evidence", "Read evidence/ and scores.jsonl; the evaluator channel failed its controls or errored"), ("accept-incumbent", "Finalize with --disposition given --ack-unobserved <DIMS> to accept with acknowledged gaps"), abandon],
            "needs_owner": [("owner-ruling", "Inspect the readings and quotations in job.jsonl and evidence/, then finalize with --disposition given or declined"), abandon],
            "no_artifact": [("fix-maker", "The Maker produced nothing; check the Maker command and method.md"), abandon],
            "dormant_channel": [("vary-method", "Two consecutive identical artifacts; revise method.md so the Maker can actually change the artifact"), ("accept-incumbent", "Finalize with --disposition given"), abandon],
            "dimension_tradeoff": [("owner-ruling", "The revision is better on one dimension and worse on another; pick which artifact ships and finalize accordingly"), abandon],
            "maker_failed": [("fix-maker", "The Maker command failed; state.json maker_error has the stderr tail"), abandon],
            "all_conform": [("finalize", "Finalize with --disposition given to bind the Owner disposition")],
            "census": [("finalize", "Finalize with --disposition given to bind the Owner disposition")]}
    for k, msg in (("interrupted", "The run was interrupted (Ctrl-C or SIGTERM); the log holds every completed event"),
                   ("runtime_error", "A runtime defect halted the job; state.json runtime_error has the detail"),
                   ("integrity_error", "A gate refused to continue; state.json integrity_error names the check and why")):
        opts[k] = [("inspect-state", msg), abandon]
    rounds = state.get("rounds", {})
    tried = [{"round": int(k), "digest": v["digest"], "dimScores": v["dim_scores"]} for k, v in sorted(rounds.items(), key=lambda kv: int(kv[0]))]
    why = {"reason": reason, "detail": state.get("integrity_error") or state.get("maker_error") or state.get("runtime_error")}
    if reason == "dimension_tradeoff" and len(tried) >= 2 and state.get("incumbent_scores"):
        inc_s, last = state["incumbent_scores"], tried[-1]["dimScores"]
        why.update(improved=[d for d in last if last[d] > inc_s[d]], regressed=[d for d in last if last[d] < inc_s[d]])
    if reason in ("no_observation", "needs_owner"):
        why["unobserved"] = sorted({(c["evaluator"], c["dimension"]) for v in rounds.values() for c in v["checks"] if c["outcome"] == "unobserved"})
    rec_map = {"cap_reached": "accept-incumbent if the incumbent meets the bar on inspection, else raise-bounds",
               "no_observation": "inspect-evidence first; accept-incumbent only with explicit --ack-unobserved",
               "needs_owner": "owner-ruling", "no_artifact": "fix-maker", "dormant_channel": "vary-method",
               "dimension_tradeoff": "owner-ruling", "maker_failed": "fix-maker", "all_conform": "finalize", "census": "finalize"}
    rec = {"terminalState": state.get("terminal_state"), "reason": reason, "incumbentRound": state.get("incumbent_round"), "tried": tried, "why": why,
           "options": [{"id": o[0], "action": o[1]} for o in opts.get(reason, [("inspect-state", "Read state.json and evidence/ before deciding")])],
           "recommendation": rec_map.get(reason, "inspect-state")}
    (job_dir / "escalation.json").write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")


def _refuse_after_disposition(job_dir):
    if _has_disposition(job_dir):
        raise gates.GateError("job already has a disposition; finalize ends the job and no seat work may follow it (start a successor job)")


def _open_meter(job_dir, cfg, state, require_anchor=False):
    # G:P9/I-07/F1 verify chain against state anchor before recovery; re-anchor state if recovery adds rows
    try:
        meter = Meter(job_dir, pricing=cfg.get("pricing", {}), bounds=cfg.get("bounds"))
        if require_anchor or (job_dir / "usage.jsonl").is_file() or state.get("usage_anchor"):
            meter.verify(anchor=state.get("usage_anchor"))
            if (job_dir / "usage.jsonl").is_file() and not state.get("usage_anchor"): state["usage_unanchored"] = True
        if meter.recover():
            state["cost_usd"] = round(meter.accrued(), 6)
            _write_state(job_dir, state, meter)
        return meter
    except ValueError as e: raise gates.GateError(f"usage.jsonl failed integrity verification: {e} (spend integrity)")


def run_job(job_dir):
    # G:P6/N8 one operation holds job_lock for its entire execution
    with job_lock(job_dir):
        return _run_job_held(job_dir)


def _run_job_held(job_dir):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg, state = _load_job_json(job_dir), json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    _refuse_after_disposition(job_dir)
    if state.get("open_round"):
        raise gates.GateError(f"round {state['open_round']} was interrupted without a named end state; recover explicitly with 'edi.py resume' (R1)")
    try:
        gates.check_pins(cfg, job_dir, state["pins"])
        if state.get("terminal_state"):
            raise gates.GateError(f"job already reached terminal state {state['terminal_state']} ({state.get('reason')}); run executes once")
        meter = _open_meter(job_dir, cfg, state)
    except gates.GateError as e:
        if not state.get("terminal_state"):
            state.update(terminal_state="needs_owner", reason="integrity_error", integrity_error=str(e)[-300:])
            _write_escalation(job_dir, state, cfg)
            _write_state(job_dir, state)
        raise
    mode = cfg.get("mode") or "scored"
    max_rounds = 1 if mode == "readings" else int(cfg["bounds"]["max_rounds"])
    max_wall, max_cost, t0 = float(cfg["bounds"]["max_wall_seconds"]), float(cfg["bounds"]["max_cost_usd"]), time.time()
    dims, floors = [d["id"] for d in cfg["bar"]["dimensions"]], {d["id"]: float(cfg.get("rejectFloor", (d.get("scale") or {}).get("floor", 0.0))) for d in cfg["bar"]["dimensions"]}
    held_hashes = {gates.sha256_file(p) for p in (job_dir / "held_out").rglob("*") if p.is_file()} if (job_dir / "held_out").is_dir() else set()
    prev_term = None
    if hasattr(signal, "SIGTERM") and threading.current_thread() is threading.main_thread():
        def _on_term(signum, frame): raise KeyboardInterrupt()
        prev_term = signal.signal(signal.SIGTERM, _on_term)
    try:
      start_r = int(state.get("resume_from") or 1)
      for r in range(start_r, max_rounds + 1):
        wall_base = float(state.get("wall_spent_seconds", 0.0))
        rem_s = max_wall - wall_base - (time.time() - t0)
        state["cost_usd"] = round(meter.accrued(), 6)
        if rem_s <= 0 or state.get("cost_usd", 0.0) >= max_cost:
            state.update(terminal_state="needs_owner", reason="cap_reached"); break
        state["open_round"] = r; _write_state(job_dir, state, meter)
        r_dir, att = job_dir / "rounds" / f"round-{r:02d}", 1
        while (r_dir / "artifact").exists():
            att += 1; r_dir = job_dir / "rounds" / f"round-{r:02d}-a{att}"
        att_tag, art_dir = (f"-a{att}" if att > 1 else ""), r_dir / "artifact"
        r_dir.mkdir(parents=True, exist_ok=True)
        needles = gates.held_out_needles(job_dir)
        pk_def, w1 = gates.scrub_held_out(list(state["defects"]) + list(state.get("rejected_defects", [])), needles)
        pk_sol, w2 = gates.scrub_held_out(state["solutions"], needles)
        pkt_mode = cfg.get("packetMode") or (cfg.get("mode") if cfg.get("mode") not in (None, "scored", "readings") else "pass_r2")
        pkt = {"round": r, "mode": pkt_mode, "packetMode": pkt_mode, "best_artifact_digest": state["incumbent_digest"], "defects": pk_def, "workable_solutions": pk_sol, "withheld": w1 + w2}
        art_text = ""
        if state.get("incumbent_round"):
            inc_w = (state.get("rounds") or {}).get(str(state["incumbent_round"])) or {}
            inc_art_dir = job_dir / inc_w.get("dir", f"rounds/round-{int(state['incumbent_round']):02d}") / "artifact"
            art_text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in sorted(inc_art_dir.rglob("*")) if p.is_file())
            shutil.copytree(inc_art_dir, art_dir, dirs_exist_ok=True)
        gates.gate_packet(pkt, job_dir, state["incumbent_digest"], artifact_text=art_text)
        (r_dir / "packet.json").write_text(json.dumps(pkt, indent=2) + "\n", encoding="utf-8")
        maker_ran = True
        if state.get("seed") and r == 1 and state["incumbent_round"] is None:
            for f in cfg["subjectFiles"]:
                pinned = state["seed"]["digests"].get(f)
                if pinned and gates.sha256_file(job_dir / cfg["seed"]["dir"] / f) != pinned:
                    raise gates.GateError(f"seed file {f} changed after freeze (pinned {pinned[:12]}...); refusing to evaluate a different draft")
                dst = art_dir / f; dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(job_dir / cfg["seed"]["dir"] / f, dst)
            ev_prod = _append(job_dir, "artifact-produced", cfg["seed"]["author"], [art_dir / f for f in cfg["subjectFiles"]], {"seed": True})
            maker_ran, state["seed_round"] = False, r
        else:
            try:
                adapters.run_maker(cfg["maker"], r_dir / "packet.json", art_dir, cfg["subjectFiles"], job_dir, timeout=rem_s, meter=meter, round_no=r)
                state["cost_usd"] = round(meter.accrued(), 6)
            except (TimeoutError, subprocess.TimeoutExpired):
                state.update(terminal_state="needs_owner", reason="maker_failed", maker_error="maker wall-clock deadline exceeded (max_wall_seconds=%s)" % (cfg.get("bounds") or {}).get("max_wall_seconds")); break
            except CapExceeded:
                state.update(terminal_state="needs_owner", reason="cap_reached"); break
            except gates.GateError: raise
            except Exception as e:
                state.update(terminal_state="needs_owner", reason="maker_failed", maker_error=str(e)[-300:]); break
        cand_digest = _subject_digest(art_dir, cfg["subjectFiles"], held_hashes)
        if cand_digest is None or cand_digest in (state["last_candidate_digest"], state["incumbent_digest"]):
            state.update(terminal_state="needs_owner", reason="no_artifact" if cand_digest is None else "dormant_channel"); break
        state["last_candidate_digest"] = cand_digest
        if maker_ran:
            ev_prod = _append(job_dir, "artifact-produced", cfg["maker"]["name"], [art_dir / f for f in cfg["subjectFiles"]], {})
        try:
            checks, ctls, s_rows, exts, d_scores, defects, solutions, _usages = _eval_batch(cfg, job_dir, r, art_dir, t0 + max_wall - wall_base, meter=meter, att_tag=att_tag)
        except CapExceeded:
            state.update(terminal_state="needs_owner", reason="cap_reached"); break
        state["rounds"][str(r)] = {"dir": r_dir.relative_to(job_dir).as_posix(), "producedAt": ev_prod["at"], "digest": cand_digest, "checks": checks, "controls": ctls, "scores": s_rows, "external": exts, "dim_scores": d_scores}
        state["cost_usd"] = round(meter.accrued(), 6)
        if mode == "readings":
            state.update(incumbent_round=r, incumbent_digest=cand_digest, incumbent_scores=d_scores, defects=defects, solutions=solutions)
            by_dim = {d: [c["outcome"] for c in checks if c["dimension"] == d] for d in dims}
            ctl_fail = any(k["outcome"] != k["expected"] for k in ctls)
            eval_fail = any((c.get("unobserved") or {}).get("failed") for c in checks)
            flap = any(len(set(w)) > 1 for w in by_dim.values())
            if ctl_fail or eval_fail or flap or any(not w for w in by_dim.values()):
                state.update(terminal_state="needs_owner", reason="no_observation"); break
            if any(all(x == "unobserved" for x in w) for w in by_dim.values()) or any( "does_not_conform" in w for w in by_dim.values()):
                state.update(terminal_state="needs_owner", reason="needs_owner"); break
            state.update(terminal_state="accepted", reason="census"); break
        if any(c["outcome"] == "unobserved" for c in checks):
            if state["incumbent_round"] is None: state.update(incumbent_round=r, incumbent_digest=cand_digest, incumbent_scores=d_scores)
            state.update(terminal_state="needs_owner", reason="no_observation"); break
        inc = state["incumbent_scores"]
        if inc is None or (any(d_scores[d] > inc[d] for d in dims) and not any(d_scores[d] < inc[d] for d in dims)):
            state.update(incumbent_round=r, incumbent_digest=cand_digest, incumbent_scores=d_scores, defects=defects, solutions=solutions)
        else: state["rejected_defects"] = list(state.get("rejected_defects", [])) + list(defects)
        if inc is not None and any(d_scores[d] > inc[d] for d in dims) and any(d_scores[d] < inc[d] for d in dims):
            state.update(terminal_state="needs_owner", reason="dimension_tradeoff"); break
        if any(state["incumbent_scores"][d] < floors[d] for d in dims):
            state.update(terminal_state="rejected", reason="below_floor"); break
        win_checks = state["rounds"][str(state["incumbent_round"])]["checks"]
        if all(c["outcome"] == "conforms" for c in win_checks) or r == max_rounds:
            state.update(terminal_state="accepted" if all(c["outcome"] == "conforms" for c in win_checks) else "needs_owner",
                         reason="all_conform" if all(c["outcome"] == "conforms" for c in win_checks) else "cap_reached"); break
        state["wall_spent_seconds"] = round(state.get("wall_spent_seconds", 0.0) + (time.time() - t0), 3)
        state["open_round"], t0 = None, time.time(); _write_state(job_dir, state, meter)
    except gates.GateError as e: state.update(terminal_state="needs_owner", reason="integrity_error", integrity_error=str(e)[-300:]); raise
    except KeyboardInterrupt: state.update(terminal_state="needs_owner", reason="interrupted")
    except Exception as e: state.update(terminal_state="needs_owner", reason="runtime_error", runtime_error=str(e)[-300:])
    finally:
        if prev_term is not None: signal.signal(signal.SIGTERM, prev_term)
        if state.get("terminal_state") is None: state.update(terminal_state="needs_owner", reason="interrupted")
        state["open_round"], state["cost_usd"] = None, round(meter.accrued(), 6)
        state["wall_spent_seconds"] = float(state.get("wall_spent_seconds", 0.0)) + (time.time() - t0)
        with job_lock(job_dir):
            if not _has_disposition(job_dir):
                if state.get("terminal_state") == "needs_owner": _write_escalation(job_dir, state, cfg)
                _write_state(job_dir, state, meter)
    return {"accepted": 0, "rejected": 1, "needs_owner": 2}[state["terminal_state"]]


def resume_job(job_dir):
    with job_lock(job_dir):
        return _resume_job_held(job_dir)


def _resume_job_held(job_dir):
    # G:R1/P2/Cut-E recover interrupted round without moving logged files; preserve spend on cap
    job_dir = pathlib.Path(job_dir).resolve()
    cfg, state = _load_job_json(job_dir), json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    _refuse_after_disposition(job_dir)
    if state.get("terminal_state"):
        raise gates.GateError(f"job already reached terminal state {state['terminal_state']} ({state.get('reason')}); nothing to resume")
    if not state.get("open_round"):
        raise gates.GateError("no interrupted round to resume (this job was not hard-killed mid-round)")
    r = int(state["open_round"])
    gates.check_pins(cfg, job_dir, state["pins"])
    meter = _open_meter(job_dir, cfg, state, require_anchor=True)
    amended = {d for row in meter._rows if row.get("kind") == "amendment" for d in row.get("amends", [])}
    round_rows = [row["digest"] for row in meter._rows if row.get("kind") == "call" and row.get("round") == r and row["digest"] not in amended]
    if round_rows:
        meter.amend(round_rows, note=f"round {r} discarded by resume")
    _append(job_dir, "round-discarded", "edi-run", [],
            {"round": r, "resumed_after_interrupt": True, "discarded_spend_usd": round(sum(meter.row_cost(d) for d in round_rows), 6) if round_rows else 0.0})
    state["resume_from"], state["open_round"] = r, None
    _write_state(job_dir, state, meter)
    return _run_job_held(job_dir)


def finalize_job(job_dir, state="given", by="Owner", ack_unobserved=None):
    # G:P6/P9 finalize is one lifecycle transaction under job_lock
    job_dir = pathlib.Path(job_dir).resolve()
    cfg, mode = _load_job_json(job_dir), None
    mode = cfg.get("mode") or "scored"
    if not (job_dir / "state.json").is_file():
        raise gates.GateError("no state.json; the job has not been frozen (finalize follows freeze)")
    with job_lock(job_dir):
        try:
            st = json.loads((job_dir / "state.json").read_bytes().decode("utf-8"))
        except FileNotFoundError:
            raise gates.GateError("no state.json; the job has not been frozen (finalize follows freeze)")
        log_path = job_dir / "job.jsonl"
        if (log_problems := custody_verify(log_path)):
            raise gates.GateError("custody log chain broken; refusing to finalize: " + "; ".join(map(str, log_problems[:3])))
        log_events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        if not log_events or log_events[0].get("type") != "job-start":
            raise gates.GateError("custody log does not begin with job-start; refusing to finalize a truncated or foreign log")
        if any(e.get("type") == "disposition" for e in log_events):
            raise gates.GateError("job already finalized (disposition event exists); finalize runs once")
        gates.check_pins(cfg, job_dir, st["pins"])
        if state != "declined" and (st.get("open_round") or not st.get("terminal_state")):
            raise gates.GateError("job has no end state (a run was interrupted mid-round, ended without a terminal state, or never ran): resume it to an end state, or finalize --disposition declined to abandon it (P6)")
        meter = _open_meter(job_dir, cfg, st)
        gates.gate_seats(cfg, job_dir, approver_by=by)
        ack = ack_unobserved if ack_unobserved is not None else cfg["disposition"].get("acknowledgesUnobserved", [])
        if not st.get("incumbent_round"):
            disp_data = {"state": state, "acknowledgesUnobserved": ack}
            if mode == "readings":
                disp_data.update(mode="readings", terminalState=st.get("terminal_state"), outcome=st.get("reason"), disagreements=[])
            _append(job_dir, "disposition", by, [], disp_data)
            return verify_job(job_dir) if mode == "readings" else _emit_gap_record(job_dir, st, cfg)
        win_r, max_r, art_dst = str(st["incumbent_round"]), str(max(int(k) for k in st["rounds"])), job_dir / "artifact"
        win = st["rounds"][win_r]
        shutil.rmtree(art_dst, ignore_errors=True)
        shutil.copytree(job_dir / win.get("dir", f"rounds/round-{int(win_r):02d}") / "artifact", art_dst)
        (job_dir / "scores.jsonl").write_text("\n".join(json.dumps(r) for r in win["scores"]) + "\n", encoding="utf-8")
        seed_author = cfg["seed"]["author"] if (cfg.get("seed") and st.get("seed_round") == int(win_r)) else None
        if win_r != max_r:
            ev_prod = _append(job_dir, "artifact-produced", seed_author or cfg["maker"]["name"], [art_dst / f for f in cfg["subjectFiles"]], {"seed": True} if seed_author else {})
            checks = [dict(c, at=_append(job_dir, "check", c["evaluator"], [], c)["at"]) for c in win["checks"]]
            for k in win["controls"]:
                _append(job_dir, "control", k["evaluator"], [], dict(k))
            for ex in win["external"]:
                _append(job_dir, "external", ex["readBy"], [], ex)
            prod_at = ev_prod["at"]
        else:
            checks, prod_at = win["checks"], win["producedAt"]
        if (job_dir / "usage.jsonl").is_file():
            if st.get("usage_unanchored") or not st.get("usage_anchor"):
                raise gates.GateError("usage.jsonl is present but state.json has no spend anchor (unanchored log); refusing to attest spend that no state write vouched for (P9)")
            try:
                with meter.file_lock():
                    meter.refresh()
                    meter.verify(anchor=st.get("usage_anchor"), exact=True)
                    rows_n, head_d = meter.anchor()[:2]
                    _append(job_dir, "spend", "edi-run", [job_dir / "usage.jsonl"],
                            {"usd": round(meter.accrued(), 6), "discardedUsd": round(meter.discarded(), 6), "rows": rows_n, "head": head_d})
            except ValueError as e:
                raise gates.GateError(f"usage.jsonl failed integrity verification: {e} (spend integrity)")
        disagreements = [{"dimension": d["id"], "checks": [i for i, c in enumerate(checks) if c["dimension"] == d["id"]],
                          "words": [c["outcome"] for c in checks if c["dimension"] == d["id"]], "note": "evaluators disagreed"}
                         for d in cfg["bar"]["dimensions"] if len({c["outcome"] for c in checks if c["dimension"] == d["id"]}) > 1]
        disp_data = {"state": state, "acknowledgesUnobserved": ack}
        if mode == "readings":
            disp_data.update(mode="readings", terminalState=st.get("terminal_state"), outcome=st.get("reason"), disagreements=disagreements)
        ev_disp = _append(job_dir, "disposition", by, [], disp_data)
        if mode == "readings":
            return verify_job(job_dir)
    violations = [{"dimension": d["id"], "description": f"{d['id']} failed conform threshold"} for d in cfg["bar"]["dimensions"] if any(c["dimension"] == d["id"] and c["outcome"] == "does_not_conform" for c in checks)]
    evals_clean = [{k: v for k, v in e.items() if k in ("id", "name", "kind", "model", "script", "sameModelAsMaker")} for e in cfg["evaluators"]]
    def _assemble():
        inputs = {"subject": {"dir": "artifact"}, "specVersion": cfg.get("specVersion", "0.9.8"), "levelClaimed": cfg["levelClaimed"],
                  "bar": dict({k: v for k, v in cfg["bar"].items() if k != "file"}, frozenAt=st["frozenAt"]),
                  "maker": ({"name": seed_author, "kind": "person", "version": cfg["seed"].get("origin", "owner-supplied"), "method": cfg["maker"]["method"], "model": None} if seed_author else {**{k: v for k, v in cfg["maker"].items() if k in ("name", "kind", "version", "method", "model")}, **({"kind": "model"} if cfg["maker"].get("kind") == "agent" else {})}),
                  "startedAt": st["startedAt"], "producedAt": prod_at, "instrument": cfg["instrument"], "evaluators": evals_clean, "checks": checks, "controls": win["controls"],
                  "disagreements": [{k: v for k, v in dg.items() if k != "words"} for dg in disagreements],
                  "violations": violations, "separationGaps": cfg.get("separationGaps", []), "retention": cfg["retention"], "external": win["external"],
                  "disposition": {"state": state, "by": by, "at": ev_disp["at"], "acknowledgesUnobserved": ack}}
        for k in ("spread", "demonstrations", "readsBeforeFreeze"):
            if k == "spread" and cfg["levelClaimed"] in ("CL2", "CL3"):
                inputs["spread"] = [{"dimension": d["id"], "evaluator": ev["id"], "runs": len(outs), "agreement": Counter(outs).most_common(1)[0][1]} for ev in evals_clean for d in cfg["bar"]["dimensions"] if (outs := [c["outcome"] for c in checks if c["evaluator"] == ev["id"] and c["dimension"] == d["id"]])]
            elif k == "demonstrations" and cfg["levelClaimed"] == "CL3": inputs["demonstrations"] = st.get("demonstrations", [])
            elif k == "readsBeforeFreeze" and cfg["levelClaimed"] == "baseline": inputs["readsBeforeFreeze"] = cfg.get("readsBeforeFreeze", [])
        (job_dir / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n", encoding="utf-8")
        proc = subprocess.run([sys.executable, str(VENDOR / "make_record.py"), str(job_dir / "inputs.json"), str(job_dir / "record.json"), "--log", str(log_path)], cwd=str(job_dir), capture_output=True, text=True)
        return proc.returncode, (proc.stderr.strip() or proc.stdout.strip())[-500:]

    try: rc, detail = _assemble()
    except Exception as e: rc, detail = -1, str(e)[-500:]  # noqa: BLE001
    if rc != 0:
        return _emit_gap_record(job_dir, st, cfg, extra=[{"invariant": "custody-assembly", "missing": detail}])
    return verify_job(job_dir)


def read_job(job_dir):
    # G:C29/Fix-2 freeze if needed and run; print terminal state and Owner finalize command; write no disposition
    job_dir = pathlib.Path(job_dir).resolve()
    cfg = _load_job_json(job_dir)
    if (cfg.get("mode") or "scored") != "readings":
        raise gates.GateError("'edi.py read' requires job.json mode == 'readings'")
    if not (job_dir / "state.json").is_file():
        freeze_job(job_dir)
    rc = run_job(job_dir)
    st = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    disp = "given" if st.get("terminal_state") == "accepted" else "given|declined"
    print(f"readings outcome: {st.get('terminal_state')}/{st.get('reason')} — close with: python3 edi.py finalize {job_dir} --disposition {disp} --by <Owner>")
    return rc


def verify_job(job_dir):
    # G:G-CUS3/C29/RD4/RD5 verify job.json pin, custody chain, and evidence/artifact digests
    job_dir, cfg = pathlib.Path(job_dir).resolve(), _load_job_json(job_dir)
    log_p = job_dir / "job.jsonl"
    if log_p.is_file():
        pins_ev = [e for e in custody_record.read(str(log_p)) if e.get("type") == "job-pin"]
        if pins_ev:
            cur_j = hashlib.sha256(json.dumps({k: v for k, v in cfg.items() if k != "disposition"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if cur_j != ((pins_ev[-1].get("data") or {}).get("pins") or {}).get("job"):
                return 1
    if (cfg.get("mode") or "scored") == "readings" and not (job_dir / "record.json").is_file():
        if not _has_disposition(job_dir):
            raise gates.GateError("no disposition in job.jsonl; the readings job has not been finalized yet (verify follows finalize)")
        if custody_verify(job_dir):
            return 1
        events = custody_record.read(str(log_p))
        for e in events:
            if e.get("type") == "check" and (ev := (e.get("data") or {}).get("evidence")):
                ev_p = job_dir / "evidence" / ev.get("name", "")
                if not ev_p.is_file() or custody_record.digest_file(ev_p) != ev.get("sha256"):
                    return 1
        if (prods := [e for e in events if e.get("type") == "artifact-produced"]):
            for path, h in (prods[-1].get("files") or {}).items():
                art_p = job_dir / "artifact" / pathlib.Path(path).name
                if not art_p.is_file() or custody_record.digest_file(art_p) != h:
                    return 1
        return 0
    try: record_bytes = (job_dir / "record.json").read_bytes()
    except FileNotFoundError: raise gates.GateError("no record.json; the job has not been finalized yet (verify follows finalize)")
    if custody_verify(job_dir): return 1
    rep = custody_check.verify(json.loads(record_bytes.decode("utf-8")), files=str(job_dir / "artifact"), evidence=str(job_dir / "evidence"), bar=str(job_dir / cfg["bar"]["file"]))
    return 0 if rep["verdict"] == "OK" else (1 if rep["verdict"] == "FAILED" else 2)
