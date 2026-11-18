"""Verification re-review (round 3, /tmp/edi_r3_verification_gpt62sol.md) —
one red-first test per blocking item, numbered as the re-review numbers them.

Run:  python3 -m unittest discover -s tests -p "test_verification_fixes.py" -v
"""
import http.server
import json
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import unittest

import test_edi  # sets up sys.path and the shared fixtures
import support_briefing as sb

import adapters
import gates
import loop
from metering import CapExceeded, Meter

PASS = sb.PASSING
SANDBOX = test_edi._SANDBOX_TYPE
PRICING = {"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}


def _meter(job, cap, out_tokens=100):
    return Meter(job, pricing=PRICING, bounds={"max_cost_usd": cap, "max_output_tokens": out_tokens})


def _body(n=400):
    return json.dumps({"messages": [{"role": "user", "content": "x" * n}]}).encode()


def _dead_pid():
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = p.pid
    p.wait()
    return pid


def _resv_rows(job):
    return [json.loads(ln) for ln in (job / "usage.resv.jsonl").read_text().splitlines()]


def _usage_rows(job):
    return [json.loads(ln) for ln in (job / "usage.jsonl").read_text().splitlines()]


def _rechain(path, rows, digest_fn):
    prev = ""
    for r in rows:
        r.pop("digest", None)
        r.pop("prev", None)
        r["prev"] = prev
        r["digest"] = digest_fn(r)
        prev = r["digest"]
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


# ------------------------------------------------------------------ blockers 1 + 2
class Blocker1_ReservationIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_vfx_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_duplicate_release_after_settle_retires_nothing(self):
        m = _meter(self.tmp / "j", 1.0)
        r = m.reserve(_body())
        m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 400000},
                     settle_reservation=r)
        before = _resv_rows(self.tmp / "j")
        with self.assertRaises(ValueError, msg="a duplicate release must be refused, never retire another hold"):
            m.release(r)
        after = _resv_rows(self.tmp / "j")
        self.assertEqual(len([x for x in after if x.get("kind") == "release"]),
                         len([x for x in before if x.get("kind") == "release"]),
                         "the duplicate release must retire nothing (no second release row)")
        self.assertTrue(any(x.get("kind") == "anomaly" for x in after), "the anomaly is logged")
        self.assertAlmostEqual(m.accrued(), 0.4, places=9)

    def test_release_of_an_unknown_id_refuses_and_logs(self):
        m = _meter(self.tmp / "j", 1.0)
        with self.assertRaises(ValueError):
            m.release("deadbeef" * 4)
        self.assertTrue(any(x.get("kind") == "anomaly" for x in _resv_rows(self.tmp / "j")))

    def test_two_equal_reserves_settle_by_identity_not_amount(self):
        # the original defect: settle-by-amount let B's settlement retire A's hold
        m = _meter(self.tmp / "j", 1.0)
        ra, rb = m.reserve(_body()), m.reserve(_body())
        self.assertNotEqual(ra, rb, "reservation ids are unique")
        m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 400000},
                     settle_reservation=rb)  # B settles first
        self.assertEqual(m.reservation_amount(ra) and m._reserved > 0.0,
                         True, "A's hold must survive B's settlement")
        m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 400000},
                     settle_reservation=ra)
        self.assertEqual(m._reserved, 0.0)


