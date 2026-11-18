"""Freeze must persist the auto-hashed bar digest into job.json (CR-2/CL1-3/CL1-14).

Found live 2026-11-18 (live-aider-job10): the auto-hash stamped the digest only
in memory; the custody record assembled from job.json at finalize carried a
null bar digest and failed CR-2 / CL1-3 / CL1-14.
"""
import hashlib
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


class BarDigestStamp(unittest.TestCase):
    def test_freeze_stamps_bar_digest_into_job_json(self):
        import test_runtime_batches
        s = test_runtime_batches.TestInitScaffold("test_init_allows_existing_empty_dir")
        s.setUp()
        try:
            job = s._make_base_job(subdir="job")
            cfg = json.loads((job / "job.json").read_text())
            cfg["bar"]["digest"] = {"sha256": None}
            (job / "job.json").write_text(json.dumps(cfg))
            from test_runtime_batches import run_cli
            r = run_cli("freeze", job)
            self.assertEqual(r.returncode, 0, r.stderr[-200:])
            stamped = json.loads((job / "job.json").read_text())["bar"]["digest"]["sha256"]
            want = hashlib.sha256((job / "bar.md").read_bytes()).hexdigest()
            self.assertEqual(stamped, want, "freeze did not persist the bar digest into job.json")
        finally:
            s.tearDown()

    def test_dry_run_stamps_nothing(self):
        import test_runtime_batches
        s = test_runtime_batches.TestInitScaffold("test_init_allows_existing_empty_dir")
        s.setUp()
        try:
            job = s._make_base_job(subdir="job2")
            cfg = json.loads((job / "job.json").read_text())
            cfg["bar"]["digest"] = {"sha256": None}
            (job / "job.json").write_text(json.dumps(cfg))
            from test_runtime_batches import run_cli
            r = run_cli("freeze", job, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr[-200:])
            still = json.loads((job / "job.json").read_text())["bar"]["digest"]["sha256"]
            self.assertIsNone(still, "dry-run wrote the bar digest to job.json (zero-write oracle broken)")
        finally:
            s.tearDown()


def _prep(tmpname):
    import test_runtime_batches
    s = test_runtime_batches.TestInitScaffold("test_init_allows_existing_empty_dir")
    s.setUp()
    try:
        job = s._make_base_job(subdir=tmpname)
        cfg = json.loads((job / "job.json").read_text())
        cfg["bar"]["digest"] = {"sha256": None}
        (job / "job.json").write_text(json.dumps(cfg))
        return s, job
    except Exception:
        s.tearDown()
        raise


class StampSafety(unittest.TestCase):
    """Opus 5.5 deep review (2026-11-18): a refused freeze must never mutate
    job.json; the persist is atomic and happens only after every gate passes."""

    def test_refused_refreeze_leaves_job_json_byte_identical(self):
        s, job = _prep("job3")
        try:
            from test_runtime_batches import run_cli
            r1 = run_cli("freeze", job)
            self.assertEqual(r1.returncode, 0, r1.stderr[-200:])
            before = (job / "job.json").read_bytes()
            r2 = run_cli("freeze", job)
            self.assertEqual(r2.returncode, 1)
            self.assertIn("freeze runs once", r2.stderr)
            self.assertEqual((job / "job.json").read_bytes(), before, "a refused re-freeze mutated job.json")
        finally:
            s.tearDown()

    def test_failed_freeze_leaves_job_json_byte_identical(self):
        s, job = _prep("job4")
        try:
            from test_runtime_batches import run_cli
            # break a gate AFTER the in-memory stamp (CL1-3 approval mismatch in gate_freeze_pre)
            good = (job / "bar.md").read_bytes()
            (job / "bar.md").write_bytes(good + b"- D3 unapproved edit\n")
            before = (job / "job.json").read_bytes()
            r = run_cli("freeze", job)
            self.assertEqual(r.returncode, 1)
            self.assertEqual((job / "job.json").read_bytes(), before, "a failed freeze mutated job.json")
            (job / "bar.md").write_bytes(good)
            r_ok = run_cli("freeze", job)
            self.assertEqual(r_ok.returncode, 0, f"corrected retry failed: {r_ok.stderr}")
        finally:
            s.tearDown()


class R4Refusals(unittest.TestCase):
    """Opus 5.5 deep review (2026-11-18): verify without a record and finalize
    without state must be clean gate refusals — exit 1, no traceback, no writes."""

    def test_verify_without_record_refuses_cleanly(self):
        import subprocess
        s, job = _prep("job5")
        try:
            r = subprocess.run([sys.executable, "edi.py", "verify", str(job)], cwd=str(ROOT), capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("gate refusal", r.stderr)
        finally:
            s.tearDown()

    def test_finalize_without_state_refuses_cleanly(self):
        import subprocess
        s, job = _prep("job6")
        try:
            r = subprocess.run([sys.executable, "edi.py", "finalize", str(job), "--disposition", "declined", "--by", "X"], cwd=str(ROOT), capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("gate refusal", r.stderr)
            self.assertFalse((job / "job.lock").exists(), "refused finalize wrote a lockfile into an unfrozen dir")
        finally:
            s.tearDown()


if __name__ == "__main__":
    unittest.main()
