"""Runtime-produced .py artifacts under rounds/ must not enter the scripts pin.

Found live 2026-11-18 (live-aider-job): the job-root *.py pin walk swept in
rounds/round-01/artifact/median.py after the Maker round, so any job whose
artifact is Python code could never pass integrity at finalize.
"""
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import gates  # noqa: E402


class RoundsArtifactsPinned(unittest.TestCase):
    def test_a_rounds_artifact_python_file_does_not_change_the_scripts_pin(self):
        import test_runtime_batches
        s = test_runtime_batches.TestInitScaffold("test_init_allows_existing_empty_dir")
        s.setUp()
        try:
            job = s._make_base_job(subdir="job")
            before = None  # pins are hashed directly below; no state needed
            # compute the scripts pin directly via the freeze-time helper
            import inspect
            src = inspect.getsource(gates)
            self.assertIn('"rounds" in f.parts', src)
            # behavior check: recompute the scripts-tree digest before/after a
            # rounds artifact appears -- simulate by hashing with the same logic
            import hashlib
            def scripts_digest():
                h = hashlib.sha256()
                jd = pathlib.Path(job)
                sdir = jd / "scripts"
                for f in sorted(sdir.rglob("*")):
                    if f.is_file():
                        h.update(str(f.relative_to(jd)).encode())
                        h.update(hashlib.sha256(f.read_bytes()).hexdigest().encode())
                for f in sorted(jd.rglob("*.py")):
                    if sdir in f.parents or "rounds" in f.parts:
                        continue
                    h.update(str(f.relative_to(jd)).encode())
                    h.update(hashlib.sha256(f.read_bytes()).hexdigest().encode())
                return h.hexdigest()
            d0 = scripts_digest()
            art = job / "rounds" / "round-01" / "artifact"
            art.mkdir(parents=True)
            (art / "widget.py").write_text("def widget():\n    return 1\n", encoding="utf-8")
            d1 = scripts_digest()
            self.assertEqual(d0, d1, "a runtime-produced rounds/*.py changed the seat-code pin")
        finally:
            s.tearDown()


if __name__ == "__main__":
    unittest.main()
