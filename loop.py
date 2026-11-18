"""Deterministic E-DI state machine (I-05 through I-07) wrapping evaluatordriven/custody tools."""
from collections import Counter
from contextlib import contextmanager
import fcntl, json, math, os, pathlib, shutil, signal, subprocess, sys, threading, time
import adapters, gates
import metering
from metering import Meter, CapExceeded, UnboundedOutput

VENDOR = pathlib.Path(__file__).resolve().parent / "vendor"
sys.path.insert(0, str(VENDOR))
import check as custody_check, record as custody_record  # noqa: E402


def _load_job_json(job_dir):
    """The job config is parsed at the STRICT boundary (verification re-review
    #8): duplicate JSON properties are refused by name, so the canonical digest
    of job.json is injective and a doubled key cannot carry two meanings."""
    try:
        return gates.strict_json_loads((pathlib.Path(job_dir) / "job.json").read_text(encoding="utf-8"))
    except ValueError as e:
        raise gates.GateError(f"job.json refused at the parse boundary: {e}")


# --- job lifecycle lock -------------------------------------------------------------
# Verification re-review blockers 5/6: ONE lock ordering rule — cross-process
# file locks (flock) are ALWAYS outer, in-process locks inner, and ALL custody
# appends plus the finalize transaction serialize on ONE job-scoped flock
# (`job.lock`), so a disposition, the appends it guards, and the state/
# escalation writes around it are one lifecycle transaction. The meter's own
# flock (`usage.lock`) is acquired only INSIDE a held job lock, never the
# reverse. Reentrant per-thread (finalize's guarded _append calls re-enter).
_JOB_LOCKS = {}
_JOB_LOCKS_GUARD = threading.Lock()


class _JobLifecycleLock:
    def __init__(self, path):
        self._path = path
        self._rl = threading.RLock()
        self._fd = None
        self._depth = 0

    @contextmanager
    def hold(self):
        # ACTUAL acquisition order (documented as the code behaves, verification
        # re-review #6): the per-thread RLock is taken FIRST, then — on the first
        # (depth-0) entry — mkdir, open and flock on job.lock. This is the
        # OPPOSITE nesting of the meter's rule ("flock outer, thread lock
        # inner"); the two are different lock families and never nest in the
        # reverse direction: the meter's usage.lock is only ever acquired INSIDE
        # a held job lock (finalize), never the reverse. Reentrancy is by
        # _depth under the RLock.
        # Exception-safe acquisition: _depth is incremented ONLY AFTER the flock
        # is successfully held, and a failed mkdir/open/flock cleans up its own
        # file descriptor and re-raises with the depth untouched — so a failed
        # acquisition leaves no residue and the NEXT caller acquires cleanly
        # (pinned by the injected-failure test).
        with self._rl:
            if self._depth == 0:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd = open(self._path, "a")
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
                except BaseException:
                    fd.close()
                    raise
                self._fd = fd
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                    self._fd.close()
                    self._fd = None


def job_lock(job_dir):
    """Reentrant job-scoped lifecycle lock (flock on job.lock, threading RLock
    inner). Every _append and every finalize/state write path takes it."""
    jd = pathlib.Path(job_dir).resolve()
    with _JOB_LOCKS_GUARD:
        lk = _JOB_LOCKS.get(str(jd))
        if lk is None:
            lk = _JOB_LOCKS[str(jd)] = _JobLifecycleLock(jd / "job.lock")
    return lk.hold()


def _has_disposition(job_dir):
    log = pathlib.Path(job_dir) / "job.jsonl"
    return log.is_file() and any(e.get("type") == "disposition" for e in custody_record.read(str(log)))


def _append(job_dir, etype, actor, files, data):
    # P5: file keys are relative to the JOB, never to the caller's working
    # directory, so finalize and verify give the same answer from anywhere.
    jd = pathlib.Path(job_dir).resolve()
    # Round-3 external review #10 + verification re-review blocker 5: a
    # disposition is a LIFECYCLE BARRIER, enforced INSIDE the same job lock the
    # append itself takes — the scan and the append are one transaction, so a
    # round writer paused mid-guard cannot interleave with a finalize's
    # disposition: it either lands before the disposition or is refused after.
    with job_lock(jd):
        if _has_disposition(jd):
            raise gates.GateError("job already has a disposition; no custody event may follow it — the job is closed (P6 lifecycle barrier)")
        rel = {(f.resolve().relative_to(jd).as_posix() if f.resolve().is_relative_to(jd) else str(f.resolve())): gates.sha256_file(f) for f in files}
        return custody_record.append(str(job_dir / "job.jsonl"), {"type": etype, "at": custody_record.now(), "actor": actor, "source": "", "files": rel, "data": data})


def custody_verify(job_dir):  # job_dir may also be the log path itself
    """Chain + content verification of job.jsonl with file re-hash checks."""
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
                if jd_file and (not legacy or not cw_file
                                or custody_record.digest_file(jd_cand) == custody_record.digest_file(cw)):
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


# --- Gap-emission (docs/2026-EDI-Runtime-Solution-Architecture.md §6) -------------
# An incomplete run emits a gap-listed record (invalid by design) instead of nothing.
GAP_RULES = (
    ("I-01", lambda st: not st.get("frozenAt"), "bar freeze never recorded"),
    ("I-05", lambda st: not st.get("rounds"), "no evaluation batch completed (no checks observed)"),
    ("I-06", lambda st: not st.get("incumbent_round"), "no artifact survived as Pareto-best incumbent"),
    ("I-07", lambda st: not st.get("terminal_state"), "loop has no terminal state"),
)


def _gap_list(st, extra=()):
    gaps = [{"invariant": inv, "missing": msg} for inv, test, msg in GAP_RULES if test(st)]
    return gaps + list(extra)


def _emit_gap_record(job_dir, st, cfg, extra=()):
    gaps = _gap_list(st, extra)
    lines = (job_dir / "job.jsonl").read_text(encoding="utf-8").splitlines()
    log_last = json.loads(lines[-1]) if lines else {}
    rec = {"recordType": "edi-run/gap-record",
           "x-edi-run": {"gaps": gaps, "terminalState": st.get("terminal_state"),
                          "reason": st.get("reason"), "startedAt": st.get("startedAt"),
                          "lastEventHash": log_last.get("hash")}}
    (job_dir / "record.json").write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    rep = custody_check.verify(rec, files=str(job_dir / "artifact"), evidence=str(job_dir / "evidence"),
                               bar=str(job_dir / cfg["bar"]["file"]))
    if rep["verdict"] == "OK":
        raise gates.GateError("gap-listed record unexpectedly verified OK (emitter defect)")
    return 1


