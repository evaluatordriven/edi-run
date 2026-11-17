"""Deterministic invariant gates (I-01 through I-04) for edi-run. Standard library only."""
from collections import Counter
from datetime import datetime
import hashlib, json, math, os

UNDISCLOSED = "undisclosed"


class GateError(Exception):
    """Raised when a deterministic doctrine or custody invariant fails."""


def sha256_file(path):
    if path.is_symlink() or not path.is_file():
        raise GateError(f"refusing missing or symlink path: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_rel(name, root):
    if not isinstance(name, str) or not name.strip() or os.path.isabs(name) or ".." in name.replace("\\", "/").split("/"):
        raise GateError(f"unsafe relative path: {name!r}")
    p = (root / name).resolve()
    if not p.is_relative_to(root.resolve()):
        raise GateError(f"path escapes root: {name!r}")
    return p


def same(a, b):
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def when(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def gate_bounds(cfg):
    b = cfg.get("bounds")
    if not isinstance(b, dict) or isinstance(b.get("max_rounds"), bool) or not isinstance(b.get("max_rounds"), int) or b["max_rounds"] < 1:
        raise GateError("bounds.max_rounds must be an int >= 1 (I-07)")
    for k in ("max_cost_usd", "max_wall_seconds"):
        v = b.get(k)
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise GateError(f"bounds.{k} must be a finite number > 0 (I-07)")
    if any(not isinstance(sf, str) or os.path.isabs(sf) or ".." in sf.replace("\\", "/").split("/") for sf in cfg.get("subjectFiles") or []):
        raise GateError("invalid subjectFiles entry")


def gate_freeze(cfg, job_dir, started_at):
    bar = cfg.get("bar") or {}
    actual = sha256_file(safe_rel(bar.get("file", "bar.md"), job_dir))
    if actual != (bar.get("digest") or {}).get("sha256"):
        raise GateError("bar.md sha256 mismatch against bar.digest (CL1-14)")
    for role in ("buyer", "specialist"):
        apps = [a for a in (bar.get("approvals") or []) if a.get("role") == role]
        if not apps or any((a.get("barDigest") or {}).get("sha256") != actual for a in apps):
            raise GateError(f"missing or mismatched {role} approval on bar.digest (CL1-3)")
    f_dt, s_dt = when(bar["frozenAt"]), when(started_at)
    if (cfg.get("levelClaimed") == "baseline" and not (f_dt > s_dt and isinstance(cfg.get("readsBeforeFreeze"), list))) or (cfg.get("levelClaimed") != "baseline" and f_dt >= s_dt):
        raise GateError("bar.frozenAt ordering invalid for claimed level (CL1-1/BL-1)")


def gate_instruments(cfg, job_dir):
    inst = cfg.get("instrument") or {}
    pins = {"bar": sha256_file(safe_rel((cfg.get("bar") or {}).get("file", "bar.md"), job_dir)),
            "method": sha256_file(safe_rel(((cfg.get("maker") or {}).get("method") or {}).get("file", ""), job_dir))}
    for k in ("rubric", "runner"):
        f = (inst.get(k) or {}).get("file")
        if not f:
            raise GateError(f"instrument.{k} file missing (CL1-5)")
        pins[k] = sha256_file(safe_rel(f, job_dir))
    return pins


def gate_seats(cfg, job_dir, approver_by=None):
    maker, bar, disp = cfg.get("maker") or {}, cfg.get("bar") or {}, cfg.get("disposition") or {}
    app_by = approver_by if approver_by is not None else disp.get("by", "")
    rf = ((cfg.get("instrument") or {}).get("runner") or {}).get("file")
    r_sha = sha256_file(safe_rel(rf, job_dir)) if rf and (job_dir / rf).is_file() else None
    evals, mk_mod = cfg.get("evaluators") or [], (maker.get("model") if maker.get("kind") in ("model", "agent") else None)
    if not evals:
        raise GateError("at least one evaluator is required")
    for e in evals:
        try:
            runs_n = int(e.get("runs", 2))
        except (TypeError, ValueError):
            raise GateError(f"evaluator {e.get('id')} runs must be an integer >= 2 (D4.5)")
        if runs_n < 2:
            raise GateError(f"evaluator {e.get('id')} runs < 2; a verdict rests on at least two scored runs (D4.5)")
        names = [e.get("name", "")]
        if e.get("kind") == "script":
            sc = e.get("script") or {}
            if not (sc.get("name") and sc.get("author") and (sc.get("digest") or {}).get("sha256")) or (r_sha and sc["digest"]["sha256"] == r_sha):
                raise GateError(f"script evaluator {e.get('id')} invalid or equals runner (CL1-5/CL1-16)")
            names.append(sc["author"])
        elif e.get("kind") == "model":
            em = e.get("model")
            if not isinstance(em, dict) or not (em.get("family") and em.get("identifier") and em.get("endpoint")):
                raise GateError(f"model evaluator {e.get('id')} missing model block (CL1-6)")
            if (cfg.get("levelClaimed") in ("CL2", "CL3") and em.get("readOnly") is not True) or (bool(mk_mod and same(em.get("identifier"), mk_mod.get("identifier"))) != bool(e.get("sameModelAsMaker"))):
                raise GateError(f"evaluator {e.get('id')} readOnly or sameModelAsMaker invalid (CL1-6/CL2-2)")
        if any(same(n, who) for n in names for who in (maker.get("name", ""), bar.get("author", ""))) or (app_by and same(e.get("name", ""), app_by)):
            raise GateError(f"evaluator {e.get('id')} violates seat separation (CL1-6)")
    if maker.get("kind") == "agent":
        stype = (maker.get("sandbox") or {}).get("type")
        if not stype:
            raise GateError("agent Maker requires maker.sandbox {type: sandbox-exec|bwrap}; the runtime must confine the agent (D3.held-out, ruling R3)")
        import adapters as _adapters
        if not _adapters.sandbox_usable(stype):
            raise GateError(f"maker.sandbox type {stype} cannot launch on this host (missing binary or user namespaces forbidden); refusing to run an unconfined agent (ruling R3)")
    if cfg.get("levelClaimed") in ("CL2", "CL3"):
        mk_name = maker.get("name", "")
        if any(same(a.get("by", ""), mk_name) for a in bar.get("approvals") or []) or same(bar.get("author", ""), mk_name):
            raise GateError("CL2/CL3 forbids maker from authoring or approving the bar (CL2-3a)")
        if maker.get("kind") in ("model", "agent"):
            if not isinstance(mk_mod, dict) or not (mk_mod.get("family") and mk_mod.get("endpoint")):
                raise GateError("model maker requires maker.model block (CL2-3b)")
            m_fam, m_ep, m_evs = mk_mod["family"].casefold(), mk_mod["endpoint"], [e for e in evals if e.get("kind") == "model"]
            diff = {e["model"]["family"].casefold() for e in m_evs if e["model"]["family"].casefold() != UNDISCLOSED and not same(e["model"]["endpoint"], m_ep)}
            if m_evs and len(diff - {m_fam}) < (2 if m_fam == UNDISCLOSED else 1):
                raise GateError("CL2 requires model evaluator differing in both family and endpoint (CL2-3b)")
            fams = [e["model"]["family"].casefold() for e in m_evs]
            same_cnt = (max(Counter(f for f in fams if f != UNDISCLOSED).values(), default=0) if m_fam == UNDISCLOSED else sum(1 for f in fams if f == m_fam)) + sum(1 for f in fams if f == UNDISCLOSED)
            if 2 * same_cnt > len(evals):
                raise GateError("CL2 forbids >50% of evaluators sharing maker's model family (CL2-3c)")


def _canon(s):
    return " ".join(s.casefold().split())


WINDOW = 32  # characters, after case/whitespace canonicalization


def held_out_needles(job_dir):
    """D4.9 needle set: full-file digests plus every WINDOW-character window of every held-out
    line (case/whitespace-canonicalized), minus any window that also occurs in bar.md or
    method.md -- wording the Maker is already given is not held-out (D3.held-out). Disclosed
    limits: paraphrase, and held-out lines shorter than WINDOW, are not matched."""
    held_dir, needles = job_dir / "held_out", set()
    pub = _canon("\n".join((job_dir / f).read_text(encoding="utf-8", errors="ignore") for f in ("bar.md", "method.md") if (job_dir / f).is_file()))
    marker = _canon(WITHHELD)
    for p in (p for p in (held_dir.rglob("*") if held_dir.is_dir() else []) if p.is_file()):
        needles.add(sha256_file(p).casefold())
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            c = _canon(ln)
            for i in range(0, len(c) - WINDOW + 1):
                w = c[i:i + WINDOW]
                if w not in pub and w not in marker:
                    needles.add(w)
    return needles


WITHHELD = "[withheld: matches held-out material]"


def _canon_map(s):
    """_canon(s), plus the source index of every canonical character (for span redaction)."""
    out, idx, gap = [], [], False
    for i, ch in enumerate(s):
        if ch.isspace():
            gap = bool(out)
            continue
        if gap:
            out.append(" ")
            idx.append(i - 1)
            gap = False
        for c in ch.casefold():
            out.append(c)
            idx.append(i)
    return "".join(out), idx


def _redact(s, needles, digests):
    """Replace only the spans of s that match held-out material; everything else survives."""
    c, idx = _canon_map(s)
    cover = [False] * len(c)
    for i in range(0, len(c) - WINDOW + 1):
        if c[i:i + WINDOW] in needles:
            cover[i:i + WINDOW] = [True] * WINDOW
    for d in digests:
        at = c.find(d)
        while at != -1:
            cover[at:at + len(d)] = [True] * len(d)
            at = c.find(d, at + 1)
    spans, j = [], 0
    while j < len(c):
        if cover[j]:
            k = j
            while k < len(c) and cover[k]:
                k += 1
            spans.append((idx[j], idx[k - 1] + 1))
            j = k
        else:
            j += 1
    for a, b in reversed(spans):
        s = s[:a] + WITHHELD + s[b:]
    return s, len(spans)


def _scrub_walk(v, needles, digests):
    if isinstance(v, str):
        return _redact(v, needles, digests) if v else (v, 0)
    if isinstance(v, dict):
        out, n = {}, 0
        for k, sv in v.items():
            out[k], w = _scrub_walk(sv, needles, digests)
            n += w
        return out, n
    if isinstance(v, list):
        out, n = [], 0
        for sv in v:
            nv, w = _scrub_walk(sv, needles, digests)
            out.append(nv)
            n += w
        return out, n
    return v, 0


def scrub_held_out(items, needles):
    """Return (items with held-out spans replaced anywhere in the structure, spans withheld) -- D4.9 filters, never forwards."""
    digests = [n for n in needles if len(n) != WINDOW]
    out, n = [], 0
    for it in items:
        it, w = _scrub_walk(it, needles, digests)
        n += w
        out.append(it)
    return out, n


def gate_packet(packet, job_dir, expected_incumbent_digest):
    if packet.get("best_artifact_digest") != expected_incumbent_digest:
        raise GateError("Maker packet best_artifact_digest does not match Pareto-best incumbent (I-04/I-06)")
    low, held_dir = _canon(json.dumps(packet, sort_keys=True)), job_dir / "held_out"
    pub_words = _canon("\n".join((job_dir / f).read_text(encoding="utf-8", errors="ignore") for f in ("bar.md", "method.md") if (job_dir / f).is_file()))
    for k in ("held_out", "scores.jsonl", "job.jsonl"):
        # wording the Maker is already given (bar/method) is not a leak (D3.held-out)
        if k in low and (k not in pub_words or k == "scores.jsonl" or k == "job.jsonl"):
            raise GateError("held-out path marker leaked into Maker packet (I-04)")
    for nd in held_out_needles(job_dir):
        if nd in low:
            raise GateError("held-out content or digest leaked into Maker packet (I-04)")
