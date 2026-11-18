"""Deterministic invariant gates (I-01 through I-04) for edi-run. Standard library only."""
from collections import Counter
from datetime import datetime, timezone
import hashlib, json, math, os, pathlib, re, unicodedata

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


def _auth_key(raw):
    """Canonicalize an AUTHORSHIP.md KEY the same way values are canonicalized:
    split/casefold via _canon, plus hyphen variants folded to spaces so
    'Bar-author' and 'Bar  author' both match the canonical 'bar author'."""
    return _canon(raw.replace("-", " "))


def parse_authorship(job_dir):
    """Lenient AUTHORSHIP.md parse: `Key: value` lines, canonicalized keys and
    values (case/whitespace-insensitive, Cf format characters stripped,
    hyphen/underscore variants folded). Blank value = unattributed. Missing
    file = everything unattributed. Continuation lines (indented) are FOLDED
    into the previous key's value -- 'Bar author:\\n  Same Person' is one
    declaration, not a silently ignored line. Duplicate keys: FIRST wins -- a
    later duplicate (or blank rebind) cannot launder an earlier declaration."""
    p = job_dir / "AUTHORSHIP.md"
    out = {}
    if not p.is_file():
        return out
    last_key = None
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line[:1] in (" ", "\t"):  # continuation of the previous declaration
            if last_key is not None:
                out[last_key] = _canon(out[last_key] + " " + line)
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = _auth_key(k)
        if not key:
            continue
        if key not in out:  # first-wins: later lines may not rebind a seat
            out[key] = _canon(v)
        last_key = key
    return out


def gate_authorship(cfg, job_dir):
    """Seat separation (CL1-6) extended to agents: the declared Operator must not
    be a declared author of the bar, rubric, method or seed, and must not be the
    job.json bar.author or maker.name. Blank = unattributed = allowed; a missing
    AUTHORSHIP.md leaves everything unattributed."""
    a = parse_authorship(job_dir)
    op = a.get("operator")
    if not op:
        return
    bar = cfg.get("bar") or {}
    maker = cfg.get("maker") or {}
    # job.json identities are declarations too: a self-dealing Operator who only
    # signs AUTHORSHIP.md with a stranger's name while naming itself in job.json
    # used to slip past the gate entirely (frontier finding f).
    for label, v in (("bar.author", bar.get("author")), ("maker.name", maker.get("name"))):
        if v and _canon(v) == op:
            raise GateError(
                f"job.json {label} ({v}) equals the declared Operator; "
                "the Operator seat cannot author an instrument it operates (CL1-6)")
    for key in ("bar author", "rubric author", "method author", "seed author"):
        auth = a.get(key)
        if auth and auth == op:
            raise GateError(
                f"AUTHORSHIP.md declares Operator equal to the {key} ({auth}); "
                "the Operator seat cannot author an instrument it operates (CL1-6)")


