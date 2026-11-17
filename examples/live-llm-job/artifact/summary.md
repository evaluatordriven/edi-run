# Incident Summary: Auth Token Cache Outage

On October 14, 2026, a Redis TTL misconfiguration caused an outage in the authentication token caching system, leading to widespread service disruptions. The issue stemmed from an incorrect time-to-live (TTL) setting that prematurely evicted tokens from the cache, resulting in failed authentication requests. The problem was resolved in commit 7f3a91c, where the TTL value was corrected to ensure tokens remained in the cache for the intended duration, restoring normal service operation.

## Root Cause & Prevention
The root cause was a misconfigured TTL value in Redis, which was fixed in commit `7f3a91c` on 2026-10-14. This correction ensured tokens are retained in the cache for the intended duration, preventing future outages.