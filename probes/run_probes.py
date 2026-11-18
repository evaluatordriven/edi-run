#!/usr/bin/env python3
"""edi-run probe gate — the 2026-11-17b external recheck, reproducible in CI.

Deterministic, loopback-only, no real LLM and no real key: every model seat points
at a local fake upstream, the key is `probe-fake-key` from the process env, and
canaries are random tokens written under the probe's own temp root.

Checks mirror the external recheck's S/C/K/P/H/E/G/R families. Each result is
PASS, FAIL, KNOWN-FAIL (a failing check explicitly listed in KNOWN_FAILS below),
or SKIP (platform-inapplicable).

Exit codes:
  0  no unexpected FAIL
  1  an unexpected FAIL (a check not in KNOWN_FAILS failed)
  2  --strict and at least one KNOWN-FAIL remains

KNOWN_FAILS is a deliberate, shrinking manifest: it names exactly the still-open
defects (C1-C7, R1, S1) so green CI cannot silently hide them. Remove an entry
in the same commit that fixes the check.
"""
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import re
import threading
import time
import uuid

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import adapters  # noqa: E402
import gates  # noqa: E402
import loop  # noqa: E402

# --- known-still-open defects, per the 2026-11-17b recheck -----------------
KNOWN_FAILS = {
    "S1": "reads outside the deny list are still default-allow; exfil via artifact possible (open: read-allowlist)",
}

FAKE_KEY = "probe-fake-key"
CANARY = "CANARY-" + uuid.uuid4().hex[:16]
OS = "darwin" if sys.platform == "darwin" else ("linux" if sys.platform.startswith("linux") else sys.platform)
SANDBOX_TYPE = "sandbox-exec" if OS == "darwin" else ("bwrap" if shutil.which("bwrap") else None)


class _FakeUpstream(http.server.BaseHTTPRequestHandler):
    """Canned OpenAI-style completions endpoint: counts calls, records headers,
    optional 500 mode, usage envelope included so metering has something to read."""
    mode = "ok"          # ok | error500 | unparseable | hang
    calls = []
    auth_headers = []

    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            body = {}
        _FakeUpstream.calls.append({"model": body.get("model", ""), "path": self.path})
        _FakeUpstream.auth_headers.append(self.headers.get("Authorization", ""))
        if _FakeUpstream.mode == "hang":
            time.sleep(30)
        if _FakeUpstream.mode == "error500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"internal error")
            return
        reply = '```json\n{"files": {"memo.md": "# Header\\nFigure: 42% (Source: Q3, 2026-10-01)\\n"}}\n```'
        if _FakeUpstream.mode == "unparseable":
            reply = "I am not json at all"
        resp = {"model": body.get("model", "probe-model"), "usage": {"total_tokens": 100},
                "choices": [{"message": {"content": reply}}]}
        raw = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_upstream():
    _FakeUpstream.calls, _FakeUpstream.auth_headers = [], []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeUpstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"


def reset_upstream(mode="ok"):
    _FakeUpstream.mode = mode
    _FakeUpstream.calls, _FakeUpstream.auth_headers = [], []


