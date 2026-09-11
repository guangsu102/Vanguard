import apiClient from "./client";

export type MemberKind = "real_user" | "system_ad_account" | "system_bot";
export type ClassificationStatus = "resolved" | "unresolved" | "conflict";
export type PresenceStatus = "present" | "pending" | "left" | "failed" | "unknown";
export type PresenceConfidence = "verified" | "observed" | "stored" | "unknown";
export type DataQuality = "reliable" | "partial" | "conflict";
export type TelegramRole = "owner" | "administrator" | "member" | "unknown";
export type SectionState =
  | "ready"
  | "not_configured"
  | "pending"
  | "degraded"
  | "disabled"
  | "stopped"
  | "unavailable";
export type CoverageStatus = "historical" | "degraded" | "paused" | "collecting" | "not_started";

export interface Coverage {
  data_scope: "managed_and_observed";
  is_complete: false;
  full_roster_supported: false;
  coverage_status: CoverageStatus;
  observation_started_at: string | null;
  last_observed_at: string | null;
  blocking_reasons: string[];
}

export interface KindCounts {
  real_user: number;
  system_ad_account: number;
  system_bot: number;
}

export interface MemberSummary {
  managed_resource_count?: number;
  observed_real_user_count?: number;
  counts_by_kind: KindCounts;
  unresolved_count: number;
  conflict_count: number;
  coverage?: Coverage;
}

export interface OperationsCenterAsset {
  asset_id: number;
  internal_name: string;
  title: string;
  visibility: string;
  asset_status: string;
  telegram_chat_id: number | null;
  telegram_chat_id_raw: string | null;
  core_group_id: number | null;
  managed_binding_id: number | null;
  guardian_bot_account_id: number | null;
  owner_account_id: number;
  managed_resource_count: number;
  updated_at: string;
}

export interface OperationsCenterSection {
  state: SectionState;
  can_view: boolean;
  can_manage: boolean;
  can_execute: boolean;
  blocking_reasons: string[];
}

export type OperationsCenterSections = Record<
  "orchestration" | "governance" | "members" | "messaging" | "activities" | "audit",
  OperationsCenterSection
>;

export interface OperationsPermissions {
  manage_orchestration: boolean;
  manage_governance: boolean;
  manage_messaging: boolean;
  manage_persona: boolean;
  manage_activities: boolean;
  view_members: boolean;
  view_audit: boolean;
}

export interface OperationsCenterSummary {
  asset: OperationsCenterAsset;
  sections: OperationsCenterSections;
  governance: {
    status: string;
    bot_account_id: number | null;
    bot_role: string | null;
    health_status: string | null;
    last_checked_at: string | null;
  };
  messaging: {
    static_enabled: boolean;
    runtime_enabled: boolean;
    dry_run: boolean;
    can_send: boolean;
    policy_count: number;
    enabled_policy_count: number;
    pending_review_count: number;
    sent_today: number;
  };
  member_summary: MemberSummary & { coverage: Coverage };
  latest_operation: { id: number; operation_type?: string; status: string; updated_at: string } | null;
  permissions: OperationsPermissions;
  snapshot_at: string;
}

export interface MemberSource {
  source: string;
  record_id: number;
  observed_at: string | null;
}

export interface OwnedGroupMember {
  member_key: string;
  member_kind: MemberKind | null;
  classification_status: ClassificationStatus;
  classification_reason: string;
  telegram_user_id: number | null;
  telegram_user_id_raw: string | null;
  display_name: string | null;
  username: string | null;
  primary_source: string;
  sources: MemberSource[];
  account_id: number | null;
  owned_bot_profile_id: number | null;
  guardian_bot_profile_id: number | null;
  parent_account_id: number | null;
  user_id: number | null;
  operation_mode: string | null;
  account_status: string | null;
  account_risk_level: string | null;
  risk_pause_until: string | null;
  risk_scope: string | null;
  persona_configured: boolean | null;
  persona_revision: number | null;
  telegram_role: TelegramRole;
  is_admin: boolean;
  admin_title: string | null;
  presence_status: PresenceStatus;
  presence_confidence: PresenceConfidence;
  raw_presence_status: string | null;
  user_state: string | null;
  warning_count: number | null;
  muted_until: string | null;
  joined_at: string | null;
  left_at: string | null;
  last_verified_at: string | null;
  last_observed_at: string | null;
  data_quality: DataQuality;
  quality_codes: string[];
  bot_health_status?: string | null;
  bot_sync_status?: string | null;
}

