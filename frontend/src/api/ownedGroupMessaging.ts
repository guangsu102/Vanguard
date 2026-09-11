import apiClient from "./client";

export type OwnedGroupMessageMode = "ai" | "template" | "off";
export type OwnedGroupMessagePurpose = "community_ai" | "template";
export type OwnedGroupMessageCategory = "community" | "promotion";
export type OwnedGroupMessageTriggerType =
  "scheduled" | "keyword" | "reply" | "manual";
export type OwnedGroupMessageExecutionStatus =
  | "queued"
  | "generating"
  | "pending_review"
  | "ready_to_send"
  | "sending"
  | "sent"
  | "skipped"
  | "failed"
  | "rejected"
  | "expired"
  | "cancelled";

export interface ScheduledTriggerConfig {
  enabled: boolean;
  timezone: "Asia/Shanghai";
  weekdays: number[];
  times: string[];
  jitter_seconds: number;
  content_category: OwnedGroupMessageCategory;
}

export interface KeywordMessageTriggerConfig {
  enabled: boolean;
  trigger_ids: number[];
  reply_to_source: boolean;
  content_category: OwnedGroupMessageCategory;
}

export interface ReplyMessageTriggerConfig {
  enabled: boolean;
  strategy: "directed" | "semantic";
  semantic_min_confidence: number;
  context_messages: number;
  content_category: OwnedGroupMessageCategory;
}

export interface ManualMessageTriggerConfig {
  enabled: boolean;
  allowed_content_categories: OwnedGroupMessageCategory[];
}

export interface OwnedGroupMessageTriggerConfig {
  version: 1;
  scheduled: ScheduledTriggerConfig;
  keyword: KeywordMessageTriggerConfig;
  reply: ReplyMessageTriggerConfig;
  manual: ManualMessageTriggerConfig;
  dedupe_window_seconds: number;
}

export interface OwnedGroupPromotionConfig {
  mode: OwnedGroupMessageMode;
  default_template_id: number | null;
  destination_url: string | null;
  cta_text: string | null;
}

export interface OwnedGroupMessagingRuntimeStatus {
  static_enabled?: boolean;
  runtime_enabled?: boolean;
  enabled?: boolean;
  dry_run?: boolean;
  can_enable?: boolean;
  can_send?: boolean;
}

export interface OwnedGroupMessagingRuntimeLimits {
  /** Canonical backend response fields. */
  global_group_daily_limit?: number;
  global_account_daily_limit?: number;
  dedupe_window_seconds?: number;
  max_attempts?: number;
  /** Backward-compatible aliases accepted from earlier frontend fixtures. */
  global_max_per_group_per_day?: number;
  global_max_per_account_per_day?: number;
  min_group_cooldown_seconds?: number;
  content_dedupe_window_seconds?: number;
  review_ttl_hours?: number;
  max_send_attempts?: number;
}

export interface OwnedGroupMessagingStats {
  sent_today?: number;
  policy_count?: number;
  enabled_policy_count?: number;
  pending_review_count?: number;
  group_sent_today?: number;
  community_sent_today?: number;
  promotion_sent_today?: number;
  last_sent_at?: string | null;
}

export interface OwnedGroupMessagingAssetSummary {
  asset_id: number;
  title?: string;
  status?: string;
  core_group_id: number | null;
  telegram_chat_id: number | null;
  governance_status: string;
  messaging_status?: OwnedGroupMessagingRuntimeStatus;
  runtime_limits?: OwnedGroupMessagingRuntimeLimits;
  stats?: OwnedGroupMessagingStats;
}

export interface OwnedGroupMessageEligibleAccount {
  account_id: number;
  display_name: string;
  status: string;
  risk_level: string;
  operation_mode: string;
  membership_status: string;
  last_verified_at: string | null;
  eligible: boolean;
  blocking_reasons: string[];
  policy_id: number | null;
}

export interface OwnedGroupEligibleAccountsResponse {
  data: OwnedGroupMessageEligibleAccount[];
  asset: OwnedGroupMessagingAssetSummary;
  correlation_id?: string | null;
}

export interface OwnedGroupMessagePolicyPersonaSummary {
  account_id: number;
  configured: boolean;
  name: string | null;
  revision: number;
  applicable: boolean;
  effective_enabled: boolean;
}

