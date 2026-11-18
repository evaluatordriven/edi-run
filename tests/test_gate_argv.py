"""A command argv DATA token must never crash gate_commands (live-aider-job).

Found live 2026-11-18: a long task-text argv token made gate_commands stat a
path with ENAMETOOLONG -> unhandled OSError instead of a clean refusal.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_edi  # noqa: E402
from test_runtime_batches import run_cli  # noqa: E402


class DataTokens(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="edi_gate_argv_")

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def test_a_long_data_token_in_the_maker_command_does_not_crash_the_gate(self):
        t = test_edi.TestEDIRuntime("test_freeze_then_run_script_maker_ok") if hasattr(test_edi.TestEDIRuntime, "test_freeze_then_run_script_maker_ok") else None
        # Build a base job via the runtime_batches helper instead.
        from test_runtime_batches import TestInitScaffold
        s = TestInitScaffold("test_init_allows_existing_empty_dir")
        s.setUp()
        try:
            job = s._make_base_job(subdir="job")
            cfg = json.loads((job / "job.json").read_text())
            long_task = "Do the work. " * 200  # far beyond NAME_MAX for any component
            cfg["maker"]["command"] = [sys.executable, "scripts/maker.py", long_task]
            (job / "job.json").write_text(json.dumps(cfg))
            r = run_cli("freeze", job, "--dry-run")
            self.assertIn(r.returncode, (0, 1), f"gate crashed: {r.stderr[-300:]}")
            self.assertNotIn("File name too long", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
        finally:
            s.tearDown()

    def test_an_out_of_job_path_token_still_refused(self):
        from test_runtime_batches import TestInitScaffold
        s = TestInitScaffold("test_init_allows_existing_empty_dir")
        s.setUp()
        try:
            job = s._make_base_job(subdir="job2")
            outside = pathlib.Path(self.t) / "evil.py"
            outside.write_text("x", encoding="utf-8")
            cfg = json.loads((job / "job.json").read_text())
            cfg["maker"]["command"] = [sys.executable, "scripts/maker.py", str(outside)]
            (job / "job.json").write_text(json.dumps(cfg))
            r = run_cli("freeze", job, "--dry-run")
            self.assertEqual(r.returncode, 1)
            self.assertIn("G1", r.stderr)
        finally:
            s.tearDown()


if __name__ == "__main__":
    unittest.main()