export interface MemberQuery {
  member_kind?: MemberKind;
  presence_status?: PresenceStatus;
  classification_status?: ClassificationStatus;
  telegram_role?: TelegramRole;
  q?: string;
  sort?: "last_observed_desc" | "name_asc" | "kind_asc";
  offset: number;
  limit: number;
}

export interface MemberPage {
  items: OwnedGroupMember[];
  total: number;
  offset: number;
  limit: number;
  coverage: Coverage;
  summary: MemberSummary;
  correlation_id: string | null;
}

export type OwnedGroupOperationStatus =
  | "draft" | "queued" | "running" | "paused" | "stopping"
  | "stopped" | "completed" | "partial_completed" | "failed" | "unknown";

export interface OperationQuery {
  status?: string;
  offset: number;
  limit: number;
}

export interface OperationListItem {
  id: number;
  group_asset_id: number;
  status: OwnedGroupOperationStatus;
  raw_status: string;
  planned_count: number | null;
  completed_count: number | null;
  skipped_count: number | null;
  failed_count: number | null;
  selection_snapshot_hash: string;
  config_snapshot_hash: string;
  idempotency_key: string;
  schedule_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditQuery {
  event_type?: string;
  result?: string;
  resource_type?: string;
  created_after?: string;
  created_before?: string;
  offset: number;
  limit: number;
}

export interface AuditEventItem {
  id: number;
  event_type: string;
  group_asset_id: number | null;
  operation_id: number | null;
  operation_item_id: number | null;
  resource_type: string | null;
  resource_id: number | null;
  actor_id: number | null;
  before_state: string | null;
  after_state: string | null;
  result: string;
  reason_code: string | null;
  correlation_id: string | null;
  created_at: string;
}

export interface OperationPage { items: OperationListItem[]; total: number }
export interface AuditEventPage { items: AuditEventItem[]; total: number }

type JsonObject = Record<string, unknown>;
interface LegacyListWire<T> { code: number; message: string; data: T[]; total: number }

const isObject = (value: unknown): value is JsonObject =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);
const stringValue = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value : fallback;
const nullableString = (value: unknown): string | null =>
  typeof value === "string" ? value : null;
const numberValue = (value: unknown, fallback = 0): number => {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};
const nullableNumber = (value: unknown): number | null => {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};
const nullableSafeInteger = (value: unknown): number | null => {
  const parsed = nullableNumber(value);
  return parsed !== null && Number.isSafeInteger(parsed) ? parsed : null;
};
const rawIdentifier = (value: unknown): string | null =>
  value === null || value === undefined || value === "" ? null : String(value);
const stringArray = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

const SECTION_STATES = new Set<SectionState>([
  "ready", "not_configured", "pending", "degraded", "disabled", "stopped", "unavailable",
]);
const MEMBER_KINDS = new Set<MemberKind>(["real_user", "system_ad_account", "system_bot"]);
const CLASSIFICATION_STATUSES = new Set<ClassificationStatus>(["resolved", "unresolved", "conflict"]);
const PRESENCE_STATUSES = new Set<PresenceStatus>(["present", "pending", "left", "failed", "unknown"]);
const PRESENCE_CONFIDENCE = new Set<PresenceConfidence>(["verified", "observed", "stored", "unknown"]);
const TELEGRAM_ROLES = new Set<TelegramRole>(["owner", "administrator", "member", "unknown"]);
const DATA_QUALITY = new Set<DataQuality>(["reliable", "partial", "conflict"]);
const COVERAGE_STATUSES = new Set<CoverageStatus>(["historical", "degraded", "paused", "collecting", "not_started"]);
const OPERATION_STATUSES = new Set<OwnedGroupOperationStatus>([
  "draft", "queued", "running", "paused", "stopping", "stopped", "completed", "partial_completed", "failed", "unknown",
]);

