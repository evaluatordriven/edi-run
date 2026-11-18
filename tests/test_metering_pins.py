"""Spend / metering / pin findings from the round-3 briefing (2026-11-18), re-homed
by concern (was tests/test_briefing_r3.py). P1 = job settings pinned across
freeze/run/finalize; P2/P3/P7/P9 = spend metering and the usage-log chain.

Model seats talk to a loopback fake server (test_edi._MockLLMHandler); no real
API is called.
"""
import json
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import support_briefing as sb
import test_edi
import gates
import loop
from metering import Meter

PASS = sb.PASSING
ROOT = pathlib.Path(loop.__file__).resolve().parent


def _model_eval(port, ident="mock-1"):
    return {"id": "E2", "name": "Model Judge", "kind": "model", "script": None, "runs": 2,
            "url": f"http://127.0.0.1:{port}/v1/chat/completions",
            "model": {"family": "mock", "identifier": ident, "endpoint": "127.0.0.1", "readOnly": True}}


class _Counting(test_edi._MockLLMHandler):
    n = 0

    def do_POST(self):
        type(self).n += 1
        return super().do_POST()


class _Base(sb._Base):
    def _hard_kill_in_round_2(self, subdir):
        """Run a 3-round job in a child process and SIGKILL it once round 2 is in flight.
        Returns (job, marker): writing `marker` makes the round-2 maker succeed on a retry."""
        job = self._make_base_job(subdir=subdir)
        cfg = self._cfg(job)
        cfg["bounds"]["max_rounds"] = 3
        self._save(job, cfg)
        marker = pathlib.Path(tempfile.mkdtemp(prefix="edi_r3_")) / "second_try"
        self.addCleanup(lambda: marker.unlink(missing_ok=True))
        (job / "scripts" / "maker.py").write_text(
            "import json, pathlib, sys, time\n"
            "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
            f"flag = pathlib.Path({str(marker)!r})\n"
            "if pkt['round'] == 1:\n"
            "    txt = '# Header\\nNo figure yet.\\n'\n"
            "elif not flag.exists():\n"
            "    time.sleep(60)\n"
            "    txt = 'x'\n"
            "else:\n"
            "    txt = '" + PASS + "'\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(txt)\n", encoding="utf-8")
        loop.freeze_job(job)
        p = subprocess.Popen([sys.executable, "-c", f"import sys; sys.path[:0]=[{str(ROOT)!r}]; import loop; loop.run_job({str(job)!r})"], cwd=str(ROOT))
        try:
            for _ in range(300):
                time.sleep(0.05)
                if json.loads((job / "state.json").read_text()).get("open_round") == 2:
                    break
            time.sleep(0.3)
        finally:
            p.send_signal(signal.SIGKILL)
            p.wait()
        st = json.loads((job / "state.json").read_text())
        self.assertEqual((st.get("open_round"), st.get("terminal_state"), st.get("incumbent_round")), (2, None, 1), "fixture: kill did not land in round 2")
        return job, marker


# ------------------------------------------------------------------ P1
class P1_JobSettingsPinned(_Base):
    """CL1-1: the bar is frozen before work. The dimensions, thresholds and evaluator wiring the loop
    scores against live in job.json; they must be as frozen as bar.md."""

    def _edits(self):
        def drop_d2(c): c["bar"]["dimensions"] = [d for d in c["bar"]["dimensions"] if d["id"] != "D2"]
        def lower_threshold(c): c["bar"]["dimensions"][0]["scale"]["threshold"] = 0.5
        def evaluator_args(c): c["evaluators"][0]["command"] = c["evaluators"][0]["command"] + ["--always-pass"]
        def raise_cap(c): c["bounds"]["max_cost_usd"] = c["bounds"]["max_cost_usd"] * 100
        return (drop_d2, lower_threshold, evaluator_args, raise_cap)

    def test_p1a_run_refuses_job_settings_changed_after_freeze(self):
        for edit in self._edits():
            with self.subTest(edit=edit.__name__):
                job = self._make_base_job(subdir="p1a_" + edit.__name__)
                loop.freeze_job(job)
                cfg = self._cfg(job); edit(cfg); self._save(job, cfg)
                with self.assertRaises(gates.GateError):
                    loop.run_job(job)

    def test_p1b_finalize_refuses_job_settings_changed_after_run(self):
        job = self._make_base_job(subdir="p1b")
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 0)
        cfg = self._cfg(job)
        cfg["bar"]["dimensions"] = [d for d in cfg["bar"]["dimensions"] if d["id"] != "D2"]
        self._save(job, cfg)
        with self.assertRaises(gates.GateError):
            loop.finalize_job(job, state="given", by="Casey Park")


