"""Final targeted fix round (verification re-review 2, /tmp/edi_r3_verification2_gpt62sol.md)
— one red-first test cluster per fixed finding (P1.1-P1.4, P2.5-P2.7).

Run:  python3 -m unittest discover -s tests -p "test_final_fixes.py" -v
"""
import http.server
import json
import math
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest

import test_edi  # sets up sys.path and the shared fixtures
import support_briefing as sb

import adapters
import gates
import loop
from metering import CapExceeded, Meter

PASS = sb.PASSING
SANDBOX = test_edi._SANDBOX_TYPE
PRICING = {"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}


def _meter(job, cap, out_tokens=100, **bounds_extra):
    return Meter(job, pricing=PRICING, bounds={"max_cost_usd": cap, "max_output_tokens": out_tokens, **bounds_extra})


def _body(n=400):
    return json.dumps({"messages": [{"role": "user", "content": "x" * n}]}).encode()


def _usage_rows(job):
    p = job / "usage.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.is_file() else []


def _resv_rows(job):
    return [json.loads(ln) for ln in (job / "usage.resv.jsonl").read_text().splitlines()]


def _rechain(path, rows, digest_fn):
    prev = ""
    for r in rows:
        r.pop("digest", None)
        r.pop("prev", None)
        r["prev"] = prev
        r["digest"] = digest_fn(r)
        prev = r["digest"]
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


# ------------------------------------------------------------------ P1.1 settlement ordering
class P11_SettlementOrdering(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_fin11_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_already_retired_reservation_settle_appends_no_usage_row(self):
        job = self.tmp / "j2"
        m = _meter(job, 1.0)
        r = m.reserve(_body())
        m.release(r)  # released WITHOUT a bill
        before = _usage_rows(job)
        with self.assertRaises(ValueError):
            m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 400000},
                         settle_reservation=r)
        self.assertEqual(_usage_rows(job), before,
                         "settling a retired reservation must append NO usage row (no second charge)")

    def test_settling_twice_bills_once_and_returns_the_existing_row(self):
        job = self.tmp / "j3"
        m = _meter(job, 1.0)
        r = m.reserve(_body())
        first = m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 400000},
                             settle_reservation=r)
        second = m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 900000},
                              settle_reservation=r)
        rows = [x for x in _usage_rows(job) if x.get("kind") == "call"]
        self.assertEqual(len(rows), 1, "a repeated settlement must not create a second charge")
        self.assertEqual(second["digest"], first["digest"], "the interrupted/repeated settlement returns the EXISTING row")
        self.assertAlmostEqual(m.accrued(), 0.4, places=9)

    def test_missing_usage_estimate_is_read_after_a_reload_not_from_a_stale_cache(self):
        job = self.tmp / "j4"
        m1 = _meter(job, 1.0)
        m2 = _meter(job, 1.0)  # a SECOND Meter constructed BEFORE the reserve (stale cache)
        r = m1.reserve(_body())
        self.assertGreater(m1.reservation_amount(r), 0.0, "fixture: the reservation has a positive worst case")
        row = m2.meter_call("maker", "m", "https://x", None,  # m2's cache predates the reserve
                            settle_reservation=r)  # usage missing -> billed at the reserved estimate
        self.assertAlmostEqual(row["cost"], m1.reservation_amount(r), places=9,
                               msg="the estimate must come from the RELOADED ledger, never a stale-cache $0")
        self.assertTrue(row.get("estimated"))


# ------------------------------------------------------------------ P1.2 anchor commits both ledgers
class P12_AnchorCommitsBothLedgers(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_fin12_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_legacy_three_field_anchor_still_verifies(self):
        job = self.tmp / "j3"
        m = _meter(job, 2.0)
        m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 10}, round_no=1)
        rows, head, total = m.anchor()[:3]
        self.assertTrue(m.verify(anchor=(rows, head, total)),
                        "a pre-2026-11 3-field anchor (legacy state.json) still verifies")