class Blocker2_IdentityReconciliation(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_vfx2_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _mark_dead(self, job):
        # shared helper: rechews BOTH ledgers with their cross-anchors remapped
        import test_external_r3_fixes
        test_external_r3_fixes._mark_resv_pids_dead(job)

    def test_one_usage_row_cannot_settle_two_reservations(self):
        job = self.tmp / "job"
        m1 = _meter(job, 2.0)
        r_a = m1.reserve(_body())
        m1.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 400000},
                      settle_reservation=r_a)  # the one $0.40 usage row, id'd to A
        r_b = m1.reserve(_body())  # an UNRELATED reservation (any amount)
        self._mark_dead(job)
        m2 = _meter(job, 2.0)
        # A: matched by id -> released. B: no usage row of its own -> recovered
        # as spend ONCE. The $0.40 row can never settle B too.
        releases = {}
        for r in _resv_rows(job):
            if r.get("kind") == "release":
                releases.setdefault(r["id"], []).append(r.get("state"))
        self.assertEqual(releases.get(r_a), ["released"], "A released exactly once, by identity")
        self.assertEqual(releases.get(r_b), ["recovered"], "B recovered exactly once, never settled by A's row")
        self.assertAlmostEqual(m2.accrued(), 0.4 + m1.reservation_amount(r_b), places=9,
                               msg="A's bill + B's recovery, and nothing double-counted")
        self.assertEqual(len([r for r in _usage_rows(job) if r.get("recovered_reservation") == r_b]), 1,
                         "B is recovered by exactly one row")

    def test_a_usage_row_without_a_reservation_id_never_settles_a_matching_amount(self):
        import metering
        job = self.tmp / "job_amount"
        m1 = _meter(job, 2.0)
        m1.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 400000},
                      round_no=1)  # a real $0.40 usage row with NO reservation id
        # a dead-process reservation whose worst-case amount is EXACTLY $0.40:
        # under cost+timestamp matching this row would settle it (the defect)
        # estimate measures len(json.dumps(body.decode())): the body string is
        # re-serialized AS A STRING (two extra quote chars), so calibrate on a probe
        # estimate measures len(json.dumps(body.decode())): the body string is
        # re-serialized AS A STRING, so its JSON quotes get escaped — calibrate
        # the constant empirically and keep the assertion as the guard
        probe = json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode()
        delta = len(json.dumps(probe.decode())) - len(probe)  # escape+quote overhead on re-dump
        big = json.dumps({"messages": [{"role": "user", "content": "x" * (1599600 - delta - (len(probe) - 1))}]}).encode()
        assert abs(m1.estimate(big) - 0.4) < 1e-12, "fixture: the reservation amount must be exactly $0.40"
        m1.reserve(big)
        self._mark_dead(job)
        m2 = _meter(job, 2.0)
        releases = {r["id"]: r.get("state") for r in _resv_rows(job) if r.get("kind") == "release"}
        rid = [r["id"] for r in _resv_rows(job) if r.get("kind") == "reserve"][0]
        self.assertEqual(releases.get(rid), "recovered",
                         "a $0.40 usage row without a reservation id must NOT settle an equal-amount reservation")
        self.assertAlmostEqual(m2.accrued(), 0.4 + 0.4, places=9,
                               msg="the real bill + the recovered reservation, never one row for both")

    def test_crash_between_usage_fsync_and_release_fsync_does_not_double_charge(self):
        import metering
        job = self.tmp / "job2"
        m1 = _meter(job, 2.0)
        r = m1.reserve(_body())
        # simulate the crash window: the usage row (with the reservation id) is
        # fsynced, the release row is NOT yet written
        with m1._file_lock(), m1._lock:
            m1._reload_locked()
            m1._append_locked({"kind": "call", "who": "maker", "model": "m", "endpoint": "https://x",
                               "usage": {"total_tokens": 400000}, "cost": 0.4, "round": 1,
                               "reservation_id": r})
        self._mark_dead(job)
        m2 = _meter(job, 2.0)  # recovery: matches the row BY ID, retires the hold, recovers NOTHING
        self.assertAlmostEqual(m2.accrued(), 0.4, places=9,
                               msg="the bill must not be charged twice (row + recovery)")
        releases = [r0.get("state") for r0 in _resv_rows(job) if r0.get("kind") == "release" and r0.get("id") == r]
        self.assertEqual(releases, ["released"], "the hold is retired exactly once, by identity")
        self.assertEqual([r0 for r0 in _usage_rows(job) if r0.get("recovered_reservation")], [],
                         "no synthetic recovery row was appended")


