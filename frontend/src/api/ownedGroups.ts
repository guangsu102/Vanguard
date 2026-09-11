import apiClient from "./client";

export type OwnedGroupVisibility = "public" | "private";
export type OwnedGroupInviteMode =
  "direct_invite" | "link_self_join" | "manual_approval";
export type OwnedGroupAssetStatus =
  | "draft"
  | "prechecking"
  | "creating"
  | "ready"
  | "create_failed"
  | "needs_attention"
  | "archived";
export type OwnedGroupResourceType = "user" | "bot";
export type OwnedBotProfileStatus =
  | "pending_verification"
  | "verified"
  | "active"
  | "disabled"
  | "verification_failed"
  | string;
export type OwnedGroupOperationStatus =
  | "draft"
  | "queued"
  | "running"
  | "paused"
  | "stopping"
  | "stopped"
  | "completed"
  | "partial_completed"
  | "failed"
  | "unknown";
export type OwnedGroupGovernanceState =
  | "disabled"
  | "pending"
  | "managed"
  | "degraded";

export interface PermissionProbe {
  status: "passed" | "failed";
  required_permissions: string[];
  granted_permissions: string[];
  missing_permissions: string[];
  checked_at?: string | null;
}

export interface GovernanceCapabilities {
  verification: boolean;
  sensitive_keywords: boolean;
  anti_spam: boolean;
  warn: boolean;
  mute: boolean;
  ban: boolean;
  announcement: boolean;
  pin_message: boolean;
  activity: boolean;
}

export interface GovernanceFailure {
  reason: string;
  message: string;
  retryable: boolean;
  missing_permissions?: string[];
  correlation_id?: string | null;
}

export interface OwnedGroupGovernanceStatus {
  asset_id: number;
  asset_status: OwnedGroupAssetStatus | string;
  telegram_chat_id: number | null;
  core_group_id: number | null;
  managed_binding_id: number | null;
  guardian_bot_account_id: number | null;
  guardian_bot_profile_id: number | null;
  owned_bot_profile_id: number | null;
  guardian_bot_display_name?: string | null;
  guardian_bot_username?: string | null;
  governance_status: OwnedGroupGovernanceState;
  binding_status: string | null;
  bot_role: string | null;
  permission_probe: PermissionProbe | null;
  capabilities: GovernanceCapabilities;
  failure: GovernanceFailure | null;
  governance_pending_at: string | null;
  governance_enabled_at: string | null;
  governance_last_checked_at: string | null;
  stale_pending: boolean;
  reused: boolean;
  correlation_id: string | null;
}

export interface OwnedGroupGovernanceCandidate {
  guardian_bot_account_id: number;
  guardian_bot_profile_id: number;
  owned_bot_profile_id: number;
  display_name?: string | null;
  username?: string | null;
  owned_profile_status: string;
  guardian_health_status: string;
  local_membership_status?: string | null;
  local_is_admin: boolean;
}

export interface OwnedGroupAsset {
  id: number;
  internal_name: string;
  title: string;
  about?: string | null;
  visibility: OwnedGroupVisibility;
  telegram_username?: string | null;
  public_link?: string | null;
  owner_account_id: number;
  invite_mode: OwnedGroupInviteMode;
  status: OwnedGroupAssetStatus | string;
  telegram_chat_id?: number | null;
  core_group_id?: number | null;
  managed_binding_id?: number | null;
  guardian_bot_account_id?: number | null;
  governance_status?: OwnedGroupGovernanceState;
  governance_pending_at?: string | null;
  governance_enabled_at?: string | null;
  governance_last_checked_at?: string | null;
  governance_last_error_code?: string | null;
  governance_last_error_message?: string | null;
  member_count: number;
  created_at: string;
  updated_at: string;
}
export interface OwnedGroupDraftInput {
  internal_name: string;
  title: string;
  about?: string;
  visibility: OwnedGroupVisibility;
  telegram_username?: string;
  owner_account_id: number;
  invite_mode: OwnedGroupInviteMode;
}
export interface OwnedGroupResourceSelection {
  resource_type: OwnedGroupResourceType;
  resource_id: number;
  admin_required?: boolean;
  admin_permissions?: Record<string, boolean>;
  admin_title?: string;
}
export interface OwnedGroupOperationInput {
  resources: OwnedGroupResourceSelection[];
  batch_size?: number;
  batch_interval_seconds?: number;
  max_parallelism?: number;
  max_attempts?: number;
  schedule_at?: string;
}

