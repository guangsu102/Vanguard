# New-join first-step budget regressions

Run from the repository root:

```powershell
backend/.venv/Scripts/python.exe -B -m pytest -c pyproject.toml -o addopts='' -q backend/tests/unit/growth_efficiency_join_floor
```

The 142 collected checks exercise the real qualification engine, SQLite reservation ledger,
existing Redis Lua allocator through FakeRedis/Lupa, installed SDK governor and
the public local service entry. Telegram responses and telemetry sinks are
isolated; there are no live Telegram, paid AI or production database requests.
`fakeredis` with Lua support and `lupa` are required; missing Lua support fails
collection instead of silently skipping the allocator checks.

The package's SQLite fixture registers the same pure models used by the release
tests. It avoids unrelated, unshipped P2/P4/P5 imports in the existing parent
fixture without editing that fixture or enabling those systems.

Coverage includes ordinary-review exhaustion with a payable join floor, the
single bounded first slice, later tokens using review again, unchanged hard
limits and spent loans, current source/claim/member fences, zero-SDK recovery,
cancellation and uncertain settlement, and identity mismatch before collection.
The claim-priority regressions also cover 31 older READY jobs, due budget waits,
certified zero-SDK recovery, invalid provenance, existing lock/hold fences, and
ordinary FIFO after effective work. They preserve the original budget authority.
Passing these tests does not establish natural advertising or joining gains.