# --- job fixture (mirrors tests/test_edi.py, trimmed to what probes need) ---
def make_job(root, subdir, maker_kind="script", url=None, pricing=None, bounds=None,
             agent_mode="normal", maker_body=None):
    job = root / subdir
    for d in ("held_out/controls", "held_out/planted", "evidence", "artifact", "scripts"):
        (job / d).mkdir(parents=True, exist_ok=True)
    bar = job / "bar.md"
    bar.write_text("# Bar\n- D1: Has header\n- D2: Has cited figure\n", encoding="utf-8")
    bar_digest = gates.sha256_file(bar)
    (job / "rubric.md").write_text("# Rubric\nCheck D1 header and D2 figure.\n", encoding="utf-8")
    (job / "method.md").write_text("# Method\nDraft memo satisfying D1 and D2.\n", encoding="utf-8")
    (job / "runner.md").write_text("# Runner\nedi-run v1\n", encoding="utf-8")
    (job / "held_out/controls/clean.md").write_text(
        "# Header\nHeld-out reference control figure: 99% (Source: Control Audit, 2026-01-01)\n", encoding="utf-8")
    (job / "held_out/controls/planted.md").write_text("No header and no figure here.\n", encoding="utf-8")
    (job / "memo.md").write_text("# Header\nNo figure yet.\n", encoding="utf-8")

    maker_script = job / "scripts" / "maker.py"
    script = AGENT_SCRIPTS.get(agent_mode, AGENT_SCRIPTS["normal"])
    maker_script.write_text(script, encoding="utf-8")

    eval_script = job / "scripts" / "eval_e1.py"
    eval_script.write_text(
        "import json, pathlib, sys\n"
        "pos = [a for a in sys.argv[1:] if not a.startswith('--')]\n"
        "target, dim = pathlib.Path(pos[0]), pos[1]\n"
        "txt = (target / 'memo.md').read_text() if target.is_dir() else target.read_text()\n"
        "score = 5.0 if txt.startswith('# Header') and 'Source:' in txt else 1.0\n"
        "print(json.dumps({'score': score, 'observation': txt.strip()[:60], 'remedy': 'Add missing element' if score < 4.0 else ''}))\n",
        encoding="utf-8")

    scale = {"anchors": {"1": "missing", "5": "present"}, "floor": 0.0, "threshold": 4.0}
    mk = {"name": "Probe Maker", "kind": maker_kind, "version": "1.0.0",
          "method": {"file": "method.md"}, "model": None}
    if maker_kind == "script":
        mk["command"] = [sys.executable, str(maker_script)]
    else:
        mk["url"] = url
        mk["model"] = {"family": "probe", "identifier": "probe-model", "endpoint": "127.0.0.1", "readOnly": False}
        mk["apiKeyEnv"] = "PROBE_FAKE_KEY"
        if maker_kind == "agent":
            mk["sandbox"] = {"type": SANDBOX_TYPE}
        mk["command"] = [sys.executable, str(maker_script)]
    if maker_body:
        mk.update(maker_body)
    cfg = {
        "job": subdir, "levelClaimed": "CL1", "specVersion": "0.9.8",
        "bounds": bounds or {"max_rounds": 2, "max_cost_usd": 1.0, "max_wall_seconds": 60},
        "mode": "pass_r2", "subjectFiles": ["memo.md"],
        "bar": {"file": "bar.md", "digest": {"sha256": bar_digest}, "frozenAt": "2026-10-05T09:00:00Z",
                "author": "Jordan Blake",
                "dimensions": [{"id": "D1", "criterion": "Starts with # Header", "scale": scale},
                               {"id": "D2", "criterion": "Cites Source:", "scale": scale}],
                "approvals": [
                    {"role": "buyer", "by": "Casey Park", "barDigest": {"sha256": bar_digest}, "at": "2026-10-05T08:40:00Z", "adopted": None},
                    {"role": "specialist", "by": "Riley Moss", "barDigest": {"sha256": bar_digest}, "at": "2026-10-05T08:50:00Z", "adopted": None}],
                "publishedAt": None, "groundTruth": [], "dissent": []},
        "maker": mk,
        "instrument": {"rubric": {"file": "rubric.md"}, "runner": {"file": "runner.md"}},
        "evaluators": [{"id": "E1", "name": "Taylor Grant", "kind": "script", "model": None,
                        "script": {"name": "eval_e1.py", "author": "Morgan Lee",
                                   "digest": {"sha256": gates.sha256_file(eval_script)}},
                        "command": [sys.executable, str(eval_script)], "runs": 2}],
        "controls": {"clean": "held_out/controls/clean.md", "planted": "held_out/controls/planted.md"},
        "retention": {"location": "local/records/" + subdir, "public": False},
        "disposition": {"state": "given", "by": "Casey Park", "acknowledgesUnobserved": []},
    }
    if pricing is not None:
        cfg["pricing"] = pricing
    (job / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return job


def memo_ok(memo):
    return memo.startswith("# Header") and "Source:" in memo


# --- agent scripts used by the probes --------------------------------------
PREAMBLE = (
    "import json, pathlib, sys, urllib.request, os\n"
    "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
    "art = pathlib.Path(sys.argv[2])\n"
    "_tail = 'none'\n"
)
WRITE_MEMO = (
    "art.mkdir(parents=True, exist_ok=True)\n"
    "(art / 'memo.md').write_text('# Header\\nFigure: 42% (Source: Q3, 2026-10-01)\\nPROBE ' + _tail + '\\n', encoding='utf-8')\n"
)


AGENT_SCRIPTS = {
    # normal conforming agent: one metered proxy call, writes the passing memo
    "normal": PREAMBLE + (
        "import urllib.request as _u\n"
        "url, model = os.environ['AGENT_PROXY_URL'], os.environ['AGENT_MODEL']\n"
        "req = _u.Request(url, data=json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'make memo'}]}).encode(), headers={'Content-Type': 'application/json'})\n"
        "with _u.urlopen(req, timeout=15) as r: r.read()\n"
        + WRITE_MEMO
    ),
    # reads a canary planted next to the JOBS ROOT (the recheck's S1 shape)
    # reads a held_out file at an absolute path baked before freeze (S2)
    "read_held_out": PREAMBLE + (
        "try:\n"
        "    val = open(pkt['_probe_ho']).read().strip()[:20]\n"
        "except Exception:\n"
        "    val = 'DENIED'\n"
        "_tail = 'LEAK=' + val\n" + WRITE_MEMO
    ),
    # counts visible pids in /proc (S3)
    "read_proc": PREAMBLE + (
        "try:\n"
        "    pids = len([q for q in os.listdir('/proc') if q.isdigit()])\n"
        "except Exception:\n"
        "    pids = -1\n"
        "_tail = 'LEAK=PROC_PIDS=%d' % pids\n" + WRITE_MEMO
    ),
    # reads /proc/1/environ for the parent secret (S4)
    "read_proc_environ": PREAMBLE + (
        "try:\n"
        "    env1 = open('/proc/1/environ').read()\n"
        "    val = 'LEAKED' if 'PROBE_PARENT_SECRET' in env1 else 'CLEAN'\n"
        "except Exception:\n"
        "    val = 'DENIED'\n"
        "_tail = 'LEAK=' + val\n" + WRITE_MEMO
    ),
    # appends to a code file outside the workspace (S5)
    "write_code": PREAMBLE + (
        "try:\n"
        "    open(PROBE_CODE_TARGET, 'a').write('x')\n"
        "    val = 'WROTE'\n"
        "except Exception:\n"
        "    val = 'DENIED'\n"
        "_tail = 'LEAK=' + val\n" + WRITE_MEMO
    ),
    # fetches a non-proxy loopback URL (S6)
    "net_probe": PREAMBLE + (
        "import urllib.request as _u\n"
        "try:\n"
        "    _u.urlopen(PROBE_NET_TARGET, timeout=5)\n"
        "    val = 'REACHED'\n"
        "except Exception:\n"
        "    val = 'BLOCKED'\n"
        "_tail = 'LEAK=' + val\n" + WRITE_MEMO
    ),
    # asks the proxy for an UNDECLARED model; records what it saw
        # ten metered calls: a missing proxy cap must show (C3)
    "many_calls": PREAMBLE + (
        "import urllib.request as _u\n"
        "url, model = os.environ['AGENT_PROXY_URL'], os.environ['AGENT_MODEL']\n"
        + WRITE_MEMO +
        "for _i in range(10):\n"
        "    _req = _u.Request(url, data=json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'x%d' % _i}]}).encode(), headers={'Content-Type': 'application/json'})\n"
        "    with _u.urlopen(_req, timeout=15) as _r: _r.read()\n"
    ),
    "read_jobsroot_canary": PREAMBLE + (
        "try:\n"
        "    val = open(os.environ['PROBE_CANARY2']).read().strip()\n"
        "except Exception:\n"
        "    val = 'DENIED'\n"
        "_tail = 'LEAK=' + val\n" + WRITE_MEMO
    ),
    "crash_after_calls": PREAMBLE + (
        "import urllib.request as _u\n"
        "url, model = os.environ['AGENT_PROXY_URL'], os.environ['AGENT_MODEL']\n"
        "for i in range(3):\n"
        "    req = _u.Request(url, data=json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'x%d' % i}]}).encode(), headers={'Content-Type': 'application/json'})\n"
        "    with _u.urlopen(req, timeout=15) as r: r.read()\n"
        "sys.stderr.write('boom after 3 metered calls')\n"
        "sys.exit(3)\n"
    ),
    "slow_calls": PREAMBLE + (
        "import urllib.request as _u, time\n"
        "url, model = os.environ['AGENT_PROXY_URL'], os.environ['AGENT_MODEL']\n"
        "for i in range(4):\n"
        "    req = _u.Request(url, data=json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'x%d' % i}]}).encode(), headers={'Content-Type': 'application/json'})\n"
        "    with _u.urlopen(req, timeout=15) as r: r.read()\n"
        "time.sleep(120)\n"
    ),
    # asks the proxy for an UNDECLARED model; records what it saw
    "wrong_model": PREAMBLE + (
        "import urllib.request as _u, urllib.error as _e\n"
        "url = os.environ['AGENT_PROXY_URL']\n"
        "req = _u.Request(url, data=json.dumps({'model': 'undeclared-model', 'messages': [{'role': 'user', 'content': 'x'}]}).encode(), headers={'Content-Type': 'application/json'})\n"
        "try:\n"
        "    with _u.urlopen(req, timeout=15) as r: code = str(r.status)\n"
        "except _e.HTTPError as e:\n"
        "    code = str(e.code)\n"
        "except Exception as e:\n"
        "    code = 'EXC:' + repr(e)[:40]\n"
        "_tail = 'PROXY_STATUS=' + code\n" + WRITE_MEMO
    ),
    # spawns a child that outlives the parent kill (K1)
    "spawn_child": PREAMBLE + (
        "import subprocess, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        + WRITE_MEMO
    ),
}



# --- probe environment ------------------------------------------------------
class ProbeEnv:
    """One throwaway root per probe run: jobs root, fake key in env, canaries."""

    def __init__(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="edi_probe_"))
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()
        self.canary2 = self.root / "canary-next-to-jobs-root.txt"
        self.canary2.write_text(CANARY, encoding="utf-8")
        # a canary INSIDE the repo checkout: the sandbox must treat the whole
        # checkout as read-only. NOT under /tmp (mounted writable for tempfile).
        self.code_target = REPO / (".edi_probe_canary_" + uuid.uuid4().hex[:8] + ".txt")
        self.code_target.write_text("untouched", encoding="utf-8")
        self._old_env = {k: os.environ.get(k) for k in ("PROBE_FAKE_KEY", "PROBE_CANARY2", "PROBE_CODE_TARGET", "TMPDIR")}
        os.environ["PROBE_FAKE_KEY"] = FAKE_KEY
        os.environ["PROBE_CANARY2"] = str(self.canary2)
        os.environ["PROBE_CODE_TARGET"] = str(self.code_target)
        os.environ["TMPDIR"] = str(self.tmpdir)
        self.upstream_srv, self.upstream_url = start_upstream()

    def close(self):
        try:
            self.upstream_srv.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)
        if self.code_target.is_file():
            self.code_target.unlink()  # never leave the repo canary behind
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


