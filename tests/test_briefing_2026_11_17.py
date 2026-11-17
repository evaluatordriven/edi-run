"""Must-fail tests for the 2026-11-17 briefing to the edi-run maintainer.

Drop this file into tests/ next to test_edi.py. It reuses that file's job builder.
Run:  python3 -m unittest discover -s tests -p "test_briefing_2026_11_17.py" -v

Every test here is red on the reviewed snapshot except test_4d, which is marked
expectedFailure: it documents a hole that closes only with OS-level confinement.
When confinement lands it will report "unexpected success" -- remove the decorator then.

Tests named test_r*_ depend on an Owner ruling (see the briefing's "Owner rulings").
Drop them if the Owner rules the other way.
"""
import http.server
import json
import sys
import threading
import unittest

import test_edi  # the existing suite's helpers (job builder, sys.path setup)
import gates
import loop

PASSING = "# Header\\nFigure: 42% (Source: Q3, 2026-10-01)\\n"


class _Base(unittest.TestCase):
    # Borrow the existing suite's fixtures without inheriting (and re-running) its tests.
    setUp = test_edi.TestEDIRuntime.setUp
    tearDown = test_edi.TestEDIRuntime.tearDown
    _make_base_job = test_edi.TestEDIRuntime._make_base_job

    def _cfg(self, job):
        return json.loads((job / "job.json").read_text())

    def _save(self, job, cfg):
        (job / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def _maker(self, job, body):
        """Replace the scripted Maker. `body` maps round -> memo text (Python literal source)."""
        (job / "scripts" / "maker.py").write_text(
            "import json, pathlib, sys\n"
            "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
            f"texts = {body}\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(texts[pkt['round']], encoding='utf-8')\n",
            encoding="utf-8",
        )

    def _events(self, job):
        return [json.loads(line) for line in (job / "job.jsonl").read_text().splitlines()]


# ---------------------------------------------------------------- Finding 1
class Finding1SeedProvenance(_Base):
    def test_1_seed_winner_is_credited_to_seed_author_in_the_log(self):
        """The artifact-produced event the record binds to must name the same maker as the record."""
        job = self._make_base_job(rounds_behavior="regress_r2", subdir="f1")
        (job / "seed").mkdir()
        (job / "seed" / "memo.md").write_text("# Header\nNo figure yet.\n", encoding="utf-8")
        cfg = self._cfg(job)
        cfg["seed"] = {"dir": "seed", "author": "Dana Voss", "origin": "Owner draft", "note": ""}
        self._save(job, cfg)
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 2)  # round 2 regresses, cap reached
        st = json.loads((job / "state.json").read_text())
        self.assertEqual((st["incumbent_round"], st["reason"]), (1, "cap_reached"))  # the seed won
        loop.finalize_job(job, state="given", by="Casey Park")
        rec = json.loads((job / "record.json").read_text())
        ev = self._events(job)
        last_prod = [e for e in ev if e["type"] == "artifact-produced"][-1]
        self.assertEqual(rec["predicate"]["maker"]["name"], "Dana Voss")
        self.assertEqual(last_prod["actor"], rec["predicate"]["maker"]["name"],
                         "custody log credits a different maker than the record for the shipped artifact")


# ---------------------------------------------------------------- Finding 2
class Finding2HeldOutCheck(_Base):
    def test_2a_prefix_collision_is_filtered_not_fatal(self):
        """D4.9: held-out material is filtered out of the next round's input. A candidate line that
        shares a 24-char prefix with a held-out file must not end the job."""
        job = self._make_base_job(subdir="f2a")
        self._maker(job, "{1: '# Header\\nHeld-out reference control figure is still pending\\n', 2: '" + PASSING + "'}")
        loop.freeze_job(job)
        try:
            rc = loop.run_job(job)
        except gates.GateError as e:
            self.fail(f"job killed by held-out heuristic instead of filtering the packet: {e}")
        self.assertEqual(rc, 0)
        pkt = (job / "rounds" / "round-02" / "packet.json").read_text().casefold()
        self.assertNotIn("held-out reference contr", pkt)  # filtered, not forwarded

    def test_2b_gate_error_during_run_is_not_labelled_interrupted(self):
        """D4.11: the Owner must not be told a gate failure was a Ctrl-C."""
        job = self._make_base_job(subdir="f2b")
        (job / "seed").mkdir()
        (job / "seed" / "memo.md").write_text("# Header\n" + "Figure: 42% (Source: Q3, 2026-10-01)\n", encoding="utf-8")
        cfg = self._cfg(job)
        cfg["seed"] = {"dir": "seed", "author": "Dana Voss", "origin": "", "note": ""}
        self._save(job, cfg)
        loop.freeze_job(job)
        (job / "seed" / "memo.md").write_text("# Header\nTAMPERED\n", encoding="utf-8")
        with self.assertRaises(gates.GateError):
            loop.run_job(job)
        st = json.loads((job / "state.json").read_text())
        esc = (job / "escalation.json").read_text()
        self.assertNotEqual(st["reason"], "interrupted")
        self.assertNotIn("Ctrl-C", esc)