# ------------------------------------------------------------------ P1.3 provider limit portability + validation
class _CapturingProvider(http.server.BaseHTTPRequestHandler):
    bodies = []  # every body the proxy actually FORWARDED to the provider
    log_message = lambda self, *a, **k: None  # noqa: E731

    def do_POST(self):
        type(self).bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))))
        out = json.dumps({"usage": {"total_tokens": 10}, "choices": [{"message": {"content": "hi"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class P13_ProviderLimit(sb._Base):
    def setUp(self):
        if SANDBOX is None:
            self.skipTest("no OS sandbox available on this host")
        super().setUp()
        _CapturingProvider.bodies = []

    def _run_agent(self, script, meter):
        srv = sb._serve(_CapturingProvider)
        try:
            job = self._agent_job("fin_proxy", script, url=f"http://127.0.0.1:{srv.server_port}/v1/chat/completions")
            (job / "method.md").write_text("m", encoding="utf-8")
            (job / "packet.json").write_text("{}", encoding="utf-8")
            cfg = json.loads((job / "job.json").read_text())
            cfg["maker"]["sandbox"] = {"type": SANDBOX}
            adapters.run_maker(cfg["maker"], job / "packet.json", job / "artifact", ["memo.md"], job, meter=meter)
            return job
        finally:
            srv.shutdown()
            srv.server_close()

    def test_invalid_limits_are_refused_before_forwarding_in_both_families(self):
        # one sandbox launch exercises every invalid shape for BOTH families
        script = (
            "import json, os, pathlib, sys, urllib.error, urllib.request\n"
            "codes = []\n"
            "cases = [10.5, -5, 'abc', float('nan'), 2000000000]\n"
            "for fam in ('max_tokens', 'max_completion_tokens'):\n"
            "    for lim in cases:\n"
            "        body = json.dumps({'model': os.environ['AGENT_MODEL'], fam: lim,\n"
            "                           'messages': [{'role': 'user', 'content': 'draft'}]}).encode()\n"
            "        req = urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST')\n"
            "        try:\n"
            "            urllib.request.urlopen(req, timeout=10).read(); codes.append('200')\n"
            "        except urllib.error.HTTPError as e:\n"
            "            codes.append(str(e.code))\n"
            "body = json.dumps({'model': os.environ['AGENT_MODEL'], 'n': 2,\n"
            "                   'messages': [{'role': 'user', 'content': 'draft'}]}).encode()\n"
            "req = urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST')\n"
            "try:\n"
            "    urllib.request.urlopen(req, timeout=10).read(); codes.append('200')\n"
            "except urllib.error.HTTPError as e:\n"
            "    codes.append(str(e.code))\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(','.join(codes), encoding='utf-8')\n")
        job = self.tmp / "jbad"
        m = _meter(job, 1.0)
        agent_job = self._run_agent(script, m)
        codes = (agent_job / "artifact" / "memo.md").read_text().split(",")
        self.assertEqual(len(codes), 11, "fixture: five invalid limits per family plus n=2")
        self.assertTrue(all(c == "400" for c in codes), f"every invalid limit is a controlled 400 refusal, got {codes}")
        self.assertEqual(_CapturingProvider.bodies, [],
                         "NOTHING invalid was forwarded: the refusal happens BEFORE the provider sees the request")
        self.assertEqual(m.accrued(), 0.0, "a refused request bills nothing")

    def test_the_forwarded_body_carries_exactly_the_configured_family(self):
        script = (
            "import json, os, pathlib, sys, urllib.request\n"
            "body = json.dumps({'model': os.environ['AGENT_MODEL'], 'max_completion_tokens': 50,\n"
            "                   'messages': [{'role': 'user', 'content': 'draft'}]}).encode()\n"
            "urllib.request.urlopen(urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST'), timeout=10).read()\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text('ok', encoding='utf-8')\n")
        job = self.tmp / "jfam"
        m = _meter(job, 1.0)  # default family: max_tokens
        self._run_agent(script, m)
        self.assertEqual(len(_CapturingProvider.bodies), 1, "the valid call was forwarded")
        fwd = _CapturingProvider.bodies[0]
        self.assertEqual(fwd.get("max_tokens"), 50, "max_completion_tokens input is honored and emitted as the configured family")
        self.assertNotIn("max_completion_tokens", fwd, "the non-configured family is stripped from the forwarded body")

    def test_per_model_config_selects_the_emitted_family(self):
        script = (
            "import json, os, pathlib, sys, urllib.request\n"
            "body = json.dumps({'model': os.environ['AGENT_MODEL'],\n"
            "                   'messages': [{'role': 'user', 'content': 'draft'}]}).encode()\n"
            "urllib.request.urlopen(urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST'), timeout=10).read()\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text('ok', encoding='utf-8')\n")
        job = self.tmp / "jemit"
        m = _meter(job, 1.0, max_tokens_param="max_completion_tokens")  # bounds choose the family
        self._run_agent(script, m)
        fwd = _CapturingProvider.bodies[0]
        self.assertEqual(fwd.get("max_completion_tokens"), 100, "the priced allowance is emitted on the configured family")
        self.assertNotIn("max_tokens", fwd, "the default family is stripped when the config selects the other one")

    def test_request_over_the_priced_allowance_never_reaches_the_provider(self):
        script = (
            "import json, os, pathlib, sys, urllib.error, urllib.request\n"
            "codes = []\n"
            "for fam in ('max_tokens', 'max_completion_tokens'):\n"
            "    body = json.dumps({'model': os.environ['AGENT_MODEL'], fam: 1000000,\n"
            "                       'messages': [{'role': 'user', 'content': 'draft'}]}).encode()\n"
            "    req = urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST')\n"
            "    try:\n"
            "        urllib.request.urlopen(req, timeout=10).read(); codes.append('200')\n"
            "    except urllib.error.HTTPError as e:\n"
            "        codes.append(str(e.code))\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(','.join(codes), encoding='utf-8')\n")
        job = self.tmp / "jover"
        m = _meter(job, 1.0)
        agent_job = self._run_agent(script, m)
        self.assertEqual((agent_job / "artifact" / "memo.md").read_text(), "400,400",
                         "an oversized limit is refused in BOTH parameter families")
        self.assertEqual(_CapturingProvider.bodies, [],
                         "the oversized request never reached the provider (asserted on the CAPTURED body, not hits==[])")
        self.assertEqual(m.accrued(), 0.0)


# ------------------------------------------------------------------ P1.4 closing admission + finalize atomicity
class P14_ClosingAdmission(sb._Base):
    def test_two_concurrent_finalizers_exactly_one_wins_the_loser_refuses_by_name(self):
        job = self._make_base_job(subdir="fin_race")
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 0)
        results, errors = [], []

        def fin():
            try:
                results.append(loop.finalize_job(job, state="given", by="Casey Park"))
            except gates.GateError as e:
                errors.append(str(e))

        barrier = threading.Barrier(2)
        def fin_sync():
            barrier.wait()
            fin()

        threads = [threading.Thread(target=fin_sync) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertFalse(any(t.is_alive() for t in threads), "a competing finalizer deadlocked")
        self.assertEqual(sorted(results), [0], "exactly ONE finalizer wins")
        self.assertEqual(len(errors), 1, "the loser refuses")
        self.assertIn("already finalized", errors[0], "the loser's refusal names the closure")
        events = [json.loads(ln) for ln in (job / "job.jsonl").read_text().splitlines()]
        self.assertEqual(len([e for e in events if e["type"] == "disposition"]), 1,
                         "exactly one disposition event exists")


# ------------------------------------------------------------------ P2.5 dependency walk
class P25_DependencyWalk(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_fin25_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _cfg_of(self, job):
        # a minimal freeze-shaped cfg: gate_instruments only reads the instrument
        # files and the seat commands
        for name in ("bar.md", "rubric.md", "method.md", "runner.md"):
            (job / name).write_text(f"# {name}\n", encoding="utf-8")
        return {"bar": {"file": "bar.md"},
                "maker": {"name": "M", "kind": "script", "command": [sys.executable, "scripts/maker.py"],
                          "method": {"file": "method.md"}},
                "instrument": {"rubric": {"file": "rubric.md"}, "runner": {"file": "runner.md"}},
                "evaluators": []}

    def test_two_packages_importing_identically_named_helpers_are_both_pinned(self):
        job = self.tmp / "job"
        for d in ("scripts", "pkgA", "pkgB"):
            (job / d).mkdir(parents=True)
        (job / "bar.md").write_text("# Bar\n", encoding="utf-8")
        (job / "rubric.md").write_text("# Rubric\n", encoding="utf-8")
        (job / "method.md").write_text("# Method\n", encoding="utf-8")
        (job / "pkgA" / "__init__.py").write_text("", encoding="utf-8")
        (job / "pkgA" / "helper.py").write_text("VALUE = 'A'\n", encoding="utf-8")
        (job / "pkgB" / "__init__.py").write_text("", encoding="utf-8")
        (job / "pkgB" / "helper.py").write_text("VALUE = 'B'\n", encoding="utf-8")
        (job / "scripts" / "maker.py").write_text(
            "import json, os, pathlib, sys\n"
            "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/..')\n"
            "import pkgA.helper, pkgB.helper\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(pkgA.helper.VALUE + pkgB.helper.VALUE, encoding='utf-8')\n",
            encoding="utf-8")
        cfg = self._cfg_of(job)
        pins = gates.gate_instruments(cfg, job)
        base = dict(pins["tree"])
        (job / "pkgB" / "helper.py").write_text("VALUE = 'tampered-B'\n", encoding="utf-8")
        self.assertNotEqual(gates.gate_instruments(cfg, job)["tree"], base,
                            "pkgB.helper (identically named) must be pinned, not suppressed by pkgA's")
        (job / "pkgB" / "helper.py").write_text("VALUE = 'B'\n", encoding="utf-8")
        (job / "pkgA" / "helper.py").write_text("VALUE = 'tampered-A'\n", encoding="utf-8")
        self.assertNotEqual(gates.gate_instruments(cfg, job)["tree"], base,
                            "pkgA.helper must be pinned too")


# ------------------------------------------------------------------ P2.6 lifecycle lock
class P26_JobLifecycleLock(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_fin26_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_nested_hold_is_reentrant_and_depth_is_tracked(self):
        lk = loop._JobLifecycleLock(self.tmp / "job.lock")
        with lk.hold():
            self.assertEqual(lk._depth, 1)
            with lk.hold():
                self.assertEqual(lk._depth, 2, "nested holds reenter under the per-thread RLock")
            self.assertEqual(lk._depth, 1)
        self.assertEqual(lk._depth, 0)
        self.assertIsNone(lk._fd, "the flock and its fd are released at depth 0")

    def test_failed_flock_acquisition_leaves_the_next_caller_clean(self):
        import fcntl as fcntl_mod
        lk = loop._JobLifecycleLock(self.tmp / "job.lock")
        real_flock = fcntl_mod.flock
        calls = {"n": 0}

        def flaky(fd, op):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("injected acquisition failure")
            return real_flock(fd, op)

        fcntl_mod.flock = flaky
        try:
            with self.assertRaises(OSError):
                with lk.hold():
                    pass
        finally:
            fcntl_mod.flock = real_flock
        self.assertEqual(lk._depth, 0, "a failed acquisition leaves NO depth residue")
        self.assertIsNone(lk._fd, "a failed acquisition leaves NO fd residue")
        with lk.hold():  # the NEXT caller acquires cleanly
            self.assertEqual(lk._depth, 1)
            self.assertIsNotNone(lk._fd)


# ------------------------------------------------------------------ P2.7 packet key scanning
class P27_PacketKeyScan(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_fin27_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        (self.tmp / "bar.md").write_text("# Bar\n", encoding="utf-8")
        (self.tmp / "method.md").write_text("# Method\n", encoding="utf-8")

    def test_a_key_matching_the_artifact_text_is_STILL_caught(self):
        art = "the maker wrote held_out/secrets.txt here and quoted it back verbatim"
        pkt = {"round": 1, "mode": "pass_r2", "best_artifact_digest": None,
               "defects": [{"see held_out/private-case.txt for details": art}],
               "workable_solutions": [], "withheld": []}
        with self.assertRaises(gates.GateError,
                               msg="a KEY never inherits the quotation exemption, even when its text quotes the artifact"):
            gates.gate_packet(pkt, self.tmp, None, artifact_text=art)

    def test_a_feedback_VALUE_quoting_the_artifact_is_exempt(self):
        art = "the quick brown figure jumps over the lazy dataset boundary again and again"
        quote = art[4:40]  # a canonicalized substring of the incumbent artifact
        pkt = {"round": 1, "mode": "pass_r2", "best_artifact_digest": None,
               "defects": [{"observation": quote}],
               "workable_solutions": [], "withheld": []}
        gates.gate_packet(pkt, self.tmp, None, artifact_text=art)  # no raise: the VALUE exemption holds


if __name__ == "__main__":
    unittest.main()