ENV = None


def leak_value(art, key):
    f = art / "leak.txt"
    if not f.is_file():
        return "(no leak file)"
    for ln in f.read_text().splitlines():
        if ln.startswith(key + "="):
            return ln.split("=", 1)[1]
    return "(no " + key + ")"


def bake_env_into_script(job, pairs):
    """The agent env carries no arbitrary vars by design, so probes that need a target
    bake it into the script text BEFORE freeze (freeze pins the script)."""
    sc = job / "scripts" / "maker.py"
    txt = sc.read_text()
    txt = "".join("%s = %r\n" % (k, v) for k, v in pairs.items()) + txt
    for var in ("PROBE_CANARY2", "PROBE_CODE_TARGET", "PROBE_NET_TARGET", "PROBE_CANARY"):
        txt = txt.replace("os.environ['%s']" % var, var)
    # a probe script must not leave a bare undefined name behind: compile-check it
    compile(txt, str(sc), "exec")
    for var in ("PROBE_CANARY2", "PROBE_CODE_TARGET", "PROBE_NET_TARGET", "PROBE_CANARY"):
        if re.search(r"\b%s\b" % var, txt) and ("%s = " % var) not in txt:
            raise RuntimeError("probe script references %s without a baked value" % var)
    sc.write_text(txt, encoding="utf-8")


