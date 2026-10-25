"""
Vanguard Configuration Module

Application configuration using Pydantic Settings with environment variable support.
"""

import hashlib
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DEV_SECRET = "vanguard-development-secret-key-must-be-replaced-in-production-with-minimum-sixty-four-characters-long"
PLACEHOLDER_VANGUARD_SIGNING_SECRET = "replace-with-shared-secret"
PLACEHOLDER_VANGUARD_CALLBACK_SECRET = "replace-with-callback-secret"


def p5_cohort_rank(account_id: int) -> int:
    """Deterministic 0-99 cohort rank for one account.

    The rank never changes, so growing ``P5_COHORT_PERCENT`` from N to M only
    adds members; an account that already joined the cohort stays in it.
    """
    digest = hashlib.sha256(
        f"vanguard:p5:cohort:v1:{int(account_id)}".encode()
    ).hexdigest()
    return int(digest[:8], 16) % 100


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    APP_ENV: str = Field(default="development", description="Application environment")
    DEBUG: bool = Field(default=True, description="Debug mode")
    JWT_SECRET: str = Field(
        default=DEFAULT_DEV_SECRET,
        description="JWT secret key (minimum 64 characters for HS256)",
    )
    JWT_ALGORITHM: str = Field(default="HS256", description="JWT algorithm")
    JWT_EXPIRATION_HOURS: int = Field(
        default=24, ge=1, le=720, description="JWT expiration in hours"
    )

    # Self-owned promotional group orchestration (P0 safety controls)
    P0_SAFETY_GATE_ENABLED: bool = Field(
        default=True,
        description="Enable P0 resource and global-stop safety checks",
    )
    P0_SAFETY_GATE_FAIL_CLOSED: bool = Field(
        default=False,
        description="Block owned-group execution when Redis safety state is unavailable",
    )
    OWNED_GROUP_MODULE_ENABLED: bool = Field(
        default=True,
        description="Enable the self-owned group orchestration module and write API",
    )
    OWNED_GROUP_EXECUTION_ENABLED: bool = Field(
        default=False,
        description="Allow the owned-group worker to perform Telegram side effects",
    )
    OWNED_GROUP_GOVERNANCE_ENABLED: bool = Field(
        default=False,
        description="Enable explicit Guardian governance binding and owned-group event handling",
    )
    OWNED_GROUP_MESSAGING_ENABLED: bool = Field(
        default=False,
        description="Enable self-owned group AI/template message orchestration and Telegram sends",
    )
    OWNED_GROUP_AI_PERSONA_ENABLED: bool = Field(
        default=False,
        description="Enable account-level Persona for new self-owned-group AI executions",
    )
    OWNED_GROUP_KILL_SWITCH_ENABLED: bool = Field(
        default=False,
        description="Static emergency stop for all owned-group execution",
    )
    OWNED_GROUP_ROLLOUT_MAX_ACCOUNTS: int = Field(
        default=2000,
        ge=1,
        le=2000,
        description="Maximum promoter accounts in one owned-group operation (1-2000)",
    )

    @field_validator("JWT_SECRET")
    @classmethod
    def validate_secret_length(cls, v: str) -> str:
        if len(v) < 64:
            raise ValueError("JWT secret must be at least 64 characters long for HS256 security")
        return v

    @field_validator("SUB2API_ALERT_WEBHOOK_SECRET")
    @classmethod
    def validate_sub2api_alert_secret(cls, value: str | None) -> str | None:
        if value is not None and value.strip() and len(value.strip()) < 32:
            raise ValueError("SUB2API_ALERT_WEBHOOK_SECRET must contain at least 32 characters")
        return value

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://vanguard:change_me_in_production@postgres:5432/vanguard",
        description="Database connection URL"
    )
    DATABASE_POOL_SIZE: int = Field(default=10, description="Database pool size")
    DATABASE_MAX_OVERFLOW: int = Field(default=20, description="Database max overflow")

    # Growth P0 rollout. Disabling these flags retains all durable evidence.
    GROWTH_METRICS_ENABLED: bool = True
    BUDGET_LEDGER_ENABLED: bool = False
    RECONCILIATION_STREAM_ENABLED: bool = False
    RECONCILIATION_SHADOW_MODE: bool = True
    RECONCILIATION_ACCOUNT_IDS: str = "34"
    METRICS_SCRAPE_TOKEN: str | None = None

    # P1 runtime reliability rollout.  These controls are deliberately
    # additive and fail closed: the existing P0 allocation and inbox path
    # remain active until the rollout is explicitly enabled.
    P1_RUNTIME_RELIABILITY_ENABLED: bool = False
    P1_SYNC_TARGET_PERCENT: int = Field(default=35, ge=20, le=35)
    P1_SYNC_CANARY_ACCOUNT_IDS: str = "2"
    P1_SYNC_ROLLOUT_MODE: str = "account_allowlist"
    P1_INBOX_CLAIM_BATCH_SIZE: int = Field(default=20, ge=1, le=128)
    P1_INBOX_CLAIM_LEASE_SECONDS: int = Field(default=90, ge=30, le=900)
    # Legacy inbox values are independent of P1 so changing a disabled P1
    # rollout cannot silently change the production event path.
    LEGACY_INBOX_CLAIM_BATCH_SIZE: int = Field(default=32, ge=1, le=128)
    LEGACY_INBOX_CLAIM_LEASE_SECONDS: int = Field(default=90, ge=30, le=900)
    P1_INBOX_COALESCE_SECONDS: int = Field(default=30, ge=0, le=300)
    P1_INBOX_BASELINE_AT: str | None = Field(
        default=None,
        description="Fixed UTC cutoff for reporting historical inbox backlog separately; unset includes all events in the ordinary SLO",
    )

    # P2 durable join/advertisement Outbox rollout.  The lanes are kept
    # independently reversible so a canary can enqueue one operation kind at
    # a time while retaining every durable attempt and reconciliation record.
    # A disabled lane leaves the legacy scheduler in charge; it never deletes
    # rows that were already written to the P2 tables.
    P2_OUTBOX_ENABLED: bool = Field(
        default=False,
        description="Enable P2 account-Actor Outbox dispatch",
    )
    P2_JOIN_OUTBOX_ENABLED: bool = Field(
        default=False,
        description="Route automatic group joins through the P2 Outbox",
    )
    P2_AD_OUTBOX_ENABLED: bool = Field(
        default=False,
        description="Route advertisement sends through the P2 Outbox",
    )
    P2_SHADOW_MODE: bool = Field(
        default=True,
        description="Create/measure P2 work without allowing a second Telegram side effect",
    )
    P2_ACCOUNT_IDS: str = Field(
        default="",
        description="Comma-separated P2 canary account IDs; empty applies to no accounts",
    )
    P2_OUTBOX_BATCH_SIZE: int = Field(default=20, ge=1, le=128)
    P2_OUTBOX_LEASE_SECONDS: int = Field(default=300, ge=30, le=1800)
    P2_RECONCILIATION_MAX_AGE_SECONDS: int = Field(default=3600, ge=60, le=604800)

    # Explicit P2/P6 direct-production scope.  It is disabled by default and
    # therefore cannot widen a normal P2 canary.  When enabled, producers are
    # structurally limited to the two approved accounts/groups and campaign;
    # an operator supplied run id is mandatory before any operation can be
    # enqueued.
    P2_DIRECT_SCOPE_ENABLED: bool = Field(default=False)
    P2_DIRECT_SCOPE_ACCOUNT_IDS: str = Field(default="33,35")
    P2_DIRECT_SCOPE_GROUP_IDS: str = Field(default="231,178")
    P2_DIRECT_SCOPE_CAMPAIGN_ID: int = Field(default=1, ge=1)
    P2_DIRECT_SCOPE_RUN_ID: str = Field(default="")

    # P3 Telegram read-cache and immutable qualification snapshot rollout.
    # Each lane is independently reversible; disabling a flag retains cache,
    # evidence and snapshots while falling back to the existing Actor/read
    # budget path.
    P3_ENTITY_CACHE: bool = Field(
        default=False,
        description="Enable Telegram username/InputPeer/ID read cache",
    )
    P3_GROUP_FACT_CACHE: bool = Field(
        default=False,
        description="Enable incremental membership and permission fact cache",
    )
    P3_SURVIVAL_CACHE: bool = Field(
        default=False,
        description="Enable short-lived advertisement survival fact cache",
    )
    P3_QUALIFICATION_SNAPSHOT: bool = Field(
        default=False,
        description="Enable immutable qualification snapshots as P2 Outbox input",
    )

    # P4 ad fairness pacing, admin metrics and history governance rollout.
    # Every lane is additive and independently reversible: disabling a flag
    # restores the pre-P4 dispatch, metrics and retention behaviour without
    # touching durable P0-P3 state.
    P4_AD_PACING: bool = Field(
        default=False,
        description="Order the growth ads lane by the deterministic fairness pacing score instead of cursor rotation",
    )
    P4_AD_PACING_SHADOW: bool = Field(
        default=False,
        description="Record pacing decisions without changing the existing ads dispatch order",
    )
    P4_AD_PACING_ACCOUNT_IDS: str = Field(
        default="",
        description="Comma-separated canary account IDs subject to real ads pacing; empty applies to all ads candidates",
    )
    P4_SURVIVAL_BACKLOG_LIMIT: int = Field(
        default=30,
        ge=1,
        le=5000,
        description="Pacing hard gate: skip ads pacing for accounts with more overdue survival rows",
    )
    P4_UNRESOLVED_BACKLOG_LIMIT: int = Field(
        default=20,
        ge=1,
        le=5000,
        description="Pacing hard gate: skip ads pacing for accounts with more unresolved ad sends",
    )
    P4_PACING_DECISION_RETENTION_DAYS: int = Field(
        default=14,
        ge=1,
        le=370,
        description="Hot retention for pacing decision and derived queue statistic rows",
    )
    P4_ADMIN_METRICS: bool = Field(
        default=False,
        description="Expose P4 pacing, queue, receipt and history governance metrics plus the summary API payload",
    )
    P4_HISTORY_GOVERNANCE: bool = Field(
        default=False,
        description="Enable history retention governance passes (dry-run first, then batched archive)",
    )
    P4_HISTORY_GOVERNANCE_DRY_RUN: bool = Field(
        default=True,
        description="Governance passes only report candidates; set false to archive and delete after review",
    )
    P4_HISTORY_HOT_RETENTION_DAYS: int = Field(
        default=90,
        ge=1,
        le=3650,
        description="Hot retention for resolved operations, attempts, receipts and Outbox rows",
    )
    P4_HISTORY_AUDIT_RETENTION_DAYS: int = Field(
        default=370,
        ge=1,
        le=3650,
        description="Retention for archived audit evidence, state transitions and daily aggregate metrics",
    )
    P4_HISTORY_ARCHIVE_BATCH_SIZE: int = Field(
        default=200,
        ge=1,
        le=2000,
        description="Maximum terminal operations archived per governance batch",
    )

    # P5 unified release control plane for the P2-P4 growth release unit.
    # ``off`` leaves the pre-P5 phase flags in charge (transparent).  Any
    # governing mode (canary/cohort/full) makes the P5 cohort the single
    # release-unit scope: it replaces the per-phase allowlists while the P2/P3
    # master and lane flags stay mandatory.  ``shadow`` runs the controller
    # (selection, gates, records) without allowing any real release traffic.
    # The runtime kill switch is the persisted stopped state written by the
    # observer; mode ``off`` must never leave a stop key that blocks legacy
    # ads/join admission.
    P5_ROLLOUT_MODE: str = Field(
        default="off",
        description="P5 rollout mode: off|shadow|canary|cohort|full",
    )
    P5_CANARY_ACCOUNT_IDS: str = Field(
        default="",
        description="Comma-separated resolved canary account IDs; empty is fail closed",
    )
    P5_COHORT_PERCENT: int = Field(
        default=0,
        ge=0,
        le=100,
        description="Stable-hash cohort percentage; growth from N% to M% keeps existing members",
    )
    P5_HISTORY_APPLY: bool = Field(
        default=False,
        description="Real history archiving requires an explicit approval after P5 observation; false keeps governance dry-run",
    )
    P5_RELEASE_ID: str = Field(
        default="",
        description="Immutable release id of the frozen P5 release candidate",
    )

    @field_validator("P5_ROLLOUT_MODE")
    @classmethod
    def validate_p5_rollout_mode(cls, value: str) -> str:
        normalized = (value or "off").strip().lower()
        if normalized not in {"off", "shadow", "canary", "cohort", "full"}:
            raise ValueError("P5_ROLLOUT_MODE must be one of off|shadow|canary|cohort|full")
        return normalized

    @property
    def p1_sync_canary_accounts(self) -> set[int]:
        return {
            int(value.strip())
            for value in self.P1_SYNC_CANARY_ACCOUNT_IDS.split(",")
            if value.strip().isdigit() and int(value.strip()) > 0
        }

    def p1_sync_enabled_for(self, account_id: int) -> bool:
        if not self.P1_RUNTIME_RELIABILITY_ENABLED:
            return False
        mode = (self.P1_SYNC_ROLLOUT_MODE or "account_allowlist").strip().lower()
        if mode in {"all", "all_accounts"}:
            return True
        return account_id in self.p1_sync_canary_accounts

    @property
    def growth_inbox_claim_batch_size(self) -> int:
        return (
            self.P1_INBOX_CLAIM_BATCH_SIZE
            if self.P1_RUNTIME_RELIABILITY_ENABLED
            else self.LEGACY_INBOX_CLAIM_BATCH_SIZE
        )

    @property
    def growth_inbox_claim_lease_seconds(self) -> int:
        return (
            self.P1_INBOX_CLAIM_LEASE_SECONDS
            if self.P1_RUNTIME_RELIABILITY_ENABLED
            else self.LEGACY_INBOX_CLAIM_LEASE_SECONDS
        )

    @property
    def p2_canary_accounts(self) -> set[int]:
        """Return explicitly selected P2 accounts; an empty value is fail closed."""
        return {
            int(value.strip())
            for value in self.P2_ACCOUNT_IDS.split(",")
            if value.strip().isdigit() and int(value.strip()) > 0
        }

    def p2_outbox_enabled_for(self, kind: str, account_id: int) -> bool:
        """Check the P2 lane and account canary gate for an operation kind.

        When the P5 rollout governs (canary/cohort/full), the P5 cohort is the
        one release-unit scope and replaces the underlying allowlist; shadow
        mode keeps the release unit record-only.  Mode ``off`` leaves the
        pre-P5 baseline flags in charge.
        """
        if not self.P2_OUTBOX_ENABLED:
            return False
        if kind == "join_group":
            if not self.P2_JOIN_OUTBOX_ENABLED:
                return False
        elif kind == "send_ad":
            if not self.P2_AD_OUTBOX_ENABLED:
                return False
        else:
            return False
        mode = self.p5_rollout_mode
        if mode == "off":
            return int(account_id) in self.p2_canary_accounts
        if mode == "shadow":
            return False
        return self.p5_cohort_member(int(account_id))

    @staticmethod
    def _positive_ids(value: str) -> set[int]:
        return {
            int(item.strip())
            for item in (value or "").split(",")
            if item.strip().isdigit() and int(item.strip()) > 0
        }

    @property
    def p2_direct_scope_accounts(self) -> set[int]:
        return self._positive_ids(self.P2_DIRECT_SCOPE_ACCOUNT_IDS)

    @property
    def p2_direct_scope_groups(self) -> set[int]:
        return self._positive_ids(self.P2_DIRECT_SCOPE_GROUP_IDS)

    def p2_direct_scope_allows(
        self,
        *,
        account_id: int,
        group_id: int,
        campaign_id: int | None = None,
        run_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Apply the explicit direct-production account/group/campaign fence."""

        if not self.P2_DIRECT_SCOPE_ENABLED:
            return True, None
        if int(account_id) not in self.p2_direct_scope_accounts:
            return False, "p2_direct_scope_account_blocked"
        if int(group_id) not in self.p2_direct_scope_groups:
            return False, "p2_direct_scope_group_blocked"
        if campaign_id is not None and int(campaign_id) != int(self.P2_DIRECT_SCOPE_CAMPAIGN_ID):
            return False, "p2_direct_scope_campaign_blocked"
        configured_run = (self.P2_DIRECT_SCOPE_RUN_ID or "").strip()
        supplied_run = (run_id or "").strip()
        if not configured_run:
            return False, "p2_direct_scope_run_id_required"
        if supplied_run != configured_run:
            return False, "p2_direct_scope_run_id_mismatch"
        return True, None

    @property
    def p5_canary_accounts(self) -> set[int]:
        """Explicitly resolved P5 canary accounts; empty is fail closed."""
        return {
            int(value.strip())
            for value in self.P5_CANARY_ACCOUNT_IDS.split(",")
            if value.strip().isdigit() and int(value.strip()) > 0
        }

    @property
    def p5_rollout_mode(self) -> str:
        """Normalized P5 rollout mode (the field validator also guards it)."""
        return (self.P5_ROLLOUT_MODE or "off").strip().lower()

    @property
    def p5_governs(self) -> bool:
        """True when the P5 cohort is the active release-unit scope."""
        return self.p5_rollout_mode in {"canary", "cohort", "full"}

    def p5_cohort_member(self, account_id: int) -> bool:
        """Cohort membership while P5 governs.

        Members are the resolved canary accounts plus the stable-hash cohort;
        canary accounts stay members at every percentage so expansion never
        removes an account that already runs the release unit.
        """
        if int(account_id) in self.p5_canary_accounts:
            return True
        return p5_cohort_rank(int(account_id)) < self.P5_COHORT_PERCENT

    @property
    def p4_pacing_canary_accounts(self) -> set[int]:
        """Empty means every ads candidate is subject to real pacing."""
        return {
            int(value.strip())
            for value in self.P4_AD_PACING_ACCOUNT_IDS.split(",")
            if value.strip().isdigit() and int(value.strip()) > 0
        }

    @property
    def p4_pacing_active(self) -> bool:
        """Real reordering requires the master flag; shadow mode only records."""
        return self.P4_AD_PACING and not self.P4_AD_PACING_SHADOW

    @property
    def reconciliation_accounts(self) -> set[int]:
        """An explicit '*' is required for all-account rollout; empty is none."""
        return {int(value.strip()) for value in self.RECONCILIATION_ACCOUNT_IDS.split(",")
                if value.strip().isdigit() and int(value.strip()) > 0}

    # Redis
    REDIS_URL: str = Field(default="redis://localhost:6379/0", description="Redis connection URL")
    REDIS_PASSWORD: str | None = Field(default=None, description="Legacy fallback; prefer the password embedded in REDIS_URL")

    # Telegram
    TELEGRAM_API_ID: str | None = Field(default=None, description="Telegram API ID")
    TELEGRAM_API_HASH: str | None = Field(default=None, description="Telegram API hash")
    BOT_TOKEN: str | None = Field(default=None, description="Telegram bot token")
    TELEGRAM_SESSION_DIR: str = Field(default="./sessions", description="Telegram session files directory")
    TELEGRAM_SESSION_ENCRYPTION_KEY: str | None = Field(default=None, description="Encryption key for Telegram StringSession values")
    TELEGRAM_READ_BUDGET_PERCENT: int = Field(
        default=100,
        ge=100,
        le=1000,
        description="Scale local minute/hour/day read budgets; 150 adds 50%, preserving account cooldowns",
    )
    SESSION_EXEC_CONCURRENCY: int = Field(
        default=2,
        ge=1,
        le=8,
        description="Concurrent forwarded stage executions in the session-owner worker; different accounts run in parallel, one account stays serial. Bounded by the database pool.",
    )
    APP_VERSION_ROLL_ENABLED: bool = Field(
        default=True,
        description="Daily roll of outdated declared Telegram app versions onto the current pool",
    )
    APP_VERSION_ROLL_BATCH_SIZE: int = Field(
        default=3,
        description="Max accounts whose app_version is rolled per day",
    )

    # QQ through NapCatQQ OneBot 11
    QQ_ONEBOT_ENABLED: bool = Field(default=False, description="Enable NapCatQQ OneBot 11")
    QQ_ONEBOT_ACCOUNT_ID: str | None = Field(default=None, description="QQ account number logged in to NapCat")
    QQ_ONEBOT_HTTP_URL: str = Field(
        default="http://napcat:3000",
        description="NapCat OneBot 11 HTTP API URL",
    )
    QQ_ONEBOT_WS_URL: str = Field(
        default="ws://napcat:3001",
        description="NapCat OneBot 11 WebSocket server URL",
    )
    QQ_ONEBOT_ACCESS_TOKEN: str | None = Field(
        default=None,
        description="Shared access token configured in NapCat OneBot network settings",
    )
    QQ_ONEBOT_REQUEST_TIMEOUT_SECONDS: float = Field(
        default=10.0,
        ge=2.0,
        le=60.0,
        description="NapCat OneBot HTTP timeout",
    )
    QQ_ONEBOT_MESSAGE_RETENTION_DAYS: int = Field(
        default=30,
        ge=1,
        le=365,
        description="QQ monitored message retention in days",
    )

    # Proxy Service
    PROXY_PROVIDER: str = Field(default="evomi", description="Proxy provider for promoter accounts: evomi/decodo")
    PROMOTER_PROXY_REQUIRED: bool = Field(default=True, description="Require proxy before promoter account connects")
    RISK_GUARD_FAIL_CLOSED: bool = Field(default=False, description="Block high-risk account actions when Redis risk budget is unavailable")

    # Evomi Proxy Service
    EVOMI_API_KEY: str | None = Field(default=None, description="Evomi API key for proxy services")
    EVOMI_PROXY_HOST: str | None = Field(default=None, description="Static Evomi proxy host")
    EVOMI_PROXY_PORT: int | None = Field(default=None, description="Static Evomi proxy port")
    EVOMI_PROXY_USERNAME: str | None = Field(default=None, description="Static Evomi proxy username")
    EVOMI_PROXY_PASSWORD: str | None = Field(default=None, description="Static Evomi proxy password")
    EVOMI_PRODUCT_CODE: str = Field(default="rp", description="Evomi product code for generated proxies")
    EVOMI_PROTOCOL: str = Field(default="http", description="Evomi proxy protocol")
    EVOMI_SESSION_TYPE: str = Field(
        default="hard",
        description="Evomi session type; hard keeps a stable Telegram proxy exit without a timed rotation",
    )
    EVOMI_SESSION_LIFETIME_MINUTES: int = Field(default=30, description="Evomi sticky session lifetime in minutes")
    EVOMI_SESSION_NAMESPACE: str = Field(default="vanguard", description="Evomi sticky session namespace")
    EVOMI_ADBLOCK: bool = Field(default=False, description="Enable Evomi adblock where supported")
    EVOMI_COUNTRY_VERIFY_ENABLED: bool = Field(default=False, description="Verify proxy exit country before use")
    EVOMI_COUNTRY_VERIFY_ATTEMPTS: int = Field(default=5, description="Maximum country verification attempts")
    EVOMI_COUNTRY_VERIFY_TIMEOUT_SECONDS: int = Field(default=8, description="Proxy country verification timeout")
    EVOMI_COUNTRY_VERIFY_URL: str = Field(
        default="http://ip-api.com/json/?fields=status,countryCode,query",
        description="Proxy country verification endpoint",
    )

    # Decodo Proxy Service (legacy fallback)
    DECODO_API_KEY: str | None = Field(default=None, description="Decodo API key for proxy services")

    # Vanguard <-> XBoard API integration
    VANGUARD_INTEGRATION_ENABLED: bool = Field(default=True, description="Enable Vanguard XBoard integration")
    VANGUARD_APP_ID: str = Field(default="vanguard", description="Inbound XBoard app id")
    VANGUARD_SIGNING_SECRET: str = Field(default=PLACEHOLDER_VANGUARD_SIGNING_SECRET, description="Inbound signing secret")
    VANGUARD_TIMESTAMP_TOLERANCE: int = Field(default=300, description="Timestamp tolerance in seconds")
    VANGUARD_CALLBACK_ENABLED: bool = Field(default=True, description="Enable Vanguard callback endpoint")
    VANGUARD_CALLBACK_APP_ID: str = Field(default="xboard", description="Callback app id")
    VANGUARD_CALLBACK_SIGNING_SECRET: str = Field(default=PLACEHOLDER_VANGUARD_CALLBACK_SECRET, description="Callback signing secret")

    # Sub2API Admin integration for campaign redeem-code coupons
    SUB2API_ENABLED: bool = Field(default=False, description="Enable Sub2API redeem-code coupon generation")
    SUB2API_BASE_URL: str = Field(default="", description="Sub2API base URL")
    SUB2API_ADMIN_API_KEY: str | None = Field(default=None, description="Sub2API admin API key")
    SUB2API_TIMEOUT: int = Field(default=5, description="Sub2API request timeout in seconds")
    SUB2API_ALERT_WEBHOOK_SECRET: str | None = Field(
        default=None,
        description="Shared HMAC secret for inbound Sub2API alert and announcement webhooks",
    )
    SUB2API_ALERT_TIMESTAMP_TOLERANCE: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Maximum accepted Sub2API alert webhook clock skew in seconds",
    )
    SUB2API_ALERT_IDEMPOTENCY_TTL_SECONDS: int = Field(
        default=2592000,
        ge=3600,
        le=7776000,
        description="Retention for per-channel Sub2API alert idempotency keys",
    )

    # LLM/AI
    OPENAI_API_KEY: str | None = Field(default=None, description="OpenAI API Key")
    OPENAI_BASE_URL: str | None = Field(default=None, description="OpenAI-compatible API base URL")
    GROUP_AD_POLICY_DEDICATED_ENABLED: bool = Field(
        default=False,
        description=(
            "Opt into the legacy dedicated group-ad-policy endpoint; "
            "when false, group policy uses the shared OpenAI-compatible endpoint"
        ),
    )
    GROUP_AD_POLICY_API_KEY: str | None = Field(default=None, description="Dedicated group ad policy API key")
    GROUP_AD_POLICY_BASE_URL: str | None = Field(default=None, description="Dedicated group ad policy OpenAI-compatible base URL")
    ANTHROPIC_API_KEY: str | None = Field(default=None, description="Anthropic API Key")
    LLM_PROVIDER: str = Field(default="openai", description="LLM provider: openai/anthropic/local")
    LLM_MODEL: str = Field(default="gpt-6.1-sol", description="Default LLM model")
    LLM_FAST_MODEL: str = Field(default="", description="Low-latency LLM model; falls back to LLM_MODEL when empty")
    LLM_MODEL_OVERRIDE: str = Field(default="", description="Pin all OpenAI-compatible requests to this model, including explicit caller models")
    LLM_REASONING_EFFORT: str = Field(default="", description="Optional reasoning effort for OpenAI-compatible requests")

    # CORS
    CORS_ORIGINS: str = Field(
        default="http://localhost:3000,http://localhost:8000",
        description="Allowed CORS origins (comma-separated)"
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ORIGINS string to list."""
        if isinstance(self.CORS_ORIGINS, str):
            return [origin.strip() for origin in self.CORS_ORIGINS.split(',') if origin.strip()]
        return self.CORS_ORIGINS if isinstance(self.CORS_ORIGINS, list) else []

    # Celery
    CELERY_BROKER_URL: str = Field(default="redis://localhost:6379/1", description="Celery broker URL")
    CELERY_RESULT_BACKEND: str = Field(default="redis://localhost:6379/2", description="Celery result backend")

    # Alert
    ALERT_CHAT_ID: str | None = Field(default=None, description="Telegram chat ID for alerts")

    # Logging
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")
    LOG_STDOUT_LEVEL: str = Field(
        default="INFO",
        description="Minimum level written to stdout",
    )
    LOG_FILE: str | None = Field(
        default=None,
        description="Optional application log file path",
    )
    LOG_RETENTION_DAYS: int = Field(
        default=15,
        ge=1,
        le=365,
        description="Daily application log files to retain",
    )

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.APP_ENV.lower() in {"production", "prod"}

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.APP_ENV.lower() in {"development", "dev", "local"}

    @model_validator(mode="after")
    def validate_production_defaults(self):
        """Fail fast if production is started with development secrets."""
        if not self.LLM_FAST_MODEL.strip():
            self.LLM_FAST_MODEL = self.LLM_MODEL

        if self.QQ_ONEBOT_ENABLED:
            account_id = (self.QQ_ONEBOT_ACCOUNT_ID or "").strip()
            if not account_id or not account_id.isdigit():
                raise ValueError(
                    "QQ_ONEBOT_ACCOUNT_ID must be a numeric QQ account when QQ_ONEBOT_ENABLED=true"
                )
            if not self.QQ_ONEBOT_ACCESS_TOKEN or len(self.QQ_ONEBOT_ACCESS_TOKEN) < 32:
                raise ValueError(
                    "QQ_ONEBOT_ACCESS_TOKEN must contain at least 32 characters when QQ_ONEBOT_ENABLED=true"
                )

        if self.APP_ENV.lower() not in {"production", "prod"}:
            return self

        errors: list[str] = []
        # Real owned-group Telegram side effects are an explicit opt-in. When
        # enabled in production, startup must prove that an enabled,
        # fail-closed safety gate protects the operation. The static kill
        # switch remains a runtime control, so operators may start in an
        # emergency-stopped state without removing the execution flag.
        if self.OWNED_GROUP_EXECUTION_ENABLED:
            if not self.OWNED_GROUP_MODULE_ENABLED:
                errors.append(
                    "OWNED_GROUP_MODULE_ENABLED must be true when "
                    "OWNED_GROUP_EXECUTION_ENABLED is enabled in production"
                )
            if not self.P0_SAFETY_GATE_ENABLED:
                errors.append(
                    "P0_SAFETY_GATE_ENABLED must be true when "
                    "OWNED_GROUP_EXECUTION_ENABLED is enabled in production"
                )
            if not self.P0_SAFETY_GATE_FAIL_CLOSED:
                errors.append(
                    "P0_SAFETY_GATE_FAIL_CLOSED must be true when "
                    "OWNED_GROUP_EXECUTION_ENABLED is enabled in production"
                )
        if self.JWT_SECRET == DEFAULT_DEV_SECRET:
            errors.append("JWT_SECRET must be set explicitly in production")
        if "change_me_in_production" in self.DATABASE_URL:
            errors.append("DATABASE_URL must not use the development password in production")
        if self.VANGUARD_INTEGRATION_ENABLED and self.VANGUARD_SIGNING_SECRET == PLACEHOLDER_VANGUARD_SIGNING_SECRET:
            errors.append("VANGUARD_SIGNING_SECRET must be set explicitly in production")
        if self.VANGUARD_CALLBACK_ENABLED and self.VANGUARD_CALLBACK_SIGNING_SECRET == PLACEHOLDER_VANGUARD_CALLBACK_SECRET:
            errors.append("VANGUARD_CALLBACK_SIGNING_SECRET must be set explicitly in production")
        if self.GROUP_AD_POLICY_DEDICATED_ENABLED and (
            not self.GROUP_AD_POLICY_API_KEY or not self.GROUP_AD_POLICY_BASE_URL
        ):
            errors.append(
                "GROUP_AD_POLICY_API_KEY and GROUP_AD_POLICY_BASE_URL must both be set "
                "when GROUP_AD_POLICY_DEDICATED_ENABLED=true"
            )
        # A governing P5 release must be attributable to one frozen, immutable
        # release record; an unattributable release unit fails closed.
        if self.p5_governs and not self.P5_RELEASE_ID.strip():
            errors.append(
                "P5_RELEASE_ID must be set when P5_ROLLOUT_MODE is canary, cohort or full in production"
            )
        if self.P2_OUTBOX_ENABLED and (
            self.P2_JOIN_OUTBOX_ENABLED or self.P2_AD_OUTBOX_ENABLED
        ) and not self.P3_QUALIFICATION_SNAPSHOT:
            errors.append(
                "P3_QUALIFICATION_SNAPSHOT must be enabled before enabling a P2 Outbox lane"
            )
        if self.P2_DIRECT_SCOPE_ENABLED:
            if not self.p2_direct_scope_accounts:
                errors.append("P2_DIRECT_SCOPE_ACCOUNT_IDS must contain at least one account")
            elif len(self.p2_direct_scope_accounts) > 2:
                errors.append("P2_DIRECT_SCOPE_ACCOUNT_IDS may contain at most two accounts")
            if not self.p2_direct_scope_groups:
                errors.append("P2_DIRECT_SCOPE_GROUP_IDS must contain at least one group")
            elif len(self.p2_direct_scope_groups) > 2:
                errors.append("P2_DIRECT_SCOPE_GROUP_IDS may contain at most two groups")
            if not self.P2_DIRECT_SCOPE_RUN_ID.strip():
                errors.append(
                    "P2_DIRECT_SCOPE_RUN_ID must be set when P2_DIRECT_SCOPE_ENABLED is enabled in production"
                )
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def effective_redis_password(self) -> str | None:
        """Use the URL credential first; REDIS_PASSWORD remains a legacy fallback."""
        try:
            return None if urlsplit(self.REDIS_URL).password is not None else self.REDIS_PASSWORD
        except ValueError:
            return self.REDIS_PASSWORD


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Global settings instance
settings = get_settings()
