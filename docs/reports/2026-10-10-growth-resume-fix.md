# Growth resume and LLM switch (2026-10-10)

## Changes

- Qualification events are queued independently of a future advertisement schedule. A new `gap` or `rules` event can wake the existing approved review immediately while the next delivery slot remains governed by its own schedule.
- A pre-send outbound wait keeps a typed reason and retry deadline through the Telegram execution wrapper. It is recorded as deferred/skipped, releases an unattempted reservation, and does not become a failed or unknown Telegram send.
- The one-day account and membership warmup check is shared by qualification, inventory, selection, risk guard, and the final send gate. Legacy accounts without a durable managed start are not reset to a new warmup period.
- The owner capacity snapshot now reports the review lane explicitly and uses the available shared database connection budget without changing the configured pool size. Per-account session execution remains fenced.

## LLM runtime

All six Vanguard application services now use the OpenAI-compatible endpoint `https://pipenai.xyz/` with model `gpt-6.1-sol`. The key is stored only in the private production environment and runtime Compose; it is not in Git or this report.

## Validation

- Unit and boundary regression: 76 passed.
- Qualification and dynamic inventory regression: 104 passed.
- The LLM endpoint listed `gpt-6.1-sol`; an application-container call through `LLMClient` returned `OK` and reported the configured model.
- Production public health returned `{"status":"healthy","version":"1.0.0"}`.
- All six updated application containers are running with restart count 0. Shared PostgreSQL and Vanguard Redis remained running and healthy.
- A read-only production sample after deployment recorded one successful advertisement for account 2 in the last 15 minutes and no new `outbound_ad_interval` failures.

Backup and release evidence is retained on the server under `/opt/vanguard/backups/growth-resume-fix/20261010t133906z/`.
