# Vanguard official GPT-6.1 Sol switch — 2026-11-05

Production now calls `gpt-6.1-sol` through `https://api.openai.com/v1` using the credential supplied in the user's local file. No credential is included in this report or Git changes.

The previous runtime already pinned default, fast and explicit OpenAI model selections to `gpt-6.1-sol`, but used the old relay. The effective changes were the OpenAI endpoint and API key for seven application services, plus a minimal client compatibility patch.

## Compatibility

The official endpoint rejected `max_tokens` and nondefault `temperature`. For this exact model on the official endpoint, the client now sends `max_completion_tokens` and omits `temperature`. Existing reasoning effort remains `low`. Other models and custom endpoints retain their existing request parameters.

## Deployment

The canonical runtime Compose remains `/opt/shared-postgres/stacks/vanguard/compose.json`. Five patch images were built from the seven services' existing images, replacing only `app/core/ai/llm_client.py`. The selected services were backend, celery-worker, celery-beat, resource-search-worker, telegram-growth-worker-survival-continuity, telegram-guardian-worker and reconciliation. The persistent private environment file was updated too. Other service configurations were checked against the saved original and are identical.

Compose's `--wait` reported that some workers have no healthcheck configured. Manual verification confirmed that all seven containers were running with zero restarts; backend and reconciliation were healthy, and worker startup/task logs showed resumed activity. Shared PostgreSQL, Redis and other services were not recreated.

## Validation

- 83 client, model-override, upstream-resilience and request-contract tests passed.
- Ruff and diff checks passed.
- The supplied key could access the exact requested model.
- The patched image returned `OK` on an isolated real API call from the production network.
- The running production backend returned `OK` using its actual configuration and client.
- The public health endpoint returned `healthy`.
- All seven application environments were checked for the target model, official endpoint and expected new key without printing the key.
- Temporary credential copies were removed. Production configuration and rollback copies remain private on the server.

Official model reference: https://developers.openai.com/api/docs/models/gpt-6.1-sol
