"""Durable, hash-chained spend metering (design: docs/2026-EDI-DESIGN-S1-C-R1.md §C1-C7).

One append-only, hash-chained log per job: `job/usage.jsonl` — the spend source of
truth. A row exists for every call whose upstream bill exists, fsynced before the
response is used, so a seat crash or kill cannot unbill or inflate spend. Meter
rows are telemetry, never opinion: they carry no request bodies and enter the
custody record only as an accrued spend figure.

Round-3 external review: admission control (reservations) is ALSO durable and
job-scoped — `job/usage.resv.jsonl`, a hash-chained ledger shared by every Meter
instance on the job. A reservation is written (fsynced) BEFORE the proxy forwards
a call, so a kill between the upstream bill and the usage row cannot erase the
spend: the next Meter construction reconciles stale-process reservations against
the usage rows and recovers unmatched ones as spend rows (fail-closed).

Verification re-review (2026-11-18): reservations are identified by an opaque
id created at reserve (never by amount); settle/release name that id exactly
once and refuse unknown-or-retired ids (an anomaly row is logged); meter_call
writes the reservation id INTO the usage row so reconciliation is
identity-based and idempotent; and the two ledgers are cross-anchored
(usage_head / resv_head / settled digests) and validated TOGETHER at every
Meter construction and state write — rollback of either ledger is detected.
"""

import fcntl
import hashlib
import json
import math
import os
import pathlib
import threading
import time
import uuid
from contextlib import contextmanager


class CapExceeded(Exception):
    """The next paid call would breach max_cost_usd — raised BEFORE the call is billed."""


class UnboundedOutput(CapExceeded):
    """No max_output_tokens is configured, so the worst-case output spend of the
    next call cannot be bounded — admission refuses (fail-closed) by name."""


def _row_digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cost_of(usage, pricing):
    """Deterministic price of a usage envelope under a pricing entry (per-Mtok rates)."""
    if not isinstance(usage, dict) or not pricing:
        return 0.0
    in_tok = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    out_tok = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    total = usage.get("total_tokens")
    if total and not (in_tok or out_tok):
        in_tok, out_tok = 0, total  # providers reporting only total_tokens price at the higher rate
    return (in_tok / 1e6) * float(pricing.get("input_usd_per_mtok", 0.0)) + (out_tok / 1e6) * float(pricing.get("output_usd_per_mtok", 0.0))


def _positive(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) and v > 0