def gate_freeze_pre(cfg, job_dir):
    """Bar-level refusal checks that must run BEFORE the first log write: a
    refused freeze leaves nothing behind (I-01 no-trace)."""
    bar = cfg.get("bar") or {}
    actual = sha256_file(safe_rel(bar.get("file", "bar.md"), job_dir))
    # auto-hash: bar.md's sha256 is computed by the tool that froze it, never
    # retyped by hand (CL1-14 semantics kept: a DECLARED digest must match).
    declared = (bar.get("digest") or {}).get("sha256")
    if declared is None:
        bar["digest"] = {"sha256": actual}
    elif actual != declared:
        raise GateError("bar.md sha256 mismatch against bar.digest (CL1-14)")
    # externally pinned pre-commitment (config `barDigest`): fail closed on any
    # mismatch between the computed digest and the pinned one, AND on any
    # present-but-unparseable pin -- a falsy pin ('', {}, null) used to be
    # silently ignored, which is fail-open (frontier finding 5). The pin must be
    # a 64-hex sha256 (case-insensitive, whitespace-trimmed) or the freeze refuses.
    if "barDigest" in cfg:
        pinned = cfg["barDigest"]
        pinned_sha = pinned.get("sha256") if isinstance(pinned, dict) else pinned
        sha = str(pinned_sha).strip() if isinstance(pinned_sha, str) else None
        if sha is None or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            raise GateError(
                f"externally pinned barDigest {pinned_sha!r} is present but not a "
                "parseable 64-hex sha256; refusing to freeze against an unreadable pin (fail-closed)")
        if sha.casefold() != actual:
            raise GateError(
                f"computed bar.md digest {actual} does not match the externally pinned "
                f"barDigest {sha}; refusing to freeze against a different bar (fail-closed)")
    gate_authorship(cfg, job_dir)
    for role in ("buyer", "specialist"):
        apps = [a for a in (bar.get("approvals") or []) if a.get("role") == role]
        if not apps or any((a.get("barDigest") or {}).get("sha256") != actual for a in apps):
            raise GateError(f"missing or mismatched {role} approval on bar.digest (CL1-3)")
    if cfg.get("levelClaimed") == "baseline" and not isinstance(cfg.get("readsBeforeFreeze"), list):
        raise GateError("baseline jobs must record readsBeforeFreeze (BL-1)")
    # frozenAt must be parseable and, for CL1+, strictly in the past: both are
    # refusals, so both run before the first log write (the post-append ordering
    # predicate is then construction-guaranteed: the work-started event is
    # emitted after freeze, so a past frozenAt can never satisfy f_dt >= s_dt)
    f_raw = (cfg.get("bar") or {}).get("frozenAt")
    if cfg.get("levelClaimed") != "baseline":
        if not f_raw:
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
    actual = sha256_file(safe_rel(bar.get("file", "bar.md"), job_dir))
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
    # the code the seats execute is an instrument too: pin the scripts tree at freeze
    # so an edit after freeze is refused at run start, like an edited seed (recheck G1)
    sdir = job_dir / "scripts"
    if sdir.is_dir():
        h = hashlib.sha256()
        import os as _os
        for root, dirs, files in _os.walk(sdir, followlinks=False):
            for d in dirs:
                if (pathlib.Path(root) / d).is_symlink():
                    raise GateError(f"symlinked directory {pathlib.Path(root, d).relative_to(job_dir)} under scripts/; pinned seat code must be plain files (G1)")
            for fn in files:
                f = pathlib.Path(root) / fn
                if f.is_symlink():
                    raise GateError(f"symlinked file {f.relative_to(job_dir)} under scripts/ (G1)")
                h.update(str(f.relative_to(job_dir)).encode())
                h.update(sha256_file(f).encode())
        # a seat script can import a helper from anywhere under the job dir, not
        # just scripts/ -- pin every python file in the job so the import graph
        # cannot escape the pin (frontier finding: job-root helper edited after
        # freeze landed tampered output with the scripts pin untouched)
        for f in sorted(job_dir.rglob("*.py")):
            if f.is_symlink():
                raise GateError(f"symlinked python file {f.relative_to(job_dir)} (G1)")
            if sdir in f.parents:
                continue
            h.update(str(f.relative_to(job_dir)).encode())
            h.update(sha256_file(f).encode())
        pins["scripts"] = h.hexdigest()
    return pins


def gate_commands(cfg, job_dir):
    """Seat commands must not point outside the job dir: pinned seat code lives
    in the job (frontier finding: an absolute command path executed a file
    tampered after freeze while the scripts pin stayed clean)."""
    cmds = []
    m = cfg.get("maker") or {}
    if m.get("command"):
        cmds.append(("maker", m["command"]))
    for e in cfg.get("evaluators") or []:
        if e.get("command"):
            cmds.append((e.get("id", "evaluator"), e["command"]))
    for who, cmd in cmds:
        if not isinstance(cmd, list):
            raise GateError(f"{who} command must be a list of argv tokens")
        # argv[0] is the interpreter, resolved from the host PATH at run time -- it is
        # environment, not seat code. Only the arguments must live in the job.
        for tok in cmd[1:]:
            if not isinstance(tok, str):
                raise GateError(f"{who} command tokens must be strings")
            cand = pathlib.Path(tok)
            if not cand.is_absolute():
                cand = job_dir / tok
            if cand.exists():
                rp = cand.resolve()
                if not rp.is_relative_to(job_dir.resolve()):
                    raise GateError(f"{who} command references {tok} outside the job dir; seat code must live in the job (G1)")


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
        import adapters as _adapters  # fail at freeze, not at round 1 (frontier review)
        try:
            _adapters._require_https(maker.get("url", ""))
        except ValueError as e:
            raise GateError(f"agent maker upstream refused: {e}")
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
    # Unicode Cf (format) characters -- zero-width space U+200B, BOM U+FEFF, etc. --
    # are stripped first: an invisible character inside a declared name must not
    # launder a self-dealing match (frontier finding: 'Jor\u200bdan Blake' used to
    # evade the Operator comparison while visually being the same name).
    s = "".join(ch for ch in str(s) if unicodedata.category(ch) != "Cf")
    return " ".join(s.casefold().split())