def _preflight_freeze_cfg(cfg):
    """Shape-validate every dict key the real append path reads, BEFORE any
    write. A missing key (e.g. an approval without 'by') used to KeyError in the
    REAL freeze only, after job.jsonl appends were already written (no-trace
    violation), while --dry-run exited 0 -- dry-run was not an oracle. Preflight
    makes both paths fail with a GateError before the first log write."""
    bar = cfg.get("bar") or {}
    why = "the real append path records it; refusing before any log write"
    if not isinstance(bar.get("author"), str) or not bar["author"].strip():
        raise gates.GateError(f"freeze pre-flight: bar.author must be a non-empty string ({why})")
    maker = cfg.get("maker") or {}
    if not isinstance(maker.get("name"), str) or not maker["name"].strip():
        raise gates.GateError(f"freeze pre-flight: maker.name must be a non-empty string ({why})")
    for i, a in enumerate(bar.get("approvals") or []):
        if not isinstance(a, dict) or not isinstance(a.get("by"), str) or not a["by"].strip():
            raise gates.GateError(f"freeze pre-flight: bar.approvals[{i}].by must be a non-empty string ({why})")
        bd = a.get("barDigest") if isinstance(a, dict) else None
        bd_sha = bd.get("sha256") if isinstance(bd, dict) else None
        if not isinstance(bd_sha, str) or not bd_sha.strip():
            raise gates.GateError(f"freeze pre-flight: bar.approvals[{i}].barDigest.sha256 must be a non-empty string ({why})")
    seed = cfg.get("seed")
    if seed is not None:
        if not isinstance(seed, dict) or not isinstance(seed.get("author"), str) or not seed["author"].strip():
            raise gates.GateError(f"freeze pre-flight: seed.author must be a non-empty string ({why})")


def init_job(job_dir):
    """Structure-only job scaffold: directories and EMPTY templates. Never
    writes starter controls or any substantive content (structure-only rule)."""
    import sys as _sys
    job_dir = pathlib.Path(job_dir)
    # a symlink target (including a dangling one) is refused outright: init must
    # never write through a link into a directory the caller did not name
    if job_dir.is_symlink():
        print(f"refusing to init: {job_dir} is a symlink; init never writes through a link", file=_sys.stderr)
        return 1
    if job_dir.exists() and not job_dir.is_dir():
        print(f"refusing to init: {job_dir} exists and is not a directory", file=_sys.stderr)
        return 1
    if job_dir.exists() and any(job_dir.iterdir()):
        print(f"refusing to init: {job_dir} exists and is not empty", file=_sys.stderr)
        return 1
    job_dir.mkdir(parents=True, exist_ok=True)
    for d in ("scripts", "seed", "rounds"):
        (job_dir / d).mkdir(exist_ok=True)
    for name in ("bar.md", "rubric.md", "method.md"):
        (job_dir / name).write_text("", encoding="utf-8")
    (job_dir / "AUTHORSHIP.md").write_text(
        "Operator:\nBar author:\nRubric author:\nMethod author:\nSeed author:\nSponsor:\n",
        encoding="utf-8")
    job = {
        "job": None, "levelClaimed": None, "specVersion": None,
        "bounds": {"max_rounds": None, "max_cost_usd": None, "max_wall_seconds": None},
        "mode": None, "subjectFiles": [],
        "bar": {"file": "bar.md", "digest": {"sha256": None}, "frozenAt": None,
                "author": None, "dimensions": [], "approvals": []},
        "maker": {"name": None, "kind": None, "version": None,
                  "method": {"file": "method.md"}, "model": None,
                  "sandbox": None, "command": None},
        "instrument": {"rubric": {"file": "rubric.md"}, "runner": {"file": None}},
        "evaluators": [], "controls": {"clean": None, "planted": None},
        "retention": {"location": None, "public": None},
        "disposition": {"state": None, "by": None, "acknowledgesUnobserved": []},
        "pricing": {}, "seed": None, "barDigest": None,
    }
    (job_dir / "job.json").write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
    return 0


def _job_snapshot(job_dir):
    snap = {}
    for p in sorted(job_dir.rglob("*")):
        rel = str(p.relative_to(job_dir)) + ("/" if p.is_dir() else "")
        if p.is_file():
            st = p.stat()
            snap[rel] = (st.st_size, st.st_mtime_ns, st.st_mode)
        else:
            snap[rel] = ("dir", p.stat().st_mtime_ns)  # mtime_ns: dir create+delete is not invisible
    return snap