# ---------------------------------------------------------------- Finding 3
class Finding3DoctrineConformance(_Base):
    def test_3a_next_round_gets_every_known_defect(self):
        """D4.9 / D5.step.5: the next round receives every known defect with its measurement,
        including defects measured on a revision that did not replace the incumbent."""
        job = self._make_base_job(subdir="f3a")
        cfg = self._cfg(job)
        cfg["bounds"]["max_rounds"] = 3
        self._save(job, cfg)
        self._maker(job, "{1: '# Header\\nNo figure yet.\\n', 2: 'Broken both\\n', 3: '" + PASSING + "'}")
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 0)
        st = json.loads((job / "state.json").read_text())
        self.assertEqual(st["rounds"]["2"]["dim_scores"]["D1"], 1.0)  # round 2 measured a D1 defect
        pkt3 = json.loads((job / "rounds" / "round-03" / "packet.json").read_text())
        self.assertTrue(any(d["dimension"] == "D1" and d.get("round") == 2 and "score" in d for d in pkt3["defects"]),
                        f"round 2's measured D1 defect was dropped from round 3's input: {pkt3['defects']}")

    def test_3b_default_is_two_scored_runs(self):
        """D4.5: a verdict rests on at least two scored runs; one run is a sample."""
        job = self._make_base_job(subdir="f3b")
        cfg = self._cfg(job)
        cfg["evaluators"][0].pop("runs", None)
        self._save(job, cfg)
        loop.freeze_job(job)
        loop.run_job(job)
        st = json.loads((job / "state.json").read_text())
        for r, rd in st["rounds"].items():
            for d in ("D1", "D2"):
                n = sum(1 for c in rd["checks"] if c["dimension"] == d and c["evaluator"] == "E1")
                self.assertGreaterEqual(n, 2, f"round {r} {d}: {n} scored run(s)")

    def test_3b_freeze_refuses_a_single_run(self):
        job = self._make_base_job(subdir="f3b1")
        cfg = self._cfg(job)
        cfg["evaluators"][0]["runs"] = 1
        self._save(job, cfg)
        with self.assertRaises(gates.GateError):
            loop.freeze_job(job)

    def test_3c_escalation_says_what_was_tried_and_why(self):
        """D4.11: an escalation carries what was tried, why no observable answer exists,
        and options with a recommendation. Key names `tried` and `why` are proposals."""
        job = self._make_base_job(rounds_behavior="tradeoff", subdir="f3c")
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 2)
        esc = json.loads((job / "escalation.json").read_text())
        self.assertEqual(esc["reason"], "dimension_tradeoff")
        tried = esc.get("tried") or []
        self.assertEqual([t.get("round") for t in tried], [1, 2])
        self.assertTrue(all(set(t.get("dimScores", {})) == {"D1", "D2"} for t in tried))
        why = json.dumps(esc.get("why"))
        self.assertIn("D1", why)
        self.assertIn("D2", why)
        self.assertTrue(esc.get("options") and esc.get("recommendation"))


# ---------------------------------------------------------------- Finding 4
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


class Finding4AgentMaker(_Base):
    def _agent_job(self, subdir, script, level="CL1", url="https://example.invalid/v1"):
        job = self._make_base_job(level=level, subdir=subdir)
        agent = job / "scripts" / "agent.py"
        agent.write_text(script, encoding="utf-8")
        cfg = self._cfg(job)
        cfg["pricing"] = {"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}
        cfg["maker"] = {"name": "Agent Maker", "kind": "agent", "version": "1", "method": {"file": "method.md"},
                        "url": url,
                        "model": {"family": "mock", "identifier": "mock-agent", "endpoint": "127.0.0.1", "readOnly": False},
                        "command": [sys.executable, str(agent)]}
        self._save(job, cfg)
        return job

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


if __name__ == "__main__":
    unittest.main()