# ------------------------------------------------------------------ blocker 3
class Blocker3_JointLedgerValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_vfx3_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_usage_rollback_past_a_settled_reservation_refuses(self):
        import metering
        job = self.tmp / "job"
        m = _meter(job, 2.0)
        r = m.reserve(_body())
        m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 400000},
                     settle_reservation=r)  # the release row now names the settled usage digest
        rows = _usage_rows(job)
        kept = [x for x in rows if not (x.get("kind") == "call" and x.get("reservation_id") == r)]
        self.assertLess(len(kept), len(rows), "fixture: the settled call row is dropped")
        _rechain(job / "usage.jsonl", kept, metering._row_digest)  # a VALID shorter chain
        with self.assertRaises(ValueError, msg="usage may not roll back past a settled reservation"):
            Meter(job, pricing=PRICING, bounds={"max_cost_usd": 2.0, "max_output_tokens": 100})

    def test_reservation_ledger_truncation_below_a_usage_row_refuses(self):
        import metering
        job = self.tmp / "job2"
        m = _meter(job, 2.0)
        m.reserve(_body())
        m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 10}, round_no=1)
        # the usage row anchors the reservation ledger's head; truncate the
        # reservation ledger to a valid EMPTY chain
        _rechain(job / "usage.resv.jsonl", [], metering._row_digest)
        (job / "usage.resv.jsonl").unlink()
        with self.assertRaises(ValueError, msg="a truncated reservation ledger is detected, not accepted"):
            Meter(job, pricing=PRICING, bounds={"max_cost_usd": 2.0, "max_output_tokens": 100})

    def test_usage_rollback_below_an_open_reservation_refuses(self):
        import metering
        job = self.tmp / "job3"
        m = _meter(job, 2.0)
        m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 10}, round_no=1)
        m.reserve(_body())  # the open reservation anchors the usage head
        _rechain(job / "usage.jsonl", [], metering._row_digest)
        (job / "usage.jsonl").unlink()
        with self.assertRaises(ValueError, msg="an open reservation's usage head must still exist"):
            Meter(job, pricing=PRICING, bounds={"max_cost_usd": 2.0, "max_output_tokens": 100})