def freeze_job(job_dir, started_at=None, dry_run=False):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg = _load_job_json(job_dir)
    # pre-flight the cfg shape FIRST: real freeze and --dry-run must agree, and
    # both must fail before any log write (no-trace promise, I-01)
    _preflight_freeze_cfg(cfg)
    # dry-run write-guard: snapshot the whole job tree; if ANY gate call touches
    # the host inside the job dir, that is a defect, detected before returning.
    # Scope is job-dir-scoped BY DESIGN: atime changes, create+delete inside the
    # guard window, and writes outside job_dir are out of scope. Directories are
    # snapshotted with mtime_ns too (a gate that creates+deletes or rewrites a
    # dir inside job_dir shows up as a changed dir mtime).
    pre_snap = _job_snapshot(job_dir) if dry_run else None
    gates.gate_bounds(cfg)
    gates.gate_seats(cfg, job_dir)
    model_ids = []
    if cfg["maker"].get("kind") in ("model", "agent"):
        model_ids.append((cfg["maker"].get("model") or {}).get("identifier") if isinstance(cfg["maker"].get("model"), dict) else cfg["maker"].get("model"))
    model_ids += [(e.get("model") or {}).get("identifier") if isinstance(e.get("model"), dict) else e.get("model") for e in cfg["evaluators"] if e.get("kind") == "model"]
    pricing_map = cfg.get("pricing", {})
    missing = [m for m in model_ids if m and m not in pricing_map and "default" not in pricing_map]
    if missing:
        raise gates.GateError(f"missing pricing for model seat(s) {missing}; add a pricing map to job.json so the cost cap is enforceable")
    empty_ids = [m for m in model_ids if not m]
    if empty_ids:
        raise gates.GateError("every model/agent seat needs a non-empty model.identifier; an empty identifier skips pricing entirely (recheck G2)")
    zero = []
    for key, p in pricing_map.items():
        if not isinstance(p, dict):
            zero.append(key)
            continue
        for k in ("input_usd_per_mtok", "output_usd_per_mtok"):
            try:
                v = float(p.get(k, 0.0))
            except (TypeError, ValueError):
                zero.append(key)
                break
            if not math.isfinite(v) or v <= 0.0:
                zero.append(key)
                break
    if zero:
        raise gates.GateError(f"pricing entries {zero} contain a zero, negative, non-numeric or non-finite rate; unpriced spend must be refused, never silently $0 (recheck G2)")
    unbounded = [m for m in model_ids if m and metering.output_bound(cfg.get("bounds"), pricing_map, m) is None]
    if unbounded:
        raise gates.GateError(f"model seat(s) {unbounded} have no max_output_tokens (set bounds.max_output_tokens or "
                              "pricing[<model>].max_output_tokens): output spend that cannot be bounded is refused at freeze, "
                              "not at the first paid call (I-07)")
    gates.gate_commands(cfg, job_dir)
    # CL1-14: stamp the computed bar digest in memory BEFORE the pins are taken;
    # it reaches job.json only through the atomic write after every gate passes.
    _bar = cfg.get("bar") or {}
    if (_bar.get("digest") or {}).get("sha256") is None:
        _bar["digest"] = {"sha256": gates.sha256_file(gates.safe_rel(_bar.get("file", "bar.md"), job_dir))}
    if (job_dir / "rounds").exists():
        # rounds/ is runtime-produced custody space; if it exists at freeze the
        # pin-vs-output boundary is already violated (Opus deep review 2026-11-18)
        raise gates.GateError("rounds/ already exists; it is runtime custody space, never present at freeze")
    if (job_dir / "job.jsonl").exists():
        # freeze-once check BEFORE any job.json mutation: a refused re-freeze
        # must not rewrite the frozen config (GPT 6.2-sol adversarial finding,
        # 2026-11-18 — the bar-digest stamp used to land first)
        raise gates.GateError("job.jsonl already exists; freeze runs once")
    pins = gates.gate_instruments(cfg, job_dir)
    # bar-level refusal checks run before ANY append: a refused freeze must
    # leave no job.jsonl for the corrected retry to trip over (I-01 no-trace)
    gates.gate_freeze_pre(cfg, job_dir)
    # Every check that can refuse the freeze runs BEFORE the first log write --
    # a refused freeze must leave nothing behind for the corrected retry to trip over.
    seed = cfg.get("seed")
    seed_pins = None
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
    demos, rub_txt = [], gates.safe_rel(cfg["instrument"]["rubric"]["file"], job_dir).read_text(encoding="utf-8")
    if cfg["levelClaimed"] == "CL3":
        p_case = gates.safe_rel(cfg["controls"]["planted"], job_dir)
        # P7: demonstration calls are billed like any other — metered against the
        # cap (a trip there is a freeze refusal), and never made by a dry run,
        # which promises to touch nothing and spend nothing.
        demo_meter = None if dry_run else Meter(job_dir, pricing=pricing_map, bounds=cfg.get("bounds"))
        if demo_meter is not None:
            demo_meter._round = 0
        for ev in cfg["evaluators"]:
            if dry_run and ev.get("kind") == "model":
                print(f"freeze --dry-run: skipped the paid CL3 planted-case demonstration for evaluator {ev.get('id')}", file=sys.stderr)
                continue
            for d in cfg["bar"]["dimensions"]:
                try:
                    res = adapters.run_evaluator(ev, p_case, d["id"], job_dir=job_dir, dim_cfg=d, rubric_txt=rub_txt, meter=demo_meter)
                except CapExceeded as e:
                    if isinstance(e, UnboundedOutput):
                        raise gates.GateError(str(e))  # review #1: refuse unbounded output by name
                    raise gates.GateError(f"cost cap reached during the CL3 planted-case demonstration: {e}")
                if res.get("score") is None or float(res["score"]) >= float((d.get("scale") or {}).get("threshold", 4.0)):
                    raise gates.GateError(f"CL3 planted case did not fail on {ev['id']}/{d['id']} (CL3-1)")
                demos.append({"dimension": d["id"], "evaluator": ev["id"], "planted": {"sha256": gates.sha256_file(p_case)}, "outcome": "does_not_conform", "at": custody_record.now()})
    if dry_run:
        # Same gate code, zero writes: the only check a real freeze performs that
        # needs the log's timestamps is the frozenAt/work-started ordering, so
        # synthesize exactly what the real path would record (in memory only).
        if cfg["levelClaimed"] == "baseline":
            s_at = started_at or (custody_record.now() if hasattr(custody_record, "now") else None)
            time.sleep(0.002)
            cfg["bar"]["frozenAt"] = custody_record.now()
        else:
            s_at = started_at or custody_record.now()
        gates.gate_freeze(cfg, job_dir, started_at=s_at)
        post_snap = _job_snapshot(job_dir)
        if post_snap != pre_snap:
            changed = sorted(k for k in set(post_snap) | set(pre_snap or {})
                             if post_snap.get(k) != (pre_snap or {}).get(k))
            raise RuntimeError(f"freeze --dry-run wrote to the host (defect): {changed[:5]}")
        return 0
    # persist the stamped digest NOW: every gate has passed and this is the
    # real (non-dry-run) path — the last point where a refusal can still
    # happen is before this write, so a refused freeze leaves job.json
    # byte-identical (Opus deep review 2026-11-18). Atomic tmp+replace.
    if not dry_run:
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
        cfg["bar"]["frozenAt"] = ev_frz["at"]
        for a in cfg["bar"]["approvals"]:
            _append(job_dir, "approval", a["by"], [bar_p], {"role": a["role"], "barDigest": a["barDigest"]["sha256"], "adopted": a.get("adopted")})
    else:
        ev_frz = _append(job_dir, "bar-frozen", cfg["bar"]["author"], [bar_p], {})
        for a in cfg["bar"]["approvals"]:
            _append(job_dir, "approval", a["by"], [bar_p], {"role": a["role"], "barDigest": a["barDigest"]["sha256"], "adopted": a.get("adopted")})
        time.sleep(0.002)
        s_at = started_at or _append(job_dir, "work-started", cfg["maker"]["name"], [], {})["at"]
    # Round-3 external review #7: the freeze-time pins are anchored OUTSIDE
    # editable state.json — in the custody chain, which the agent/operator cannot
    # rewrite. check_pins verifies state.json's pins AGAINST this event.
    _append(job_dir, "job-pin", "edi-run", [], {"pins": pins})
    gates.gate_freeze(cfg, job_dir, started_at=s_at)
    ho = job_dir / "held_out"
    if ho.is_dir():
        for f in ho.rglob("*"):
            os.chmod(f, 0o700 if f.is_dir() else 0o600)
        os.chmod(ho, 0o700)
    state = {"status": "frozen", "pins": pins, "startedAt": s_at, "frozenAt": ev_frz["at"], "demonstrations": demos, "incumbent_round": None, "incumbent_digest": None, "incumbent_scores": None, "last_candidate_digest": None, "defects": [], "solutions": [], "rounds": {}, "terminal_state": None, "reason": None, "cost_usd": 0.0, "seed": ({"dir": seed["dir"], "author": seed["author"], "digests": seed_pins} if seed else None)}
    (job_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return 0


def _eval_batch(cfg, job_dir, round_n, art_dir, deadline, meter=None):
    if meter is not None:
        meter._round = round_n  # evaluator meter rows carry their round for R1 amendments
    (job_dir / "evidence").mkdir(parents=True, exist_ok=True)
    bid, dims = f"B{round_n}", cfg["bar"]["dimensions"]
    scales, ordered = {d["id"]: d.get("scale") for d in dims}, sorted(cfg["evaluators"], key=lambda e: 0 if e["kind"] == "script" else 1)
    checks, controls, scores_rows, externals, per_dim, defects, solutions, usages = [], [], [], [], {d["id"]: [] for d in dims}, [], [], []
    clean_p, planted_p = gates.safe_rel(cfg["controls"]["clean"], job_dir), gates.safe_rel(cfg["controls"]["planted"], job_dir)
    rub_txt = gates.safe_rel(cfg["instrument"]["rubric"]["file"], job_dir).read_text(encoding="utf-8")
    for ev in ordered:
        ev_id, bad_dims = ev["id"], set()
        for d in dims:
            did, thr = d["id"], float((scales[d["id"]] or {}).get("threshold", 4.0))
            for tag, path, exp in (("clean", clean_p, "conforms"), ("planted", planted_p, "does_not_conform")):
                cid, res = f"K-{bid}-{ev_id}-{did}-{tag}", adapters.run_evaluator(ev, path, did, job_dir=job_dir, timeout=max(1.0, deadline - time.time()), dim_cfg=d, rubric_txt=rub_txt, meter=meter)
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
                if ev["kind"] == "model" and res.get("_usage"):
                    usages.append({"evaluator": ev_id, "model": ev.get("model", {}).get("identifier") if isinstance(ev.get("model"), dict) else ev.get("model"), "case": tag, "usage": res["_usage"]})
        for d in dims:
            did, sc_cfg = d["id"], scales[d["id"]]
            for run_i in range(1, int(ev.get("runs", 2)) + 1):
                har = job_dir / "evidence" / f"harness-{ev_id}-{bid}.json" if ev["kind"] == "model" and run_i == 1 else None
                res = adapters.run_evaluator(ev, art_dir, did, job_dir=job_dir, harness_out=har, timeout=max(1.0, deadline - time.time()), dim_cfg=d, rubric_txt=rub_txt, meter=meter)
                if did in bad_dims or "unobserved" in res or res.get("score") is None:
                    u = res.get("unobserved") or {"channel": ev_id, "reason": "evaluator failed batch control (CL1-9)"}
                    chk = {"dimension": did, "evaluator": ev_id, "run": run_i, "batch": bid, "at": custody_record.now(), "outcome": "unobserved", "score": None, "failureCondition": f"score < {sc_cfg['threshold']}" if sc_cfg else d["criterion"], "evidence": None, "unobserved": u}
                else:
                    score, out = float(res["score"]), ("conforms" if float(res["score"]) >= float(sc_cfg["threshold"]) else "does_not_conform")
                    if res.get("_usage"):
                        usages.append({"evaluator": ev_id, "model": ev.get("model", {}).get("identifier") if isinstance(ev.get("model"), dict) else ev.get("model"), "usage": res["_usage"]})
                    ev_name = f"reading-{did}-{ev_id}-r{round_n}-run{run_i}.json"
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
    if meter is not None:
        # P9 (round-3 external review #5): every state write anchors the usage log
        # as (row count, head digest, accrued TOTAL) — the anchor proves the
        # LATEST billed total, not a prefix. A later state write re-anchors any
        # suffix appended in the meantime.
        state["usage_anchor"] = list(meter.anchor())
        state.pop("usage_unanchored", None)  # the log is anchored now
    tmp = job_dir / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, job_dir / "state.json")


