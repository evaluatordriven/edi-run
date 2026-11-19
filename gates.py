"""Deterministic invariant gates (I-01 through I-04) for edi-run. Standard library only."""
from collections import Counter
from datetime import datetime, timezone
import hashlib, json, math, os, pathlib, re, unicodedata

UNDISCLOSED = "undisclosed"
MAX_PINNED_SEAT_FILES = 512


class GateError(Exception):
    """Raised when a deterministic doctrine or custody invariant fails."""


def strict_json_loads(text):
    # G:parse-boundary refuse duplicate JSON properties so canonicalization is injective
    def _pairs(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError(f"duplicate JSON property {k!r}")
            out[k] = v
        return out
    return json.loads(text, object_pairs_hook=_pairs)


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
    mode = cfg.get("mode") or "scored"
    if mode not in ("scored", "readings"):
        raise GateError(f"unknown job.json mode {mode!r}; supported: 'scored' or 'readings' (C29)")
    b = cfg.get("bounds")
    if not isinstance(b, dict):
        raise GateError("bounds must be a dict (I-07)")
    mr = b.get("max_rounds")
    if mode == "readings":
        if mr is not None and (isinstance(mr, bool) or not isinstance(mr, int) or mr != 1):
            raise GateError("readings mode requires bounds.max_rounds to be 1 or absent (C29)")
    elif isinstance(mr, bool) or not isinstance(mr, int) or mr < 1:
        raise GateError("bounds.max_rounds must be an int >= 1 (I-07)")
    for k in ("max_cost_usd", "max_wall_seconds"):
        v = b.get(k)
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise GateError(f"bounds.{k} must be a finite number > 0 (I-07)")
    if (mo := b.get("max_output_tokens")) is not None:
        ok = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
        if (isinstance(mo, dict) and not all(ok(v) for v in mo.values())) or (not isinstance(mo, dict) and not ok(mo)):
            raise GateError("bounds.max_output_tokens must be a finite number > 0 (or a per-model map of them) (I-07)")
    if any(not isinstance(sf, str) or os.path.isabs(sf) or ".." in sf.replace("\\", "/").split("/") for sf in cfg.get("subjectFiles") or []):
        raise GateError("invalid subjectFiles entry")


def _auth_key(raw):
    return _canon(raw.replace("-", " "))


def parse_authorship(job_dir):
    # G:CL1-6 AUTHORSHIP.md parser (Cf stripped, continuation folded, first-wins)
    p, out, last_key = job_dir / "AUTHORSHIP.md", {}, None
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line[:1] in (" ", "\t"):
            if last_key is not None:
                out[last_key] = _canon(out[last_key] + " " + line)
        elif ":" in line:
            k, v = line.split(":", 1)
            if (key := _auth_key(k)):
                out.setdefault(key, _canon(v))
                last_key = key
    return out


def gate_authorship(cfg, job_dir):
    # G:CL1-6 operator cannot author bar, rubric, method, seed, or maker
    a = parse_authorship(job_dir)
    if not (op := a.get("operator")):
        return
    bar, maker = cfg.get("bar") or {}, cfg.get("maker") or {}
    for label, v in (("bar.author", bar.get("author")), ("maker.name", maker.get("name"))):
        if v and _canon(v) == op:
            raise GateError(f"job.json {label} ({v}) equals the declared Operator; the Operator seat cannot author an instrument it operates (CL1-6)")
    for key in ("bar author", "rubric author", "method author", "seed author"):
        if (auth := a.get(key)) and auth == op:
            raise GateError(f"AUTHORSHIP.md declares Operator equal to the {key} ({auth}); the Operator seat cannot author an instrument it operates (CL1-6)")


def gate_freeze_pre(cfg, job_dir):
    # G:I-01/CL1-1/CL1-3/CL1-14/C29 pre-write bar and approval validation
    bar, mode = cfg.get("bar") or {}, cfg.get("mode") or "scored"
    if not ("publishedAt" in bar and isinstance(bar.get("groundTruth"), list) and isinstance(bar.get("dissent"), list)):
        raise GateError("bar must carry publishedAt (null allowed), groundTruth [] and dissent [] (custody CR-2)")
    actual = sha256_file(safe_rel(bar.get("file", "bar.md"), job_dir))
    if actual != (bar.get("digest") or {}).get("sha256"):
        raise GateError("bar.md sha256 mismatch against bar.digest (CL1-14)")
    if "barDigest" in cfg:
        pinned = cfg["barDigest"]
        pinned_sha = pinned.get("sha256") if isinstance(pinned, dict) else pinned
        sha = str(pinned_sha).strip() if isinstance(pinned_sha, str) else None
        if sha is None or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            raise GateError(f"externally pinned barDigest {pinned_sha!r} is present but not a parseable 64-hex sha256; refusing to freeze against an unreadable pin (fail-closed)")
        if sha.casefold() != actual:
            raise GateError(f"computed bar.md digest {actual} does not match the externally pinned barDigest {sha}; refusing to freeze against a different bar (fail-closed)")
    gate_authorship(cfg, job_dir)
    if mode == "readings":
        buyers = [a for a in (bar.get("approvals") or []) if a.get("role") == "buyer"]
        if not buyers or any((a.get("barDigest") or {}).get("sha256") != actual for a in buyers):
            raise GateError("missing or mismatched buyer approval on bar.digest (CL1-3)")
        if any(a.get("role") == "specialist" for a in (bar.get("approvals") or [])):
            raise GateError("readings mode forbids specialist approval on bar (C29)")
        for d in bar.get("dimensions") or []:
            if any(d.get(k) is not None for k in ("scale", "threshold", "floor")):
                raise GateError(f"readings mode dimension {d.get('id')} cannot carry scale, threshold, or floor (C29)")
    else:
        for role in ("buyer", "specialist"):
            apps = [a for a in (bar.get("approvals") or []) if a.get("role") == role]
            if not apps or any((a.get("barDigest") or {}).get("sha256") != actual for a in apps):
                raise GateError(f"missing or mismatched {role} approval on bar.digest (CL1-3)")
    if cfg.get("levelClaimed") == "baseline" and not isinstance(cfg.get("readsBeforeFreeze"), list):
        raise GateError("baseline jobs must record readsBeforeFreeze (BL-1)")
    if cfg.get("levelClaimed") != "baseline":
        if not (f_raw := (cfg.get("bar") or {}).get("frozenAt")):
            raise GateError("CL1+ jobs must carry bar.frozenAt (CL1-1)")
        try:
            f_dt = when(f_raw)
        except Exception as e:
            raise GateError(f"bar.frozenAt is not a parseable timestamp: {f_raw!r}") from e
        if f_dt >= datetime.now(timezone.utc):
            raise GateError(f"bar.frozenAt {f_raw} is not in the past; a bar frozen after work began is not a frozen bar (CL1-1)")


def gate_freeze(cfg, job_dir, started_at):
    bar = cfg.get("bar") or {}
    gate_freeze_pre(cfg, job_dir)
    f_dt, s_dt = when(bar["frozenAt"]), when(started_at)
    if (cfg.get("levelClaimed") == "baseline" and not (f_dt > s_dt and isinstance(cfg.get("readsBeforeFreeze"), list))) or (cfg.get("levelClaimed") != "baseline" and f_dt >= s_dt):
        raise GateError("bar.frozenAt ordering invalid for claimed level (CL1-1/BL-1)")


def gate_instruments(cfg, job_dir):
    # G:I-01/I-02/P1 pin bar, method, rubric, runner, job.json settings, and frozen tree
    inst = cfg.get("instrument") or {}
    pins = {"bar": sha256_file(safe_rel((cfg.get("bar") or {}).get("file", "bar.md"), job_dir)),
            "method": sha256_file(safe_rel(((cfg.get("maker") or {}).get("method") or {}).get("file", ""), job_dir))}
    for k in ("rubric", "runner"):
        if not (f := (inst.get(k) or {}).get("file")):
            raise GateError(f"instrument.{k} file missing (CL1-5)")
        pins[k] = sha256_file(safe_rel(f, job_dir))
    j = {k: v for k, v in (cfg or {}).items() if k != "disposition"}
    pins["job"] = hashlib.sha256(json.dumps(j, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    pins["tree"] = tree_manifest(job_dir)
    return pins


RUNTIME_WRITTEN = {"rounds", "partial", "artifact", "evidence", "job.json", "job.jsonl", "job.lock", "state.json",
                   "state.json.tmp", "job.json.tmp-freeze", "usage.jsonl", "usage.resv.jsonl", "usage.lock",
                   "scores.jsonl", "inputs.json", "record.json", "escalation.json"}
NEVER_AUTHORED = {"__pycache__", ".DS_Store"}


def tree_manifest(job_dir):
    # G:I-01/I-02/I-04/G1 {relative path: sha256} of every frozen file under job_dir
    job_dir, out = pathlib.Path(job_dir).resolve(), {}
    for root, dirs, files in os.walk(job_dir, followlinks=False):
        here = pathlib.Path(root)
        skip = RUNTIME_WRITTEN if here == job_dir else set()
        dirs[:] = sorted(d for d in dirs if d not in skip and d not in NEVER_AUTHORED)
        for name in dirs + sorted(files):
            if name in skip or name in NEVER_AUTHORED:
                continue
            f, rel = here / name, (here / name).relative_to(job_dir).as_posix()
            if f.is_symlink():
                raise GateError(f"symlinked {rel} in the frozen job tree; pinned material must be plain files (G1)")
            if f.is_file():
                out[rel] = sha256_file(f)
                if len(out) > MAX_PINNED_SEAT_FILES:
                    raise GateError(f"frozen job tree exceeds the {MAX_PINNED_SEAT_FILES}-file cap (G1)")
    return out


def _chain_job_pins(job_dir):
    # G:P7 pins anchored in custody chain at freeze
    log = pathlib.Path(job_dir) / "job.jsonl"
    if not log.is_file():
        raise GateError("no custody log; the job pin is not anchored in the chain (P7)")
    last = None
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                e = json.loads(line)
            except ValueError as er:
                raise GateError(f"custody log is not valid JSON: {er}") from er
            if e.get("type") == "job-pin":
                last = (e.get("data") or {}).get("pins")
    if last is None:
        raise GateError("no job-pin event in the custody chain; the job pin is not anchored (P7)")
    return last


def check_pins(cfg, job_dir, frozen):
    # G:P1/P7 three-way check: current cfg/tree vs state.json pins vs custody chain job-pin
    cur, frozen = gate_instruments(cfg, job_dir), frozen or {}
    if (drift := sorted(k for k in set(cur) | set(frozen) if cur.get(k) != frozen.get(k))):
        what = ", ".join("job.json settings" if k == "job" else _tree_drift(cur.get(k), frozen.get(k)) if k == "tree" else k for k in drift)
        raise GateError(f"frozen instruments modified after freeze: {what} (I-01/I-02)")
    chain = _chain_job_pins(job_dir) or {}
    if (drift := sorted(k for k in set(cur) | set(chain) if cur.get(k) != chain.get(k))):
        what = ", ".join("job.json settings" if k == "job" else k for k in drift)
        raise GateError(f"state.json pins do not match the freeze-time job-pin event in the custody chain: {what} (P7)")


def _tree_drift(cur, frozen):
    cur, frozen = cur or {}, frozen or {}
    moved = sorted(f for f in set(cur) | set(frozen) if cur.get(f) != frozen.get(f))
    return "files " + ", ".join(moved[:5]) + (f" (+{len(moved) - 5} more)" if len(moved) > 5 else "")


def gate_commands(cfg, job_dir):
    # G:G1 seat command operands resolving to existing files must reside inside job_dir and outside excluded paths
    m, jd = cfg.get("maker") or {}, job_dir.resolve()
    cmds = ([("maker", m["command"])] if m.get("command") else []) + \
           [(e.get("id", "evaluator"), e["command"]) for e in (cfg.get("evaluators") or []) if e.get("command")]
    for who, cmd in cmds:
        if not isinstance(cmd, list):
            raise GateError(f"{who} command must be a list of argv tokens")
        for tok in cmd[1:]:
            if not isinstance(tok, str):
                raise GateError(f"{who} command tokens must be strings")
            cand = pathlib.Path(tok) if pathlib.Path(tok).is_absolute() else (job_dir / tok)
            try: exists = cand.exists()
            except OSError: exists = False
            if exists:
                try: rp = cand.resolve()
                except OSError as e: raise GateError(f"{who} command token {tok!r} cannot be resolved ({e})")
                if not rp.is_relative_to(jd):
                    raise GateError(f"{who} command references {tok} outside the job dir; seat code must live in the job (G1)")
                parts = rp.relative_to(jd).parts
                if parts and (parts[0] in RUNTIME_WRITTEN or any(p in NEVER_AUTHORED for p in parts)):
                    raise GateError(f"{who} command references unpinned runtime/cache path {tok} (G1)")


def gate_seats(cfg, job_dir, approver_by=None):
    # G:CL1-5/CL1-6/CL2-2/CL2-3/D4.5 seat separation, run counts, and sandbox requirements
    maker, bar, disp = cfg.get("maker") or {}, cfg.get("bar") or {}, cfg.get("disposition") or {}
    app_by = approver_by if approver_by is not None else disp.get("by", "")
    rf = ((cfg.get("instrument") or {}).get("runner") or {}).get("file")
    r_sha = sha256_file(safe_rel(rf, job_dir)) if rf and (job_dir / rf).is_file() else None
    evals, mk_mod = cfg.get("evaluators") or [], (maker.get("model") if maker.get("kind") in ("model", "agent") else None)
    if not evals:
        raise GateError("at least one evaluator is required")
    for e in evals:
        try: runs_n = int(e.get("runs", 2))
        except (TypeError, ValueError): raise GateError(f"evaluator {e.get('id')} runs must be an integer >= 2 (D4.5)")
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
        if not (stype := (maker.get("sandbox") or {}).get("type")):
            raise GateError("agent Maker requires maker.sandbox {type: sandbox-exec|bwrap}; the runtime must confine the agent (D3.held-out, ruling R3)")
        import adapters as _adapters
        if not _adapters.sandbox_usable(stype):
            raise GateError(f"maker.sandbox type {stype} cannot launch on this host (missing binary or user namespaces forbidden); refusing to run an unconfined agent (ruling R3)")
        try: _adapters._require_https(maker.get("url", ""))
        except ValueError as e: raise GateError(f"agent maker upstream refused: {e}")
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
    return " ".join("".join(ch for ch in str(s) if unicodedata.category(ch) != "Cf").casefold().split())


WINDOW = 32
MIN_LINE = 16
WITHHELD = "[withheld: matches held-out material]"


def held_out_needles(job_dir):
    # G:I-04/D4.9 extract 32-char sliding windows, file sha256s, and 16..31-char lines from held_out/
    held_dir, windows, digests, lines = job_dir / "held_out", set(), set(), set()
    pub = _canon("\n".join((job_dir / f).read_text(encoding="utf-8", errors="ignore") for f in ("bar.md", "method.md") if (job_dir / f).is_file()))
    marker = _canon(WITHHELD)
    for p in (p for p in (held_dir.rglob("*") if held_dir.is_dir() else []) if p.is_file()):
        digests.add(sha256_file(p).casefold())
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not (c := _canon(ln)) or c in pub or c in marker:
                continue
            if len(c) < WINDOW:
                if len(c) >= MIN_LINE:
                    lines.add(c)
            else:
                windows.update(w for i in range(len(c) - WINDOW + 1) if (w := c[i:i + WINDOW]) not in pub and w not in marker)
    return windows, digests, lines


def _canon_map(s):
    out, idx, gap = [], [], False
    for i, ch in enumerate(s):
        if ch.isspace():
            gap = bool(out); continue
        if gap:
            out.append(" "); idx.append(i - 1); gap = False
        for c in ch.casefold():
            out.append(c); idx.append(i)
    return "".join(out), idx


def _line_bounded(c, at, ln):
    end = at + len(ln)
    return (at == 0 or not c[at - 1].isalnum()) and (end == len(c) or not c[end].isalnum())


def _redact(s, needles, digests, lines):
    c, idx = _canon_map(s)
    cover = [False] * len(c)
    for i in range(0, len(c) - WINDOW + 1):
        if c[i:i + WINDOW] in needles:
            cover[i:i + WINDOW] = [True] * WINDOW
    for d in digests:
        at = c.find(d)
        while at != -1:
            cover[at:at + len(d)] = [True] * len(d); at = c.find(d, at + 1)
    for ln in lines:
        at = c.find(ln)
        while at != -1:
            if _line_bounded(c, at, ln): cover[at:at + len(ln)] = [True] * len(ln)
            at = c.find(ln, at + 1)
    spans, j = [], 0
    while j < len(c):
        if cover[j]:
            k = j
            while k < len(c) and cover[k]: k += 1
            spans.append((idx[j], idx[k - 1] + 1)); j = k
        else: j += 1
    for a, b in reversed(spans):
        s = s[:a] + WITHHELD + s[b:]
    return s, len(spans)


def _scrub_walk(v, needles, digests, lines):
    if isinstance(v, str):
        return _redact(v, needles, digests, lines) if v else (v, 0)
    if isinstance(v, dict):
        out, n = {}, 0
        for k, sv in v.items():
            out[k], w = _scrub_walk(sv, needles, digests, lines); n += w
        return out, n
    if isinstance(v, list):
        out, n = [], 0
        for sv in v:
            nv, w = _scrub_walk(sv, needles, digests, lines); out.append(nv); n += w
        return out, n
    return v, 0


def scrub_held_out(items, needle_set):
    return _scrub_walk(list(items), *needle_set)


def gate_packet(packet, job_dir, expected_incumbent_digest, artifact_text=""):
    # G:I-04/I-06/P4 verify incumbent digest and refuse leaked held-out paths, needles, or digests
    if packet.get("best_artifact_digest") != expected_incumbent_digest:
        raise GateError("Maker packet best_artifact_digest does not match Pareto-best incumbent (I-04/I-06)")
    low = _canon(json.dumps(packet, sort_keys=True))
    pub_words = _canon("\n".join((job_dir / f).read_text(encoding="utf-8", errors="ignore") for f in ("bar.md", "method.md") if (job_dir / f).is_file()))
    c_art = _canon(artifact_text or "")

    def _strings(v, feedback=False):
        if isinstance(v, str): yield v, feedback
        elif isinstance(v, dict):
            for k, sv in v.items():
                yield str(k), False; yield from _strings(sv, feedback or k in ("defects", "workable_solutions"))
        elif isinstance(v, list):
            for sv in v: yield from _strings(sv, feedback)

    for k in ("held_out", "scores.jsonl", "job.jsonl"):
        for s, fb in _strings(packet):
            c = _canon(s)
            if not (fb and c and c in c_art) and k in c and (k not in pub_words or k in ("scores.jsonl", "job.jsonl")):
                raise GateError("held-out path marker leaked into Maker packet (I-04)")
    needles, digests, lines = held_out_needles(job_dir)
    if any(nd in low for nd in needles | digests):
        raise GateError("held-out content or digest leaked into Maker packet (I-04)")
    for ln in lines:
        at = low.find(ln)
        while at != -1:
            if _line_bounded(low, at, ln):
                raise GateError("held-out content or digest leaked into Maker packet (I-04)")
            at = low.find(ln, at + 1)
