"""Probe-gate and CI wiring (NOTE round 3 items e, f).

(e) the probe gate's S1 entry applies to macOS only: on Linux the allowlist
    already passes it, so running it there only printed a STALE KNOWN-FAIL;
(f) CI FAILS (never skips) when no sandbox is available, so a green run means
    the sandbox was actually tested.
"""
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent


class TestProbeS1IsMacOSOnly(unittest.TestCase):
    def test_s1_skips_on_non_darwin_hosts(self):
        import probes.run_probes as rp
        old = rp.OS
        try:
            rp.OS = "linux"
            status, detail = rp.check_s1()
            self.assertEqual(status, "SKIP",
                             "S1 must skip on Linux: the allowlist already passes it there")
        finally:
            rp.OS = old

    def test_s1_still_runs_on_darwin_hosts(self):
        import probes.run_probes as rp
        source = (REPO / "probes" / "run_probes.py").read_text(encoding="utf-8")
        # the darwin path is reached before the skip: the darwin guard is the
        # FIRST check in check_s1
        body = source.split("def check_s1():", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('if OS != "darwin":', body)
        self.assertLess(body.index('if OS != "darwin":'), body.index("agent_job"))


class TestCIFailsWithoutSandbox(unittest.TestCase):
    def test_ci_requires_a_working_sandbox_on_both_legs(self):
        yml = (REPO / ".github" / "workflows" / "ci.yml")
        text = yml.read_text(encoding="utf-8")
        self.assertEqual(text.count("sandbox_usable"), 2,
                         "both the Linux and macOS legs must require a working sandbox")
        self.assertNotIn("continue-on-error", text)
        # the hard check must run the runtime's own sandbox_usable probe, whose
        # failure exits nonzero — a skip is no longer possible
        for stype in ("bwrap", "sandbox-exec"):
            self.assertIn(f"sandbox_usable('{stype}')", text)


if __name__ == "__main__":
    unittest.main()
