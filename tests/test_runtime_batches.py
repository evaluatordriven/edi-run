"""Runtime batch additions: init scaffold, freeze --dry-run, auto-hash + AUTHORSHIP gate.

RED-first: written against behavior that did not exist yet. Standalone base-job
builder (structure mirrors tests/test_edi.py::TestEDIRuntime._make_base_job) so
this module does not re-run the inherited suite.
"""
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def run_cli(*args):
    return subprocess.run([sys.executable, "edi.py", *[str(a) for a in args]],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=120)


class RuntimeBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_rb_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_base_job(self, level="CL1", subdir="job"):
        """Structure mirror of TestEDIRuntime._make_base_job (CL1, script seats)."""
        job_dir = self.tmp / subdir
        if job_dir.exists():
            shutil.rmtree(job_dir)
        for d in ("held_out/controls", "held_out/planted", "evidence", "artifact", "scripts"):
            (job_dir / d).mkdir(parents=True)
        bar_path = job_dir / "bar.md"
        bar_path.write_text("# Bar\n- D1: Has header\n- D2: Has cited figure\n", encoding="utf-8")
        bar_digest = sha256_bytes(bar_path.read_bytes())
        (job_dir / "rubric.md").write_text("# Rubric\nCheck D1 header and D2 figure.\n", encoding="utf-8")
        (job_dir / "method.md").write_text("# Method\nDraft memo satisfying D1 and D2.\n", encoding="utf-8")
        (job_dir / "runner.md").write_text("# Runner\nedi-run v1\n", encoding="utf-8")
        (job_dir / "held_out/controls/clean.md").write_text(
            "# Header\nHeld-out reference control figure: 99% (Source: Control Audit, 2026-01-01)\n", encoding="utf-8")
        (job_dir / "held_out/controls/planted.md").write_text("No header and no figure here.\n", encoding="utf-8")
        maker_script = job_dir / "scripts" / "maker.py"
        maker_script.write_text(
            "import json, pathlib, sys\n"
            "out = pathlib.Path(sys.argv[2]) / 'memo.md'\n"
            "out.write_text('# Header\\nFigure: 42% (Source: Q3, 2026-10-01)\\n', encoding='utf-8')\n",
            encoding="utf-8")
        eval_script = job_dir / "scripts" / "eval_e1.py"
        eval_script.write_text(
            "import json, pathlib, sys\n"
            "pos = [a for a in sys.argv[1:] if not a.startswith('--')]\n"
            "target, dim = pathlib.Path(pos[0]), pos[1]\n"
            "txt = (target / 'memo.md').read_text() if target.is_dir() else target.read_text()\n"
            "score = 5.0 if (txt.startswith('# Header') if dim == 'D1' else 'Source:' in txt) else 1.0\n"
            "print(json.dumps({'score': score, 'observation': txt.strip()[:60], 'remedy': ''}))\n",
            encoding="utf-8")
        scale = {"anchors": {"1": "missing", "5": "present"}, "floor": 0.0, "threshold": 4.0}
        job_cfg = {
            "job": "test-memo-job", "levelClaimed": level, "specVersion": "0.9.8",
            "bounds": {"max_rounds": 2, "max_cost_usd": 1.0, "max_wall_seconds": 60},
            "packetMode": "pass_r2", "subjectFiles": ["memo.md"],
            "bar": {
                "file": "bar.md", "digest": {"sha256": bar_digest},
                "frozenAt": "2026-10-05T09:00:00Z", "author": "Jordan Blake",
                "dimensions": [
                    {"id": "D1", "criterion": "Starts with # Header", "scale": scale},
                    {"id": "D2", "criterion": "Cites Source:", "scale": scale},
                ],
                "approvals": [
                    {"role": "buyer", "by": "Casey Park", "barDigest": {"sha256": bar_digest},
                     "at": "2026-10-05T08:40:00Z", "adopted": None},
                    {"role": "specialist", "by": "Riley Moss", "barDigest": {"sha256": bar_digest},
                     "at": "2026-10-05T08:50:00Z", "adopted": None},
                ],
                "publishedAt": None, "groundTruth": [], "dissent": [],
            },
            "maker": {"name": "Alex Kim", "kind": "script", "version": "1.0.0",
                      "method": {"file": "method.md"}, "model": None,
                      "command": [sys.executable, str(maker_script)]},
            "instrument": {"rubric": {"file": "rubric.md"}, "runner": {"file": "runner.md"}},
            "evaluators": [{
                "id": "E1", "name": "Taylor Grant", "kind": "script", "model": None,
                "script": {"name": "eval_e1.py", "author": "Morgan Lee",
                           "digest": {"sha256": sha256_bytes(eval_script.read_bytes())}},
                "command": [sys.executable, str(eval_script)], "runs": 2,
            }],
            "controls": {"clean": "held_out/controls/clean.md", "planted": "held_out/controls/planted.md"},
            "retention": {"location": "local/records/test-memo-job", "public": False},
            "disposition": {"state": "given", "by": "Casey Park", "acknowledgesUnobserved": []},
        }
        (job_dir / "job.json").write_text(json.dumps(job_cfg, indent=2), encoding="utf-8")
        return job_dir