/**
 * Safe representation returned by the owned-group Bot profile API.
 * `bot_token` is deliberately absent: it is accepted only by registration
 * and must never be kept in Pinia state, rendered in a table, or echoed back.
 */
export interface OwnedBotProfile {
  id: number;
  owner_account_id: number;
  account_id: number;
  bot_user_id?: number | null;
  bot_username?: string | null;
  display_name?: string | null;
  status: OwnedBotProfileStatus;
  enabled: boolean;
  last_verified_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface OwnedBotProfileRegisterInput {
  owner_account_id: number;
  account_id: number;
  bot_token?: string;
}

export interface OwnedBotProfileListResponse {
  data: OwnedBotProfile[];
  total: number;
}

export interface OwnedGroupPrecheckResult {
  asset_id: number;
  allowed: boolean;
  reason: string;
  details?: Record<string, unknown> | null;
  selection_snapshot: OwnedGroupResourceSelection[];
  selection_snapshot_hash: string;
  config_snapshot: Record<string, unknown>;
  config_snapshot_hash: string;
  owner_auto_included: boolean;
}
export interface OwnedGroupOperation {
  id: number;
  group_asset_id: number;
  status: string;
  planned_count: number;
  selection_snapshot_hash: string;
  config_snapshot_hash: string;
  idempotency_key: string;
  created_at: string;
}
export interface OwnedGroupControlResponse {
  id: number;
  group_asset_id?: number | null;
  status: string;
  message: string;
  reason_code?: string | null;
  updated_at?: string | null;
}
export interface OwnedGroupOperationDetail extends OwnedGroupControlResponse {
  group_asset_id: number;
  status: OwnedGroupOperationStatus;
  planned_count: number;
  completed_count: number;
  skipped_count: number;
  failed_count: number;
  last_error?: string | null;
  schedule_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}
export interface OwnedGroupListResponse {
  data: OwnedGroupAsset[];
  total: number;
}

/**
 * Invite links are bearer credentials.  The API only returns the plaintext
 * link from the explicit, authenticated invite-link endpoint; it is never
 * persisted by the frontend or included in an asset/operation response.
 */
export interface OwnedGroupInviteLink {
  id: number | null;
  group_asset_id: number;
  link_type: string;
  link: string | null;
  is_active: boolean;
  status: string;
  available: boolean;
  created_at?: string | null;
  revoked_at?: string | null;
}

export interface OwnedGroupInviteLinkListResponse {
  data: OwnedGroupInviteLink[];
  total: number;
}

export interface OwnedGroupInviteLinkMutationResponse {
  id?: number | null;
  group_asset_id?: number;
  link_type?: string;
  status?: string;
  available?: boolean;
  created_at?: string | null;
  revoked_at?: string | null;
  message?: string;
}

const EMPTY_GOVERNANCE_CAPABILITIES: GovernanceCapabilities = {
  verification: false,
  sensitive_keywords: false,
  anti_spam: false,
  warn: false,
  mute: false,
  ban: false,
  announcement: false,
  pin_message: false,
  activity: false,
};

const unwrap = (payload: any): any => payload?.data ?? payload;

const normalizeStringList = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

const normalizeGovernanceFailure = (
  value: unknown,
): GovernanceFailure | null => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  return {
    reason: String(raw.reason ?? raw.reason_code ?? "governance_failed"),
    message: redactOwnedGroupError(raw.message, "Guardian 治理操作失败"),
    retryable: Boolean(raw.retryable),
    missing_permissions: normalizeStringList(raw.missing_permissions),
    correlation_id:
      typeof raw.correlation_id === "string" ? raw.correlation_id : null,
  };
};