# ------------------------------------------------------------------ blocker 4
class _Provider(http.server.BaseHTTPRequestHandler):
    status = 200
    usage = None  # None -> omit the envelope entirely
    bodies = []  # every body actually FORWARDED to the provider (final round: the
                 # oversized-request assertion must be on the captured body, not hits==[])
    log_message = lambda self, *a, **k: None  # noqa: E731

    def do_POST(self):
        type(self).bodies.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        body = {}
        if type(self).usage is not None:
            body["usage"] = type(self).usage
        body["choices"] = [{"message": {"content": "hi"}}]
        out = json.dumps(body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class Blocker4_ProxyBoundEnforcement(sb._Base):
    def _run_agent(self, script, meter):
        srv = sb._serve(_Provider)
        try:
            job = self._agent_job("vfx_proxy", script, url=f"http://127.0.0.1:{srv.server_port}/v1/chat/completions")
            (job / "method.md").write_text("m", encoding="utf-8")
            (job / "packet.json").write_text("{}", encoding="utf-8")
            cfg = json.loads((job / "job.json").read_text())
            cfg["maker"]["sandbox"] = {"type": SANDBOX}
            adapters.run_maker(cfg["maker"], job / "packet.json", job / "artifact", ["memo.md"], job, meter=meter)
            return job
        finally:
            srv.shutdown()
            srv.server_close()

    def setUp(self):
        if SANDBOX is None:
            self.skipTest("no OS sandbox available on this host")
        super().setUp()

    def test_request_demanding_more_than_the_priced_allowance_is_refused(self):
        hits = []
        _Provider.hits = hits
        _Provider.bodies = []  # reset per test: class-level capture across tests
        script = (
            "import json, os, pathlib, sys, urllib.error, urllib.request\n"
            "body = json.dumps({'model': os.environ['AGENT_MODEL'], 'max_tokens': 1000000,"
            " 'messages': [{'role': 'user', 'content': 'draft'}]}).encode()\n"
            "req = urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST')\n"
            "try:\n"
            "    r = urllib.request.urlopen(req, timeout=10); code, detail = str(r.status), r.read().decode()\n"
            "except urllib.error.HTTPError as e:\n"
            "    code, detail = str(e.code), e.read().decode()\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(code + '|' + detail, encoding='utf-8')\n")
        m = _meter(self.tmp / "j", 1.0)
        job = self._run_agent(script, m)
        memo = (job / "artifact" / "memo.md").read_text()
        self.assertTrue(memo.startswith("400"), f"the oversized request must be refused, got {memo[:40]}")
        self.assertIn("priced allowance", memo, "the error names the bound it enforces")
        self.assertEqual(hits, [], "the oversized request never reached the provider")
        self.assertEqual(_Provider.bodies, [],
                         "asserted on the CAPTURED forwarded body: nothing carrying the oversized limit was forwarded")
        self.assertEqual(m.accrued(), 0.0, "a refused request bills nothing")

    def test_missing_usage_is_billed_at_the_reserved_estimate(self):
        _Provider.status, _Provider.usage = 200, None
        script = (
            "import json, os, pathlib, sys, urllib.request\n"
            "body = json.dumps({'model': os.environ['AGENT_MODEL'],"
            " 'messages': [{'role': 'user', 'content': 'draft'}]}).encode()\n"
            "urllib.request.urlopen(urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST'), timeout=10).read()\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text('ok', encoding='utf-8')\n")
        job = self.tmp / "jest"
        m = _meter(job, 1.0)
        self._run_agent(script, m)
        rows = _usage_rows(job)
        self.assertEqual(len(rows), 1, "the 200-with-missing-usage call is recorded, not skipped")
        self.assertTrue(rows[0].get("estimated"), "the row is flagged estimated=true")
        self.assertTrue(rows[0].get("usage_missing"))
        self.assertGreater(rows[0]["cost"], 0.0, "billed_at_estimate: never $0")
        self.assertAlmostEqual(rows[0]["cost"], m.reservation_amount(rows[0]["reservation_id"]), places=9,
                               msg="the row carries the reserved estimate")
        self.assertTrue(rows[0].get("reservation_id"), "the row records the reservation id it settled")
        self.assertEqual(m._reserved, 0.0, "the hold is settled, not left dangling")

    def test_non_200_releases_without_charge_and_latches_nothing(self):
        _Provider.status, _Provider.usage = 500, {"total_tokens": 10}
        script = (
            "import json, os, pathlib, sys, urllib.error, urllib.request\n"
            "for i in range(2):\n"
            "    body = json.dumps({'model': os.environ['AGENT_MODEL'],"
            " 'messages': [{'role': 'user', 'content': 'draft'}]}).encode()\n"
            "    req = urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST')\n"
            "    try:\n"
            "        urllib.request.urlopen(req, timeout=10).read()\n"
            "    except urllib.error.HTTPError:\n"
            "        pass\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text('done', encoding='utf-8')\n")
        job = self.tmp / "jerr"
        m = _meter(job, 1.0)
        self._run_agent(script, m)
        self.assertEqual(m.accrued(), 0.0, "an upstream error is not billed")
        self.assertEqual(m._reserved, 0.0, "the hold is released")
        rows = _usage_rows(job) if (job / "usage.jsonl").is_file() else []
        self.assertTrue(all(r.get("kind") != "call" for r in rows),
                        "no usage row for an unbilled error")
        cap = []
        for r in _resv_rows(job):
            if r.get("kind") == "release":
                cap.append(r)
        self.assertTrue(cap, "the reservation was released through the ledger")

    def test_duplicate_json_properties_in_a_request_body_are_refused(self):
        script = (
            "import os, pathlib, sys, urllib.error, urllib.request\n"
            "body = b\"{'model': 'x'}\".replace(b\"'\", b'\"')\n"
            "body = b'{\"model\": \"mock-agent\", \"model\": \"mock-agent\", \"messages\": []}'\n"
            "req = urllib.request.Request(os.environ['AGENT_PROXY_URL'], data=body, method='POST')\n"
            "try:\n"
            "    r = urllib.request.urlopen(req, timeout=10); code = str(r.status)\n"
            "except urllib.error.HTTPError as e:\n"
            "    code = str(e.code)\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(code, encoding='utf-8')\n")
        m = _meter(self.tmp / "jdup", 1.0)
        job = self._run_agent(script, m)
        self.assertEqual((job / "artifact" / "memo.md").read_text(), "400",
                         "a body with a duplicate JSON property is refused at the parse boundary")


# ------------------------------------------------------------------ blocker 5
class Blocker5_FinalizeLifecycleTransaction(sb._Base):
    def test_concurrent_round_append_during_finalize_loses_cleanly(self):
        job = self._make_base_job(subdir="vfx_race")
        loop.freeze_job(job)
        Meter(job, pricing=PRICING).meter_call("maker", "m", "https://x", None,
                                               usage={"total_tokens": 1000}, round_no=1)  # a spend log so the finalize transaction includes the spend append
        self.assertEqual(loop.run_job(job), 0)
        # a competing writer issues its round-append while finalize HOLDS the
        # job lock: it must block, then lose cleanly (refused by the in-lock
        # disposition scan) once finalize closes the job — never interleave
        orig = loop.custody_record.append
        outcome = []

        def hooked(log_path, event):
            if event["type"] == "spend" and not outcome:
                outcome.append("spend-seen")
                def racer():
                    try:
                        loop._append(job, "audit-note", "racer", [], {"n": 1})  # a record-neutral event: the check payload family is bound by make_record
                        outcome.append("racer-succeeded")
                    except gates.GateError as e:
                        outcome.append(f"racer-refused: {e}")
                threading.Thread(target=racer).start()
                time.sleep(0.3)  # the racer is now parked on the job lock
            return orig(log_path, event)

        loop.custody_record.append = hooked
        try:
            self.assertEqual(loop.finalize_job(job, state="given", by="Casey Park"), 0)
        finally:
            loop.custody_record.append = orig
        time.sleep(2.0)  # the racer acquires the lock right after finalize releases it
        events = self._events(job)
        types = [e["type"] for e in events]
        self.assertEqual(types[-1], "disposition", "the disposition is the last event")
        self.assertTrue(outcome[0] == "spend-seen")
        refusals = [o for o in outcome if o.startswith("racer-refused")]
        self.assertTrue(refusals and all("disposition" in o for o in refusals),
                        f"the concurrent append must lose cleanly citing the disposition, got {outcome}")
        self.assertNotIn("racer-succeeded", outcome, "an append after the disposition must not succeed")
        self.assertNotIn("audit-note", types, "the refused append wrote nothing")

    def test_run_final_writes_nothing_after_a_disposition(self):
        job = self._make_base_job(subdir="vfx_fin")
        cfg = self._cfg(job)
        cfg["bounds"]["max_rounds"] = 2
        self._save(job, cfg)
        marker = pathlib.Path(tempfile.mkdtemp(prefix="edi_vfx5_")) / "go"
        self.addCleanup(lambda: marker.unlink(missing_ok=True))
        (job / "scripts" / "maker.py").write_text(
            "import json, pathlib, sys, time\n"
            "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
            f"flag = pathlib.Path({str(marker)!r})\n"
            "while not flag.exists():\n"
            "    time.sleep(0.05)\n"
            "txt = '# Header\\nNo figure yet.\\n' if pkt['round'] == 1 else '" + PASS + "'\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(txt)\n", encoding="utf-8")
        loop.freeze_job(job)
        result = {}

        def runner():
            try:
                result["rc"] = loop.run_job(job)
            except Exception as e:  # noqa: BLE001
                result["err"] = e

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        try:
            for _ in range(200):
                time.sleep(0.05)
                if json.loads((job / "state.json").read_text()).get("open_round"):
                    break
            time.sleep(0.2)
            before = (job / "state.json").read_text()
            loop._append(job, "disposition", "Casey Park", [], {"state": "given", "acknowledgesUnobserved": []})
            marker.write_text("")
            t.join(60)
            self.assertEqual((job / "state.json").read_text(), before,
                             "the run's finally wrote state AFTER a disposition (blocked by the job lock + scan)")
            self.assertFalse((job / "escalation.json").is_file(),
                             "no escalation package after a disposition")
        finally:
            marker.write_text("")
            t.join(5)


# ------------------------------------------------------------------ blocker 6
class Blocker6_LockOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_vfx6_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_verify_append_and_reserve_concurrent_never_deadlock(self):
        m = _meter(self.tmp / "j", 10.0)
        stop = threading.Event()
        errors = []

        def verifier():
            try:
                while not stop.is_set():
                    with m.file_lock():  # flock outer...
                        m.refresh()      # ...thread lock inner
                    m.meter_call("maker", "m", "https://x", None, usage={"total_tokens": 10}, round_no=1)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def reserver():
            try:
                while not stop.is_set():
                    try:
                        rid = m.reserve(_body())
                    except CapExceeded:
                        continue
                    m.release(rid)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=verifier), threading.Thread(target=reserver)]
        for t in threads:
            t.start()
        stop.wait(10.0)
        stop.set()
        for t in threads:
            t.join(30)  # bounded wait: a deadlock shows up as a live thread here
        self.assertFalse(any(t.is_alive() for t in threads),
                         "verify+append and reserve deadlocked (lock-order inversion)")
        self.assertEqual(errors, [])