const normalizeCoverage = (value: unknown): Coverage => {
  const raw = isObject(value) ? value : {};
  const rawStatus = stringValue(raw.coverage_status, "not_started") as CoverageStatus;
  const blocking = stringArray(raw.blocking_reasons);
  if (!COVERAGE_STATUSES.has(rawStatus)) blocking.push(`unknown_coverage_status:${String(raw.coverage_status ?? "")}`);
  if (!blocking.includes("telegram_full_roster_not_loaded")) blocking.push("telegram_full_roster_not_loaded");
  return {
    data_scope: "managed_and_observed",
    is_complete: false,
    full_roster_supported: false,
    coverage_status: COVERAGE_STATUSES.has(rawStatus) ? rawStatus : "degraded",
    observation_started_at: nullableString(raw.observation_started_at),
    last_observed_at: nullableString(raw.last_observed_at),
    blocking_reasons: blocking,
  };
};

const normalizeKindCounts = (value: unknown): KindCounts => {
  const raw = isObject(value) ? value : {};
  return {
    real_user: Math.max(0, numberValue(raw.real_user)),
    system_ad_account: Math.max(0, numberValue(raw.system_ad_account)),
    system_bot: Math.max(0, numberValue(raw.system_bot)),
  };
};

const normalizeMemberSummary = (value: unknown): MemberSummary => {
  const raw = isObject(value) ? value : {};
  return {
    ...(raw.managed_resource_count === undefined ? {} : { managed_resource_count: Math.max(0, numberValue(raw.managed_resource_count)) }),
    ...(raw.observed_real_user_count === undefined ? {} : { observed_real_user_count: Math.max(0, numberValue(raw.observed_real_user_count)) }),
    counts_by_kind: normalizeKindCounts(raw.counts_by_kind),
    unresolved_count: Math.max(0, numberValue(raw.unresolved_count)),
    conflict_count: Math.max(0, numberValue(raw.conflict_count)),
    ...(raw.coverage === undefined ? {} : { coverage: normalizeCoverage(raw.coverage) }),
  };
};

const normalizeSection = (value: unknown): OperationsCenterSection => {
  const raw = isObject(value) ? value : {};
  const candidate = stringValue(raw.state, "unavailable") as SectionState;
  const reasons = stringArray(raw.blocking_reasons);
  if (!SECTION_STATES.has(candidate)) reasons.push(`unknown_section_state:${String(raw.state ?? "")}`);
  return {
    state: SECTION_STATES.has(candidate) ? candidate : "unavailable",
    can_view: raw.can_view === true,
    can_manage: raw.can_manage === true,
    can_execute: raw.can_execute === true,
    blocking_reasons: reasons,
  };
};

const SECTION_KEYS = ["orchestration", "governance", "members", "messaging", "activities", "audit"] as const;

