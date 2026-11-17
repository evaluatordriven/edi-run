# Incident Summary: Queue Backlog Spike
On 2026-09-12 the worker pool stalled for 14 minutes due to an unbounded retry loop in the webhook dispatcher, delaying 1,200 outbound notifications before automatic failover recovered throughput.

## Root Cause & Prevention
Fixed in commit `9c4e21b` on 2026-09-12 by capping exponential backoff and adding a circuit breaker.