def agent_job(subdir, mode, url=None, pricing=None, bounds=None, extra_env=None, maker_body=None):
    job = make_job(ENV.jobs, subdir, maker_kind="agent", url=url, pricing=pricing,
                   bounds=bounds, agent_mode=mode, maker_body=maker_body)
    pairs = {"PROBE_CANARY2": str(ENV.canary2)}
    pairs.update(extra_env or {})
    bake_env_into_script(job, pairs)
    loop.freeze_job(job)
    return job


def agent_leak(job):
    rounds = job / "rounds"
    rs = sorted(rounds.glob("round-*")) if rounds.is_dir() else []
    if not rs:
        return "(no round artifact)"
    memo = rs[-1] / "artifact" / "memo.md"
    if not memo.is_file():
        return "(no memo)"
    for ln in memo.read_text().splitlines():
        if ln.startswith("PROBE "):
            return ln[len("PROBE "):]
    return "(no PROBE line)"



P1_PRICING = {"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}


def metered_spend(job):
    st = json.loads((job / "state.json").read_text())
    return st.get("cost_usd", 0.0), st.get("terminal_state"), st.get("reason")


def check_p1():
    if SANDBOX_TYPE is None:
        return "SKIP", "no sandbox on host"
    reset_upstream()
    job = make_job(ENV.jobs, "p1", maker_kind="agent", url=ENV.upstream_url,
                   pricing={"declared-model": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}},
                   agent_mode="wrong_model")
    cfg = json.loads((job / "job.json").read_text())
    cfg["maker"]["model"]["identifier"] = "declared-model"
    (job / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    sc = job / "scripts" / "maker.py"
    sc.write_text(sc.read_text().replace("'undeclared-model'", "'undeclared-" + uuid.uuid4().hex[:6] + "'"), encoding="utf-8")
    loop.freeze_job(job)
    loop.run_job(job)
    val = agent_leak(job)
    code = val.split("=", 1)[-1] if "=" in val else val
    saw_undeclared = [c for c in _FakeUpstream.calls if "undeclared-" in c["model"]]
    ok = saw_undeclared == [] and (code.startswith("403") or code.endswith("403"))
    return ("PASS", "undeclared model 403'd by the proxy, never forwarded (agent saw %s)" % code[:24]) if ok else ("FAIL", "agent saw %s; upstream calls: %s" % (code, _FakeUpstream.calls[:2]))


def check_s2():
    if SANDBOX_TYPE is None:
        return "SKIP", "no sandbox on host"
    ho = ENV.jobs / "s2" / "held_out" / "controls" / "clean.md"
    job = make_job(ENV.jobs, "s2", maker_kind="agent", url=ENV.upstream_url,
                   pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}},
                   agent_mode="read_held_out")
    sc = job / "scripts" / "maker.py"
    sc.write_text(sc.read_text().replace("pkt['_probe_ho']", repr(str(ho.resolve()))), encoding="utf-8")
    loop.freeze_job(job)
    loop.run_job(job)
    val = agent_leak(job).replace("LEAK=", "", 1)
    return ("PASS", "held_out unreadable (" + val + ")") if val == "DENIED" else ("FAIL", "held_out value: " + val)