def _cost_of(usage, pricing):
    u = usage or {}
    rate_in, rate_out = float(pricing.get("input_usd_per_mtok", 0.0)), float(pricing.get("output_usd_per_mtok", 0.0))
    if "prompt_tokens" in u or "completion_tokens" in u:
        return (float(u.get("prompt_tokens", 0) or 0) * rate_in + float(u.get("completion_tokens", 0) or 0) * rate_out) / 1e6
    # providers that report only a total: price it at the conservative (higher) rate
    total = float(u.get("total_tokens", 0) or 0)
    return total * max(rate_in, rate_out) / 1e6


def _accrue(state, usages, pricing):
    for u in usages:
        key = u["model"] or u["evaluator"]
        prev = state.get("token_usage", {}).get(key, {})
        state["token_usage"] = {**state.get("token_usage", {}), key: {k: prev.get(k, 0) + v for k, v in u["usage"].items() if isinstance(v, (int, float))}}
        state["cost_usd"] = round(state.get("cost_usd", 0.0) + _cost_of(u["usage"], pricing.get(key, pricing.get("default", {}))), 6)


def _write_escalation(job_dir, state, cfg):
    reason = state.get("reason")
    opts = {"cap_reached": [("raise-bounds", "Edit max_rounds/max_cost_usd/max_wall_seconds in job.json, then re-freeze a successor job carrying the incumbent forward"), ("accept-incumbent", "Finalize with --disposition given to accept the current best artifact"), ("abandon", "Finalize with --disposition declined")],
            "no_observation": [("inspect-evidence", "Read evidence/ and scores.jsonl; the evaluator channel failed its controls or errored"), ("accept-incumbent", "Finalize with --disposition given --ack-unobserved <DIMS> to accept with acknowledged gaps"), ("abandon", "Finalize with --disposition declined")],
            "no_artifact": [("fix-maker", "The Maker produced nothing; check the Maker command and method.md"), ("abandon", "Finalize with --disposition declined")],
            "dormant_channel": [("vary-method", "Two consecutive identical artifacts; revise method.md so the Maker can actually change the artifact"), ("accept-incumbent", "Finalize with --disposition given"), ("abandon", "Finalize with --disposition declined")],
            "dimension_tradeoff": [("owner-ruling", "The revision is better on one dimension and worse on another; pick which artifact ships and finalize accordingly"), ("abandon", "Finalize with --disposition declined")],
            "maker_failed": [("fix-maker", "The Maker command failed; state.json maker_error has the stderr tail"), ("abandon", "Finalize with --disposition declined")],
            "interrupted": [("inspect-state", "The run was interrupted (Ctrl-C or SIGTERM); the log holds every completed event"), ("abandon", "Finalize with --disposition declined")],
            "runtime_error": [("inspect-state", "A runtime defect halted the job; state.json runtime_error has the detail"), ("abandon", "Finalize with --disposition declined")],
            "integrity_error": [("inspect-state", "A gate refused to continue; state.json integrity_error names the check and why"), ("abandon", "Finalize with --disposition declined")],
            "all_conform": [("finalize", "Finalize with --disposition given to bind the Owner disposition")]}
    rounds = state.get("rounds", {})
    tried = [{"round": int(k), "digest": v["digest"], "dimScores": v["dim_scores"]} for k, v in sorted(rounds.items(), key=lambda kv: int(kv[0]))]
    why = {"reason": reason, "detail": state.get("integrity_error") or state.get("maker_error") or state.get("runtime_error")}
    if reason == "dimension_tradeoff" and len(tried) >= 2 and state.get("incumbent_scores"):
        inc_s, last = state["incumbent_scores"], tried[-1]["dimScores"]
        why.update(improved=[d for d in last if last[d] > inc_s[d]], regressed=[d for d in last if last[d] < inc_s[d]])
    if reason == "no_observation":
        why["unobserved"] = sorted({(c["evaluator"], c["dimension"]) for v in rounds.values() for c in v["checks"] if c["outcome"] == "unobserved"})
    rec = {"terminalState": state.get("terminal_state"), "reason": reason, "incumbentRound": state.get("incumbent_round"), "tried": tried, "why": why,
           "options": [{"id": o[0], "action": o[1]} for o in opts.get(reason, [("inspect-state", "Read state.json and evidence/ before deciding")])],
           "recommendation": {"cap_reached": "accept-incumbent if the incumbent meets the bar on inspection, else raise-bounds",
                              "no_observation": "inspect-evidence first; accept-incumbent only with explicit --ack-unobserved",
                              "no_artifact": "fix-maker", "dormant_channel": "vary-method", "dimension_tradeoff": "owner-ruling",
                              "maker_failed": "fix-maker", "interrupted": "inspect-state", "runtime_error": "inspect-state", "integrity_error": "inspect-state", "all_conform": "finalize"}.get(reason, "inspect-state")}
    (job_dir / "escalation.json").write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")