# ------------------------------------------------------------------ P2 P3 P7 P9 (spend)
class Spend(_Base):
    def test_p2_resume_keeps_the_killed_rounds_spend_on_the_cap(self):
        """README: 'a kill cannot erase billed work'. Discarded work is still billed work."""
        job = self._make_base_job(subdir="p2")
        cfg = self._cfg(job)
        cfg["pricing"] = {"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}
        cfg["bounds"]["max_cost_usd"] = 1.0
        self._save(job, cfg)
        loop.freeze_job(job)
        Meter(job, pricing=cfg["pricing"], bounds=cfg["bounds"]).meter_call(
            "maker", "m", "https://x", None, usage={"prompt_tokens": 450000, "completion_tokens": 450000}, round_no=1)
        st = json.loads((job / "state.json").read_text()); st["open_round"] = 1
        (job / "state.json").write_text(json.dumps(st))
        loop.resume_job(job)
        st = json.loads((job / "state.json").read_text())
        self.assertAlmostEqual(st["cost_usd"], 0.90, places=6, msg="the discarded round's $0.90 vanished from the cap total")

    def test_p3_an_evaluator_cap_trip_ends_cap_reached(self):
        srv = sb._serve(test_edi._MockLLMHandler)
        try:
            job = self._make_base_job(subdir="p3")
            cfg = self._cfg(job)
            cfg["evaluators"].append(_model_eval(srv.server_port))
            cfg["pricing"] = {"mock-1": {"input_usd_per_mtok": 1000.0, "output_usd_per_mtok": 1000.0}}
            cfg["bounds"]["max_cost_usd"] = 0.0001
            self._save(job, cfg)
            loop.freeze_job(job)
            loop.run_job(job)
            st = json.loads((job / "state.json").read_text())
            self.assertEqual((st["terminal_state"], st["reason"]), ("needs_owner", "cap_reached"))
        finally:
            srv.shutdown(); srv.server_close()

    def _cl3_job(self, subdir, port):
        job = self._make_base_job(level="CL3", subdir=subdir)
        cfg = self._cfg(job)
        cfg["evaluators"].append(_model_eval(port))
        cfg["pricing"] = {"mock-1": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}
        self._save(job, cfg)
        return job

    def test_p7a_dry_run_makes_no_paid_calls(self):
        _Counting.n = 0
        srv = sb._serve(_Counting)
        try:
            job = self._cl3_job("p7a", srv.server_port)
            self.assertEqual(loop.freeze_job(job, dry_run=True), 0)
            self.assertEqual(_Counting.n, 0, "freeze --dry-run billed model calls")
        finally:
            srv.shutdown(); srv.server_close()

    def test_p7b_cl3_demonstration_calls_are_metered(self):
        _Counting.n = 0
        srv = sb._serve(_Counting)
        try:
            job = self._cl3_job("p7b", srv.server_port)
            loop.freeze_job(job)
            rows = [json.loads(ln) for ln in (job / "usage.jsonl").read_text().splitlines()] if (job / "usage.jsonl").is_file() else []
            self.assertGreater(_Counting.n, 0)
            self.assertEqual(len([r for r in rows if r.get("kind") == "call"]), _Counting.n, "CL3 demonstration calls were not metered")
        finally:
            srv.shutdown(); srv.server_close()

    def test_p9_finalize_refuses_a_truncated_usage_log(self):
        """The spend log's hash chain must be anchored, or deleting its newest rows goes unseen."""
        srv = sb._serve(test_edi._MockLLMHandler)
        try:
            job = self._make_base_job(subdir="p9")
            cfg = self._cfg(job)
            cfg["evaluators"].append(_model_eval(srv.server_port))
            cfg["pricing"] = {"mock-1": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}
            self._save(job, cfg)
            loop.freeze_job(job)
            loop.run_job(job)
            lines = (job / "usage.jsonl").read_text().splitlines()
            self.assertGreater(len(lines), 2)
            (job / "usage.jsonl").write_text("\n".join(lines[:-2]) + "\n")
            with self.assertRaises(gates.GateError):
                loop.finalize_job(job, state="given", by="Casey Park")
        finally:
            srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