class TestInitScaffold(RuntimeBatchTest):
    def test_init_scaffolds_structure_only(self):
        job = self.tmp / "fresh"
        r = run_cli("init", job)
        self.assertEqual(r.returncode, 0, r.stderr)
        for d in ("scripts", "seed"):
            self.assertTrue((job / d).is_dir(), d)
        self.assertFalse((job / "rounds").exists(), "init must not create rounds/ (N14)")
        for f in ("bar.md", "rubric.md", "method.md"):
            self.assertTrue((job / f).is_file(), f)
            self.assertEqual((job / f).read_text(encoding="utf-8"), "", f)
        auth = (job / "AUTHORSHIP.md").read_text(encoding="utf-8")
        for key in ("Operator", "Bar author", "Rubric author", "Method author",
                    "Seed author", "Sponsor"):
            self.assertIn(f"{key}:", auth)
        for line in auth.splitlines():  # values blank: no invented attribution
            if line.strip():
                self.assertEqual(line.split(":", 1)[1].strip(), "", line)
        cfg = json.loads((job / "job.json").read_text(encoding="utf-8"))
        self.assertIn("subjectFiles", cfg)
        self.assertEqual(cfg["subjectFiles"], [])
        self.assertEqual(cfg["bar"]["approvals"], [])
        self.assertEqual(cfg["bar"]["dimensions"], [])
        self.assertIsNone(cfg["bar"]["digest"]["sha256"])
        self.assertIsNone(cfg["bar"]["frozenAt"])
        self.assertIsNone(cfg["maker"]["model"])
        self.assertIn("sandbox", cfg["maker"])
        self.assertIn("pricing", cfg)
        for p in job.rglob("*"):  # no starter controls, no substantive content
            if p.is_file():
                txt = p.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn("held_out", txt, p)
                if p.name.endswith(".md"):  # job.json may name schema fields only
                    self.assertNotIn("planted", txt.lower(), p)
                    self.assertNotIn("clean", txt.lower(), p)

    def test_init_refuses_nonempty_dir_exit_1(self):
        job = self.tmp / "occupied"
        job.mkdir()
        (job / "something.txt").write_text("keep me", encoding="utf-8")
        r = run_cli("init", job)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not empty", (r.stderr + r.stdout).lower())

    def test_init_allows_existing_empty_dir(self):
        job = self.tmp / "empty"
        job.mkdir()
        self.assertEqual(run_cli("init", job).returncode, 0)

    def test_init_usage_error_exit_2(self):
        self.assertEqual(run_cli("init").returncode, 2)