def _refuse_after_disposition(job_dir):
    """P6: a disposition ends the job — no seat work may follow it in the chain."""
    log = pathlib.Path(job_dir) / "job.jsonl"
    if log.is_file() and any(e.get("type") == "disposition" for e in custody_record.read(str(log))):
        raise gates.GateError("job already has a disposition; finalize ends the job and no seat work may follow it (start a successor job)")


def run_job(job_dir):
    """One operation holds the job for its whole length (P6): a finalize waits for
    the run to end, so no paid call or custody append can follow a disposition."""
    with job_lock(job_dir):
        return _run_job_held(job_dir)


def _run_job_held(job_dir):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg, state = _load_job_json(job_dir), json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    _refuse_after_disposition(job_dir)
    if state.get("open_round"):
        # R1: a hard-killed run is recoverable, never silently resumable — the
        # explicit resume re-verifies every pin and re-accrues spend from the
        # verified usage log before continuing at the recorded boundary. This
        # refusal must NOT pass through the integrity handler below: an
        # interrupted run is recoverable, not integrity-broken, and the state
        # must stay untouched so `resume` can proceed.
        raise gates.GateError(f"round {state['open_round']} was interrupted without a named end state; recover explicitly with 'edi.py resume' (R1)")
    try:
        gates.check_pins(cfg, job_dir, state["pins"])
        if state.get("terminal_state"):
            raise gates.GateError(f"job already reached terminal state {state['terminal_state']} ({state.get('reason')}); run executes once")
        # the meter loads the durable usage log at construction — a torn or
        # tampered log must be an integrity refusal HERE, inside the mapped try
        try:
            meter = Meter(job_dir, pricing=cfg.get("pricing", {}), bounds=cfg.get("bounds"))
            meter.recover()  # this operation holds the job: open reservations are a dead run's
            if (job_dir / "usage.jsonl").is_file() or state.get("usage_anchor"):
                meter.verify(anchor=state.get("usage_anchor"))  # P9: the anchor catches truncated newest rows
                if (job_dir / "usage.jsonl").is_file() and not state.get("usage_anchor"):
                    # review #5: a missing anchor never DISABLES the check — the run
                    # may proceed, but the unanchored log is recorded in state and
                    # the very next state write re-anchors it.
                    state["usage_unanchored"] = True
        except ValueError as e:
            raise gates.GateError(f"usage.jsonl failed integrity verification: {e} (spend integrity)")
    except gates.GateError as e:
        if not state.get("terminal_state"):
            # D4.11: a pre-run integrity refusal leaves a named end state and an escalation
            # package. An already-terminal job keeps its recorded end state untouched.
            state.update(terminal_state="needs_owner", reason="integrity_error", integrity_error=str(e)[-300:])
            _write_escalation(job_dir, state, cfg)
            _write_state(job_dir, state)
        raise
    # time.time(), not monotonic: the budget must keep running while the box sleeps.
    # Trade-off accepted and documented: an NTP step can move the budget once, which is
    # the honest failure mode versus silently excluding suspended time.
    max_rounds, max_wall, max_cost, t0 = int(cfg["bounds"]["max_rounds"]), float(cfg["bounds"]["max_wall_seconds"]), float(cfg["bounds"]["max_cost_usd"]), time.time()
    dims, floors = [d["id"] for d in cfg["bar"]["dimensions"]], {d["id"]: float(cfg.get("rejectFloor", (d.get("scale") or {}).get("floor", 0.0))) for d in cfg["bar"]["dimensions"]}
    held_hashes = {gates.sha256_file(p) for p in (job_dir / "held_out").rglob("*") if p.is_file()} if (job_dir / "held_out").is_dir() else set()
    pricing = cfg.get("pricing", {})
    # the spend source of truth: durable, hash-chained usage rows (C1-C7). A
    # pre-existing log is verified before anything accrues onto it.

    prev_term = None
    if hasattr(signal, "SIGTERM") and threading.current_thread() is threading.main_thread():
        def _on_term(signum, frame):
            raise KeyboardInterrupt()
        prev_term = signal.signal(signal.SIGTERM, _on_term)
    try:
      start_r = int(state.get("resume_from") or 1)  # a resumed run continues at the recorded boundary
      for r in range(start_r, max_rounds + 1):
        wall_base = float(state.get("wall_spent_seconds", 0.0))
        rem_s = max_wall - wall_base - (time.time() - t0)
        state["cost_usd"] = round(meter.accrued(), 6)  # derived from the durable usage log
        if rem_s <= 0 or state.get("cost_usd", 0.0) >= max_cost:
            state.update(terminal_state="needs_owner", reason="cap_reached")
            break
        # mark work in flight durably: a hard kill leaves open_round set with no
        # terminal state, which run_job refuses (recovery is explicit `edi.py resume`)
        state["open_round"] = r
        _write_state(job_dir, state, meter)
        r_dir = job_dir / "rounds" / f"round-{r:02d}"
        att = 1
        while (r_dir / "artifact").exists():
            att += 1
            r_dir = job_dir / "rounds" / f"round-{r:02d}-a{att}"
        art_dir = r_dir / "artifact"
        r_dir.mkdir(parents=True, exist_ok=True)
        needles = gates.held_out_needles(job_dir)
        pk_def, w1 = gates.scrub_held_out(list(state["defects"]) + list(state.get("rejected_defects", [])), needles)
        pk_sol, w2 = gates.scrub_held_out(state["solutions"], needles)
        pkt = {"round": r, "mode": cfg.get("mode", "pass_r2"), "best_artifact_digest": state["incumbent_digest"], "defects": pk_def, "workable_solutions": pk_sol, "withheld": w1 + w2}
        # review #12: the P4 exemption covers only text the candidate authored —
        # pass the incumbent artifact so a feedback string is exempt exactly when
        # it quotes the artifact; evaluator-invented text gets the full scan.
        art_text = ""
        if state.get("incumbent_round"):
            inc_w = (state.get("rounds") or {}).get(str(state["incumbent_round"])) or {}
            inc_art_dir = job_dir / inc_w.get("dir", f"rounds/round-{int(state['incumbent_round']):02d}") / "artifact"
            art_text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in sorted(inc_art_dir.rglob("*")) if p.is_file())
        gates.gate_packet(pkt, job_dir, state["incumbent_digest"], artifact_text=art_text)
        (r_dir / "packet.json").write_text(json.dumps(pkt, indent=2) + "\n", encoding="utf-8")
        if state["incumbent_round"]:
            inc_w = (state.get("rounds") or {}).get(str(state["incumbent_round"])) or {}
            shutil.copytree(job_dir / inc_w.get("dir", f"rounds/round-{state['incumbent_round']:02d}") / "artifact", art_dir, dirs_exist_ok=True)
        maker_ran = True
        if state.get("seed") and r == 1 and state["incumbent_round"] is None:
            # Round 1 evaluates the Owner-supplied seed as-is: an unvalidated candidate,
            # never credited to the Maker. If it already meets the bar, no Maker call is paid.
            for f in cfg["subjectFiles"]:
                pinned = state["seed"]["digests"].get(f)
                if pinned and gates.sha256_file(job_dir / cfg["seed"]["dir"] / f) != pinned:
                    raise gates.GateError(f"seed file {f} changed after freeze (pinned {pinned[:12]}...); refusing to evaluate a different draft")
                dst = art_dir / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(job_dir / cfg["seed"]["dir"] / f, dst)
            ev_prod = _append(job_dir, "artifact-produced", cfg["seed"]["author"], [art_dir / f for f in cfg["subjectFiles"]], {"seed": True})
            maker_ran = False
            state["seed_round"] = r
        else:
            try:
                mk = adapters.run_maker(cfg["maker"], r_dir / "packet.json", art_dir, cfg["subjectFiles"], job_dir, timeout=rem_s, meter=meter, round_no=r)
                if isinstance(mk, dict) and mk.get("usage"):
                    mk_id = cfg["maker"].get("model", {}).get("identifier") if isinstance(cfg["maker"].get("model"), dict) else None
                    if "agent_calls" in mk["usage"]:
                        rows = [{"evaluator": "maker", "model": c.get("model") or mk_id, "case": "agent", "usage": c.get("usage") or {}} for c in mk["usage"]["agent_calls"] if not c.get("refused")]
                    else:
                        rows = [{"evaluator": "maker", "model": mk_id, "case": "maker", "usage": mk["usage"]}]
                    _accrue(state, rows, pricing)
                    state["cost_usd"] = round(meter.accrued(), 6)
            except (TimeoutError, subprocess.TimeoutExpired):
                state.update(terminal_state="needs_owner", reason="maker_failed",
                             maker_error="maker wall-clock deadline exceeded (max_wall_seconds=%s)" % (cfg.get("bounds") or {}).get("max_wall_seconds"))
                break
            except CapExceeded as e:
                if isinstance(e, UnboundedOutput):
                    state.update(terminal_state="needs_owner", reason="integrity_error",
                                 integrity_error=str(e)[-300:])
                    raise gates.GateError(str(e))  # unbounded output is an integrity refusal, not a cap trip
                state.update(terminal_state="needs_owner", reason="cap_reached")  # a mid-round cap trip is the same terminal as a boundary trip
                break
            except gates.GateError:
                raise  # integrity/security violations fail loud, never become dispositions
            except Exception as e:  # Maker runtime crash is a halted run, not a traceback
                state.update(terminal_state="needs_owner", reason="maker_failed", maker_error=str(e)[-300:])
                break
        cand_digest = _subject_digest(art_dir, cfg["subjectFiles"], held_hashes)
        if cand_digest is None or cand_digest in (state["last_candidate_digest"], state["incumbent_digest"]):
            state.update(terminal_state="needs_owner", reason="no_artifact" if cand_digest is None else "dormant_channel")
            break
        state["last_candidate_digest"] = cand_digest
        if maker_ran:
            ev_prod = _append(job_dir, "artifact-produced", cfg["maker"]["name"], [art_dir / f for f in cfg["subjectFiles"]], {})
        try:
            checks, ctls, s_rows, exts, d_scores, defects, solutions, usages = _eval_batch(cfg, job_dir, r, art_dir, t0 + max_wall - wall_base, meter=meter)
        except CapExceeded as e:
            if isinstance(e, UnboundedOutput):
                state.update(terminal_state="needs_owner", reason="integrity_error", integrity_error=str(e)[-300:])
                raise gates.GateError(str(e))
            state.update(terminal_state="needs_owner", reason="cap_reached")  # emitted checks stay; the round never becomes incumbent
            break
        state["rounds"][str(r)] = {"dir": r_dir.relative_to(job_dir).as_posix(), "producedAt": ev_prod["at"], "digest": cand_digest, "checks": checks, "controls": ctls, "scores": s_rows, "external": exts, "dim_scores": d_scores}
        _accrue(state, usages, pricing)
        state["cost_usd"] = round(meter.accrued(), 6)  # the meter is authoritative; _accrue's figure is display-only
        if any(c["outcome"] == "unobserved" for c in checks):
            if state["incumbent_round"] is None:
                state.update(incumbent_round=r, incumbent_digest=cand_digest, incumbent_scores=d_scores)
            state.update(terminal_state="needs_owner", reason="no_observation")
            break
        inc = state["incumbent_scores"]
        if inc is None or (any(d_scores[d] > inc[d] for d in dims) and not any(d_scores[d] < inc[d] for d in dims)):
            state.update(incumbent_round=r, incumbent_digest=cand_digest, incumbent_scores=d_scores, defects=defects, solutions=solutions)
        else:
            state["rejected_defects"] = list(state.get("rejected_defects", [])) + list(defects)
        if inc is not None and any(d_scores[d] > inc[d] for d in dims) and any(d_scores[d] < inc[d] for d in dims):
            state.update(terminal_state="needs_owner", reason="dimension_tradeoff")
            break
        if any(state["incumbent_scores"][d] < floors[d] for d in dims):
            state.update(terminal_state="rejected", reason="below_floor")
            break
        win_checks = state["rounds"][str(state["incumbent_round"])]["checks"]
        if all(c["outcome"] == "conforms" for c in win_checks) or r == max_rounds:
            state.update(terminal_state="accepted" if all(c["outcome"] == "conforms" for c in win_checks) else "needs_owner", reason="all_conform" if all(c["outcome"] == "conforms" for c in win_checks) else "cap_reached")
            break
        # wall clock never pauses: bank elapsed time before the next round
        state["wall_spent_seconds"] = round(state.get("wall_spent_seconds", 0.0) + (time.time() - t0), 3)
        state["open_round"] = None  # boundary write: a kill here discards nothing — the round is complete
        t0 = time.time()
        _write_state(job_dir, state, meter)
    except gates.GateError as e:
        state.update(terminal_state="needs_owner", reason="integrity_error", integrity_error=str(e)[-300:])
        raise  # integrity/security violations fail loud (finally still records the halt)
    except KeyboardInterrupt:
        state.update(terminal_state="needs_owner", reason="interrupted")  # Ctrl-C / SIGINT
    except Exception as e:  # any runtime defect still leaves a saved, named end state
        state.update(terminal_state="needs_owner", reason="runtime_error", runtime_error=str(e)[-300:])
    finally:
        if prev_term is not None:
            signal.signal(signal.SIGTERM, prev_term)
        if state.get("terminal_state") is None:  # SIGTERM / hard interruption of any kind
            state.update(terminal_state="needs_owner", reason="interrupted")
        state["open_round"] = None  # a named end state reached in-process needs no resume
        # the meter is authoritative at EVERY exit: a crash or timeout after
        # metered calls still shows their spend (C1/C2)
        state["cost_usd"] = round(meter.accrued(), 6)
        state["wall_spent_seconds"] = float(state.get("wall_spent_seconds", 0.0)) + (time.time() - t0)
        # Verification re-review blocker 5: the finally is a WRITE PATH too —
        # it must never emit state or an escalation after a disposition. The
        # check and the writes are one transaction under the job lock, so a
        # finalize that completes concurrently closes the job before this
        # write can land (the job is the finalize's, not this run's).
        with job_lock(job_dir):
            if _has_disposition(job_dir):
                pass  # the job was closed by a concurrent finalize; write nothing after its disposition
            else:
                if state.get("terminal_state") == "needs_owner":
                    _write_escalation(job_dir, state, cfg)
                _write_state(job_dir, state, meter)
    return {"accepted": 0, "rejected": 1, "needs_owner": 2}[state["terminal_state"]]


