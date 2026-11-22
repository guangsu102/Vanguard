# Review read v5 regression tests

This package is the permanent home for eleven test modules added during
the 2026-11-04 repair. The original copies under `tmp/review-v5-fix-six-20261104`
remain as provenance and as inputs to the frozen-production test runner.

Coverage includes public candidate IDs with independent account validation,
dependency-aware history reuse, original message-version TTL and collection
freshness, review status, terminal technical-event recovery, fair execution and
cancellation, outbound receipts, bounded storage, replay idempotency, diagnostic
redaction and storage budgets, maintenance heartbeat attestation, committed
business-transition attribution, bounded completion accounting, and the
offline read-reduction evidence validator.

The first nine modules retain all 259 assertion ASTs and all 72 test-function
names. The two v2 modules retain their additional 92 assertion ASTs. Their
shared helper retains three v2-compatible attribution/cutover cases with 11
unchanged assertion ASTs; obsolete permanent PostgreSQL marker cases were not
migrated.
The heartbeat module additionally retains 17 new assertion ASTs across three
real cross-process file-lock cases covering startup failure, periodic failure,
successful recovery, duplicate starts, and cancellation while retrying.
Only imports and fixture source resolution changed: shared
`tests.fakes.session_exec_redis.FakeRedis`, the package-relative owner helper,
loading the actual backend diagnostics source in the isolated heartbeat
fixture, and loading the checked-in validator from
`scripts/ops/review_read_v5/validate_benefit.py`. Tests have no dependency on
temporary source paths. Existing backend model/database fixtures are reused.

The package's autouse fixture rebinds the existing `event_loop` before and after
each test. The backend loop has session scope, while synchronous heartbeat
tests call `asyncio.run`, which clears the current thread's loop binding. The
fixture restores the still-live test loop; it does not alter application
behavior, replace the database fixture, or weaken assertions.

Validation against the actual checkout, preserving its unpublished local
changes: **174 passed, 0 failed, 0 errors, 0 skipped, 0 deselected** in 33.62
seconds (2653 warnings). Product source hashes did not change during the run.
The structured result is
`tmp/review-v5-fix-six-20261104/formal-test-maintenance-retry-result.json`.
The initial run's 66 event-loop harness failures and the intermediate
109-passing-test and 171-passing-test results remain separate provenance
records under `tmp`.

Run from `backend`:

```powershell
python -m pytest tests/unit/review_read_v5 -q -o asyncio_mode=auto
```

The validator and its input schema are byte-identical to the final
`business_transition_v2` tools. SHA-256 values:

- Validator: `26751e21b2ecf5f8d93359a9f554003a3fa4dd289927d91f74be3cb7e33b2e4e`
- Schema: `3d9102af34f24399ff69b66520dae3ced97b8c5b100dfa64ea489171b603afaa`

The validator only reads an explicitly supplied local input file and prints
its result. This offline suite does not certify production read-volume
reduction; that requires complete, comparable 24-hour production evidence.