export type OwnedGroupMessageExecutionPersonaSource =
  | "configured"
  | "neutral_default"
  | "feature_disabled_default"
  | "legacy_default"
  | "legacy_untracked";

export interface OwnedGroupMessageExecutionPersonaSummary {
  source: OwnedGroupMessageExecutionPersonaSource;
  name: string | null;
  revision: number | null;
  hash_prefix: string | null;
}

export interface OwnedGroupMessagePolicy {
  id: number;
  owned_group_asset_id: number;
  core_group_id: number;
  account_id: number;
  mode: OwnedGroupMessageMode;
  default_template_id: number | null;
  trigger_config: OwnedGroupMessageTriggerConfig;
  promotion_config: OwnedGroupPromotionConfig;
  daily_limit: number;
  cooldown_seconds: number;
  allowed_topics: string[];
  require_review: boolean;
  enabled: boolean;
  revision: number;
  created_at?: string;
  updated_at?: string;
  account_display_name?: string;
  account_eligible?: boolean;
  account_blocking_reasons?: string[];
  sent_today?: number;
  community_sent_today?: number;
  promotion_sent_today?: number;
  group_sent_today?: number;
  remaining_today?: number;
  last_sent_at?: string | null;
  cooldown_until?: string | null;
  pending_review_count?: number;
  persona: OwnedGroupMessagePolicyPersonaSummary;
}

export interface OwnedGroupMessagePolicyCreateInput {
  account_id: number;
  mode: OwnedGroupMessageMode;
  default_template_id: number | null;
  trigger_config: OwnedGroupMessageTriggerConfig;
  promotion_config: OwnedGroupPromotionConfig;
  daily_limit: number;
  cooldown_seconds: number;
  allowed_topics: string[];
  require_review: boolean;
  enabled: boolean;
}

export interface OwnedGroupMessagePolicyReplaceInput extends Omit<
  OwnedGroupMessagePolicyCreateInput,
  "account_id"
> {
  revision: number;
}

export interface OwnedGroupMessagePreviewInput {
  trigger_type: OwnedGroupMessageTriggerType;
  content_category: OwnedGroupMessageCategory;
  topic?: string | null;
  instruction?: string | null;
  template_id?: number | null;
  variables?: Record<string, string>;
}

export interface OwnedGroupMessagePreviewResult {
  content: string;
  normalized_content: string;
  content_hash: string;
  message_purpose: OwnedGroupMessagePurpose;
  content_category: OwnedGroupMessageCategory;
  warnings: string[];
  would_require_review: boolean;
}

export interface OwnedGroupMessageManualExecutionInput extends OwnedGroupMessagePreviewInput {
  trigger_type: "manual";
  reply_to_message_id?: number | null;
  scheduled_at?: string | null;
}

export interface OwnedGroupMessageExecutionTimelineItem {
  status: OwnedGroupMessageExecutionStatus | string;
  at: string;
  actor_id?: number | null;
  note?: string | null;
}

export interface OwnedGroupMessageExecutionSummary {
  id: number;
  execution_id?: number;
  policy_id: number;
  owned_group_asset_id: number;
  core_group_id: number;
  telegram_chat_id: number;
  account_id: number;
  account_display_name?: string;
  trigger_type: OwnedGroupMessageTriggerType;
  message_purpose: OwnedGroupMessagePurpose;
  content_category: OwnedGroupMessageCategory;
  mode_snapshot: Exclude<OwnedGroupMessageMode, "off">;
  policy_revision: number;
  status: OwnedGroupMessageExecutionStatus;
  source_message_id?: number | null;
  reply_to_message_id?: number | null;
  keyword_trigger_id?: number | null;
  template_id?: number | null;
  topic?: string | null;
  content?: string | null;
  content_summary?: string | null;
  content_hash?: string | null;
  idempotency_key?: string;
  correlation_id: string;
  scheduled_at?: string | null;
  next_retry_at?: string | null;
  attempt_count: number;
  revision: number;
  requested_by?: number | null;
  reviewer_id?: number | null;
  reviewer_name?: string | null;
  reviewed_at?: string | null;
  review_expires_at?: string | null;
  telegram_message_id?: number | null;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
  sent_at?: string | null;
  timeline?: OwnedGroupMessageExecutionTimelineItem[];
  persona?: OwnedGroupMessageExecutionPersonaSummary | null;
  prompt_template_version?: string | null;
  prompt_hash_prefix?: string | null;
  governance_rules_hash_prefix?: string | null;
}