export const normalizeOperationsCenterSummary = (value: unknown): OperationsCenterSummary => {
  if (!isObject(value) || !isObject(value.asset) || !isObject(value.sections) || !isObject(value.member_summary)) {
    throw new Error("Invalid operations center response envelope");
  }
  const raw = isObject(value) ? value : {};
  const asset = isObject(raw.asset) ? raw.asset : {};
  const rawSections = isObject(raw.sections) ? raw.sections : {};
  const governance = isObject(raw.governance) ? raw.governance : {};
  const messaging = isObject(raw.messaging) ? raw.messaging : {};
  const permissions = isObject(raw.permissions) ? raw.permissions : {};
  const memberSummary = normalizeMemberSummary(raw.member_summary);
  const normalizedSections = Object.fromEntries(
    SECTION_KEYS.map((key) => [key, normalizeSection(rawSections[key])]),
  ) as unknown as OperationsCenterSections;
  const parsedTelegramChatId = nullableSafeInteger(asset.telegram_chat_id);
  const telegramChatId = parsedTelegramChatId !== 0 ? parsedTelegramChatId : null;
  if (asset.telegram_chat_id !== null && asset.telegram_chat_id !== undefined && telegramChatId === null) {
    normalizedSections.governance.state = "unavailable";
    normalizedSections.governance.can_execute = false;
    normalizedSections.governance.blocking_reasons.push("telegram_chat_id_unsafe");
    normalizedSections.activities.state = "unavailable";
    normalizedSections.activities.can_execute = false;
    normalizedSections.activities.blocking_reasons.push("telegram_chat_id_unsafe");
  }
  if (!Number.isSafeInteger(numberValue(asset.asset_id)) || numberValue(asset.asset_id) <= 0) {
    throw new Error("Invalid operations center asset response");
  }
  const latest = isObject(raw.latest_operation) ? raw.latest_operation : null;
  return {
    asset: {
      asset_id: numberValue(asset.asset_id),
      internal_name: stringValue(asset.internal_name),
      title: stringValue(asset.title),
      visibility: stringValue(asset.visibility),
      asset_status: stringValue(asset.asset_status, "unknown"),
      telegram_chat_id: telegramChatId,
      telegram_chat_id_raw: rawIdentifier(asset.telegram_chat_id),
      core_group_id: nullableNumber(asset.core_group_id),
      managed_binding_id: nullableNumber(asset.managed_binding_id),
      guardian_bot_account_id: nullableNumber(asset.guardian_bot_account_id),
      owner_account_id: numberValue(asset.owner_account_id),
      managed_resource_count: Math.max(0, numberValue(asset.managed_resource_count)),
      updated_at: stringValue(asset.updated_at),
    },
    sections: normalizedSections,
    governance: {
      status: stringValue(governance.status, "disabled"),
      bot_account_id: nullableNumber(governance.bot_account_id),
      bot_role: nullableString(governance.bot_role),
      health_status: nullableString(governance.health_status),
      last_checked_at: nullableString(governance.last_checked_at),
    },
    messaging: {
      static_enabled: messaging.static_enabled === true,
      runtime_enabled: messaging.runtime_enabled === true,
      dry_run: messaging.dry_run === true,
      can_send: messaging.can_send === true,
      policy_count: Math.max(0, numberValue(messaging.policy_count)),
      enabled_policy_count: Math.max(0, numberValue(messaging.enabled_policy_count)),
      pending_review_count: Math.max(0, numberValue(messaging.pending_review_count)),
      sent_today: Math.max(0, numberValue(messaging.sent_today)),
    },
    member_summary: {
      ...memberSummary,
      managed_resource_count: memberSummary.managed_resource_count ?? Math.max(0, numberValue(asset.managed_resource_count)),
      observed_real_user_count: memberSummary.observed_real_user_count ?? 0,
      coverage: normalizeCoverage(memberSummary.coverage ?? (isObject(raw.member_summary) ? raw.member_summary.coverage : undefined)),
    },
    latest_operation: latest ? {
      id: numberValue(latest.id),
      ...(typeof latest.operation_type === "string" ? { operation_type: latest.operation_type } : {}),
      status: stringValue(latest.status, "unknown"),
      updated_at: stringValue(latest.updated_at),
    } : null,
    permissions: {
      manage_orchestration: permissions.manage_orchestration === true,
      manage_governance: permissions.manage_governance === true,
      manage_messaging: permissions.manage_messaging === true,
      manage_persona: permissions.manage_persona === true,
      manage_activities: permissions.manage_activities === true,
      view_members: permissions.view_members === true,
      view_audit: permissions.view_audit === true,
    },
    snapshot_at: stringValue(raw.snapshot_at),
  };
};