const normalizeGovernanceStatus = (
  payload: unknown,
): OwnedGroupGovernanceStatus => {
  const raw = unwrap(payload) as Record<string, any>;
  const probe = raw?.permission_probe;
  return {
    asset_id: Number(raw?.asset_id ?? 0),
    asset_status: raw?.asset_status ?? "draft",
    telegram_chat_id:
      raw?.telegram_chat_id === null || raw?.telegram_chat_id === undefined
        ? null
        : Number(raw.telegram_chat_id),
    core_group_id:
      raw?.core_group_id === null || raw?.core_group_id === undefined
        ? null
        : Number(raw.core_group_id),
    managed_binding_id:
      raw?.managed_binding_id === null || raw?.managed_binding_id === undefined
        ? null
        : Number(raw.managed_binding_id),
    guardian_bot_account_id:
      raw?.guardian_bot_account_id === null ||
      raw?.guardian_bot_account_id === undefined
        ? null
        : Number(raw.guardian_bot_account_id),
    guardian_bot_profile_id:
      raw?.guardian_bot_profile_id === null ||
      raw?.guardian_bot_profile_id === undefined
        ? null
        : Number(raw.guardian_bot_profile_id),
    owned_bot_profile_id:
      raw?.owned_bot_profile_id === null ||
      raw?.owned_bot_profile_id === undefined
        ? null
        : Number(raw.owned_bot_profile_id),
    guardian_bot_display_name:
      typeof raw?.guardian_bot_display_name === "string"
        ? raw.guardian_bot_display_name
        : null,
    guardian_bot_username:
      typeof raw?.guardian_bot_username === "string"
        ? raw.guardian_bot_username
        : null,
    governance_status: raw?.governance_status ?? "disabled",
    binding_status: raw?.binding_status ?? null,
    bot_role: raw?.bot_role ?? null,
    permission_probe:
      probe && typeof probe === "object"
        ? {
            status: probe.status === "passed" ? "passed" : "failed",
            required_permissions: normalizeStringList(
              probe.required_permissions,
            ),
            granted_permissions: normalizeStringList(
              probe.granted_permissions,
            ),
            missing_permissions: normalizeStringList(
              probe.missing_permissions,
            ),
            checked_at: probe.checked_at ?? null,
          }
        : null,
    capabilities: {
      ...EMPTY_GOVERNANCE_CAPABILITIES,
      ...(raw?.capabilities && typeof raw.capabilities === "object"
        ? raw.capabilities
        : {}),
    },
    failure: normalizeGovernanceFailure(raw?.failure),
    governance_pending_at: raw?.governance_pending_at ?? null,
    governance_enabled_at: raw?.governance_enabled_at ?? null,
    governance_last_checked_at: raw?.governance_last_checked_at ?? null,
    stale_pending: Boolean(raw?.stale_pending),
    reused: Boolean(raw?.reused),
    correlation_id:
      typeof raw?.correlation_id === "string" ? raw.correlation_id : null,
  };
};

const normalizeGovernanceCandidates = (
  payload: unknown,
): OwnedGroupGovernanceCandidate[] => {
  const rows = unwrap(payload);
  if (!Array.isArray(rows)) return [];
  return rows.map((raw: Record<string, any>) => ({
    guardian_bot_account_id: Number(raw.guardian_bot_account_id),
    guardian_bot_profile_id: Number(raw.guardian_bot_profile_id),
    owned_bot_profile_id: Number(raw.owned_bot_profile_id),
    display_name:
      typeof raw.display_name === "string" ? raw.display_name : null,
    username: typeof raw.username === "string" ? raw.username : null,
    owned_profile_status: String(raw.owned_profile_status ?? "unknown"),
    guardian_health_status: String(raw.guardian_health_status ?? "unknown"),
    local_membership_status:
      typeof raw.local_membership_status === "string"
        ? raw.local_membership_status
        : null,
    local_is_admin: Boolean(raw.local_is_admin),
  }));
};