def output_bound(bounds, pricing, model=None):
    """Worst-case output tokens for a call: bounds.max_output_tokens (number or
    per-model map), else the pricing entry's. None = unbounded (admission refuses)."""
    mo = (bounds or {}).get("max_output_tokens")
    if isinstance(mo, dict):
        if model and _positive(mo.get(model)):
            return float(mo[model])
        vals = [v for v in mo.values() if _positive(v)]
        mo = max(vals) if vals else None
    if _positive(mo):
        return float(mo)
    for key in ((model,) if model else ()) + ("default",):
        p = (pricing or {}).get(key)
        if isinstance(p, dict) and _positive(p.get("max_output_tokens")):
            return float(p["max_output_tokens"])
    return None


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class Meter:
    RESV_NAME = "usage.resv.jsonl"
    LOCK_NAME = "usage.lock"

    def __init__(self, job_dir, pricing=None, bounds=None):
        self.path = pathlib.Path(job_dir) / "usage.jsonl"
        self.resv_path = pathlib.Path(job_dir) / self.RESV_NAME
        self.lock_path = pathlib.Path(job_dir) / self.LOCK_NAME
        self.pricing = pricing or {}
        self.bounds = bounds or {}
        self.max_cost = float((bounds or {}).get("max_cost_usd", math.inf))
        self._lock = threading.Lock()  # the bwrap proxy is threaded; appends must serialize
        self._reserved = 0.0  # worst-case spend of THIS instance's in-flight calls
        self._mine = []  # [(id, amount)] of this instance's active reservations
        # Lock ORDER (one rule, everywhere): the cross-process file lock (flock) is
        # ALWAYS the outer lock, the in-process threading lock the inner one.
        with self._file_lock(), self._lock:
            self._rows = self._load()
            self._resv_rows = self._load_resv()
            self._validate_joint_locked()
            self._reconcile_locked()  # billed-but-rowless calls of dead processes re-enter the log

    # -- cross-process exclusion ---------------------------------------------
    @contextmanager
    def _file_lock(self):
        """Cross-process advisory lock on the job's meter files: two Meter
        instances (or two invocations) on one job serialize their ledger writes."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    # -- chain ---------------------------------------------------------------
    def _read_rows(self, path):
        rows = []
        if path.is_file():
            for i, ln in enumerate(path.read_text(encoding="utf-8").splitlines()):
                if ln.strip():
                    try:
                        rows.append(json.loads(ln))
                    except ValueError as e:
                        raise ValueError(f"{path.name} line {i+1} is torn or not valid JSON — refusing to accrue onto a damaged log") from e
        return rows

    def _load(self):
        """Load AND chain-verify the usage ledger: admission and recovery must
        never consume an unverified chain (review blocker 3)."""
        rows = self._read_rows(self.path)
        prev = ""
        for i, row in enumerate(rows):
            expect = _row_digest({k: v for k, v in row.items() if k != "digest"})
            if row.get("digest") != expect:
                raise ValueError(f"usage.jsonl row {i} content does not match its digest (tampered)")
            if row.get("prev") != prev:
                raise ValueError(f"usage.jsonl chain broken at row {i}")
            prev = row.get("digest")
        return rows

    def _load_resv(self):
        """Load and chain-verify the reservation ledger: a hand-edited ledger is
        an integrity failure, never a silently enlarged budget."""
        rows = self._read_rows(self.resv_path)
        prev = ""
        for i, row in enumerate(rows):
            expect = _row_digest({k: v for k, v in row.items() if k != "digest"})
            if row.get("digest") != expect:
                raise ValueError(f"{self.resv_path.name} row {i} content does not match its digest (tampered)")
            if row.get("prev") != prev:
                raise ValueError(f"{self.resv_path.name} chain broken at row {i}")
            prev = row.get("digest")
        return rows

    def _reload_locked(self):
        """Re-read both ledgers under the locks: another Meter instance on this
        job may have appended since our last look (the chain's prev must never
        be computed from a stale cache). Both ledgers are validated TOGETHER:
        chains intact is necessary but not sufficient — the accounting
        relationship between them is checked too (review blocker 3)."""
        self._rows = self._load()
        self._resv_rows = self._load_resv()
        self._validate_joint_locked()

    def _validate_joint_locked(self):
        """Joint ledger validation (review blocker 3): the two hash chains are
        anchored to each other, so a rollback or truncation of EITHER file is
        detectable even though a valid prefix of one chain alone would verify.
        - every reserve row carries `usage_head` (the usage ledger's head digest
          at reserve time): it must still be present in usage.jsonl;
        - every release row that settled a bill names the settled usage row's
          digest: it must still be present (usage may not roll back past a
          settled reservation);
        - every usage row carries `resv_head` (the reservation ledger's head
          digest at append time — the reservation-head commitment kept INSIDE
          the usage row): it must still be present in the reservation ledger,
          so truncating the reservation ledger below a recorded usage row
          refuses."""
        usage_digests = {r.get("digest") for r in self._rows}
        resv_digests = {r.get("digest") for r in self._resv_rows}
        for i, r in enumerate(self._resv_rows):
            uh = r.get("usage_head") or ""
            if r.get("kind") == "reserve" and uh and uh not in usage_digests:
                raise ValueError(f"{self.RESV_NAME} row {i} anchors usage head {str(uh)[:12]}... "
                                 "that is absent from usage.jsonl (usage ledger rolled back below an open reservation)")
            settled = r.get("settled") or ""
            if r.get("kind") == "release" and settled and settled not in usage_digests:
                raise ValueError(f"{self.RESV_NAME} row {i} names settled usage digest {str(settled)[:12]}... "
                                 "absent from usage.jsonl (usage ledger rolled back past a settled reservation)")
        for i, u in enumerate(self._rows):
            rh = u.get("resv_head") or ""
            if rh and rh not in resv_digests:
                raise ValueError(f"usage.jsonl row {i} anchors reservation head {str(rh)[:12]}... "
                                 f"absent from {self.RESV_NAME} (reservation ledger truncated below a recorded usage row)")

    def refresh(self):
        """Public re-read for a caller that ALREADY holds the file lock (finalize
        holds it across verify + spend append): only the threading lock is taken,
        since flock is per open-file-description and a second acquisition in the
        same process would deadlock."""
        with self._lock:
            self._reload_locked()

    file_lock = _file_lock  # public alias: finalize holds the meter lock across verify + spend append

    def verify(self, anchor=None, exact=False):
        """The chain is verified at resume and finalize: a truncated or hand-edited
        usage.jsonl refuses instead of re-accruing. Each row's digest is
        RECOMPUTED from its content, so editing a value breaks the chain.
        anchor: (row count, head digest, accrued total) recorded at every state
        write (P9) — the chain alone cannot see its NEWEST rows deleted; the
        anchor can, and the accrued total catches a recomputed chain whose
        dollar figures were rewritten.
        exact=True (finalize): the log must have EXACTLY the anchored row count —
        a suffix appended after the last state write is not attested; re-anchor
        it with a later state write first.
        NOTE: a MISSING anchor never disables the check — callers refuse an
        unanchored log (finalize) or record it unanchored and re-anchor (run)."""
        prev = ""
        for i, row in enumerate(self._rows):
            expect = _row_digest({k: v for k, v in row.items() if k != "digest"})
            if row.get("digest") != expect:
                raise ValueError(f"usage.jsonl row {i} content does not match its digest (tampered)")
            if row.get("prev") != prev:
                raise ValueError(f"usage.jsonl chain broken at row {i}")
            prev = row.get("digest")
        if anchor:
            n, d = int(anchor[0]), str(anchor[1])
            total = float(anchor[2]) if len(anchor) > 2 and anchor[2] is not None else None
            if n > len(self._rows):
                raise ValueError(f"usage.jsonl has {len(self._rows)} rows but {n} were anchored (truncated)")
            if exact and n != len(self._rows):
                raise ValueError(f"usage.jsonl has {len(self._rows)} rows but {n} were anchored "
                                 "(a suffix was appended after the last anchored state write; re-anchor before attesting)")
            if n and self._rows[n - 1].get("digest") != d:
                raise ValueError(f"usage.jsonl row {n - 1} does not match its anchor (rewritten)")
            if total is not None:
                prefix = sum(float(r.get("cost", 0.0)) for r in self._rows[:n] if r.get("kind") == "call")
                if abs(prefix - total) > 1e-9:
                    raise ValueError(f"usage.jsonl accrued total ${prefix:.6f} does not match the anchored ${total:.6f} (rewritten)")
        # verification re-review #2 (final round): a 5-field anchor also commits
        # the RESERVATION ledger's tail — removing an unmatched reserve row (or
        # any row) after the state write is caught here, at the next validation.
        # Pre-2026-11 3-field anchors (legacy state.json) skip this check.
        if anchor and len(anchor) >= 5 and anchor[3] is not None and anchor[4]:
            rn, rd = int(anchor[3]), str(anchor[4])
            if rn > len(self._resv_rows):
                raise ValueError(f"{self.RESV_NAME} has {len(self._resv_rows)} rows but {rn} were anchored (truncated)")
            if rn and self._resv_rows[rn - 1].get("digest") != rd:
                raise ValueError(f"{self.RESV_NAME} row {rn - 1} does not match its anchor (rewritten or truncated)")
        return True

    def head(self):
        """(row count, head digest) — the P9 anchor's first two fields."""
        return (len(self._rows), self._rows[-1]["digest"] if self._rows else "")

    def anchor(self):
        """(usage rows, usage head digest, accrued total, resv rows, resv head
        digest) — recorded at EVERY state write: the anchor proves the LATEST
        billed total AND the LATEST reservation-ledger tail, not just a prefix
        (verification re-review #2, final round: removing an unmatched reserve
        row or a recovered usage row after a state write is detectable at the
        next validation, because the anchor names both ledgers' heads).
        Every state write also re-validates the reservation ledger and
        usage.jsonl TOGETHER (verification re-review blocker 3): the anchor of a
        rolled-back pair of ledgers is never issued.
        MIGRATION (pre-ID usage rows): recovery matches only rows carrying
        reservation_id; legacy rows without an id are anchored as-is — an exact
        association cannot be safely inferred for them, so they are committed by
        the usage anchor alone (documented deferral, DECISIONS.log)."""
        self._validate_joint_locked()
        return (len(self._rows), self._rows[-1]["digest"] if self._rows else "", round(self.accrued(), 10),
                len(self._resv_rows), self._resv_rows[-1]["digest"] if self._resv_rows else "")

    def row_cost(self, digest):
        for row in self._rows:
            if row.get("digest") == digest:
                return float(row.get("cost", 0.0))
        return 0.0

    def accrued(self):
        """Every billed call counts toward the cap, including calls of a round that
        resume later discarded (P2): discarding work does not unbill it. Amendment
        rows only LABEL rows for the report; they never subtract."""
        return sum(float(r.get("cost", 0.0)) for r in self._rows if r.get("kind") == "call")

    def discarded(self):
        """Spend of rows an amendment has labelled discarded (P2 reporting)."""
        amended = {d for r in self._rows if r.get("kind") == "amendment" for d in r.get("amends", [])}
        return sum(float(r.get("cost", 0.0)) for r in self._rows if r.get("kind") == "call" and r.get("digest") in amended)

    # -- writes --------------------------------------------------------------
    def _append(self, row):
        with self._file_lock(), self._lock:
            self._reload_locked()
            return self._append_locked(row)

    def _append_locked(self, row):
        row["ts"] = row.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        row["prev"] = self._rows[-1]["digest"] if self._rows else ""
        # the usage row anchors the reservation ledger's head AT APPEND TIME —
        # the cross-commitment joint validation checks on every load (review blocker 3)
        row["resv_head"] = self._resv_rows[-1]["digest"] if self._resv_rows else ""
        digest = _row_digest({k: v for k, v in row.items() if k != "digest"})
        row["digest"] = digest
        new = not self.path.is_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if new:
            dfd = os.open(str(self.path.parent), os.O_RDONLY)  # the directory entry must be durable too
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        self._rows.append(row)
        return row

    def _append_resv_locked(self, row):
        """Append to the reservation ledger (caller holds both locks)."""
        row["ts"] = row.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        row["prev"] = self._resv_rows[-1]["digest"] if self._resv_rows else ""
        digest = _row_digest({k: v for k, v in row.items() if k != "digest"})
        row["digest"] = digest
        new = not self.resv_path.is_file()
        self.resv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.resv_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if new:
            dfd = os.open(str(self.resv_path.parent), os.O_RDONLY)  # the directory entry must be durable too
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        self._resv_rows.append(row)
        return row

    # -- reservation ledger --------------------------------------------------
    def _active_resv(self):
        """Reservations not yet retired by a release/reconcile row: activity is
        the absence of a matching release row, not the reserve row's own state
        field (the ledger is append-only)."""
        released = {r.get("id") for r in self._resv_rows if r.get("kind") == "release"}
        return [r for r in self._resv_rows
                if r.get("kind") == "reserve" and r.get("id") not in released]

    def _reserved_total_locked(self):
        return sum(float(r.get("amount", 0.0)) for r in self._active_resv())

    def _reconcile_locked(self):
        """Round-3 external review #2: reservations held by DEAD processes are
        reconciled against the usage rows at construction — BY REQUEST IDENTITY,
        not by cost+timestamp similarity (verification re-review blocker 2):
        meter_call writes the reservation id INTO the usage row, so a recovered
        reservation is matched to exactly the row that bill produced, and one
        usage row can never settle two unrelated reservations. A reservation
        with no matching usage row is billed-but-unrecorded (or in-flight when
        the process died) — recovered AS SPEND, fail-closed, exactly ONCE: the
        recovery appends a release row with state "recovered", so the
        reservation leaves the active set and is never re-recovered (a crash
        between the recovery's usage fsync and its release fsync is healed by
        the next reconciliation finding the usage row by id, not re-charging).
        Live-process reservations (other Meter instances, same or other process)
        stay active: they are shared admission control, not orphans."""
        usage_by_rid = {}
        for u in self._rows:
            if u.get("kind") != "call":
                continue
            rid = u.get("reservation_id") or u.get("recovered_reservation")
            if rid and rid not in usage_by_rid:
                usage_by_rid[rid] = u
        for r in list(self._active_resv()):
            if _pid_alive(r.get("pid")):
                continue  # a live holder's reservation is shared state, not an orphan
            rid = r.get("id")
            if rid in usage_by_rid:
                self._append_resv_locked({"kind": "release", "id": rid, "state": "released",
                                          "note": "reconciled: matched usage row by reservation id"})
            else:
                amount = float(r.get("amount", 0.0))
                self._append_locked({"kind": "call", "who": "meter-recovery", "model": r.get("model", ""),
                                     "endpoint": "", "usage": {}, "cost": round(amount, 10),
                                     "recovered_reservation": rid, "reservation_id": rid, "round": r.get("round"),
                                     **({} if amount else {"usage_missing": True})})
                self._append_resv_locked({"kind": "release", "id": rid, "state": "recovered",
                                          "note": "reconciled: recovered as spend (kill before the usage row)"})
        self._sync_mine_locked()

    def _sync_mine_locked(self):
        active_ids = {r.get("id") for r in self._active_resv()}
        self._mine = [(i, a) for (i, a) in self._mine if i in active_ids]
        self._reserved = sum(a for (_i, a) in self._mine)

    # -- the enforcement point ----------------------------------------------
    def output_bound(self, model=None):
        """The configured worst-case output token count for a call: bounds
        ('max_output_tokens', number or per-model map), else the pricing entry's
        'max_output_tokens'. None means UNBOUNDED — admission refuses."""
        mo = (self.bounds or {}).get("max_output_tokens")
        if isinstance(mo, dict):
            vals = [v for v in mo.values() if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
            if model and isinstance(mo.get(model), (int, float)) and not isinstance(mo.get(model), bool) and mo[model] > 0:
                return float(mo[model])
            mo = max(vals) if vals else None
        if isinstance(mo, (int, float)) and not isinstance(mo, bool) and mo > 0:
            return float(mo)
        for key in ((model,) if model else ()) + ("default",):
            p = self.pricing.get(key)
            if isinstance(p, dict):
                v = p.get("max_output_tokens")
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    return float(v)
        return None

    def _output_rate(self, model=None):
        rate = 0.0
        for key in ((model,) if model else ()) + ("default",):
            p = self.pricing.get(key)
            if isinstance(p, dict) and p.get("output_usd_per_mtok"):
                rate = max(rate, float(p["output_usd_per_mtok"]))
        if rate == 0.0:  # conservative: the highest output rate on the books
            for p in self.pricing.values():
                if isinstance(p, dict):
                    rate = max(rate, float(p.get("output_usd_per_mtok", 0.0)))
        return rate

    def check_cap(self, body=None):
        accrued = self.accrued()
        if accrued >= self.max_cost:
            raise CapExceeded(f"cost cap reached: ${accrued:.6f} accrued >= ${self.max_cost:.6f} cap")
        est = self.estimate(body) if body is not None else 0.0
        last = float(self._rows[-1].get("cost", 0.0)) if self._rows and self._rows[-1].get("kind") == "call" else 0.0
        if accrued + max(est, last) >= self.max_cost:
            raise CapExceeded(f"next call would breach the cap: ${accrued:.6f} + est ${max(est, last):.6f} >= ${self.max_cost:.6f}")

    def reserve(self, body=None, model=None):
        """Reserve the next call's worst-case spend — request PLUS the configured
        max output tokens (round-3 external review #1: the request estimate alone
        never bounds what a response can cost) — UNDER THE LOCKS, before the
        proxy forwards it. The reservation is durable and job-scoped: two Meter
        instances on one job share the same balance, and a kill cannot erase it
        (recovery at construction). Returns an OPAQUE RESERVATION ID (verification
        re-review blocker 1): settlement and release name THAT id exactly once —
        never an amount-match search that can retire another request's hold.
        The caller MUST release(<id>) (or meter_call(settle_reservation=<id>))
        once done. The row records the usage ledger's head at reserve time
        (usage_head) — the joint-validation anchor (blocker 3)."""
        with self._file_lock(), self._lock:
            self._reload_locked()
            self._reconcile_locked()
            total = self.accrued() + self._reserved_total_locked()
            if total >= self.max_cost:
                raise CapExceeded(f"cost cap reached: ${total:.6f} accrued+reserved >= ${self.max_cost:.6f} cap")
            rsv = max(self.estimate(body, model=model) if body is not None else 0.0,
                      float(self._rows[-1].get("cost", 0.0)) if self._rows and self._rows[-1].get("kind") == "call" else 0.0)
            if total + rsv >= self.max_cost:
                raise CapExceeded(f"next call would breach the cap: ${total:.6f} + worst case ${rsv:.6f} >= ${self.max_cost:.6f}")
            rid = uuid.uuid4().hex
            self._append_resv_locked({"kind": "reserve", "id": rid, "pid": os.getpid(), "amount": round(rsv, 12),
                                      "model": model or "", "round": getattr(self, "_round", None), "state": "active",
                                      "usage_head": self._rows[-1]["digest"] if self._rows else ""})
            self._mine.append((rid, rsv))
            self._reserved += rsv
            return rid

    def reservation_amount(self, rid):
        """The reserved worst-case amount of a reservation id (from the ledger)."""
        for r in self._resv_rows:
            if r.get("kind") == "reserve" and r.get("id") == rid:
                return float(r.get("amount", 0.0))
        return 0.0

    def release(self, rid):
        with self._file_lock(), self._lock:
            self._reload_locked()
            self._release_locked(rid)

    def _release_locked(self, rid, settled_digest=None):
        """Retire the reservation with EXACTLY this id (verification re-review
        blocker 1): unknown or already-retired ids are REFUSED (an anomaly row
        is logged in the reservation ledger; nothing is latched, nothing
        retired) — a release can never fall back to 'any active reservation of
        that amount' and steal another request's hold."""
        rid = str(rid)
        if not any(r.get("kind") == "reserve" and r.get("id") == rid for r in self._resv_rows):
            self._append_resv_locked({"kind": "anomaly", "id": rid,
                                      "note": "release/settle refused: unknown reservation id"})
            raise ValueError(f"reservation {rid} is unknown — refusing to release or settle an "
                             "unknown reservation (anomaly logged in the reservation ledger)")
        if rid in {r.get("id") for r in self._resv_rows if r.get("kind") == "release"}:
            self._append_resv_locked({"kind": "anomaly", "id": rid,
                                      "note": "release/settle refused: reservation already retired"})
            raise ValueError(f"reservation {rid} was already settled or released — refusing to "
                             "retire it twice (duplicate release refused; anomaly logged)")
        self._append_resv_locked({"kind": "release", "id": rid, "state": "released",
                                  **({"settled": settled_digest} if settled_digest else {})})
        self._sync_mine_locked()

    def estimate(self, body, model=None):
        """Worst-case cost of a call: the serialized request PLUS the configured
        max output tokens priced at the model's output rate (round-3 external
        review #1). With no configured output bound the output spend is
        unbounded — refuse by name (fail-closed), never estimate as if free."""
        if isinstance(body, (bytes, bytearray)):  # the proxy passes raw request bytes
            body = body.decode("utf-8", "replace")
        try:
            n = len(json.dumps(body))
        except Exception:
            n = 0
        rate = 0.0  # conservative: the HIGHER of the input/output rates bounds the request part
        for p in self.pricing.values():
            if isinstance(p, dict):
                rate = max(rate, float(p.get("input_usd_per_mtok", 0.0)), float(p.get("output_usd_per_mtok", 0.0)))
        req = (n / 4.0) / 1e6 * rate
        bound = self.output_bound(model)
        if bound is None:
            raise UnboundedOutput(
                f"unbounded output spend: no max_output_tokens configured for model {model or '(default)'} "
                "(set bounds.max_output_tokens or pricing[<model>].max_output_tokens); "
                "refusing to admit a call whose output spend cannot be bounded")
        return req + (bound / 1e6) * self._output_rate(model)

    def meter_call(self, who, model, endpoint, body, response=None, usage=None, round_no=None, settle_reservation=None):
        """Record one billed call. Called BEFORE the response is consumed by the
        caller, so the row exists even if the caller crashes next line.
        settle_reservation: retire the caller's reservation IN THE SAME LOCKED
        TRANSITION as the append (round-3 external review #4: a successful call
        must never count twice for admission, and the two must not be two steps).
        The usage row RECORDS THE RESERVATION ID (verification re-review blocker
        2): reconciliation is identity-based, so the row that bill produced can
        only ever settle that one reservation. A 200 whose usage envelope is
        MISSING is billed AT THE RESERVED ESTIMATE and flagged estimated=true
        (review blocker 4) — never silently $0."""
        if usage is None and isinstance(response, dict):
            usage = (response.get("usage") or {}) if "usage" in response else {}
        with self._file_lock(), self._lock:
            # verification re-review #1 (final round): EVERYTHING happens under the
            # locks — the reservation-id validation, the missing-usage amount
            # lookup and the append — so a repeat settlement can never append a
            # usage row before it is refused, and a second Meter instance cannot
            # estimate $0 from a stale cache (the amount lookup runs AFTER the
            # reload below).
            self._reload_locked()
            if settle_reservation is not None:
                rid = str(settle_reservation)
                known = any(r.get("kind") == "reserve" and r.get("id") == rid for r in self._resv_rows)
                retired = any(r.get("kind") == "release" and r.get("id") == rid for r in self._resv_rows)
                prior = next((u for u in self._rows
                              if u.get("kind") == "call" and u.get("reservation_id") == rid), None)
                if not known:
                    # unknown id: refuse WITHOUT appending any usage row (the
                    # anomaly row in the reservation ledger is the only trace)
                    self._append_resv_locked({"kind": "anomaly", "id": rid,
                                              "note": "settle refused: unknown reservation id; NO usage row was written"})
                    raise ValueError(f"reservation {rid} is unknown — refusing to settle it "
                                     "(no usage row was appended; anomaly logged in the reservation ledger)")
                if prior is not None:
                    # interrupted settlement: the usage row (with this id) was
                    # fsynced but the release was not. Finish it WITHOUT a second
                    # charge: retire the hold against the EXISTING row. (If the
                    # reservation is ALREADY retired, the settlement completed
                    # earlier — return the existing row, never release twice.)
                    if not retired:
                        self._release_locked(rid, settled_digest=prior["digest"])
                    return prior
                if retired:
                    # already settled AND its usage row is gone: refuse, append nothing
                    self._append_resv_locked({"kind": "anomaly", "id": rid,
                                              "note": "settle refused: reservation already retired; NO usage row was written"})
                    raise ValueError(f"reservation {rid} was already settled — refusing to bill it twice "
                                     "(no usage row was appended; anomaly logged in the reservation ledger)")
            if not usage and settle_reservation is not None:
                # billed-at-estimate: the reserved worst case stands in for the missing
                # envelope — read from the RELOADED ledger, never a stale cache
                cost = self.reservation_amount(settle_reservation)
            else:
                cost = cost_of(usage or {}, self.pricing.get(model, self.pricing.get("default", {})))
            row = self._append_locked({"kind": "call", "who": who, "model": model, "endpoint": endpoint,
                                       "usage": usage or {}, "cost": round(cost, 10), "round": round_no,
                                       **({"reservation_id": settle_reservation} if settle_reservation is not None else {}),
                                       **({} if usage else {"usage_missing": True,
                                                            **({"estimated": True} if settle_reservation is not None else {})})})  # a billed reply with no envelope meters a flagged row — at the reserved estimate when one exists, never silently free
            if settle_reservation is not None:
                self._release_locked(settle_reservation, settled_digest=row["digest"])
        return row

    def amend(self, digests, note=""):
        """Append-only superseding row: never rewrite a metered row in place."""
        return self._append({"kind": "amendment", "amends": list(digests), "discarded_round": True, "note": note})
