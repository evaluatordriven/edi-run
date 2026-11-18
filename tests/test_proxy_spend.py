"""NOTE-to-maintainer round-3 (2026-11-18) spend/concurrency fixes, by concern.

(a) parallel agent calls cannot overshoot the cap: spend is RESERVED under the
    meter lock before each proxy forward;
(b) the proxy refuses stream:true with a clear error (metering streams is out
    of scope — a streamed call would be recorded as free).
"""
import http.server
import json
import pathlib
import sys
import tempfile
import threading
import unittest

import loop
from metering import CapExceeded, Meter

import test_edi

_SANDBOX_TYPE = test_edi._SANDBOX_TYPE


class TestParallelCallsCannotOvershootTheCap(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_r3_race_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _meter(self, cap):
        # round-3 external review #1: a reservation must bound OUTPUT spend too,
        # so the meter carries a max_output_tokens bound (100 tokens at $1/Mtok
        # = $0.0001 of output worst case per call).
        return Meter(self.tmp / "race-job", pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}},
                     bounds={"max_cost_usd": cap, "max_output_tokens": 100})

    def test_eight_parallel_reserves_cannot_all_pass_a_cap_one_call_would_breach(self):
        """Eight simultaneous agent calls used to all pass check_cap (each saw the
        pre-race accrued total) and blow through the cap. Reservation must hold
        the worst-case spend (request PLUS max output) under the lock BEFORE
        forwarding, so concurrent reserves cannot all fit."""
        m = self._meter(0.001)
        body = json.dumps({"messages": [{"role": "user", "content": "x" * 800}]}).encode()  # est ~0.0003 with output
        refused, passed = [], []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            try:
                passed.append(m.reserve(body))
            except CapExceeded:
                refused.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 8 x ~0.0003 = 0.0024 > the 0.001 cap: at least four reserves must refuse
        est = m.estimate(body)
        self.assertGreaterEqual(len(refused), 4,
                                "parallel reserves all passed: concurrent calls can overshoot the cap")
        self.assertLessEqual(len(passed) * est + m.accrued(), 0.001 + 1e-12,
                             "reserved + accrued exceeds the cap")
        for rsv in passed:
            m.release(rsv)
        self.assertEqual(m._reserved, 0.0)  # nothing leaked: releases return capacity exactly

    def test_settled_calls_never_push_accrued_past_the_cap(self):
        m = self._meter(0.0009)
        body = json.dumps({"messages": [{"role": "user", "content": "x" * 800}]}).encode()  # est ~0.0003 with output
        ok = 0
        for _ in range(12):  # simulate 12 back-to-back parallel-style reserve/settle cycles
            try:
                rsv = m.reserve(body)
            except CapExceeded:
                break
            ok += 1
            m.meter_call("maker", "m", "https://x", None,
                         usage={"prompt_tokens": 200, "completion_tokens": 0},  # actual = the request part only
                         settle_reservation=rsv)
        self.assertGreater(ok, 0)
        self.assertLessEqual(m.accrued(), 0.0009, "settled spend exceeded the cap")
        self.assertEqual(m._reserved, 0.0)

    def test_release_restores_capacity(self):
        m = self._meter(0.0007)
        body = json.dumps({"messages": [{"role": "user", "content": "x" * 800}]}).encode()
        r1 = m.reserve(body)  # ~0.0003 (request + output)
        m.reserve(body)  # second fits: ~0.0006 < 0.0007
        with self.assertRaises(CapExceeded):
            m.reserve(body)  # a third full-worst-case reservation breaches the cap
        m.release(r1)
        m.reserve(body)  # capacity restored by the release


class TestProxyRefusesStreaming(unittest.TestCase):
    def test_stream_true_is_refused_with_a_clear_error_and_never_metered(self):
        if _SANDBOX_TYPE is None:
            self.skipTest("no OS sandbox available on this host")
        seen = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))))
                out = json.dumps({"model": "m", "usage": {"total_tokens": 10},
                                  "choices": [{"message": {"content": "hi"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_r3_stream_"))
            self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
            factory = test_edi.TestEDIRuntime.__dict__["_make_base_job"].__get__(self)
            job = factory(subdir="job_stream")
            (job / "seed").mkdir()
            (job / "seed" / "memo.md").write_text("# Header\nNo figure yet.\n", encoding="utf-8")
            agent = job / "scripts" / "agent.py"
            agent.write_text(
                "import json, os, pathlib, sys, urllib.error, urllib.request\n"
                "pkt_path, art = sys.argv[1], sys.argv[2]\n"
                "body = json.dumps({'model': os.environ['AGENT_MODEL'], 'stream': True,\n"
                "                   'messages': [{'role': 'user', 'content': 'draft'}]}).encode()\n"
                "req = urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST')\n"
                "try:\n"
                "    with urllib.request.urlopen(req, timeout=10) as r: code = str(r.status)\n"
                "except urllib.error.HTTPError as e:\n"
                "    code = str(e.code)\n"
                "    detail = e.read().decode('utf-8', 'replace')\n"
                "except Exception as e:\n"
                "    code, detail = 'EXC', str(e)\n"
                "else:\n"
                "    detail = ''\n"
                "pathlib.Path(art, 'memo.md').write_text('# Header\\\\nFigure: 42% (Source: Q3, 2026-10-01)\\\\n'\n"
                "    + 'STREAM_STATUS=' + code + '\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            cfg = json.loads((job / "job.json").read_text())
            cfg["pricing"] = {"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}
            cfg["seed"] = {"dir": "seed", "author": "Dana Voss", "origin": "Owner draft", "note": ""}
            cfg["maker"] = {"name": "Agent Maker", "kind": "agent", "version": "1.0.0", "method": {"file": "method.md"},
                            "url": f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                            "model": {"family": "mock", "identifier": "mock-agent", "endpoint": "127.0.0.1", "readOnly": False},
                            "sandbox": {"type": _SANDBOX_TYPE},
                            "command": [sys.executable, str(agent)]}
            (job / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            loop.freeze_job(job)
            loop.run_job(job)
            memo = (job / "rounds" / "round-02" / "artifact" / "memo.md").read_text()
            self.assertIn("STREAM_STATUS=400", memo,
                          "the proxy served a stream:true call it cannot meter (expected a 400 refusal)")
            self.assertEqual(seen, [], "a stream:true call reached the upstream unbilled")
            proxy_log = (job / "rounds" / "proxy-usage.jsonl")
            if proxy_log.is_file():
                refused = [json.loads(ln) for ln in proxy_log.read_text().splitlines()]
                self.assertTrue(any("stream" in str(r.get("refused", "")) for r in refused),
                                "the refused stream call left no audit trace in proxy-usage.jsonl")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