const normalizeBotProfile = (raw: any): OwnedBotProfile => ({
  id: Number(raw?.id ?? 0),
  owner_account_id: Number(raw?.owner_account_id ?? raw?.ownerAccountId ?? 0),
  account_id: Number(raw?.account_id ?? raw?.accountId ?? 0),
  bot_user_id: raw?.bot_user_id ?? raw?.botUserId ?? null,
  bot_username: raw?.bot_username ?? raw?.botUsername ?? null,
  display_name: raw?.display_name ?? raw?.displayName ?? null,
  status: raw?.status || "pending_verification",
  enabled: Boolean(raw?.enabled),
  last_verified_at: raw?.last_verified_at ?? raw?.lastVerifiedAt ?? null,
  created_at: raw?.created_at ?? raw?.createdAt ?? "",
  updated_at: raw?.updated_at ?? raw?.updatedAt ?? "",
});

const normalizeBotProfileList = (payload: any): OwnedBotProfileListResponse => {
  const body = payload?.data ?? payload;
  const rows = Array.isArray(body)
    ? body
    : Array.isArray(body?.data)
      ? body.data
      : [];
  const total = Array.isArray(body)
    ? Number(payload?.total ?? rows.length)
    : Number(body?.total ?? payload?.total ?? rows.length);
  return { data: rows.map(normalizeBotProfile), total };
};

const normalizeInviteLink = (raw: any): OwnedGroupInviteLink => ({
  id: raw?.id === null || raw?.id === undefined ? null : Number(raw.id),
  group_asset_id: Number(raw?.group_asset_id ?? raw?.groupAssetId ?? 0),
  link_type: String(raw?.link_type ?? raw?.linkType ?? "private"),
  // This value is held only in the active component/store memory for the
  // explicit reveal flow.  It is never written to localStorage or logs.
  link: typeof raw?.link === "string" ? raw.link : null,
  is_active: Boolean(raw?.is_active ?? raw?.isActive),
  status: String(raw?.status ?? "unavailable"),
  available: Boolean(raw?.available && typeof raw?.link === "string"),
  created_at: raw?.created_at ?? raw?.createdAt ?? null,
  revoked_at: raw?.revoked_at ?? raw?.revokedAt ?? null,
});

const normalizeInviteLinkList = (
  payload: any,
): OwnedGroupInviteLinkListResponse => {
  const body = payload?.data ?? payload;
  const rows = Array.isArray(body)
    ? body
    : Array.isArray(body?.data)
      ? body.data
      : [];
  const total = Array.isArray(body)
    ? Number(payload?.total ?? rows.length)
    : Number(body?.total ?? payload?.total ?? rows.length);
  return { data: rows.map(normalizeInviteLink), total };
};

const normalizeInviteLinkMutation = (
  payload: any,
): OwnedGroupInviteLinkMutationResponse => {
  const body = payload?.data ?? payload;
  if (!body || typeof body !== "object" || Array.isArray(body)) return {};
  return {
    id: body.id === null || body.id === undefined ? null : Number(body.id),
    group_asset_id:
      body.group_asset_id === undefined
        ? undefined
        : Number(body.group_asset_id),
    link_type: body.link_type ?? body.linkType,
    status: body.status,
    available:
      body.available === undefined ? undefined : Boolean(body.available),
    created_at: body.created_at ?? body.createdAt ?? null,
    revoked_at: body.revoked_at ?? body.revokedAt ?? null,
    message: typeof body.message === "string" ? body.message : undefined,
  };
};

