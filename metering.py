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
        with self._lock, self._file_lock():
            self._rows = self._load()
            self._resv_rows = self._load_resv()
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
        return self._read_rows(self.path)

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
        be computed from a stale cache)."""
        self._rows = self._load()
        self._resv_rows = self._load_resv()

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
        return True

    def head(self):
        """(row count, head digest) — the P9 anchor's first two fields."""
        return (len(self._rows), self._rows[-1]["digest"] if self._rows else "")

    def anchor(self):
        """(row count, head digest, accrued total) — recorded at EVERY state write:
        the anchor proves the LATEST billed total, not just a prefix digest."""
        return (len(self._rows), self._rows[-1]["digest"] if self._rows else "", round(self.accrued(), 10))

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
        with self._lock, self._file_lock():
            self._reload_locked()
            return self._append_locked(row)

    def _append_locked(self, row):
        row["ts"] = row.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        row["prev"] = self._rows[-1]["digest"] if self._rows else ""
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
        self.resv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.resv_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
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
        reconciled against the usage rows at construction. A reservation with a
        matching usage row is retired; an unmatched one is billed-but-unrecorded
        (or in-flight when the process died) — recovered AS SPEND, fail-closed,
        so a kill between the upstream bill and the usage row cannot erase it.
        Live-process reservations (other Meter instances, same or other process)
        stay active: they are shared admission control, not orphans."""
        for r in list(self._active_resv()):
            if _pid_alive(r.get("pid")):
                continue  # a live holder's reservation is shared state, not an orphan
            amount = float(r.get("amount", 0.0))
            match = next((u for u in self._rows
                          if u.get("kind") == "call" and not u.get("recovered_reservation")
                          and abs(float(u.get("cost", 0.0)) - amount) <= 1e-9
                          and str(u.get("ts", "")) >= str(r.get("ts", ""))), None)
            if match is not None:
                self._append_resv_locked({"kind": "release", "id": r.get("id"), "state": "released",
                                          "note": "reconciled: matched usage row"})
            else:
                self._append_locked({"kind": "call", "who": "meter-recovery", "model": r.get("model", ""),
                                     "endpoint": "", "usage": {}, "cost": round(amount, 10),
                                     "recovered_reservation": r.get("id"), "round": r.get("round"),
                                     **({} if amount else {"usage_missing": True})})
                self._append_resv_locked({"kind": "release", "id": r.get("id"), "state": "released",
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
        (recovery at construction). Returns the reserved amount; the caller MUST
        release() it (or meter_call(settle_reservation=...) it) once done."""
        with self._lock, self._file_lock():
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
                                      "model": model or "", "round": getattr(self, "_round", None), "state": "active"})
            self._mine.append((rid, rsv))
            self._reserved += rsv
            return rsv

    def release(self, amount):
        with self._lock, self._file_lock():
            self._reload_locked()
            self._release_locked(amount)

    def _release_locked(self, amount, settled_digest=None):
        """Retire THIS instance's oldest active reservation of exactly `amount`
        (one transition with the usage-row append when settling)."""
        target = None
        for rid, a in self._mine:
            if abs(a - float(amount)) <= 1e-12:
                target = rid
                break
        if target is None:
            for r in self._active_resv():  # fall back to any active reservation of that amount
                if abs(float(r.get("amount", 0.0)) - float(amount)) <= 1e-12:
                    target = r.get("id")
                    break
        if target is None:
            return
        self._append_resv_locked({"kind": "release", "id": target, "state": "released",
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
        must never count twice for admission, and the two must not be two steps)."""
        if usage is None and isinstance(response, dict):
            usage = (response.get("usage") or {}) if "usage" in response else {}
        cost = cost_of(usage or {}, self.pricing.get(model, self.pricing.get("default", {})))
        with self._lock, self._file_lock():
            self._reload_locked()
            row = self._append_locked({"kind": "call", "who": who, "model": model, "endpoint": endpoint,
                                       "usage": usage or {}, "cost": round(cost, 10), "round": round_no,
                                       **({} if usage else {"usage_missing": True})})  # a billed reply with no envelope meters a flagged $0 row — never silently free
            if settle_reservation is not None:
                self._release_locked(settle_reservation, settled_digest=row["digest"])
        return row

    def amend(self, digests, note=""):
        """Append-only superseding row: never rewrite a metered row in place."""
        return self._append({"kind": "amendment", "amends": list(digests), "discarded_round": True, "note": note})
