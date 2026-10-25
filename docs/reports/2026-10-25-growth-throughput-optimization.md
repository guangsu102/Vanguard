# Production growth throughput optimization

## Decision

P6.3 and P6.4 are retired. No production Telegram canary, campaign/run scope,
or 24–48 hour observation window will be started from those procedures.

The P2 join Outbox and P3 qualification snapshot flags remain disabled because
the required pre-join fresh snapshot producer is not available. P0/P1 metrics,
audit, reconciliation protection, and account safety checks remain in place.

## Findings

The production degradation was caused by application scheduling pressure:

- growth workers could have an empty local account pool and return
  `account_not_in_pool` before attempting an eligible ad or join;
- review execution capacity was used as a hard join eligibility gate, so a
  small review queue stopped new joins even when the account budget and risk
  state were healthy;
- owner capacity waits used a long retry cadence, leaving a freed slot idle;
- the proxy rotation was healthy and was not the primary blocker.

## Changes

1. The legacy ads/join scheduler hydrates the targeted account into its local
   `AccountPool` when no session owner is registered. Hydration failures remain
   retryable and do not call Telegram.
2. `join_wait_execution_capacity` is retained in admission telemetry but is no
   longer a hard eligibility blocker. Durable review backlog watermarks,
   Telegram read budget, risk pauses, unresolved outcomes, and reconciliation
   requirements remain hard stops.
3. Session-owner capacity waits retry after 15 seconds instead of 30 seconds.

All changes are reversible by reverting the application release. No production
PostgreSQL, Redis, proxy, Telegram session, or feature flag was changed by this
code update.

## Validation

Targeted scheduler, actor, account-pool, join-admission, and P2 tests pass. The
existing inbox recovery test currently fails independently because its manually
created row has no `available_at` value while the active P1 reliability query
requires one; this change does not touch the inbox query or model.

