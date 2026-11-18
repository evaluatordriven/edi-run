"""Durable, hash-chained spend metering (design: docs/2026-EDI-DESIGN-S1-C-R1.md §C1-C7).

One append-only, hash-chained log per job: `job/usage.jsonl` — the spend source of
truth. A row exists for every call whose upstream bill exists, fsynced before the
response is used, so a seat crash or kill cannot unbill or inflate spend. Meter
rows are telemetry, never opinion: they carry no request bodies and enter the
custody record only as an accrued spend figure.
"""

import hashlib
import json
import math
import os
import pathlib
import threading
import time


class CapExceeded(Exception):
    """The next paid call would breach max_cost_usd — raised BEFORE the call is billed."""


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


class Meter:
    def __init__(self, job_dir, pricing=None, bounds=None):
        self.path = pathlib.Path(job_dir) / "usage.jsonl"
        self.pricing = pricing or {}
        self.max_cost = float((bounds or {}).get("max_cost_usd", math.inf))
        self._lock = threading.Lock()  # the bwrap proxy is threaded; appends must serialize
        self._rows = self._load()

    # -- chain ---------------------------------------------------------------
    def _load(self):
        rows = []
        if self.path.is_file():
            for i, ln in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
                if ln.strip():
                    try:
                        rows.append(json.loads(ln))
                    except ValueError as e:
                        raise ValueError(f"usage.jsonl line {i+1} is torn or not valid JSON — refusing to accrue onto a damaged log") from e
        return rows

    def verify(self):
        """The chain is verified at resume and finalize: a truncated or hand-edited
        usage.jsonl refuses instead of re-accruing. Each row's digest is
        RECOMPUTED from its content, so editing a value breaks the chain."""
        prev = ""
        for i, row in enumerate(self._rows):
            expect = _row_digest({k: v for k, v in row.items() if k != "digest"})
            if row.get("digest") != expect:
                raise ValueError(f"usage.jsonl row {i} content does not match its digest (tampered)")
            if row.get("prev") != prev:
                raise ValueError(f"usage.jsonl chain broken at row {i}")
            prev = row.get("digest")
        return True

    def row_cost(self, digest):
        for row in self._rows:
            if row.get("digest") == digest:
                return float(row.get("cost", 0.0))
        return 0.0

    def accrued(self):
        total = 0.0
        amended = set()
        for row in self._rows:
            if row.get("kind") == "amendment":
                amended.update(row.get("amends", []))
        for row in self._rows:
            if row.get("digest") in amended:
                continue
            if row.get("kind") != "call":
                continue
            total += float(row.get("cost", 0.0))
        return total

    # -- writes --------------------------------------------------------------
    def _append(self, row):
        with self._lock:
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

    # -- the enforcement point ----------------------------------------------
    def check_cap(self, body=None):
        accrued = self.accrued()
        if accrued >= self.max_cost:
            raise CapExceeded(f"cost cap reached: ${accrued:.6f} accrued >= ${self.max_cost:.6f} cap")
        est = self.estimate(body) if body is not None else 0.0
        last = float(self._rows[-1].get("cost", 0.0)) if self._rows and self._rows[-1].get("kind") == "call" else 0.0
        if accrued + max(est, last) >= self.max_cost:
            raise CapExceeded(f"next call would breach the cap: ${accrued:.6f} + est ${max(est, last):.6f} >= ${self.max_cost:.6f}")

    def estimate(self, body):
        if isinstance(body, (bytes, bytearray)):  # the proxy passes raw request bytes
            body = body.decode("utf-8", "replace")
        try:
            n = len(json.dumps(body))
        except Exception:
            n = 0
        rate = 0.0
        for p in self.pricing.values():
            if isinstance(p, dict):
                rate = max(rate, float(p.get("input_usd_per_mtok", 0.0)))
        return (n / 4.0) / 1e6 * rate

    def meter_call(self, who, model, endpoint, body, response=None, usage=None, round_no=None):
        """Record one billed call. Called BEFORE the response is consumed by the
        caller, so the row exists even if the caller crashes next line."""
        if usage is None and isinstance(response, dict):
            usage = (response.get("usage") or {}) if "usage" in response else {}
        cost = cost_of(usage or {}, self.pricing.get(model, self.pricing.get("default", {})))
        row = self._append({"kind": "call", "who": who, "model": model, "endpoint": endpoint,
                            "usage": usage or {}, "cost": round(cost, 10), "round": round_no,
                            **({} if usage else {"usage_missing": True})})  # a billed reply with no envelope meters a flagged $0 row — never silently free
        return row

    def amend(self, digests, note=""):
        """Append-only superseding row: never rewrite a metered row in place."""
        return self._append({"kind": "amendment", "amends": list(digests), "discarded_round": True, "note": note})