def check_s1():
    if SANDBOX_TYPE is None:
        return "SKIP", "no sandbox on host"
    job = agent_job("s1", "read_jobsroot_canary", url=ENV.upstream_url,
                    pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}})
    loop.run_job(job)
    val = agent_leak(job).replace("LEAK=", "", 1)
    return ("PASS", "canary next to jobs root unreadable") if val == "DENIED" else ("FAIL", "canary read: " + val)


def _allow_agent_job(subdir, mode):
    # the S1 allowlist switch IN the frozen maker config (staged: the default
    # profile remains the deny list until S7-S9 pass on every CI leg)
    return agent_job(subdir, mode, url=ENV.upstream_url,
                     pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}},
                     maker_body={"sandbox": {"type": SANDBOX_TYPE, "reads": "allowlist"}})


def check_s7():
    if SANDBOX_TYPE != "bwrap":
        return "SKIP", "allowlist is a bwrap/Linux capability; sandbox-exec cannot express it (deny precedence + deny-default aborts, verified)"
    reset_upstream()
    job = _allow_agent_job("s7", "normal")
    loop.run_job(job)
    rounds = sorted((job / "rounds").glob("round-*"))
    memo = rounds[-1] / "artifact" / "memo.md" if rounds else None
    if not (memo and memo.is_file()):
        return "FAIL", "allowlisted agent produced no artifact (interpreter/stdlib/workspace broken?)"
    st = json.loads((job / "state.json").read_text())
    if st.get("terminal_state") != "accepted":
        return "FAIL", "allowlisted agent run did not conform: %s/%s" % (st.get("terminal_state"), st.get("reason"))
    return "PASS", "allowlisted seat: interpreter, stdlib, relay and workspace all work"


def check_s8():
    if SANDBOX_TYPE != "bwrap":
        return "SKIP", "allowlist is a bwrap/Linux capability; sandbox-exec cannot express it (deny precedence + deny-default aborts, verified)"
    reset_upstream()
    job = _allow_agent_job("s8", "read_jobsroot_canary")
    loop.run_job(job)
    val = agent_leak(job).replace("LEAK=", "", 1)
    return ("PASS", "allowlist: canary next to jobs root is not even visible") if val == "DENIED" else ("FAIL", "allowlist canary read: " + val)


def check_s9():
    if SANDBOX_TYPE != "bwrap":
        return "SKIP", "allowlist is a bwrap/Linux capability; sandbox-exec cannot express it (deny precedence + deny-default aborts, verified)"
    reset_upstream()
    job = _allow_agent_job("s9", "read_held_out")
    loop.run_job(job)
    val = agent_leak(job).replace("LEAK=", "", 1)
    return ("PASS", "allowlist: held-out control unreadable") if val == "DENIED" else ("FAIL", "allowlist held_out read: " + val)


def check_s3():
    if OS != "linux" or SANDBOX_TYPE != "bwrap":
        return "SKIP", "proc-probing needs bwrap/linux"
    job = agent_job("s3", "read_proc", url=ENV.upstream_url,
                    pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}})
    loop.run_job(job)
    val = agent_leak(job)
    try:
        n = int(val.split("=")[-1])
    except Exception:
        n = -2
    return ("PASS", "own /proc only: %d pids" % n) if 0 <= n <= 5 else ("FAIL", "proc pids: " + val)


