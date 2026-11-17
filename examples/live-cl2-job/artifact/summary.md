# Incident Summary: Auth Token Cache Outage

On October 14, 2026, a Redis TTL misconfiguration caused an outage in the authentication token caching system, leading to widespread service disruptions. The issue was traced to an incorrect time-to-live (TTL) setting that prematurely expired tokens, preventing users from maintaining active sessions. The problem was resolved in commit `7f3a91c`, where the TTL was corrected to align with expected session durations, restoring normal service operations.

## Root Cause & Prevention
The root cause was an improperly configured TTL value in Redis, which was fixed in commit `7f3a91c` on 2026-10-14. To prevent recurrence, we have implemented additional validation checks for Redis configurations and added automated tests to verify TTL settings during deployment.