/** Keep server errors useful without ever echoing a Bot token or invite URL. */
export const redactOwnedGroupError = (
  value: unknown,
  fallback = "Request failed",
): string => {
  const source = value instanceof Error ? value.message : String(value ?? "");
  const redacted = source
    .replace(/bot\d+:[A-Za-z0-9_-]+/gi, "bot<redacted>")
    .replace(/\b\d{6,12}:[A-Za-z0-9_-]{10,}\b/g, "bot<redacted>")
    .replace(
      /(?:token|api[_-]?hash|session(?:_string)?)\s*[=:]\s*[^\s,;]+/gi,
      "credential=<redacted>",
    )
    .replace(
      /https?:\/\/(?:www\.)?(?:t\.me|telegram\.me)\/[^\s]+/gi,
      "<invite-redacted>",
    )
    .replace(/\s+/g, " ")
    .trim();
  return redacted || fallback;
};

export const getOwnedGroupGovernanceFailure = (
  error: unknown,
  fallback = "Guardian 治理操作失败",
): GovernanceFailure => {
  const responseData = (error as any)?.response?.data;
  const detail = responseData?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    return normalizeGovernanceFailure(detail) as GovernanceFailure;
  }
  const source =
    typeof detail === "string"
      ? detail
      : error instanceof Error
        ? error.message
        : responseData?.message;
  return {
    reason: "governance_request_failed",
    message: redactOwnedGroupError(source, fallback),
    retryable: false,
    missing_permissions: [],
    correlation_id: null,
  };
};