# ------------------------------------------------------------------ finding 7
class Finding7_DependencyWalk(sb._Base):
    def test_package_init_is_pinned(self):
        job = self._make_base_job(subdir="vfx_pkg")
        (job / "pkg").mkdir()
        (job / "pkg" / "__init__.py").write_text("MARKER = 'init'\n", encoding="utf-8")
        (job / "pkg" / "worker.py").write_text("VALUE = 7\n", encoding="utf-8")
        (job / "scripts" / "maker.py").write_text(
            "import json, os, pathlib, sys\n"
            "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/..')\n"
            "import pkg.worker\n"
            "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
            "txt = '# Header\\nNo figure yet.\\n' if pkt['round'] == 1 else '" + PASS + "'\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(txt + str(pkg.worker.VALUE), encoding='utf-8')\n", encoding="utf-8")
        cfg = self._cfg(job)
        self._save(job, cfg)
        loop.freeze_job(job)
        (job / "pkg" / "__init__.py").write_text("MARKER = 'tampered'\n", encoding="utf-8")
        with self.assertRaises(gates.GateError, msg="the package __init__.py a seat import executes must be pinned"):
            loop.run_job(job)

    def test_relative_imports_are_pinned(self):
        job = self._make_base_job(subdir="vfx_rel")
        pkg = job / "scripts" / "seatpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "helper.py").write_text("VALUE = 3\n", encoding="utf-8")
        (pkg / "up.py").write_text("UP = 5\n", encoding="utf-8")
        (job / "toputil.py").write_text("TOP = 9\n", encoding="utf-8")
        (pkg / "main.py").write_text(
            "import json, pathlib, sys\n"
            "from . import helper\n"
            "from ..toputil import TOP\n"
            "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
            "txt = '# Header\\nNo figure yet.\\n' if pkt['round'] == 1 else '" + PASS + "'\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(txt + str(helper.VALUE + TOP), encoding='utf-8')\n", encoding="utf-8")
        cfg = self._cfg(job)
        cfg["maker"]["command"] = [sys.executable, "scripts/seatpkg/main.py"]
        self._save(job, cfg)
        loop.freeze_job(job)
        (pkg / "helper.py").write_text("VALUE = 4\n", encoding="utf-8")
        with self.assertRaises(gates.GateError, msg="`from . import helper` must be pinned"):
            loop.run_job(job)
        (pkg / "helper.py").write_text("VALUE = 3\n", encoding="utf-8")
        (job / "toputil.py").write_text("TOP = 10\n", encoding="utf-8")
        with self.assertRaises(gates.GateError, msg="`from ..toputil import X` must be pinned"):
            loop.run_job(job)

    def test_duplicate_imports_do_not_explode_the_walk(self):
        job = self._make_base_job(subdir="vfx_dup")
        chain = "import leaf, leaf\nVALUE = 1\n"
        prev = "leaf"
        for i in range(24):  # each level imports the next TWICE: exponential without the cache
            name = f"lay{i}"
            (job / f"{name}.py").write_text(f"import {prev}, {prev}\nVALUE = 1\n", encoding="utf-8")
            prev = name
        (job / "leaf.py").write_text("VALUE = 1\n", encoding="utf-8")
        (job / "scripts" / "maker.py").write_text(
            f"import json, pathlib, sys\nimport {prev}\n"
            "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
            "txt = '# Header\\nNo figure yet.\\n' if pkt['round'] == 1 else '" + PASS + "'\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(txt, encoding='utf-8')\n", encoding="utf-8")
        t0 = time.time()
        cfg = self._cfg(job)
        self._save(job, cfg)
        loop.freeze_job(job)  # must complete, not multiply exponentially
        self.assertLess(time.time() - t0, 30.0, "duplicate imports multiplied the walk")

    def test_walk_cap_refuses_by_name(self):
        job = self._make_base_job(subdir="vfx_cap")
        for i in range(5):
            (job / f"h{i}.py").write_text("VALUE = 1\n", encoding="utf-8")
        (job / "scripts" / "maker.py").write_text(
            "import json, pathlib, sys\n"
            "import h0, h1, h2, h3, h4\n"
            "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
            "txt = '# Header\\nNo figure yet.\\n' if pkt['round'] == 1 else '" + PASS + "'\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text(txt, encoding='utf-8')\n", encoding="utf-8")
        cfg = self._cfg(job)
        self._save(job, cfg)
        orig = gates.MAX_PINNED_SEAT_FILES
        gates.MAX_PINNED_SEAT_FILES = 3
        try:
            with self.assertRaises(gates.GateError) as ctx:
                loop.freeze_job(job)
            self.assertIn("cap", str(ctx.exception), "the refusal names the cap")
        finally:
            gates.MAX_PINNED_SEAT_FILES = orig