def check_s4():
    if OS != "linux" or SANDBOX_TYPE != "bwrap":
        return "SKIP", "proc-probing needs bwrap/linux"
    job = agent_job("s4", "read_proc_environ", url=ENV.upstream_url,
                    pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}})
    loop.run_job(job)
    val = agent_leak(job).replace("LEAK=", "", 1)
    return ("PASS", "parent /proc/1/environ unreachable") if val in ("DENIED", "CLEAN") else ("FAIL", val)


def check_s5():
    if SANDBOX_TYPE is None:
        return "SKIP", "no sandbox on host"
    job = agent_job("s5", "write_code", url=ENV.upstream_url,
                    pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}},
                    extra_env={"PROBE_CODE_TARGET": str(ENV.code_target)})
    loop.run_job(job)
    val = agent_leak(job).replace("LEAK=", "", 1)
    untouched = ENV.code_target.read_text() == "untouched"
    return ("PASS", "code writes denied (" + val + ")") if (val == "DENIED" and untouched) else ("FAIL", val + " target=" + ENV.code_target.read_text()[:20])


def check_s6():
    if SANDBOX_TYPE is None:
        return "SKIP", "no sandbox on host"
    class _Quiet(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"side-channel")
    decoy = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
    threading.Thread(target=decoy.serve_forever, daemon=True).start()
    try:
        job = agent_job("s6", "net_probe", url=ENV.upstream_url,
                        pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}},
                        extra_env={"PROBE_NET_TARGET": "http://127.0.0.1:%d/steal" % decoy.server_port})
        loop.run_job(job)
    finally:
        decoy.shutdown()
    val = agent_leak(job).replace("LEAK=", "", 1)
    return ("PASS", "non-proxy loopback blocked (" + val + ")") if val == "BLOCKED" else ("FAIL", val)


def check_c1():
    if SANDBOX_TYPE is None:
        return "SKIP", "no sandbox on host"
    reset_upstream()
    job = agent_job("c1", "crash_after_calls", url=ENV.upstream_url,
                    pricing=P1_PRICING, bounds={"max_rounds": 2, "max_cost_usd": 10.0, "max_wall_seconds": 120})
    loop.run_job(job)
    cost, ts, reason = metered_spend(job)
    calls = len(_FakeUpstream.calls)
    if calls == 0:
        return "SKIP", "no upstream calls observed"
    if cost > 0:
        return "PASS", "crash spend counted (%d calls -> $%s)" % (calls, cost)
    return "FAIL", "%d metered upstream calls but cost_usd=%s (%s/%s)" % (calls, cost, ts, reason)


def check_c2():
    if SANDBOX_TYPE is None:
        return "SKIP", "no sandbox on host"
    reset_upstream()
    job = agent_job("c2", "slow_calls", url=ENV.upstream_url,
                    pricing=P1_PRICING, bounds={"max_rounds": 2, "max_cost_usd": 10.0, "max_wall_seconds": 6})
    loop.run_job(job)
    cost, ts, reason = metered_spend(job)
    calls = len(_FakeUpstream.calls)
    if calls == 0:
        return "SKIP", "no upstream calls observed"
    if cost > 0:
        return "PASS", "timeout spend counted (%d calls -> $%s, %s/%s)" % (calls, cost, ts, reason)
    return "FAIL", "%d metered calls before the deadline kill but cost_usd=%s (%s/%s)" % (calls, cost, ts, reason)


def check_c3():
    if SANDBOX_TYPE is None:
        return "SKIP", "no sandbox on host"
    reset_upstream()
    job = agent_job("c3", "many_calls", url=ENV.upstream_url,
                    pricing=P1_PRICING, bounds={"max_rounds": 2, "max_cost_usd": 0.0003, "max_wall_seconds": 60})
    loop.run_job(job)
    cost, ts, reason = metered_spend(job)
    if cost <= 0.0003 and reason == "cap_reached":
        return "PASS", "cap held and named ($%s <= 0.0003, terminal cap_reached)" % cost
    return "FAIL", "cap $0.0003: cost_usd=%s terminal %s/%s with %d upstream calls" % (cost, ts, reason, len(_FakeUpstream.calls))


def check_k1():
    if SANDBOX_TYPE is None:
        return "SKIP", "no sandbox on host"
    marker = "edi_probe_child_" + uuid.uuid4().hex[:8]
    job = make_job(ENV.jobs, "k1", maker_kind="agent", url=ENV.upstream_url,
                   pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}},
                   agent_mode="spawn_child")
    sc = job / "scripts" / "maker.py"
    sc.write_text(sc.read_text().replace("'import time; time.sleep(60)'", "'import time; time.sleep(60)  # %s'" % marker), encoding="utf-8")
    loop.freeze_job(job)
    loop.run_job(job)
    time.sleep(2)
    out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    if out.returncode == 0:
        subprocess.run(["pkill", "-f", marker])
        return "FAIL", "child survived the kill"
    return "PASS", "no orphaned child"