class TestFreezeDryRun(RuntimeBatchTest):
    def _snapshot(self, job_dir):
        snap = {}
        for p in sorted(job_dir.rglob("*")):
            if p.is_file():
                st = p.stat()
                snap[str(p.relative_to(job_dir))] = (st.st_size, st.st_mtime_ns,
                                                     sha256_bytes(p.read_bytes()))
            else:
                snap[str(p.relative_to(job_dir)) + "/"] = "dir"
        return snap

    def test_dry_run_passes_readonly_then_real_freeze_still_works(self):
        job_dir = self._make_base_job()
        before = self._snapshot(job_dir)
        r = run_cli("freeze", job_dir, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._snapshot(job_dir), before, "dry-run wrote to the job dir")
        self.assertFalse((job_dir / "job.jsonl").exists())
        self.assertFalse((job_dir / "state.json").exists())
        self.assertEqual(run_cli("freeze", job_dir).returncode, 0)

    def test_dry_run_fails_with_same_gate_message(self):
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["bar"]["digest"]["sha256"] = "0" * 64
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        r = run_cli("freeze", job_dir, "--dry-run")
        self.assertEqual(r.returncode, 1)
        self.assertIn("bar.md sha256 mismatch against bar.digest (CL1-14)", r.stderr)
        self.assertFalse((job_dir / "job.jsonl").exists())

    def test_dry_run_refuses_already_frozen_job(self):
        job_dir = self._make_base_job()
        self.assertEqual(run_cli("freeze", job_dir).returncode, 0)
        r = run_cli("freeze", job_dir, "--dry-run")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stderr)


