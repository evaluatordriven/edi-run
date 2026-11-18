"""Durable, hash-chained spend metering (I-07; design: docs/2026-EDI-DESIGN-S1-C-R1.md C1-C7)."""
from contextlib import contextmanager
import hashlib, json, math, os, pathlib, threading, time, uuid


class CapExceeded(Exception):
    """Next paid call would breach max_cost_usd -- raised BEFORE the call is billed."""


class UnboundedOutput(CapExceeded):
    """No max_output_tokens configured -- admission refuses by name."""


def _row_digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cost_of(usage, pricing):
    if not isinstance(usage, dict) or not pricing:
        return 0.0
    rate_in, rate_out = float(pricing.get("input_usd_per_mtok", 0.0)), float(pricing.get("output_usd_per_mtok", 0.0))
    in_tok = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    out_tok = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    total = usage.get("total_tokens")
    if total and not (in_tok or out_tok):
        return (float(total) / 1e6) * max(rate_in, rate_out)
    return (in_tok / 1e6) * rate_in + (out_tok / 1e6) * rate_out


def _positive(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) and v > 0


def output_bound(bounds, pricing, model=None):
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


class Meter:
    def __init__(self, job_dir, pricing=None, bounds=None):
        self.path = pathlib.Path(job_dir) / "usage.jsonl"
        self.pricing, self.bounds = pricing or {}, bounds or {}
        self.max_cost = float(self.bounds.get("max_cost_usd", math.inf))
        self._lock = threading.RLock()
        self._rows = self._load()

    def recover(self):
        # G:I-07/C2 open reservations at operation start belong to a dead run; recover once at worst case
        with self._lock:
            for rid, r in list(self._open().items()):
                amount = float(r.get("amount", 0.0))
                self._append_locked({"kind": "call", "who": "meter-recovery", "model": r.get("model", ""),
                                     "endpoint": "", "usage": {}, "cost": round(amount, 10), "round": r.get("round"),
                                     "reservation_id": rid, "recovered_reservation": rid,
                                     **({} if amount else {"usage_missing": True})})

    def _load(self):
        rows = []
        if self.path.is_file():
            for i, ln in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
                if ln.strip():
                    try: rows.append(json.loads(ln))
                    except ValueError as e: raise ValueError(f"usage.jsonl line {i+1} is torn or not valid JSON — refusing to accrue onto a damaged log") from e
        self._check(rows); return rows

    @staticmethod
    def _check(rows):
        prev = ""
        for i, row in enumerate(rows):
            if row.get("digest") != _row_digest({k: v for k, v in row.items() if k != "digest"}):
                raise ValueError(f"usage.jsonl row {i} content does not match its digest (tampered)")
            if row.get("prev") != prev: raise ValueError(f"usage.jsonl chain broken at row {i}")
            prev = row["digest"]

    def refresh(self):
        with self._lock: self._rows = self._load()

    @contextmanager
    def file_lock(self):
        with self._lock: yield

    def verify(self, anchor=None, exact=False):
        # G:P9 verify hash chain + state anchor (row count, head digest, accrued total)
        self._check(self._rows)
        if anchor:
            n, d = int(anchor[0]), str(anchor[1])
            total = float(anchor[2]) if len(anchor) > 2 and anchor[2] is not None else None
            if n > len(self._rows): raise ValueError(f"usage.jsonl has {len(self._rows)} rows but {n} were anchored (truncated)")
            if exact and n != len(self._rows):
                raise ValueError(f"usage.jsonl has {len(self._rows)} rows but {n} were anchored (a suffix was appended after the last anchored state write; re-anchor before attesting)")
            if n and self._rows[n - 1].get("digest") != d: raise ValueError(f"usage.jsonl row {n - 1} does not match its anchor (rewritten)")
            if total is not None:
                prefix = sum(float(r.get("cost", 0.0)) for r in self._rows[:n] if r.get("kind") == "call")
                if abs(prefix - total) > 1e-9: raise ValueError(f"usage.jsonl accrued total ${prefix:.6f} does not match the anchored ${total:.6f} (rewritten)")
        return True

    def head(self):
        return (len(self._rows), self._rows[-1]["digest"] if self._rows else "")

    def anchor(self):
        return self.head() + (round(self.accrued(), 10),)

    def row_cost(self, digest):
        return next((float(r.get("cost", 0.0)) for r in self._rows if r.get("digest") == digest), 0.0)

    def accrued(self):
        return sum(float(r.get("cost", 0.0)) for r in self._rows if r.get("kind") == "call")

    def discarded(self):
        amended = {d for r in self._rows if r.get("kind") == "amendment" for d in r.get("amends", [])}
        return sum(float(r.get("cost", 0.0)) for r in self._rows if r.get("kind") == "call" and r.get("digest") in amended)

    def token_usage(self):
        out = {}
        for r in self._rows:
            if r.get("kind") == "call" and (key := r.get("model") or r.get("who")):
                prev = out.get(key, {})
                out[key] = {k: prev.get(k, 0) + v for k, v in (r.get("usage") or {}).items() if isinstance(v, (int, float))}
        return out

    def _open(self):
        retired = {r.get("reservation_id") for r in self._rows if r.get("kind") == "call"} | \
                  {r.get("id") for r in self._rows if r.get("kind") == "release"}
        return {r["id"]: r for r in self._rows if r.get("kind") == "reserve" and r.get("id") not in retired}

    def _last_call_cost(self):
        return next((float(r.get("cost", 0.0)) for r in reversed(self._rows) if r.get("kind") == "call"), 0.0)

    def _append_locked(self, row):
        row["ts"] = row.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        row["prev"] = self._rows[-1]["digest"] if self._rows else ""
        row["digest"] = _row_digest({k: v for k, v in row.items() if k != "digest"})
        new = not self.path.is_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n"); f.flush(); os.fsync(f.fileno())
        if new:
            dfd = os.open(str(self.path.parent), os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        self._rows.append(row); return row

    @property
    def _reserved(self):
        return sum(float(r.get("amount", 0.0)) for r in self._open().values())

    def _append(self, row):
        with self._lock: return self._append_locked(row)

    def amend(self, digests, note=""):
        with self._lock: return self._append_locked({"kind": "amendment", "amends": list(digests), "discarded_round": True, "note": note})

    def max_tokens_param(self, model=None):
        return ((self.bounds or {}).get("max_tokens_param")
                or ((self.pricing.get(model) or {}).get("max_tokens_param") if isinstance(self.pricing.get(model), dict) else None)
                or ((self.pricing.get("default") or {}).get("max_tokens_param") if isinstance(self.pricing.get("default"), dict) else None)
                or "max_tokens")

    @contextmanager
    def hold(self, who, model, endpoint, req_dict, round_no=None):
        # G:C3/C4/N11 enforce output-token limit on req_dict, reserve worst-case, settle or release
        emit_key = self.max_tokens_param(model)
        other_key = "max_completion_tokens" if emit_key == "max_tokens" else "max_tokens"
        mt = req_dict.get("max_tokens", req_dict.get("max_completion_tokens"))
        bound = self.output_bound(model)
        if bound is not None:
            req_dict.pop(other_key, None)
            req_dict[emit_key] = int(mt) if mt is not None and float(mt) <= bound else int(bound)
        body = json.dumps(req_dict, sort_keys=True).encode("utf-8")
        rid = self.reserve(body, model=model)
        settled = [False]
        def settle(usage):
            self.meter_call(who, model, endpoint, body, usage=usage, round_no=round_no if round_no is not None else getattr(self, "_round", None), settle_reservation=rid)
            settled[0] = True
        try:
            yield body, settle
        finally:
            if not settled[0]:
                self.release(rid)

    def output_bound(self, model=None):
        return output_bound(self.bounds, self.pricing, model)

    def _output_rate(self, model=None):
        rates = [float(p["output_usd_per_mtok"]) for k in ((model,) if model else ()) + ("default",)
                 if isinstance(p := self.pricing.get(k), dict) and p.get("output_usd_per_mtok")]
        if not rates:
            rates = [float(p.get("output_usd_per_mtok", 0.0)) for p in self.pricing.values() if isinstance(p, dict)]
        return max(rates, default=0.0)

    def estimate(self, body, model=None):
        if isinstance(body, (bytes, bytearray)): body = body.decode("utf-8", "replace")
        try: n = len(json.dumps(body))
        except Exception: n = 0
        rate = max([max(float(p.get("input_usd_per_mtok", 0.0)), float(p.get("output_usd_per_mtok", 0.0)))
                    for p in self.pricing.values() if isinstance(p, dict)], default=0.0)
        if (bound := self.output_bound(model)) is None:
            raise UnboundedOutput(f"unbounded output spend: no max_output_tokens configured for model {model or '(default)'} (set bounds.max_output_tokens or pricing[<model>].max_output_tokens); refusing to admit a call whose output spend cannot be bounded")
        return (n / 4.0) / 1e6 * rate + (bound / 1e6) * self._output_rate(model)

    def reserve(self, body=None, model=None):
        with self._lock:
            self._rows = self._load()
            spent = self.accrued() + self._reserved
            if spent >= self.max_cost: raise CapExceeded(f"cost cap reached: ${spent:.6f} accrued+reserved >= ${self.max_cost:.6f} cap")
            rsv = max(self.estimate(body, model=model) if body is not None else 0.0, self._last_call_cost())
            if spent + rsv >= self.max_cost: raise CapExceeded(f"next call would breach the cap: ${spent:.6f} + worst case ${rsv:.6f} >= ${self.max_cost:.6f}")
            rid = uuid.uuid4().hex
            self._append_locked({"kind": "reserve", "id": rid, "amount": round(rsv, 12), "model": model or "", "round": getattr(self, "_round", None)})
            return rid

    def reservation_amount(self, rid):
        return next((float(r.get("amount", 0.0)) for r in self._rows if r.get("kind") == "reserve" and r.get("id") == rid), 0.0)

    def release(self, rid):
        with self._lock:
            self._rows, rid = self._load(), str(rid)
            if rid not in self._open(): raise ValueError(f"reservation {rid} is unknown or already retired — refusing to release it")
            self._append_locked({"kind": "release", "id": rid})

    def meter_call(self, who, model, endpoint, body, response=None, usage=None, round_no=None, settle_reservation=None):
        # G:C1/C2 record billed call before caller consumes response; settle reservation by id in one row
        if usage is None and isinstance(response, dict):
            usage = (response.get("usage") or {}) if "usage" in response else {}
        with self._lock:
            self._rows = self._load()
            rid = None if settle_reservation is None else str(settle_reservation)
            if rid is not None:
                prior = next((r for r in self._rows if r.get("kind") == "call" and r.get("reservation_id") == rid), None)
                if prior is not None:
                    return prior
                if rid not in self._open():
                    raise ValueError(f"reservation {rid} is unknown or released — refusing to bill it (no call row written)")
            cost = self.reservation_amount(rid) if (not usage and rid is not None) else \
                cost_of(usage or {}, self.pricing.get(model, self.pricing.get("default", {})))
            return self._append_locked({"kind": "call", "who": who, "model": model, "endpoint": endpoint,
                                        "usage": usage or {}, "cost": round(cost, 10), "round": round_no,
                                        **({"reservation_id": rid} if rid is not None else {}),
                                        **({} if usage else {"usage_missing": True, **({"estimated": True} if rid else {})})})
