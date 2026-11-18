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


if __name__ == "__main__":
    unittest.main()
