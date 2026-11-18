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

import gates  # noqa: E402
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


class BareFilenameOperand(unittest.TestCase):
    """Adversarial round (GPT 6.2-sol, 2026-11-18): a slash-less argv token that
    resolves to an existing file is a file operand and must be containment-checked,
    while long free-text data tokens must still pass harmlessly."""

    def setUp(self):
        import tempfile
        self.jdir = pathlib.Path(tempfile.mkdtemp(prefix="barename_"))
        (self.jdir / "scripts").mkdir()

    def test_bare_name_symlink_out_of_job_refused(self):
        import tempfile
        outside = pathlib.Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("x")
        (self.jdir / "input").symlink_to(outside)
        cfg = {"maker": {"kind": "script", "command": ["python3", "scripts/mk.py", "input"]}, "evaluators": []}
        with self.assertRaises(gates.GateError):
            gates.gate_commands(cfg, self.jdir)

    def test_long_data_token_does_not_crash(self):
        cfg = {"maker": {"kind": "script", "command": ["python3", "scripts/mk.py", "x" * 2000]}, "evaluators": []}
        gates.gate_commands(cfg, self.jdir)  # must not raise

    def test_long_slashful_data_token_takes_the_oserror_path(self):
        """A slashful over-long token exercises the OSError branch too (Opus 5.5, 2026-11-18)."""
        cfg = {"maker": {"kind": "script", "command": ["python3", "scripts/mk.py", "a/" + "x" * 300]}, "evaluators": []}
        gates.gate_commands(cfg, self.jdir)  # must not raise


if __name__ == "__main__":
    unittest.main()