class TestAutoHashAndAuthorship(RuntimeBatchTest):
    def test_freeze_computes_bar_digest_when_absent(self):
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["bar"]["digest"] = {"sha256": None}
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        r = run_cli("freeze", job_dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        computed = sha256_bytes((job_dir / "bar.md").read_bytes())
        events = [json.loads(l) for l in (job_dir / "job.jsonl").read_text().splitlines()]
        digests = [e["data"]["barDigest"] for e in events if e.get("type") == "approval"]
        self.assertTrue(digests and all(d == computed for d in digests))
        # corrected doctrine (live-aider-job10, 2026-11-18): the stamp is PERSISTED
        # into job.json, or the custody record assembles with a null bar digest
        self.assertEqual(json.loads((job_dir / "job.json").read_text())["bar"]["digest"]["sha256"], computed)

    def test_freeze_refuses_wrong_declared_digest(self):
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["bar"]["digest"]["sha256"] = "1" * 64
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        r = run_cli("freeze", job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CL1-14", r.stderr)

    def test_pinned_bardigest_fail_closed_on_mismatch(self):
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["barDigest"] = "2" * 64
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        r = run_cli("freeze", job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("pinned", r.stderr.lower())
        self.assertFalse((job_dir / "job.jsonl").exists())

    def test_pinned_bardigest_match_passes(self):
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["barDigest"] = sha256_bytes((job_dir / "bar.md").read_bytes())
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.assertEqual(run_cli("freeze", job_dir).returncode, 0)

    def test_authorship_gate_refuses_operator_as_bar_author(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator: Jordan Blake\nBar author: Jordan Blake\nRubric author:\n"
            "Method author:\nSeed author:\nSponsor:\n", encoding="utf-8")
        r = run_cli("freeze", job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CL1-6", r.stderr)
        self.assertFalse((job_dir / "job.jsonl").exists())

    def test_authorship_gate_case_insensitive_keys_and_values(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "OPERATOR: jordan blake\nbar author: Jordan  Blake\n", encoding="utf-8")
        r = run_cli("freeze", job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CL1-6", r.stderr)

    def test_authorship_gate_refuses_operator_as_method_author(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator: Dana Writer\nMethod author: Dana Writer\n", encoding="utf-8")
        r = run_cli("freeze", job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CL1-6", r.stderr)

    def test_authorship_blank_or_absent_is_allowed(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator:\nBar author: Jordan Blake\nRubric author:\nSponsor: Casey Park\n",
            encoding="utf-8")
        self.assertEqual(run_cli("freeze", job_dir).returncode, 0)
        job2 = self._make_base_job(subdir="job2")
        self.assertEqual(run_cli("freeze", job2).returncode, 0)

    def test_dry_run_enforces_authorship_gate(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator: Jordan Blake\nBar author: Jordan Blake\n", encoding="utf-8")
        r = run_cli("freeze", job_dir, "--dry-run")
        self.assertEqual(r.returncode, 1)
        self.assertIn("CL1-6", r.stderr)

    def test_init_refuses_regular_file_not_dir(self):
        blocker = self.tmp / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        r = run_cli("init", blocker)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a directory", r.stderr)

    def test_init_refuses_symlink_to_dir(self):
        target = self.tmp / "real_target"
        target.mkdir()
        link = self.tmp / "linked_job"
        link.symlink_to(target)
        r = run_cli("init", link)
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink", (r.stderr + r.stdout).lower())
        self.assertFalse((target / "job.json").exists(), "init wrote through the symlink")

    def test_init_refuses_dangling_symlink_cleanly(self):
        link = self.tmp / "dangling_job"
        link.symlink_to(self.tmp / "never_created")
        r = run_cli("init", link)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("symlink", (r.stderr + r.stdout).lower())


class TestAuthorshipBypasses(RuntimeBatchTest):
    """Each case was a self-dealing bypass: the gate used to pass where it must refuse."""

    def _freeze(self, job_dir):
        return run_cli("freeze", job_dir)

    def test_double_space_inside_key_no_longer_bypasses(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator: Jordan Blake\nBar  author: Jordan Blake\n", encoding="utf-8")
        r = self._freeze(job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CL1-6", r.stderr)

    def test_hyphen_key_variant_no_longer_bypasses(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator: Jordan Blake\nbar-author: Jordan Blake\n", encoding="utf-8")
        r = self._freeze(job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CL1-6", r.stderr)

    def test_duplicate_line_last_wins_rebind_refused(self):
        # last-wins used to let a later blank/alias line rebind Operator and
        # launder the self-dealing declaration; first-wins closes it.
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator: Jordan Blake\nBar author: Jordan Blake\nOperator:\n",
            encoding="utf-8")
        r = self._freeze(job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CL1-6", r.stderr)

    def test_zero_width_space_in_value_no_longer_bypasses(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator: Jordan Blake\nBar author: Jor​dan Blake\n",
            encoding="utf-8")
        r = self._freeze(job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CL1-6", r.stderr)

    def test_continuation_line_folded_into_previous_key(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator: Jordan Blake\nBar author:\n  Jordan Blake\n", encoding="utf-8")
        r = self._freeze(job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CL1-6", r.stderr)

    def test_job_json_bar_author_crosschecked_against_operator(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator: Jordan Blake\nBar author:\n", encoding="utf-8")
        r = self._freeze(job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("bar.author", r.stderr)

    def test_job_json_maker_name_crosschecked_against_operator(self):
        job_dir = self._make_base_job()
        (job_dir / "AUTHORSHIP.md").write_text(
            "Operator: Alex Kim\nBar author: Jordan Blake\n", encoding="utf-8")
        r = self._freeze(job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("maker.name", r.stderr)


class TestFreezePreflightDryRunParity(RuntimeBatchTest):
    """A real-path KeyError (missing cfg key) used to crash freeze AFTER appends
    were written while --dry-run exited 0. Both must fail cleanly BEFORE writes."""

    def _drop_first_approval_by(self, job_dir):
        cfg = json.loads((job_dir / "job.json").read_text())
        del cfg["bar"]["approvals"][0]["by"]
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def test_missing_approval_by_fails_both_dry_run_and_real_before_writes(self):
        job_dir = self._make_base_job()
        self._drop_first_approval_by(job_dir)
        r_dry = run_cli("freeze", job_dir, "--dry-run")
        self.assertEqual(r_dry.returncode, 1, "dry-run must be an oracle: fail like the real path")
        self.assertNotIn("Traceback", r_dry.stderr)
        self.assertFalse((job_dir / "job.jsonl").exists())
        r_real = run_cli("freeze", job_dir)
        self.assertEqual(r_real.returncode, 1)
        self.assertNotIn("Traceback", r_real.stderr)
        self.assertFalse((job_dir / "job.jsonl").exists(),
                         "refused freeze must not leave a partial log (I-01 no-trace)")

    def test_missing_bar_author_fails_both_before_writes(self):
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["bar"]["author"] = None
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        r_dry = run_cli("freeze", job_dir, "--dry-run")
        self.assertEqual(r_dry.returncode, 1)
        r_real = run_cli("freeze", job_dir)
        self.assertEqual(r_real.returncode, 1)
        self.assertFalse((job_dir / "job.jsonl").exists())

    def test_missing_maker_name_fails_both_before_writes(self):
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["maker"]["name"] = None
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        r_dry = run_cli("freeze", job_dir, "--dry-run")
        self.assertEqual(r_dry.returncode, 1)
        r_real = run_cli("freeze", job_dir)
        self.assertEqual(r_real.returncode, 1)
        self.assertFalse((job_dir / "job.jsonl").exists())

    def test_missing_seed_author_fails_both_before_writes(self):
        job_dir = self._make_base_job()
        seed_dir = job_dir / "seed_src"
        seed_dir.mkdir()
        (seed_dir / "memo.md").write_text("# Header\nFigure: 1% (Source: x)\n", encoding="utf-8")
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["seed"] = {"dir": "seed_src", "author": "Jordan Blake", "origin": "", "note": ""}
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        # sanity: a well-formed seed author freezes fine (CL1-6 seats hold)
        self.assertEqual(run_cli("freeze", job_dir).returncode, 0)
        job2 = self._make_base_job(subdir="job2")
        seed2 = job2 / "seed_src"
        seed2.mkdir()
        (seed2 / "memo.md").write_text("# Header\nFigure: 1% (Source: x)\n", encoding="utf-8")
        cfg = json.loads((job2 / "job.json").read_text())
        cfg["seed"] = {"dir": "seed_src", "origin": "", "note": ""}  # author missing
        (job2 / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        r_dry = run_cli("freeze", job2, "--dry-run")
        self.assertEqual(r_dry.returncode, 1)
        r_real = run_cli("freeze", job2)
        self.assertEqual(r_real.returncode, 1)
        self.assertFalse((job2 / "job.jsonl").exists())


class TestPinnedBarDigestShape(RuntimeBatchTest):
    def _pin(self, job_dir, value):
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["barDigest"] = value
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def test_empty_string_pin_refused(self):
        job_dir = self._make_base_job()
        self._pin(job_dir, "")
        r = run_cli("freeze", job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("barDigest", r.stderr)
        self.assertFalse((job_dir / "job.jsonl").exists())

    def test_empty_dict_pin_refused(self):
        job_dir = self._make_base_job()
        self._pin(job_dir, {})
        r = run_cli("freeze", job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("barDigest", r.stderr)

    def test_null_pin_refused_when_key_present(self):
        job_dir = self._make_base_job()
        self._pin(job_dir, None)
        r = run_cli("freeze", job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("barDigest", r.stderr)

    def test_non_hex_pin_refused(self):
        job_dir = self._make_base_job()
        self._pin(job_dir, "not-a-digest")
        r = run_cli("freeze", job_dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("barDigest", r.stderr)

    def test_uppercase_hex_pin_accepted_and_matched(self):
        job_dir = self._make_base_job()
        self._pin(job_dir, sha256_bytes((job_dir / "bar.md").read_bytes()).upper())
        self.assertEqual(run_cli("freeze", job_dir).returncode, 0)


class TestCliGateRefusalFormatting(RuntimeBatchTest):
    def test_dry_run_on_bare_scaffold_prints_one_line_refusal(self):
        job = self.tmp / "bare"
        self.assertEqual(run_cli("init", job).returncode, 0)
        r = run_cli("freeze", job, "--dry-run")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        err_lines = [l for l in r.stderr.splitlines() if l.strip()]
        self.assertEqual(len(err_lines), 1, r.stderr)
        self.assertTrue(err_lines[0].startswith("gate refusal:"), r.stderr)


class TestJobSnapshotDirs(unittest.TestCase):
    def test_snapshot_records_directory_mtime_ns(self):
        import loop
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            (d / "sub").mkdir()
            snap = loop._job_snapshot(d)
            self.assertEqual(len(snap["sub/"]), 2)
            self.assertEqual(snap["sub/"][0], "dir")
            self.assertIsInstance(snap["sub/"][1], int)


if __name__ == "__main__":
    unittest.main()
