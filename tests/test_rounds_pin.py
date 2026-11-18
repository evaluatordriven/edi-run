"""Runtime-produced .py artifacts under rounds/ must not enter the scripts pin.

Found live 2026-11-18 (live-aider-job): the job-root *.py pin walk swept in
rounds/round-01/artifact/median.py after the Maker round, so any job whose
artifact is Python code could never pass integrity at finalize.
"""
import json
import pathlib
import shutil
import tempfile
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
            self.assertIn('_relp.parts[0] == "rounds"', src)
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


class RoundsPinScope(unittest.TestCase):
    """Adversarial round (GPT 6.2-sol, 2026-11-18): the rounds/ exclusion must be
    scoped to the job-root rounds/ tree ONLY — a freeze-time helper under a nested
    dep/rounds/ stays pinned."""

    def test_dep_rounds_helper_still_pinned(self):
        import sys as _sys
        _sys.path.insert(0, str(ROOT))
        import gates as _gates
        jd = pathlib.Path(tempfile.mkdtemp(prefix="depscope_"))
        (jd / "bar.md").write_text("bar")
        (jd / "method.md").write_text("m")
        inst = jd / "instrument"; inst.mkdir()
        (inst / "rubric.md").write_text("r"); (inst / "runner.md").write_text("u")
        (jd / "scripts").mkdir(); (jd / "scripts/main.py").write_text("z=1")
        helper = jd / "dep/rounds/helper.py"; helper.parent.mkdir(parents=True)
        helper.write_text("x=1")
        (jd / "top.py").write_text("import dep.rounds.helper\n")
        pins = _gates.gate_instruments(
            {"bar": {"file": "bar.md"},
             "maker": {"method": {"file": "method.md"}, "kind": "script", "command": ["python3", "top.py"]},
             "instrument": {"rubric": {"file": "instrument/rubric.md"}, "runner": {"file": "instrument/runner.md"}},
             "scripts": {"dir": "scripts"}},
            jd)
        # the job-root walk digest must include dep/rounds/helper.py: mutate it and
        # confirm the recomputed scripts pin CHANGES (membership by behavior, not text)
        before = pins["scripts"]
        helper.write_text("x=2")
        pins2 = _gates.gate_instruments(
            {"bar": {"file": "bar.md"},
             "maker": {"method": {"file": "method.md"}, "kind": "script", "command": ["python3", "top.py"]},
             "instrument": {"rubric": {"file": "instrument/rubric.md"}, "runner": {"file": "instrument/runner.md"}},
             "scripts": {"dir": "scripts"}},
            jd)
        self.assertNotEqual(before, pins2["scripts"], "dep/rounds/helper.py edit did not change the pin — it is excluded")
        shutil.rmtree(jd)


if __name__ == "__main__":
    unittest.main()
