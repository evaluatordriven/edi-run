"""Deterministic invariant gates (I-01 through I-04) for edi-run. Standard library only."""
from collections import Counter
from datetime import datetime
import hashlib
import json

UNDISCLOSED = "undisclosed"


class GateError(Exception):
    """Raised when a deterministic doctrine or custody invariant fails."""


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def same(a, b):
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def when(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def gate_bounds(cfg):
    b = cfg.get("bounds")
    if not isinstance(b, dict):
        raise GateError("missing bounds object (I-07)")
    for k in ("max_rounds", "max_cost_usd", "max_wall_seconds"):
        v = b.get(k)
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise GateError(f"bounds.{k} must be explicitly supplied and > 0 (I-07)")


def gate_freeze(cfg, job_dir, started_at):
    bar = cfg.get("bar") or {}
    bar_file = job_dir / bar.get("file", "bar.md")
    if not bar_file.is_file():
        raise GateError(f"bar file missing: {bar_file} (I-01)")
    actual = sha256_file(bar_file)
    if actual != (bar.get("digest") or {}).get("sha256"):
        raise GateError("bar.md sha256 mismatch against bar.digest (CL1-14)")
    for role in ("buyer", "specialist"):
        apps = [a for a in (bar.get("approvals") or []) if a.get("role") == role]
        if not apps or any((a.get("barDigest") or {}).get("sha256") != actual for a in apps):
            raise GateError(f"missing or mismatched {role} approval on bar.digest (CL1-3)")
    f_dt, s_dt = when(bar["frozenAt"]), when(started_at)
    if cfg.get("levelClaimed") == "baseline":
        if not (f_dt > s_dt and isinstance(cfg.get("readsBeforeFreeze"), list)):
            raise GateError("baseline requires bar.frozenAt > startedAt and readsBeforeFreeze[] (BL-1)")
    elif f_dt >= s_dt:
        raise GateError("bar.frozenAt must be strictly earlier than startedAt (CL1-1)")


def gate_instruments(cfg, job_dir):
    inst = cfg.get("instrument") or {}
    for k in ("rubric", "runner"):
        f = (inst.get(k) or {}).get("file")
        if not f or not (job_dir / f).is_file():
            raise GateError(f"instrument.{k} file missing (CL1-5)")
    if cfg.get("levelClaimed") == "CL3":
        demos = cfg.get("demonstrations") or []
        for ev in cfg.get("evaluators") or []:
            for dim in (cfg.get("bar") or {}).get("dimensions") or []:
                if not any(d.get("evaluator") == ev["id"] and d.get("dimension") == dim["id"]
                           and d.get("outcome") == "does_not_conform" for d in demos):
                    raise GateError(f"CL3 missing planted failure demonstration for {ev['id']}/{dim['id']} (CL3-1)")


def gate_seats(cfg, job_dir):
    maker, bar, disp = cfg.get("maker") or {}, cfg.get("bar") or {}, cfg.get("disposition") or {}
    rf = ((cfg.get("instrument") or {}).get("runner") or {}).get("file")
    r_sha = sha256_file(job_dir / rf) if rf and (job_dir / rf).is_file() else None
    evals = cfg.get("evaluators") or []
    if not evals:
        raise GateError("at least one evaluator is required")
    for e in evals:
        names = [e.get("name", "")]
        if e.get("kind") == "script":
            sc = e.get("script") or {}
            if not (sc.get("name") and sc.get("author") and (sc.get("digest") or {}).get("sha256")):
                raise GateError(f"script evaluator {e.get('id')} missing script metadata (CL1-5)")
            if r_sha and sc["digest"]["sha256"] == r_sha:
                raise GateError(f"script evaluator {e.get('id')} cannot be instrument.runner (CL1-16)")
            names.append(sc["author"])
        if any(same(n, who) for n in names for who in (maker.get("name", ""), bar.get("author", ""))):
            raise GateError(f"evaluator {e.get('id')} is or was authored by maker or bar author (CL1-6)")
        if same(e.get("name", ""), disp.get("by", "")):
            raise GateError(f"approver cannot be evaluator {e.get('id')} (CL1-6)")
    if cfg.get("levelClaimed") in ("CL2", "CL3"):
        mk_name = maker.get("name", "")
        if any(same(a.get("by", ""), mk_name) for a in bar.get("approvals") or []) or same(bar.get("author", ""), mk_name):
            raise GateError("CL2/CL3 forbids maker from authoring or approving the bar (CL2-3a)")
        if maker.get("kind") == "model":
            m_mod = maker.get("model")
            if not m_mod:
                raise GateError("model maker requires maker.model block (CL2-3b)")
            m_fam, m_ep = m_mod["family"].casefold(), m_mod["endpoint"]
            m_evs = [e for e in evals if e.get("kind") == "model"]
            if any(not (e.get("model") and e["model"].get("readOnly") is True) for e in m_evs):
                raise GateError("CL2 model evaluators require model block with readOnly=True (CL2-2)")
            diff = {e["model"]["family"].casefold() for e in m_evs
                    if e["model"]["family"].casefold() != UNDISCLOSED and not same(e["model"]["endpoint"], m_ep)}
            if m_evs and len(diff - {m_fam}) < (2 if m_fam == UNDISCLOSED else 1):
                raise GateError("CL2 requires model evaluator differing in both family and endpoint (CL2-3b)")
            fams = [e["model"]["family"].casefold() for e in m_evs]
            undisc = sum(1 for f in fams if f == UNDISCLOSED)
            same_cnt = (max(Counter(f for f in fams if f != UNDISCLOSED).values(), default=0) + undisc
                        if m_fam == UNDISCLOSED else sum(1 for f in fams if f == m_fam) + undisc)
            if 2 * same_cnt > len(evals):
                raise GateError("CL2 forbids >50% of evaluators sharing maker's model family (CL2-3c)")


def gate_packet(packet, job_dir, expected_incumbent_digest):
    if packet.get("best_artifact_digest") != expected_incumbent_digest:
        raise GateError("Maker packet best_artifact_digest does not match Pareto-best incumbent (I-04/I-06)")
    raw, held_dir = json.dumps(packet, sort_keys=True), job_dir / "held_out"
    if any(k in raw for k in ("held_out", "scores.jsonl", "job.jsonl")):
        raise GateError("held-out path marker leaked into Maker packet (I-04)")
    if held_dir.is_dir() and any(sha256_file(p) in raw for p in held_dir.rglob("*") if p.is_file()):
        raise GateError("held-out digest leaked into Maker packet (I-04)")