export const normalizeMember = (value: unknown): OwnedGroupMember => {
  const raw = isObject(value) ? value : {};
  if (typeof raw.member_key !== "string" || !raw.member_key) throw new Error("Invalid member response item");
  const qualityCodes = stringArray(raw.quality_codes);
  const parsedTelegramUserId = nullableSafeInteger(raw.telegram_user_id);
  const telegramUserId = parsedTelegramUserId !== null && parsedTelegramUserId > 0
    ? parsedTelegramUserId
    : null;
  if (raw.telegram_user_id !== null && raw.telegram_user_id !== undefined && telegramUserId === null) {
    qualityCodes.push("telegram_user_id_unsafe");
  }
  const rawKind = raw.member_kind;
  const kind = MEMBER_KINDS.has(rawKind as MemberKind) ? rawKind as MemberKind : null;
  const rawClassification = stringValue(raw.classification_status, "unresolved");
  let classification = CLASSIFICATION_STATUSES.has(rawClassification as ClassificationStatus)
    ? rawClassification as ClassificationStatus
    : "unresolved";
  if (!CLASSIFICATION_STATUSES.has(rawClassification as ClassificationStatus)) {
    qualityCodes.push(`unknown_classification_status:${rawClassification}`);
  }
  if (rawKind !== null && rawKind !== undefined && !kind) {
    classification = "unresolved";
    qualityCodes.push(`unknown_member_kind:${String(rawKind)}`);
  }
  const rawPresence = stringValue(raw.presence_status, "unknown");
  const presence = PRESENCE_STATUSES.has(rawPresence as PresenceStatus) ? rawPresence as PresenceStatus : "unknown";
  if (presence === "unknown" && rawPresence !== "unknown") qualityCodes.push(`unknown_presence_status:${rawPresence}`);
  const rawConfidence = stringValue(raw.presence_confidence, "unknown");
  const confidence = PRESENCE_CONFIDENCE.has(rawConfidence as PresenceConfidence) ? rawConfidence as PresenceConfidence : "unknown";
  if (confidence === "unknown" && rawConfidence !== "unknown") qualityCodes.push(`unknown_presence_confidence:${rawConfidence}`);
  const rawRole = stringValue(raw.telegram_role, "unknown");
  const role = TELEGRAM_ROLES.has(rawRole as TelegramRole) ? rawRole as TelegramRole : "unknown";
  if (role === "unknown" && rawRole !== "unknown") qualityCodes.push(`unknown_telegram_role:${rawRole}`);
  const rawQuality = stringValue(raw.data_quality, qualityCodes.length ? "partial" : "reliable");
  let quality = DATA_QUALITY.has(rawQuality as DataQuality) ? rawQuality as DataQuality : "partial";
  if (!DATA_QUALITY.has(rawQuality as DataQuality)) qualityCodes.push(`unknown_data_quality:${rawQuality}`);
  if (classification === "conflict") quality = "conflict";
  else if ((classification === "unresolved" || qualityCodes.length > 0) && quality === "reliable") quality = "partial";
  const rawSources = Array.isArray(raw.sources) ? raw.sources : [];
  return {
    member_key: stringValue(raw.member_key),
    member_kind: kind,
    classification_status: classification,
    classification_reason: stringValue(raw.classification_reason, "identity_incomplete"),
    telegram_user_id: telegramUserId,
    telegram_user_id_raw: rawIdentifier(raw.telegram_user_id),
    display_name: nullableString(raw.display_name),
    username: nullableString(raw.username)?.replace(/^@+/, "") ?? null,
    primary_source: stringValue(raw.primary_source),
    sources: rawSources.filter(isObject).map((source) => ({
      source: stringValue(source.source),
      record_id: numberValue(source.record_id),
      observed_at: nullableString(source.observed_at),
    })),
    account_id: nullableNumber(raw.account_id),
    owned_bot_profile_id: nullableNumber(raw.owned_bot_profile_id),
    guardian_bot_profile_id: nullableNumber(raw.guardian_bot_profile_id),
    parent_account_id: nullableNumber(raw.parent_account_id),
    user_id: nullableNumber(raw.user_id),
    operation_mode: nullableString(raw.operation_mode),
    account_status: nullableString(raw.account_status),
    account_risk_level: nullableString(raw.account_risk_level),
    risk_pause_until: nullableString(raw.risk_pause_until),
    risk_scope: nullableString(raw.risk_scope),
    persona_configured: typeof raw.persona_configured === "boolean" ? raw.persona_configured : null,
    persona_revision: nullableNumber(raw.persona_revision),
    telegram_role: role,
    is_admin: raw.is_admin === true,
    admin_title: nullableString(raw.admin_title),
    presence_status: presence,
    presence_confidence: confidence,
    raw_presence_status: nullableString(raw.raw_presence_status) ?? rawPresence,
    user_state: nullableString(raw.user_state),
    warning_count: nullableNumber(raw.warning_count),
    muted_until: nullableString(raw.muted_until),
    joined_at: nullableString(raw.joined_at),
    left_at: nullableString(raw.left_at),
    last_verified_at: nullableString(raw.last_verified_at),
    last_observed_at: nullableString(raw.last_observed_at),
    data_quality: quality,
    quality_codes: [...new Set(qualityCodes)],
    bot_health_status: nullableString(raw.bot_health_status),
    bot_sync_status: nullableString(raw.bot_sync_status),
  };
};

const requireLegacyWire = <T>(value: unknown): LegacyListWire<T> => {
  if (!isObject(value) || value.code !== 0 || !Array.isArray(value.data) || !Number.isFinite(Number(value.total))) {
    throw new Error("Invalid legacy list response");
  }
  return value as unknown as LegacyListWire<T>;
};