export const ownedGroupsApi = {
  precheck: async (id: number): Promise<OwnedGroupControlResponse> =>
    unwrap((await apiClient.post("/owned-groups/" + id + "/precheck")).data),
  /**
   * Recover a create-timeout asset by verifying a known Telegram group.  The
   * backend never issues a group-create RPC from this endpoint.
   */
  reconcileAsset: async (
    id: number,
    telegramChatId?: number,
    telegramUsername?: string,
  ): Promise<OwnedGroupControlResponse> =>
    unwrap(
      (
        await apiClient.post(`/owned-groups/${id}/reconcile`, {
          ...(telegramChatId === undefined
            ? {}
            : { telegram_chat_id: telegramChatId }),
          ...(telegramUsername?.trim()
            ? { telegram_username: telegramUsername.trim() }
            : {}),
        })
      ).data,
    ),
  precheckOperation: async (
    id: number,
    data: OwnedGroupOperationInput,
  ): Promise<OwnedGroupPrecheckResult> =>
    unwrap(
      (await apiClient.post(`/owned-groups/${id}/operations/precheck`, data))
        .data,
    ),
  getOperation: async (id: number): Promise<OwnedGroupOperationDetail> =>
    unwrap((await apiClient.get("/owned-groups/operations/" + id)).data),
  pauseOperation: async (id: number): Promise<OwnedGroupControlResponse> =>
    unwrap(
      (await apiClient.post("/owned-groups/operations/" + id + "/pause")).data,
    ),
  resumeOperation: async (id: number): Promise<OwnedGroupControlResponse> =>
    unwrap(
      (await apiClient.post("/owned-groups/operations/" + id + "/resume")).data,
    ),
  stopOperation: async (id: number): Promise<OwnedGroupControlResponse> =>
    unwrap(
      (await apiClient.post("/owned-groups/operations/" + id + "/stop")).data,
    ),
  retryOperation: async (id: number): Promise<OwnedGroupControlResponse> =>
    unwrap(
      (await apiClient.post("/owned-groups/operations/" + id + "/retry")).data,
    ),
  reconcileOperation: async (id: number): Promise<OwnedGroupControlResponse> =>
    unwrap(
      (await apiClient.post("/owned-groups/operations/" + id + "/reconcile"))
        .data,
    ),
  list: (params?: {
    status?: string;
    visibility?: OwnedGroupVisibility;
    limit?: number;
    offset?: number;
  }) => apiClient.get<OwnedGroupListResponse>("/owned-groups", { params }),
  getById: async (id: number): Promise<OwnedGroupAsset> =>
    unwrap((await apiClient.get(`/owned-groups/${id}`)).data),
  getGovernance: async (
    assetId: number,
  ): Promise<OwnedGroupGovernanceStatus> =>
    normalizeGovernanceStatus(
      (await apiClient.get("/owned-groups/" + assetId + "/governance")).data,
    ),
  getGovernanceCandidates: async (
    assetId: number,
  ): Promise<OwnedGroupGovernanceCandidate[]> =>
    normalizeGovernanceCandidates(
      (
        await apiClient.get(
          "/owned-groups/" + assetId + "/governance/candidates",
        )
      ).data,
    ),
  bindGovernance: async (
    assetId: number,
    guardianBotAccountId: number,
  ): Promise<OwnedGroupGovernanceStatus> =>
    normalizeGovernanceStatus(
      (
        await apiClient.post(
          "/owned-groups/" + assetId + "/governance/bind",
          { guardian_bot_account_id: guardianBotAccountId },
        )
      ).data,
    ),
  reconcileGovernance: async (
    assetId: number,
  ): Promise<OwnedGroupGovernanceStatus> =>
    normalizeGovernanceStatus(
      (
        await apiClient.post(
          "/owned-groups/" + assetId + "/governance/reconcile",
          {},
        )
      ).data,
    ),
  /**
   * Explicitly reveal currently usable invite links for one asset.  Callers
   * should clear the returned plaintext when the asset is deselected/unmounted.
   */
  listInviteLinks: async (
    id: number,
  ): Promise<OwnedGroupInviteLinkListResponse> =>
    normalizeInviteLinkList(
      (await apiClient.get(`/owned-groups/${id}/invite-links`)).data,
    ),
  revokeInviteLink: async (
    assetId: number,
    linkId: number,
  ): Promise<OwnedGroupInviteLinkMutationResponse> =>
    normalizeInviteLinkMutation(
      (
        await apiClient.post(
          `/owned-groups/${assetId}/invite-links/${linkId}/revoke`,
        )
      ).data,
    ),
  regenerateInviteLink: async (
    assetId: number,
    requestNeeded?: boolean,
  ): Promise<OwnedGroupInviteLinkMutationResponse> =>
    normalizeInviteLinkMutation(
      (
        await apiClient.post(
          `/owned-groups/${assetId}/invite-links/regenerate`,
          requestNeeded === undefined ? {} : { request_needed: requestNeeded },
        )
      ).data,
    ),
  createDraft: async (data: OwnedGroupDraftInput): Promise<OwnedGroupAsset> =>
    unwrap((await apiClient.post("/owned-groups/drafts", data)).data),
  submitOperation: async (
    id: number,
    data: OwnedGroupOperationInput,
    idempotencyKey: string,
  ): Promise<OwnedGroupOperation> =>
    unwrap(
      (
        await apiClient.post(`/owned-groups/${id}/operations`, data, {
          headers: { "Idempotency-Key": idempotencyKey },
        })
      ).data,
    ),
  /** List safe Bot profile metadata; token fields are intentionally not typed. */
  listBotProfiles: async (params?: {
    enabled?: boolean;
    status?: string;
    limit?: number;
    offset?: number;
  }): Promise<OwnedBotProfileListResponse> =>
    normalizeBotProfileList(
      (await apiClient.get("/owned-groups/bot-profiles", { params })).data,
    ),
  /** Send a BotFather token once; callers must clear their input after settle. */
  registerBotProfile: async (
    data: OwnedBotProfileRegisterInput,
  ): Promise<OwnedBotProfile> => {
    const payload = {
      ...data,
      bot_token: data.bot_token?.trim() || undefined,
    };
    return normalizeBotProfile(
      unwrap(
        (await apiClient.post("/owned-groups/bot-profiles", payload)).data,
      ),
    );
  },
  verifyBotProfile: async (id: number): Promise<OwnedBotProfile> =>
    normalizeBotProfile(
      unwrap(
        (await apiClient.post(`/owned-groups/bot-profiles/${id}/verify`)).data,
      ),
    ),
  setBotProfileEnabled: async (
    id: number,
    enabled: boolean,
  ): Promise<OwnedBotProfile> =>
    normalizeBotProfile(
      unwrap(
        (await apiClient.patch(`/owned-groups/bot-profiles/${id}`, { enabled }))
          .data,
      ),
    ),
};
