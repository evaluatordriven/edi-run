"""Deterministic invariant gates (I-01 through I-04) for edi-run. Standard library only."""
from collections import Counter
from datetime import datetime, timezone
import hashlib, json, math, os, pathlib, re, unicodedata

UNDISCLOSED = "undisclosed"
MAX_PINNED_SEAT_FILES = 512  # the seat dependency walk is bounded: a pathological import graph refuses by name, never silently truncates


class GateError(Exception):
    """Raised when a deterministic doctrine or custody invariant fails."""


def strict_json_loads(text):
    """JSON parse that REFUSES duplicate object properties (verification
    re-review #8): last-wins silently collapses `{"a":1,"a":2}` into one value,
    so canonicalization is not injective and a config/request can carry two
    meanings. Raises ValueError naming the duplicate key."""
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
    b = cfg.get("bounds")
    if not isinstance(b, dict) or isinstance(b.get("max_rounds"), bool) or not isinstance(b.get("max_rounds"), int) or b["max_rounds"] < 1:
        raise GateError("bounds.max_rounds must be an int >= 1 (I-07)")
    for k in ("max_cost_usd", "max_wall_seconds"):
        v = b.get(k)
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise GateError(f"bounds.{k} must be a finite number > 0 (I-07)")
    # round-3 external review #1: admission control needs an output bound
    mo = b.get("max_output_tokens")
    if mo is not None:
        def _ok(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
        if isinstance(mo, dict):
            if not all(_ok(v) for v in mo.values()):
                raise GateError("bounds.max_output_tokens (per-model map) entries must be finite numbers > 0 (review #1)")
        elif not _ok(mo):
            raise GateError("bounds.max_output_tokens must be a finite number > 0 (or a per-model map of them) (review #1)")
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
    # P1: the settings the loop scores against (dimensions, thresholds, evaluator
    # wiring, controls, bounds) live in job.json and are as frozen as bar.md.
    # Only the Owner's disposition block is chosen later, at finalize.
    # Round-3 external review #7: the digest is computed from the cfg INSTANCE the
    # caller passes — the same single read the gate and the Meter consume — never
    # from a second read of job.json, which a two-read TOCTOU could split.
    j = {k: v for k, v in (cfg or {}).items() if k != "disposition"}
    pins["job"] = hashlib.sha256(json.dumps(j, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # NOTE round 3 (c): EVERY file a seat command can run is pinned, not just *.py
    # under the job tree -- an Evaluator script in another language at the job root
    # was swapped after freeze while the pins stayed clean.
    h = hashlib.sha256()
    seat_cmds = []
    mk = cfg.get("maker") or {}
    if mk.get("command"):
        seat_cmds.append(mk["command"])
    for e in cfg.get("evaluators") or []:
        if e.get("command"):
            seat_cmds.append(e["command"])
        sn = (e.get("script") or {}).get("name")
        if sn:
            seat_cmds.append([sn])
    seen_files = {}
    for cmd in seat_cmds:
        for tok in cmd:
            if not isinstance(tok, str) or tok.startswith("-"):
                continue
            cand = pathlib.Path(tok)
            if not cand.is_absolute():
                cand = job_dir / tok
            try:
                rp = cand.resolve()
                inside = rp.is_relative_to(job_dir.resolve())
            except OSError:
                continue
            try:
                is_file = rp.is_file()
            except OSError:  # e.g. ENAMETOOLONG on a data token (live-aider-job, 2026-11-18)
                continue
            if inside and is_file:
                seen_files[str(rp)] = rp
    # Round-3 external review #9 + verification re-review #7: the pin must cover
    # the dependencies a seat script actually invokes, not only the files named
    # in the command tokens. (a) Python seat scripts: AST-walk local imports
    # (transitively, bounded to the job dir, stdlib-only) — INCLUDING package
    # __init__.py files (importing pkg.worker executes pkg/__init__.py first)
    # and RELATIVE imports at any level (`from . import helper`,
    # `from ..pkg import mod`); (b) non-Python seat scripts: scan the text for
    # referenced files that exist under the job dir (seed/ and held_out/ are
    # material, not seat code, and stay out of the seat-code pin).
    def _local_imports(py_path, acc, seen_mods):
        """AST-walk a Python seat script's local imports; return newly found files.
        seen_mods caches RESOLVED FILE PATHS, not import spellings (verification
        re-review #5, final round): two packages importing identically named
        helpers resolve to DIFFERENT files and both get pinned, while the same
        file reached through different spellings (duplicate imports, diamonds)
        still expands only once."""
        import ast
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as e:
            raise GateError(f"pinned seat script {py_path.relative_to(job_dir)} is not parseable Python "
                            f"({e}); seat code must be statically analyzable (G1)")
        mods = []  # (level, dotted name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods += [(0, a.name) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                lvl = node.level or 0
                base = node.module or ""
                if base:
                    mods.append((lvl, base))
                for a in node.names:  # `from . import helper`, `from .pkg import mod`
                    mods.append((lvl, (base + "." if base else "") + a.name))
        bases = [py_path.parent, job_dir]
        found = []
        for lvl, name in mods:
            parts = name.split(".") if name else []
            # relative import level N: walk up N-1 package levels from the file's dir
            pkg_dir = py_path.parent
            for _ in range(max(0, lvl - 1)):
                pkg_dir = pkg_dir.parent
            rel_bases = [pkg_dir] if lvl else bases
            for base in rel_bases:
                # every package prefix's __init__.py PLUS the module file or package init:
                # `import pkg.worker` executes pkg/__init__.py, then pkg/worker.py (or pkg/worker/__init__.py)
                cands = [base.joinpath(*parts[:i], "__init__.py") for i in range(1, len(parts) + 1)]
                if parts:
                    cands.append(base.joinpath(*parts[:-1], (parts[-1] + ".py")))
                for cand in cands:
                    try:
                        rp = cand.resolve()
                    except OSError:
                        continue
                    if rp.is_file() and rp.is_relative_to(job_dir.resolve()) and str(rp) not in acc \
                            and str(rp) not in seen_mods:  # cache by RESOLVED PATH: same file never re-expands
                        seen_mods.add(str(rp))
                        found.append(rp)
        return list(dict.fromkeys(found))  # dedupe within one level too

    def _text_refs(path, acc):
        """Scan a non-Python seat script's text for referenced job files; return newly found files."""
        try:
            txt = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        found = []
        for tok in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z0-9]{1,8}", txt):
            cands = [(path.parent / tok).resolve(), (job_dir / tok).resolve()]
            for cand in cands:
                try:
                    inside = cand.is_relative_to(job_dir.resolve())
                except OSError:
                    continue
                if not inside or not cand.is_file() or str(cand) in acc or str(cand) == str(path):
                    continue
                relp = cand.relative_to(job_dir.resolve())
                if relp.parts and relp.parts[0] in ("seed", "held_out", "evidence", "rounds", "partial"):
                    continue  # material, not seat code
                found.append(cand)
        return found

    # bounded transitive walk: imports of Python seats, referenced files of others
    frontier = list(seen_files.values())
    seen_mods = set()  # resolved-path cache: the same file never re-expands
    WALK_DEPTH_LIMIT = 64
    for _depth in range(WALK_DEPTH_LIMIT):
        newly = []
        for fp in frontier:
            finder = _local_imports if pathlib.Path(fp).suffix == ".py" else _text_refs
            args = (fp, seen_files, seen_mods) if finder is _local_imports else (fp, seen_files)
            newly += [p for p in finder(*args) if str(p) not in seen_files]
        newly = list(dict.fromkeys(newly))  # dedupe on ENQUEUE: a diamond or duplicate import cannot multiply work
        for p in newly:
            seen_files[str(p)] = p
        if len(seen_files) > MAX_PINNED_SEAT_FILES:
            raise GateError(f"the seat dependency walk pinned {len(seen_files)} files, over the "
                            f"{MAX_PINNED_SEAT_FILES} cap ({len(seen_files) - MAX_PINNED_SEAT_FILES} "
                            "over): declare such dependencies explicitly instead of importing them "
                            "transitively; the pin never silently truncates (G1)")
        frontier = newly
        if not frontier:
            break
    if frontier:
        # verification re-review #5 (final round): a nonempty frontier at the
        # depth limit is a REFUSAL, never a silent truncation — an import chain
        # deeper than the limit is not pinned, and silently exiting would pin a
        # dependency graph the seats do not actually run
        raise GateError(f"the seat dependency walk exceeded the depth limit of {WALK_DEPTH_LIMIT} "
                        "levels without closing the import frontier: declare such dependencies "
                        "explicitly instead of importing them transitively; the pin never "
                        "silently truncates (G1)")
    for rp in sorted(seen_files):
        h.update(str(pathlib.Path(rp).relative_to(job_dir.resolve())).encode())
        h.update(sha256_file(seen_files[rp]).encode())
    if seen_files:
        pins["seat_files"] = h.hexdigest()
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
            if "rounds" in f.parts:
                continue  # runtime-produced custody artifacts (a maker's .py artifact) are outputs, not seat code; including them made every coding-artifact job unfinalizable (live-aider-job, 2026-11-18)
            h.update(str(f.relative_to(job_dir)).encode())
            h.update(sha256_file(f).encode())
        pins["scripts"] = h.hexdigest()
    return pins


def _chain_job_pins(job_dir):
    """The pins recorded in the custody chain at freeze (the last job-pin event).
    The chain is written once at freeze and is not editable state: it is the
    freeze commitment that state.json's pins are checked AGAINST (round-3
    external review #7)."""
    log = pathlib.Path(job_dir) / "job.jsonl"
    if not log.is_file():
        raise GateError("no custody log; the job pin is not anchored in the chain (P7)")
    last = None
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
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
    """Refuse when any frozen pin moved, and NAME which (P1: run, resume and
    finalize all compare through here). Three-way check: the current cfg digest,
    state.json's frozen pins, AND the freeze-time job-pin event in the custody
    chain must all agree — editing job.json and re-digesting state.json in
    lockstep cannot forge a pin past the chain."""
    cur, frozen = gate_instruments(cfg, job_dir), frozen or {}
    drift = sorted(k for k in set(cur) | set(frozen) if cur.get(k) != frozen.get(k))
    if drift:
        what = ", ".join("job.json settings" if k == "job" else k for k in drift)
        raise GateError(f"frozen instruments modified after freeze: {what} (I-01/I-02)")
    chain = _chain_job_pins(job_dir) or {}
    drift = sorted(k for k in set(cur) | set(chain) if cur.get(k) != chain.get(k))
    if drift:
        what = ", ".join("job.json settings" if k == "job" else k for k in drift)
        raise GateError(f"state.json pins do not match the freeze-time job-pin event in the custody chain: {what} (P7)")


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
            # Only tokens that LOOK like paths are probed as paths: an argv token
            # that is neither absolute nor a plausible relative path (no slash,
            # or too long / malformed for the filesystem) is data, not a file
            # reference. stat() can raise OSError (e.g. ENAMETOOLONG) on data
            # tokens -- that must never crash the gate (live-aider-job, 2026-11-18).
            looks_like_path = "/" in tok and not tok.startswith("-") and len(tok) < 1024
            cand = pathlib.Path(tok)
            if not cand.is_absolute():
                cand = job_dir / tok
            try:
                exists = cand.exists() if looks_like_path else False
            except OSError:
                exists = False
            if exists:
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


def gate_packet(packet, job_dir, expected_incumbent_digest, artifact_text=""):
    if packet.get("best_artifact_digest") != expected_incumbent_digest:
        raise GateError("Maker packet best_artifact_digest does not match Pareto-best incumbent (I-04/I-06)")
    low, held_dir = _canon(json.dumps(packet, sort_keys=True)), job_dir / "held_out"
    pub_words = _canon("\n".join((job_dir / f).read_text(encoding="utf-8", errors="ignore") for f in ("bar.md", "method.md") if (job_dir / f).is_file()))
    # P4 (round-3 external review #12): the exemption is narrowed to text the
    # CANDIDATE authored — a feedback string is exempt from the path-marker scan
    # only when it is a quotation of the artifact itself. Every field the runtime
    # writes itself (including evaluator-authored observations that are NOT
    # artifact quotes) gets the full scan.
    c_art = _canon(artifact_text or "")

    def _strings(v, feedback=False):
        if isinstance(v, str):
            yield v, feedback
        elif isinstance(v, dict):
            for k, sv in v.items():
                # verification re-review #9 (final round): dict KEYS are scanned
                # UNCONDITIONALLY — a key never inherits the quotation
                # exemption, because "it quotes the artifact" is a claim about
                # VALUES the candidate authored, and a smuggled marker in a key
                # position is a leak regardless of what the artifact contains.
                # The exemption applies only to VALUES under the feedback keys.
                yield str(k), False
                yield from _strings(sv, feedback or k in ("defects", "workable_solutions"))
        elif isinstance(v, list):
            for sv in v:
                yield from _strings(sv, feedback)

    for k in ("held_out", "scores.jsonl", "job.jsonl"):
        # wording the Maker is already given (bar/method) is not a leak (D3.held-out)
        for s, fb in _strings(packet):
            c = _canon(s)
            if fb and c and c in c_art:
                continue  # the candidate's own words, quoted back
            if k in c and (k not in pub_words or k == "scores.jsonl" or k == "job.jsonl"):
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
