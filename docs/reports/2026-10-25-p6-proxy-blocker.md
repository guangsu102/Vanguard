# P6 direct production verification — proxy blocker

Date: 2026-10-25 (Asia/Shanghai)

The requested direct-production cohort remains account `33` + group `231` and
account `35` + group `178`; account `4` is excluded because it owns the main
groups. `P2_JOIN_OUTBOX_ENABLED` remains disabled because no fresh pre-join
qualification producer has been verified.

## Read-only production evidence

- Promoter accounts `2`, `4`, `33`, `34`, `35` are bound to static proxy rows
  `1` through `5` respectively.
- All five proxy rows point at `isp-2.evomi.com:12346` with SOCKS5 protocol.
- All five rows have `success_rate = 0`, `consecutive_failures >= 589`, and no
  observed healthy proxy.
- A correct SOCKS5 probe from the backend container to `https://api.telegram.org`
  returned `Connection not allowed by ruleset` for every proxy.
- The backend container has no Evomi API key or static gateway credentials, so
  it cannot safely generate replacement proxy rows.
- At the time of the check, `growth_operation`, `growth_outbox`,
  `qualification_snapshot`, and `campaign_execution` each contained zero rows.
- P2/P3/P4 rollout flags remain disabled and P2 shadow mode remains enabled.

## Decision

P6.3/P6.4 is blocked before Actor or Telegram execution. No production flags,
database business rows, shared PostgreSQL/Redis services, or Telegram writes
were changed. A valid Evomi credential/gateway or replacement proxy inventory
is required before repeating the read-only preflight; after recovery, rerun all
proxy health checks and require every selected account proxy to be healthy
before enabling the account+group+campaign+run scoped advertisement lane.