const normalizeOperation = (value: unknown): OperationListItem => {
  const raw = isObject(value) ? value : {};
  const rawStatus = stringValue(raw.status, "unknown");
  return {
    id: numberValue(raw.id), group_asset_id: numberValue(raw.group_asset_id),
    status: OPERATION_STATUSES.has(rawStatus as OwnedGroupOperationStatus) ? rawStatus as OwnedGroupOperationStatus : "unknown",
    raw_status: rawStatus,
    planned_count: nullableNumber(raw.planned_count), completed_count: nullableNumber(raw.completed_count),
    skipped_count: nullableNumber(raw.skipped_count), failed_count: nullableNumber(raw.failed_count),
    selection_snapshot_hash: stringValue(raw.selection_snapshot_hash),
    config_snapshot_hash: stringValue(raw.config_snapshot_hash), idempotency_key: stringValue(raw.idempotency_key),
    schedule_at: nullableString(raw.schedule_at), created_at: stringValue(raw.created_at), updated_at: stringValue(raw.updated_at),
  };
};

const normalizeAuditEvent = (value: unknown): AuditEventItem => {
  const raw = isObject(value) ? value : {};
  return {
    id: numberValue(raw.id), event_type: stringValue(raw.event_type), group_asset_id: nullableNumber(raw.group_asset_id),
    operation_id: nullableNumber(raw.operation_id), operation_item_id: nullableNumber(raw.operation_item_id),
    resource_type: nullableString(raw.resource_type), resource_id: nullableNumber(raw.resource_id), actor_id: nullableNumber(raw.actor_id),
    before_state: nullableString(raw.before_state), after_state: nullableString(raw.after_state), result: stringValue(raw.result),
    reason_code: nullableString(raw.reason_code), correlation_id: nullableString(raw.correlation_id), created_at: stringValue(raw.created_at),
  };
};

const compactParams = (value: object): JsonObject => Object.fromEntries(
  Object.entries(value).filter(([, item]) => item !== undefined && item !== null && item !== ""),
);

export const ownedGroupOperationsApi = {
  async getSummary(assetId: number, signal?: AbortSignal): Promise<OperationsCenterSummary> {
    const response = await apiClient.get(`/owned-groups/${assetId}/operations-center`, { signal });
    if (!isObject(response.data) || !isObject(response.data.data)) throw new Error("Invalid operations center response envelope");
    const payload = response.data.data;
    return normalizeOperationsCenterSummary(payload);
  },
  async listMembers(assetId: number, query: MemberQuery, signal?: AbortSignal): Promise<MemberPage> {
    const response = await apiClient.get(`/owned-groups/${assetId}/members`, { params: compactParams({ ...query }), signal });
    if (!isObject(response.data) || !Array.isArray(response.data.data) || !Number.isFinite(Number(response.data.total)) || !isObject(response.data.coverage) || !isObject(response.data.summary)) {
      throw new Error("Invalid member response envelope");
    }
    const raw = response.data;
    const rows = raw.data as unknown[];
    return {
      items: rows.map(normalizeMember), total: Math.max(0, numberValue(raw.total)), offset: Math.max(0, numberValue(raw.offset, query.offset)),
      limit: Math.max(1, numberValue(raw.limit, query.limit)), coverage: normalizeCoverage(raw.coverage),
      summary: normalizeMemberSummary(raw.summary), correlation_id: nullableString(raw.correlation_id),
    };
  },
  async listOperations(assetId: number, query: OperationQuery, signal?: AbortSignal): Promise<OperationPage> {
    const response = await apiClient.get(`/owned-groups/${assetId}/operations`, { params: compactParams(query), signal });
    const wire = requireLegacyWire<unknown>(response.data);
    return { items: wire.data.map(normalizeOperation), total: Math.max(0, numberValue(wire.total)) };
  },
  async listAuditEvents(assetId: number, query: AuditQuery, signal?: AbortSignal): Promise<AuditEventPage> {
    const response = await apiClient.get("/owned-groups/audit-events", { params: compactParams({ asset_id: assetId, ...query }), signal });
    const wire = requireLegacyWire<unknown>(response.data);
    return { items: wire.data.map(normalizeAuditEvent), total: Math.max(0, numberValue(wire.total)) };
  },
};