def check_h1():
    srv, url = start_upstream()
    try:
        http_url = url.replace("127.0.0.1", "127.0.0.2")
        job = make_job(ENV.jobs, "h1", maker_kind="model", url=http_url, pricing=P1_PRICING)
        try:
            loop.freeze_job(job)
            refused = False
        except gates.GateError:
            refused = True
        if refused:
            return "PASS", "http upstream refused at freeze"
        loop.run_job(job)
        if any(h.startswith("Bearer ") for h in _FakeUpstream.auth_headers):
            return "FAIL", "key shipped over plain http"
        return "PASS", "no key over plain http (refused at run)"
    finally:
        srv.shutdown()


def check_e1():
    reset_upstream("error500")
    job = make_job(ENV.jobs, "e1", maker_kind="model", url=ENV.upstream_url, pricing=P1_PRICING)
    loop.freeze_job(job)
    loop.run_job(job)
    st = json.loads((job / "state.json").read_text())
    if st.get("maker_error"):
        return "PASS", "upstream 500 reaches the maker as an error (%s/%s)" % (st.get("terminal_state"), st.get("reason"))
    return "FAIL", "5xx swallowed: %s/%s" % (st.get("terminal_state"), st.get("reason"))


def check_e2():
    # C6: a billed reply the evaluator cannot parse still meters — the row is
    # written from the envelope BEFORE the content parse
    reset_upstream("unparseable")
    import metering
    m = metering.Meter(ENV.jobs / "e2-meter", pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}, bounds={"max_cost_usd": 1.0})
    ev = {"id": "E1", "kind": "model", "url": ENV.upstream_url, "model": {"identifier": "declared-model"}, "apiKeyEnv": "PROBE_NONE"}
    target = ENV.jobs / "e2-target.md"
    target.write_text("# target\n", encoding="utf-8")
    try:
        adapters.run_evaluator(ev, target, "D1", meter=m)
    except Exception:
        pass  # unparseable content -> unobserved/raise; the ROW must exist regardless
    rows = m._rows
    return ("PASS", "billed unparseable reply metered (%d row, cost_hint set)" % len(rows)) \
        if rows and rows[0].get("kind") == "call" else ("FAIL", "no meter row for a billed unparseable reply")


def check_g1():
    job = make_job(ENV.jobs, "g1", maker_kind="script", agent_mode="normal")
    loop.freeze_job(job)
    (job / "scripts" / "maker.py").write_text("# tampered after freeze\n", encoding="utf-8")
    try:
        loop.run_job(job)
        return "FAIL", "edited seat script ran after freeze"
    except gates.GateError:
        return "PASS", "edited script refused"
    except Exception as e:
        return "FAIL", "non-GateError refusal: %s" % e


def check_g2():
    job = make_job(ENV.jobs, "g2", maker_kind="model", url=ENV.upstream_url,
                   pricing={"default": {"input_usd_per_mtok": 0, "output_usd_per_mtok": 1.0}})
    try:
        loop.freeze_job(job)
        return "FAIL", "$0 pricing accepted at freeze"
    except gates.GateError:
        return "PASS", "$0 pricing refused"
    except Exception as e:
        return "FAIL", "non-GateError refusal: %s" % e