def resume_job(job_dir):
    with job_lock(job_dir):
        return _resume_job_held(job_dir)


def _resume_job_held(job_dir):
    """R1: the ONLY recovery path for a hard-killed run. Discards the partial
    round (out of rounds/, which stays immutable, into job/partial/), marks its
    meter rows discarded via an append-only amendment, re-verifies every pin and
    the usage chain, and hands the job back to run_job at the recorded boundary.
    The resumed round reuses the discarded round's number and consumes its
    max_rounds slot (the attempt was made and paid for)."""
    job_dir = pathlib.Path(job_dir).resolve()
    cfg, state = _load_job_json(job_dir), json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    _refuse_after_disposition(job_dir)  # P6: no seat work may follow a disposition
    if state.get("terminal_state"):
        raise gates.GateError(f"job already reached terminal state {state['terminal_state']} ({state.get('reason')}); nothing to resume")
    if not state.get("open_round"):
        raise gates.GateError("no interrupted round to resume (this job was not hard-killed mid-round)")
    r = int(state["open_round"])
    # pins re-verified: the custody chain never continues onto moved ground
    gates.check_pins(cfg, job_dir, state["pins"])
    try:
        meter = Meter(job_dir, pricing=cfg.get("pricing", {}), bounds=cfg.get("bounds"))
        meter.recover()  # this operation holds the job: open reservations are a dead run's
        meter.verify(anchor=state.get("usage_anchor"))  # a damaged or truncated usage log refuses the resume; spend re-accrues from it verbatim
        if (job_dir / "usage.jsonl").is_file() and not state.get("usage_anchor"):
            state["usage_unanchored"] = True  # review #5: proceed, but record and re-anchor
    except ValueError as e:
        # typed, like run_job: a damaged spend log is an integrity refusal, not a crash
        raise gates.GateError(f"usage.jsonl failed integrity verification: {e} (spend integrity)")
    round_rows = [row["digest"] for row in meter._rows if row.get("kind") == "call" and row.get("round") == r and "discarded_round" not in row]
    if round_rows:
        meter.amend(round_rows, note=f"round {r} discarded by resume")
    _append(job_dir, "round-discarded", "edi-run", [],
            {"round": r, "resumed_after_interrupt": True,
             "discarded_spend_usd": round(sum(meter.row_cost(d) for d in round_rows), 6) if round_rows else 0.0})
    state["resume_from"] = r
    state["open_round"] = None
    _write_state(job_dir, state, meter)
    return _run_job_held(job_dir)


