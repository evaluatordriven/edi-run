"""Shared plumbing for the edi-run briefing tests (formerly the dated
2026-11-17/18 review-artifact files), re-homed by concern.

Import this module for the fixtures: PASSING, _Base, _R2Base and _serve.
It reuses test_edi's job builder without inheriting (and re-running) its tests.
"""
import http.server
import json
import sys
import threading
import unittest

import test_edi  # the existing suite's helpers (job builder, sys.path setup)

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


class _R2Base(_Base):
    def _sandboxed_agent_job(self, subdir, script, url="https://example.invalid/v1"):
        if test_edi._SANDBOX_TYPE is None:
            self.skipTest("no OS sandbox available on this host")
        job = self._agent_job(subdir, script, url=url)
        cfg = self._cfg(job)
        cfg["maker"]["sandbox"] = {"type": test_edi._SANDBOX_TYPE}
        self._save(job, cfg)
        return job

    def _memo_tail(self, job):
        return (job / "rounds" / "round-01" / "artifact" / "memo.md").read_text().splitlines()[-1]


def _serve(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
