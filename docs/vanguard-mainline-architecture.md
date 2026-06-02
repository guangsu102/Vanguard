# Vanguard Mainline Architecture

Vanguard is converged around one source of truth:

- Backend/Admin is the configuration and data center.
- Telegram Workers are the only long-running execution surface.
- XBoard integration uses the HMAC signed `/api/v1/...` protocol from `Xboard-master`.
- `bot-matrix` is legacy/reference code, not a separate runtime authority.

## Runtime Roles

`growth_user_worker` uses Telegram user accounts for acquisition work:

- keyword-based group discovery and auto-join
- group/private message listening
- private replies and ad delivery
- acquisition tracking code creation and conversion attribution

`guardian_bot_worker` uses Bot API or bot/admin-capable Telegram clients for group operations:

- group event listening
- verification, moderation, punishment
- service commands and status replies
- managed group binding and permission state

Both roles read backend database configuration and report heartbeat state to
`telegram_worker_status`. Manual automation from the admin API is queued to the
Celery `automation` queue; API request threads do not run long Telegram jobs.

Current worker runtime behavior:

- `growth_user_worker` syncs enabled promoter accounts from the backend database
  into the backend `AccountPool`, keeps eligible Telethon user-account sessions
  connected, and dispatches group/private messages plus member-join events into
  `AcquisitionEventHandler`.
- `guardian_bot_worker` polls configured Bot API profiles, dispatches messages
  and join/leave events into `GuardianBot`, and updates bot profile heartbeats.
- Remaining growth runtime expansion points are richer auto-join/ad scheduling
  orchestration and production hardening around reconnect/backoff telemetry.

## XBoard Protocol

The supported protocol is HMAC signed `/api/v1/...`:

- `POST /api/v1/events/ingest`
- `GET /api/v1/users/status`
- `POST /api/v1/coupons/report`
- `POST /api/v1/xboard/callback/status`

Headers:

- `X-App-Id`
- `X-Timestamp`
- `X-Request-Id`
- `X-Signature`

Signing string:

```text
HTTP_METHOD
PATH
QUERY_STRING
X-Timestamp
X-Request-Id
RAW_BODY
```

Inbound Vanguard endpoints use `VANGUARD_APP_ID` and `VANGUARD_SIGNING_SECRET`.
XBoard callbacks use `VANGUARD_CALLBACK_APP_ID` and
`VANGUARD_CALLBACK_SIGNING_SECRET`.

## Tracking Ownership

`AcquisitionTracking.tracking_code` is the primary Telegram acquisition
attribution key. XBoard events and callbacks update AcquisitionTracking first.
CampaignTracking is reserved for campaign reward, coupon, trial, and managed
group campaign execution records.