def finalize_job(job_dir, state="given", by="Owner", ack_unobserved=None):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg = _load_job_json(job_dir)
    # Verification re-review blocker 4 (final round): the WHOLE finalize is one
    # lifecycle transaction — the state re-read, the custody verify, the pins
    # check and every append hold the job-scoped flock from the first byte, so a
    # competing finalizer cannot rewrite artifact/scores between the read and
    # the attest: it re-reads INSIDE the lock, sees the disposition, and refuses
    # by name. (Previously the state read and custody verify ran BEFORE the
    # lock — a second finalizer could slip a rewrite into that window.)
    if not (job_dir / "state.json").is_file():
        # fast refusal BEFORE the lock: job_lock would otherwise create a
        # lockfile in a dir with no state — a write on a refused finalize
        # (Opus deep review 2026-11-18); the in-lock check below still guards
        # the read race
        raise gates.GateError("no state.json; the job has not been frozen (finalize follows freeze)")
    with job_lock(job_dir):
        try:
            state_bytes = (job_dir / "state.json").read_bytes()
        except FileNotFoundError:
            # a never-frozen (or pre-run) dir has no state: declining or finalizing
            # it would attest custody that does not exist (live-aider-job8, 2026-11-18);
            # is_file() then read() can race a concurrent removal — same refusal
            st = None
        st = json.loads(state_bytes.decode(encoding="utf-8")) if state_bytes is not None else None
        if st is None:
            raise gates.GateError("no state.json; the job has not been frozen (finalize follows freeze)")
        log_path = job_dir / "job.jsonl"
        log_problems = custody_verify(log_path)
        if log_problems:
            raise gates.GateError("custody log chain broken; refusing to finalize: " + "; ".join(map(str, log_problems[:3])))
        log_events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        if not log_events or log_events[0].get("type") != "job-start":
            raise gates.GateError("custody log does not begin with job-start; refusing to finalize a truncated or foreign log")
        if any(e.get("type") == "disposition" for e in log_events):
            raise gates.GateError("job already finalized (disposition event exists); finalize runs once")
        gates.check_pins(cfg, job_dir, st["pins"])
        # P6: finalize binds the Owner's word on a FINISHED outcome — never on a run
        # that is still mid-round or that produced a best artifact without an end
        # state. `declined` stays available: a clean way to abandon an interrupted job.
        # Round-3 external review #10: the end-state check applies to EVERY non-
        # declined finalize, including a frozen-never-run job (which used to slip to
        # the no-incumbent branch and bind a disposition without any end state).
        if state != "declined" and (st.get("open_round") or not st.get("terminal_state")):
            raise gates.GateError("job has no end state (a run was interrupted mid-round, ended without a terminal state, or never ran): "
                                  "resume it to an end state, or finalize --disposition declined to abandon it (P6)")
        try:
            meter = Meter(job_dir, pricing=cfg.get("pricing", {}), bounds=cfg.get("bounds"))
            meter.recover()  # this operation holds the job: open reservations are a dead run's
            if (job_dir / "usage.jsonl").is_file() or st.get("usage_anchor"):
                meter.verify(anchor=st.get("usage_anchor"))  # P9: spend enters the chain only from a verified log
        except ValueError as e:
            raise gates.GateError(f"usage.jsonl failed integrity verification: {e} (spend integrity)")
        gates.gate_seats(cfg, job_dir, approver_by=by)
        # Verification re-review blocker 5: finalize is a LIFECYCLE TRANSACTION —
        # the disposition scan, every custody append (spend included), and the
        # state/escalation writes around them hold the job-scoped flock as ONE
        # transition. A competing round-append therefore either lands before the
        # disposition or is refused after it — it can never interleave.
        ack = ack_unobserved if ack_unobserved is not None else cfg["disposition"].get("acknowledgesUnobserved", [])
        if not st.get("incumbent_round"):
            _append(job_dir, "disposition", by, [], {"state": state, "acknowledgesUnobserved": ack})
            return _emit_gap_record(job_dir, st, cfg)
        win_r, max_r, art_dst, log_path = str(st["incumbent_round"]), str(max(int(k) for k in st["rounds"])), job_dir / "artifact", job_dir / "job.jsonl"
        win = st["rounds"][win_r]
        shutil.rmtree(art_dst, ignore_errors=True)
        shutil.copytree(job_dir / win.get("dir", f"rounds/round-{int(win_r):02d}") / "artifact", art_dst)
        (job_dir / "scores.jsonl").write_text("\n".join(json.dumps(r) for r in win["scores"]) + "\n", encoding="utf-8")
        seed_author = cfg["seed"]["author"] if (cfg.get("seed") and st.get("seed_round") == int(win_r)) else None
        if win_r != max_r:
            ev_prod = _append(job_dir, "artifact-produced", seed_author or cfg["maker"]["name"], [art_dst / f for f in cfg["subjectFiles"]], {"seed": True} if seed_author else {})
            checks = [dict(c, at=_append(job_dir, "check", c["evaluator"], [], c)["at"]) for c in win["checks"]]
            for k in win["controls"]:
                _append(job_dir, "control", k["evaluator"], [], dict(k))  # full payload: the re-logged batch is self-describing
            for ex in win["external"]:
                _append(job_dir, "external", ex["readBy"], [], ex)
            prod_at = ev_prod["at"]
        else:
            checks, prod_at = win["checks"], win["producedAt"]
        if (job_dir / "usage.jsonl").is_file():
            # P9 + round-3 external review #5/#6: the spend attestation happens under
            # the meter's cross-process lock, with the log RE-READ and verified
            # EXACTLY against the anchor first — the verified bytes and the attested
            # bytes are the same bytes. A null anchor never disables the check: an
            # unanchored log is refused (run/resume may proceed and re-anchor; a
            # finalize may not attest).
            if not st.get("usage_anchor"):
                raise gates.GateError("usage.jsonl is present but state.json has no spend anchor (unanchored log); "
                                      "refusing to attest spend that no state write vouched for (P9)")
            try:
                with meter.file_lock():
                    meter.refresh()
                    meter.verify(anchor=st.get("usage_anchor"), exact=True)
                    rows_n, head_d = meter.anchor()[:2]
                    _append(job_dir, "spend", "edi-run", [job_dir / "usage.jsonl"],
                            {"usd": round(meter.accrued(), 6), "discardedUsd": round(meter.discarded(), 6),
                             "rows": rows_n, "head": head_d})
            except ValueError as e:
                raise gates.GateError(f"usage.jsonl failed integrity verification: {e} (spend integrity)")
        ev_disp = _append(job_dir, "disposition", by, [], {"state": state, "acknowledgesUnobserved": ack})
    violations = [{"dimension": d["id"], "description": f"{d['id']} failed conform threshold"} for d in cfg["bar"]["dimensions"] if any(c["dimension"] == d["id"] and c["outcome"] == "does_not_conform" for c in checks)]
    disagreements = [{"dimension": d["id"], "checks": [i for i, c in enumerate(checks) if c["dimension"] == d["id"]], "note": "evaluators disagreed"} for d in cfg["bar"]["dimensions"] if len({c["outcome"] for c in checks if c["dimension"] == d["id"]}) > 1]
    evals_clean = [{k: v for k, v in e.items() if k in ("id", "name", "kind", "model", "script", "sameModelAsMaker")} for e in cfg["evaluators"]]
    def _assemble():
        inputs = {"subject": {"dir": "artifact"}, "specVersion": cfg.get("specVersion", "0.9.8"), "levelClaimed": cfg["levelClaimed"], "bar": dict({k: v for k, v in cfg["bar"].items() if k != "file"}, frozenAt=st["frozenAt"]),
                  "maker": ({"name": seed_author, "kind": "person", "version": cfg["seed"].get("origin", "owner-supplied"), "method": cfg["maker"]["method"], "model": None} if seed_author else {**{k: v for k, v in cfg["maker"].items() if k in ("name", "kind", "version", "method", "model")}, **({"kind": "model"} if cfg["maker"].get("kind") == "agent" else {})}), "startedAt": st["startedAt"], "producedAt": prod_at, "instrument": cfg["instrument"],
                  "evaluators": evals_clean, "checks": checks, "controls": win["controls"], "disagreements": disagreements, "violations": violations, "separationGaps": cfg.get("separationGaps", []),
                  "retention": cfg["retention"], "external": win["external"], "disposition": {"state": state, "by": by, "at": ev_disp["at"], "acknowledgesUnobserved": ack}}
        if cfg["levelClaimed"] in ("CL2", "CL3"):
            inputs["spread"] = [{"dimension": d["id"], "evaluator": ev["id"], "runs": len(outs), "agreement": Counter(outs).most_common(1)[0][1]} for ev in evals_clean for d in cfg["bar"]["dimensions"] if (outs := [c["outcome"] for c in checks if c["evaluator"] == ev["id"] and c["dimension"] == d["id"]])]
        if cfg["levelClaimed"] == "CL3":
            inputs["demonstrations"] = st.get("demonstrations", [])
        if cfg["levelClaimed"] == "baseline":
            inputs["readsBeforeFreeze"] = cfg.get("readsBeforeFreeze", [])
        (job_dir / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n", encoding="utf-8")
        proc = subprocess.run([sys.executable, str(VENDOR / "make_record.py"), str(job_dir / "inputs.json"), str(job_dir / "record.json"), "--log", str(log_path)], cwd=str(job_dir), capture_output=True, text=True)
        return proc.returncode, (proc.stderr.strip() or proc.stdout.strip())[-500:]

    try:
        rc, detail = _assemble()
    except Exception as e:  # noqa: BLE001 -- §6: assembly failure becomes a diagnosable gap record
        rc, detail = -1, str(e)[-500:]
    if rc != 0:
        return _emit_gap_record(job_dir, st, cfg, extra=[{"invariant": "custody-assembly", "missing": detail}])
    return verify_job(job_dir)


def verify_job(job_dir):
    job_dir = pathlib.Path(job_dir).resolve()
    cfg = _load_job_json(job_dir)
    try:
        record_bytes = (job_dir / "record.json").read_bytes()
    except FileNotFoundError:
        # is_file() then read() can race a concurrent removal; a missing record
        # is always the clean refusal, never a traceback (GPT 6.2-sol finding, 2026-11-18)
        raise gates.GateError("no record.json; the job has not been finalized yet (verify follows finalize)")
    if custody_verify(job_dir):
        return 1
    rep = custody_check.verify(json.loads(record_bytes.decode(encoding="utf-8")), files=str(job_dir / "artifact"), evidence=str(job_dir / "evidence"), bar=str(job_dir / cfg["bar"]["file"]))
    return 0 if rep["verdict"] == "OK" else (1 if rep["verdict"] == "FAILED" else 2)