export interface OwnedGroupMessageExecutionDetail extends OwnedGroupMessageExecutionSummary {
  trigger_context?: Record<string, unknown> | null;
  policy_snapshot?: Record<string, unknown> | null;
  prompt_context?: Record<string, unknown> | null;
  promotion_config_snapshot?: OwnedGroupPromotionConfig | null;
  audit_event_ids?: number[];
}

export interface OwnedGroupMessageExecutionPage {
  items: OwnedGroupMessageExecutionSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface OwnedGroupMessageExecutionAccepted {
  id?: number;
  execution_id: number;
  status: OwnedGroupMessageExecutionStatus;
  correlation_id: string | null;
  scheduled_at?: string | null;
}

export interface OwnedGroupMessageExecutionFilters {
  policy_id?: number;
  account_id?: number;
  trigger_type?: OwnedGroupMessageTriggerType | "";
  content_category?: OwnedGroupMessageCategory | "";
  status?: OwnedGroupMessageExecutionStatus | "";
  created_from?: string;
  created_to?: string;
  page?: number;
  page_size?: number;
}

export type OwnedGroupTemplateMessageType =
  "interaction" | "qa" | "share" | "guide";

export interface OwnedGroupMessageTemplate {
  id: number;
  name: string;
  content: string;
  message_type: OwnedGroupTemplateMessageType;
  content_category: OwnedGroupMessageCategory;
  template_variables: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface OwnedGroupMessageTemplateInput {
  name: string;
  content: string;
  message_type: OwnedGroupTemplateMessageType;
  template_variables: string[];
  enabled: boolean;
}

export interface OwnedGroupMessageTemplateListResponse {
  data: OwnedGroupMessageTemplate[];
  total: number;
}

export interface OwnedGroupMessagingError {
  code: string;
  message: string;
  details: Record<string, unknown>;
  retryable: boolean;
  correlation_id: string | null;
  status?: number;
}

const unwrap = <T>(payload: unknown): T => {
  if (payload && typeof payload === "object" && "data" in payload) {
    return (payload as { data: T }).data;
  }
  return payload as T;
};

const listPayload = <T>(payload: unknown): { rows: T[]; total: number } => {
  if (Array.isArray(payload))
    return { rows: payload as T[], total: payload.length };
  if (payload && typeof payload === "object") {
    const outer = payload as Record<string, unknown>;
    if (Array.isArray(outer.data)) {
      return {
        rows: outer.data as T[],
        total: Number(outer.total ?? outer.data.length),
      };
    }
    const body =
      outer.data && typeof outer.data === "object"
        ? (outer.data as Record<string, unknown>)
        : outer;
    const rows = Array.isArray(body.items)
      ? (body.items as T[])
      : Array.isArray(body.data)
        ? (body.data as T[])
        : [];
    return {
      rows,
      total: Number(body.total ?? outer.total ?? rows.length),
    };
  }
  return { rows: [], total: 0 };
};

const policyCreatePayload = (
  input: OwnedGroupMessagePolicyCreateInput,
): OwnedGroupMessagePolicyCreateInput => ({
  account_id: input.account_id,
  mode: input.mode,
  default_template_id: input.default_template_id,
  trigger_config: input.trigger_config,
  promotion_config: input.promotion_config,
  daily_limit: input.daily_limit,
  cooldown_seconds: input.cooldown_seconds,
  allowed_topics: input.allowed_topics,
  require_review: input.require_review,
  enabled: input.enabled,
});

const policyReplacePayload = (
  input: OwnedGroupMessagePolicyReplaceInput,
): OwnedGroupMessagePolicyReplaceInput => ({
  revision: input.revision,
  mode: input.mode,
  default_template_id: input.default_template_id,
  trigger_config: input.trigger_config,
  promotion_config: input.promotion_config,
  daily_limit: input.daily_limit,
  cooldown_seconds: input.cooldown_seconds,
  allowed_topics: input.allowed_topics,
  require_review: input.require_review,
  enabled: input.enabled,
});

const templatePayload = (
  input: OwnedGroupMessageTemplateInput,
): OwnedGroupMessageTemplateInput => ({
  name: input.name.trim(),
  content: input.content,
  message_type: input.message_type,
  template_variables: [...input.template_variables],
  enabled: input.enabled,
});

export const getOwnedGroupMessagingError = (
  error: unknown,
  fallback = "群内消息操作失败",
): OwnedGroupMessagingError => {
  const response = (
    error as {
      response?: { status?: number; data?: Record<string, unknown> };
    }
  )?.response;
  const payload = response?.data ?? {};
  const nested =
    payload.error && typeof payload.error === "object"
      ? (payload.error as Record<string, unknown>)
      : payload.detail && typeof payload.detail === "object"
        ? (payload.detail as Record<string, unknown>)
        : payload;
  const message =
    typeof nested.message === "string" && nested.message.trim()
      ? nested.message
      : typeof payload.detail === "string" && payload.detail.trim()
        ? payload.detail
        : error instanceof Error && error.message
          ? error.message
          : fallback;
  return {
    code: String(nested.code ?? "OWNED_GROUP_MESSAGING_REQUEST_FAILED"),
    message,
    details:
      nested.details && typeof nested.details === "object"
        ? (nested.details as Record<string, unknown>)
        : {},
    retryable: Boolean(nested.retryable),
    correlation_id:
      typeof payload.correlation_id === "string"
        ? payload.correlation_id
        : typeof nested.correlation_id === "string"
          ? nested.correlation_id
          : null,
    status: response?.status,
  };
};

export const ownedGroupMessagingApi = {
  getEligibleAccounts: async (
    assetId: number,
  ): Promise<OwnedGroupEligibleAccountsResponse> => {
    const payload = (
      await apiClient.get(`/owned-groups/${assetId}/messages/eligible-accounts`)
    ).data as Record<string, unknown>;
    const inner =
      payload.data &&
      typeof payload.data === "object" &&
      !Array.isArray(payload.data)
        ? (payload.data as Record<string, unknown>)
        : payload;
    const rows = Array.isArray(payload.data)
      ? payload.data
      : Array.isArray(inner.data)
        ? inner.data
        : [];
    return {
      data: rows as OwnedGroupMessageEligibleAccount[],
      asset: (inner.asset ??
        payload.asset ??
        {}) as OwnedGroupMessagingAssetSummary,
      correlation_id:
        typeof payload.correlation_id === "string"
          ? payload.correlation_id
          : null,
    };
  },

  listPolicies: async (
    assetId: number,
    params?: {
      enabled?: boolean;
      mode?: OwnedGroupMessageMode;
      account_id?: number;
    },
  ): Promise<{ data: OwnedGroupMessagePolicy[]; total: number }> => {
    const result = listPayload<OwnedGroupMessagePolicy>(
      (
        await apiClient.get(`/owned-groups/${assetId}/messages/policies`, {
          params,
        })
      ).data,
    );
    return { data: result.rows, total: result.total };
  },

  getPolicy: async (
    assetId: number,
    policyId: number,
  ): Promise<OwnedGroupMessagePolicy> =>
    unwrap(
      (
        await apiClient.get(
          `/owned-groups/${assetId}/messages/policies/${policyId}`,
        )
      ).data,
    ),

  createPolicy: async (
    assetId: number,
    input: OwnedGroupMessagePolicyCreateInput,
  ): Promise<OwnedGroupMessagePolicy> =>
    unwrap(
      (
        await apiClient.post(
          `/owned-groups/${assetId}/messages/policies`,
          policyCreatePayload(input),
        )
      ).data,
    ),

  replacePolicy: async (
    assetId: number,
    policyId: number,
    input: OwnedGroupMessagePolicyReplaceInput,
  ): Promise<OwnedGroupMessagePolicy> =>
    unwrap(
      (
        await apiClient.put(
          `/owned-groups/${assetId}/messages/policies/${policyId}`,
          policyReplacePayload(input),
        )
      ).data,
    ),

  preview: async (
    assetId: number,
    policyId: number,
    input: OwnedGroupMessagePreviewInput,
  ): Promise<OwnedGroupMessagePreviewResult> =>
    unwrap(
      (
        await apiClient.post(
          `/owned-groups/${assetId}/messages/policies/${policyId}/preview`,
          input,
        )
      ).data,
    ),

  createExecution: async (
    assetId: number,
    policyId: number,
    input: OwnedGroupMessageManualExecutionInput,
    idempotencyKey: string,
  ): Promise<OwnedGroupMessageExecutionAccepted> => {
    const payload: OwnedGroupMessageManualExecutionInput = {
      trigger_type: "manual",
      content_category: input.content_category,
      topic: input.topic ?? null,
      instruction: input.instruction ?? null,
      template_id: input.template_id ?? null,
      variables: input.variables ?? {},
      reply_to_message_id: input.reply_to_message_id ?? null,
      scheduled_at: input.scheduled_at ?? null,
    };
    const response = (
      await apiClient.post(
        `/owned-groups/${assetId}/messages/policies/${policyId}/executions`,
        payload,
        { headers: { "Idempotency-Key": idempotencyKey } },
      )
    ).data as Record<string, unknown>;
    const accepted = unwrap<OwnedGroupMessageExecutionAccepted>(response);
    return {
      ...accepted,
      correlation_id:
        accepted.correlation_id ??
        (typeof response.correlation_id === "string"
          ? response.correlation_id
          : null),
    };
  },

  listExecutions: async (
    assetId: number,
    params?: OwnedGroupMessageExecutionFilters,
  ): Promise<OwnedGroupMessageExecutionPage> => {
    const cleanParams = params
      ? Object.fromEntries(
          Object.entries(params).filter(
            ([, value]) =>
              value !== "" && value !== undefined && value !== null,
          ),
        )
      : undefined;
    const payload = (
      await apiClient.get(`/owned-groups/${assetId}/messages/executions`, {
        params: cleanParams,
      })
    ).data;
    const outer = payload as Record<string, unknown>;
    const body =
      outer.data && typeof outer.data === "object" && !Array.isArray(outer.data)
        ? (outer.data as Record<string, unknown>)
        : outer;
    const items = Array.isArray(outer.data)
      ? outer.data
      : Array.isArray(body.items)
        ? body.items
        : Array.isArray(body.data)
          ? body.data
          : [];
    return {
      items: items as OwnedGroupMessageExecutionSummary[],
      total: Number(body.total ?? outer.total ?? items.length),
      page: Number(body.page ?? outer.page ?? params?.page ?? 1),
      page_size: Number(
        body.page_size ?? outer.page_size ?? params?.page_size ?? 20,
      ),
    };
  },

  getExecution: async (
    assetId: number,
    executionId: number,
  ): Promise<OwnedGroupMessageExecutionDetail> =>
    unwrap(
      (
        await apiClient.get(
          `/owned-groups/${assetId}/messages/executions/${executionId}`,
        )
      ).data,
    ),

  approveExecution: async (
    assetId: number,
    executionId: number,
    input: { revision: number; content_override?: string | null },
  ): Promise<OwnedGroupMessageExecutionDetail> =>
    unwrap(
      (
        await apiClient.post(
          `/owned-groups/${assetId}/messages/executions/${executionId}/approve`,
          {
            revision: input.revision,
            content_override: input.content_override ?? null,
          },
        )
      ).data,
    ),

  rejectExecution: async (
    assetId: number,
    executionId: number,
    input: { revision: number; reason: string },
  ): Promise<OwnedGroupMessageExecutionDetail> =>
    unwrap(
      (
        await apiClient.post(
          `/owned-groups/${assetId}/messages/executions/${executionId}/reject`,
          { revision: input.revision, reason: input.reason.trim() },
        )
      ).data,
    ),

  listTemplates: async (
    assetId: number,
    params?: {
      message_type?: OwnedGroupTemplateMessageType;
      content_category?: OwnedGroupMessageCategory;
      enabled?: boolean;
    },
  ): Promise<OwnedGroupMessageTemplateListResponse> => {
    const result = listPayload<OwnedGroupMessageTemplate>(
      (
        await apiClient.get(`/owned-groups/${assetId}/messages/templates`, {
          params,
        })
      ).data,
    );
    return { data: result.rows, total: result.total };
  },

  createTemplate: async (
    assetId: number,
    input: OwnedGroupMessageTemplateInput,
  ): Promise<OwnedGroupMessageTemplate> =>
    unwrap(
      (
        await apiClient.post(
          `/owned-groups/${assetId}/messages/templates`,
          templatePayload(input),
        )
      ).data,
    ),

  updateTemplate: async (
    assetId: number,
    templateId: number,
    input: OwnedGroupMessageTemplateInput,
  ): Promise<OwnedGroupMessageTemplate> =>
    unwrap(
      (
        await apiClient.put(
          `/owned-groups/${assetId}/messages/templates/${templateId}`,
          templatePayload(input),
        )
      ).data,
    ),
};