WINDOW = 32  # characters, after case/whitespace canonicalization


MIN_LINE = 16  # whole-line needles shorter than this are skipped: a 2-char "needle"
               # substring-matched would scrub legitimate words and deny D4.9 feedback


def held_out_needles(job_dir):
    """D4.9 needle set, classified: (windows, digests, lines).
    windows: every WINDOW-character window of every held-out line (case/whitespace-
    canonicalized); digests: full-file sha256; lines: canonical held-out lines of
    MIN_LINE..WINDOW-1 chars, matched as WHOLE lines with word boundaries. Minus any
    needle that also occurs in bar.md or method.md -- wording the Maker is already
    given is not held-out (D3.held-out). Disclosed limits: paraphrase is not matched;
    held-out lines shorter than MIN_LINE are not matched (the sandbox is the
    exclusion mechanism for those)."""
    held_dir, windows, digests, lines = job_dir / "held_out", set(), set(), set()
    pub = _canon("\n".join((job_dir / f).read_text(encoding="utf-8", errors="ignore") for f in ("bar.md", "method.md") if (job_dir / f).is_file()))
    marker = _canon(WITHHELD)
    for p in (p for p in (held_dir.rglob("*") if held_dir.is_dir() else []) if p.is_file()):
        digests.add(sha256_file(p).casefold())
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            c = _canon(ln)
            if not c or c in pub or c in marker:
                continue
            if len(c) < WINDOW:
                if len(c) >= MIN_LINE:  # whole-line needle, word-bounded at match time
                    lines.add(c)
                continue
            for i in range(0, len(c) - WINDOW + 1):
                w = c[i:i + WINDOW]
                if w not in pub and w not in marker:
                    windows.add(w)
    return windows, digests, lines


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


def _line_bounded(c, at, ln):
    """True when the needle occurrence at `at` is delimited by word boundaries:
    start/end of the canonical text or a non-alphanumeric character on each side
    (spaces AND punctuation/JSON quotes -- a bare JSON value or a quoted echo is
    as much a leak as a prose echo).""" 
    end = at + len(ln)
    before_ok = at == 0 or not c[at - 1].isalnum()
    after_ok = end == len(c) or not c[end].isalnum()
    return before_ok and after_ok


def _line_cover(c, lines, cover):
    """Mark whole-line needle spans: a line needle matches only when delimited by
    word boundaries in the canonical text."""
    for ln in lines:
        at = c.find(ln)
        while at != -1:
            if _line_bounded(c, at, ln):
                cover[at:at + len(ln)] = [True] * len(ln)
            at = c.find(ln, at + 1)


def _redact(s, needles, digests, lines):
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
    _line_cover(c, lines, cover)
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


def _scrub_walk(v, needles, digests, lines):
    if isinstance(v, str):
        return _redact(v, needles, digests, lines) if v else (v, 0)
    if isinstance(v, dict):
        out, n = {}, 0
        for k, sv in v.items():
            out[k], w = _scrub_walk(sv, needles, digests, lines)
            n += w
        return out, n
    if isinstance(v, list):
        out, n = [], 0
        for sv in v:
            nv, w = _scrub_walk(sv, needles, digests, lines)
            out.append(nv)
            n += w
        return out, n
    return v, 0


def scrub_held_out(items, needle_set):
    """Return (items with held-out spans replaced anywhere in the structure, spans withheld) -- D4.9 filters, never forwards."""
    needles, digests, lines = needle_set
    out, n = [], 0
    for it in items:
        it, w = _scrub_walk(it, needles, digests, lines)
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
    needles, digests, lines = held_out_needles(job_dir)
    for nd in needles:
        if nd in low:
            raise GateError("held-out content or digest leaked into Maker packet (I-04)")
    for d in digests:
        if d in low:
            raise GateError("held-out content or digest leaked into Maker packet (I-04)")
    for ln in lines:  # whole-line needles: word-bounded, so common short words do not fire
        at = low.find(ln)
        while at != -1:
            if _line_bounded(low, at, ln):
                raise GateError("held-out content or digest leaked into Maker packet (I-04)")
            at = low.find(ln, at + 1)