def check_r1():
    reset_upstream("hang")  # the kill must land MID-ROUND: a fast fake upstream finishes before the kill
    job = make_job(ENV.jobs, "r1", maker_kind="agent", url=ENV.upstream_url, pricing=P1_PRICING, agent_mode="normal")
    loop.freeze_job(job)
    # loop.py has no CLI: run the job through a real in-process call in the child
    proc = subprocess.Popen([sys.executable, "-c", "import sys, loop; loop.run_job(sys.argv[1])", str(job)], cwd=str(REPO),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(3)
    proc.kill()  # hard kill: no finally runs, open_round persists with no terminal state
    proc.wait()
    try:
        loop.run_job(job)
        return "FAIL", "silent resume attempted on a hard-killed run"
    except gates.GateError as e:
        if "edi.py resume" not in str(e):
            return "FAIL", "refusal does not name the remedy: %s" % str(e)[:120]
    # explicit resume: pins re-verified, partial round discarded, run completes
    time.sleep(31)  # the single-threaded fake upstream is still inside its hang sleep; let it drain
    reset_upstream()  # the resumed run gets a healthy upstream
    st = json.loads((job / "state.json").read_text())
    cost_before = st.get("cost_usd", 0.0)
    rc = loop.resume_job(job)
    st = json.loads((job / "state.json").read_text())
    if st.get("terminal_state") not in ("accepted", "rejected", "needs_owner"):
        return "FAIL", "resume did not reach a named end state: %s/%s" % (st.get("terminal_state"), st.get("reason"))
    if st.get("cost_usd", 0.0) < cost_before - 1e-9:
        return "FAIL", "resume lost spend: %s < %s" % (st.get("cost_usd"), cost_before)
    # the resume must leave the job FINALIZABLE: the superseded artifact events
    # must not pin files the resumed attempt rewrote (record.verify files=True)
    frc = loop.finalize_job(job, state="given", by="Probe R1")
    if frc != 0:
        return "FAIL", "finalize after resume refused (rc=%s) — superseded events still pin artifacts" % frc
    return "PASS", "silent resume refused; explicit resume completed (%s/%s, spend %s -> %s); finalize OK" % (
        st.get("terminal_state"), st.get("reason"), cost_before, st.get("cost_usd"))


CHECKS = [
    ("S2", "held_out unreadable by its job path", lambda: check_s2(), None),
    ("S1", "canary next to the jobs root unreadable", lambda: check_s1(), KNOWN_FAILS["S1"]),
    ("S7", "allowlisted seat still works (workspace+stdlib+relay)", lambda: check_s7(), None),
    ("S8", "allowlist: canary next to jobs root invisible", lambda: check_s8(), None),
    ("S9", "allowlist: held-out control unreadable", lambda: check_s9(), None),
    ("S3", "own /proc only (no host pids)", lambda: check_s3(), None),
    ("S4", "loop key unreadable via /proc/environ", lambda: check_s4(), None),
    ("S5", "agent cannot write edi-run code", lambda: check_s5(), None),
    ("S6", "no network except the proxy", lambda: check_s6(), None),
    ("P1", "undeclared model refused by the proxy", lambda: check_p1(), None),
    ("C1", "agent spend counted on crash", lambda: check_c1(), None),
    ("C2", "agent spend counted on timeout", lambda: check_c2(), None),
    ("C3", "proxy enforces the cost cap", lambda: check_c3(), None),
    ("K1", "agent children die on kill", lambda: check_k1(), None),
    ("H1", "no key over plain http", lambda: check_h1(), None),
    ("E1", "upstream 5xx reaches the maker as an error", check_e1, None),
    ("E2", "billed unparseable reply still meters", check_e2, None),
    ("G1", "seat script edited after freeze is refused", lambda: check_g1(), None),
    ("G2", "$0 prices refused at freeze", lambda: check_g2(), None),
    ("R1", "resume after a hard kill is clean", lambda: check_r1(), None),
]


def main():
    global ENV
    strict = "--strict" in sys.argv
    ENV = ProbeEnv()
    results = []
    try:
        for name, label, fn, known in CHECKS:
            t0 = time.time()
            try:
                status, detail = fn()
            except Exception as e:
                # an exception is never laundered into KNOWN-FAIL: a probe bug or
                # environment failure presents as ERROR and closes the gate
                status, detail = "ERROR", "exception: %s: %s" % (type(e).__name__, str(e)[:160])
            if status == "FAIL" and known is not None:
                detail = detail + " [" + known + "]"
                status = "KNOWN-FAIL"
            dt = time.time() - t0
            results.append((name, label, status, detail, dt))
            print("%-4s %-44s %-10s %5.1fs  %s" % (name, label, status, dt, detail), flush=True)
    finally:
        ENV.close()
    n_fail = sum(1 for r in results if r[2] == "FAIL") + sum(1 for r in results if r[2] == "ERROR")
    n_known = sum(1 for r in results if r[2] == "KNOWN-FAIL")
    n_pass = sum(1 for r in results if r[2] == "PASS")
    n_skip = sum(1 for r in results if r[2] == "SKIP")
    print("\n%d PASS / %d FAIL / %d KNOWN-FAIL / %d SKIP" % (n_pass, n_fail, n_known, n_skip))
    stale = [r[0] for r in results if r[2] == "PASS" and any(k[0] == r[0] for k in CHECKS if k[3])]
    for sid in stale:
        print("STALE KNOWN-FAIL: %s now passes - remove its KNOWN_FAILS entry in this commit" % sid)
    if n_fail:
        print("UNEXPECTED FAILURES - gate closed")
        return 1
    if strict and (n_known or stale):
        print("--strict: %d KNOWN-FAIL remain, %d stale entries (S1/C1-C3/R1 backlog)" % (n_known, len(stale)))
        return 2
    if n_known:
        print("KNOWN-FAIL items are the open backlog; remove them from KNOWN_FAILS in the commit that fixes them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