# ------------------------------------------------------------------ finding 8
class Finding8_DuplicateJsonProperties(sb._Base):
    def test_job_json_duplicate_key_refused_at_freeze(self):
        job = self._make_base_job(subdir="vfx_dupkey")
        raw = (job / "job.json").read_text()
        # inject a literal duplicate "bounds" key
        (job / "job.json").write_text(raw.replace('"bounds": {', '"bounds": {"max_rounds": 9,', 1))
        with self.assertRaises(gates.GateError, msg="a duplicate JSON property is refused at the parse boundary"):
            loop.freeze_job(job)

    def test_strict_parse_accepts_normal_json(self):
        self.assertEqual(gates.strict_json_loads('{"a": [1, 2], "b": {"c": null}}'), {"a": [1, 2], "b": {"c": None}})


# ------------------------------------------------------------------ finding 9
class Finding9_KeyScan(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_vfx9_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        (self.tmp / "bar.md").write_text("# Bar\n", encoding="utf-8")
        (self.tmp / "method.md").write_text("# Method\n", encoding="utf-8")

    def test_marker_smuggled_in_a_dict_key_is_caught(self):
        pkt = {"round": 1, "mode": "pass_r2", "best_artifact_digest": None,
               "defects": [{"see held_out/private-case.txt for the full note": "details"}],
               "workable_solutions": [], "withheld": []}
        with self.assertRaises(gates.GateError, msg="a path marker in a KEY is a leak too"):
            gates.gate_packet(pkt, self.tmp, None)

    def test_clean_packet_still_passes(self):
        pkt = {"round": 1, "mode": "pass_r2", "best_artifact_digest": None,
               "defects": [{"observation": "no figure yet"}], "workable_solutions": [], "withheld": []}
        gates.gate_packet(pkt, self.tmp, None)  # no raise


if __name__ == "__main__":
    unittest.main()
