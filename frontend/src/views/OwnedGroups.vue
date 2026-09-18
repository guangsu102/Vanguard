<script setup lang="ts">
import {
  computed,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import dayjs from "dayjs";
import { useRoute, useRouter } from "vue-router";
import { accountsApi, type Account, type AccountType } from "@/api/accounts";
import { getApiErrorMessage } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { useOwnedGroupStore } from "@/stores/ownedGroup";
import {
  getOwnedGroupGovernanceFailure,
  redactOwnedGroupError,
  type GovernanceCapabilities,
  type GovernanceFailure,
  type OwnedBotProfile,
  type OwnedGroupAsset,
  type OwnedGroupGovernanceCandidate,
  type OwnedGroupGovernanceState,
  type OwnedGroupInviteLink,
  type OwnedGroupOperationStatus,
  type OwnedGroupPrecheckResult,
  type OwnedGroupResourceSelection,
  type OwnedGroupResourceType,
} from "@/api/ownedGroups";

const store = useOwnedGroupStore();
const authStore = useAuthStore();
const route = useRoute();
const router = useRouter();
const accounts = ref<Account[]>([]);
const botAccounts = ref<Account[]>([]);
const errorMessage = ref("");
const actionMessage = ref("");
const botProfileError = ref("");
const selectedId = ref<number | null>(null);
const selectedResources = ref<string[]>([]);
type ResourceAdminConfig = {
  admin_required: boolean;
  admin_title: string;
  admin_permissions: Record<string, boolean>;
};

const resourceConfigs = reactive<Record<string, ResourceAdminConfig>>({});
const inviteLinksLoadedFor = ref<number | null>(null);
const inviteLinkAction = ref<string | null>(null);
const inviteLinksError = ref("");
const precheckResult = ref<OwnedGroupPrecheckResult | null>(null);
const creating = ref(false);
const deletingDraftId = ref<number | null>(null);
const dissolvingId = ref<number | null>(null);
const resolvingDissolutionId = ref<number | null>(null);
const prechecking = ref(false);
const submitting = ref(false);
const controlling = ref(false);
const reconcilingAsset = ref(false);
const reconcileChatId = ref("");
const reconcileUsername = ref("");
const botRegistrationVisible = ref(false);
const registeringBot = ref(false);
const verifyingBotId = ref<number | null>(null);
const governanceDialogVisible = ref(false);
const selectedGuardianBotAccountId = ref<number | undefined>();
const governanceActionError = ref<GovernanceFailure | null>(null);
const botForm = reactive({
  owner_account_id: undefined as number | undefined,
  account_id: undefined as number | undefined,
  bot_token: "",
});
let pollTimer: ReturnType<typeof setInterval> | undefined;
const MAX_PLANNED_RESOURCES = 2000;
const expandedResourceKeys = ref<string[]>([]);

const loadAccountsByType = async (
  accountType: AccountType,
): Promise<Account[]> => {
  const collected = new Map<number, Account>();
  let cursor: string | undefined;
  while (true) {
    const response = await accountsApi.list({
      limit: MAX_PLANNED_RESOURCES,
      account_type: accountType,
      ...(cursor ? { cursor } : {}),
    });
    response.list.forEach((account) => collected.set(account.id, account));
    const nextCursor = response.nextCursor || undefined;
    if (!response.hasMore || !nextCursor || nextCursor === cursor) break;
    cursor = nextCursor;
  }
  return Array.from(collected.values());
};

const ADMIN_PERMISSION_OPTIONS = [
  { key: "invite_users", label: "邀请成员" },
  { key: "change_info", label: "修改群信息" },
  { key: "post_messages", label: "发布消息" },
  { key: "edit_messages", label: "编辑消息" },
  { key: "delete_messages", label: "删除消息" },
  { key: "ban_users", label: "封禁成员" },
  { key: "pin_messages", label: "置顶消息" },
  { key: "add_admins", label: "添加管理员" },
  { key: "manage_topics", label: "管理话题" },
  { key: "manage_call", label: "管理语音/视频" },
  { key: "anonymous", label: "匿名管理员" },
] as const;

const GOVERNANCE_STATUS_LABELS: Record<OwnedGroupGovernanceState, string> = {
  disabled: "未接入",
  pending: "接入中",
  managed: "治理正常",
  degraded: "治理降级",
};

const GOVERNANCE_CAPABILITY_OPTIONS: Array<{
  key: keyof GovernanceCapabilities;
  label: string;
}> = [
  { key: "verification", label: "入群验证" },
  { key: "sensitive_keywords", label: "敏感词" },
  { key: "anti_spam", label: "反垃圾" },
  { key: "warn", label: "警告" },
  { key: "mute", label: "禁言" },
  { key: "ban", label: "封禁" },
  { key: "announcement", label: "公告" },
  { key: "pin_message", label: "置顶" },
  { key: "activity", label: "活动" },
];

const GOVERNANCE_PERMISSION_LABELS: Record<string, string> = {
  can_delete_messages: "删除消息",
  can_restrict_members: "限制成员",
  can_invite_users: "邀请成员",
  can_pin_messages: "置顶消息",
};

const draft = reactive({
  internal_name: "",
  title: "",
  about: "",
  visibility: "public" as "public" | "private",
  telegram_username: "",
  owner_account_id: undefined as number | undefined,
  invite_mode: "direct_invite" as const,
});

const ACCOUNT_STATUS_LABELS: Record<string, string> = {
  offline: "离线",
  online: "在线",
  working: "工作中",
  idle: "空闲",
  restricted: "受限",
  error: "错误",
  banned: "封禁",
};

const SPAM_STATUS_LABELS: Record<string, string> = {
  unknown: "未检测",
  queued: "排队中",
  checking: "检测中",
  clear: "正常",
  restricted: "受限（非封号）",
  flagged: "号码风控（未封号）",
  error: "检测异常",
};

const RISK_LEVEL_LABELS: Record<string, string> = {
  normal: "正常",
  watch: "观察",
  limited: "限流",
  frozen: "冻结",
  quarantined: "隔离",
};

const hasFutureAccountHold = (value?: string) => {
  if (!value) return false;
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    ? value
    : value + "Z";
  const deadline = dayjs(normalized);
  return deadline.isValid() && deadline.isAfter(dayjs());
};

const accountEligibilityReason = (account: Account): string => {
  if (!account.is_active) return "系统已停用";
  if (account.status === "restricted") return "Telegram 账号受限";
  if (account.status === "banned") return "Telegram 账号已封禁";
  if (account.status === "error") return "账号连接错误";
  if (account.spam_check_status === "restricted") return "SpamBot 已确认受限";
  if (["frozen", "quarantined"].includes(account.risk_level || "normal")) {
    return "账号风控已阻断";
  }
  if (
    account.risk_level === "limited" ||
    hasFutureAccountHold(account.risk_pause_until) ||
    hasFutureAccountHold(account.risk_recovery_until)
  ) {
    return "账号处于风控暂停期";
  }
  if (!["online", "idle"].includes(account.status)) {
    return "账号当前未就绪";
  }
  return "";
};

const accountOptionLabel = (account: Account): string => {
  const phone = account.phone || account.identifier || "账号 #" + account.id;
  const displayName =
    account.display_name && account.display_name !== phone
      ? "（" + account.display_name + "）"
      : "";
  const runtime = account.is_active
    ? ACCOUNT_STATUS_LABELS[account.status] || account.status
    : "系统停用";
  const spam =
    SPAM_STATUS_LABELS[account.spam_check_status || "unknown"] ||
    account.spam_check_status ||
    "未检测";
  const riskPaused =
    hasFutureAccountHold(account.risk_pause_until) ||
    hasFutureAccountHold(account.risk_recovery_until);
  const risk = riskPaused
    ? "暂停"
    : RISK_LEVEL_LABELS[account.risk_level || "normal"] ||
      account.risk_level ||
      "正常";
  return (
    phone +
    displayName +
    " · " +
    runtime +
    " · SpamBot" +
    spam +
    " · 风控" +
    risk
  );
};

const ownedGroupOwnerOptionLabel = (account: Account): string =>
  "账号类型 " +
  (account.operation_mode || "growth") +
  " · 已创建 " +
  (account.owned_group_created_count ?? 0) +
  " 个群 · " +
  accountOptionLabel(account);

const promoterOptions = computed(() =>
  accounts.value.filter((account) => account.account_type === "promoter"),
);
const ownerOptions = computed(() =>
  promoterOptions.value.filter((account) => !accountEligibilityReason(account)),
);
const resourceOptions = computed(() => promoterOptions.value);
const botProfiles = computed(() => store.botProfiles);
const botProfileOptions = computed(() =>
  botProfiles.value.filter((profile) => profile.enabled),
);
const selectedAsset = computed(
  () =>
    store.list.find((asset) => asset.id === selectedId.value) || store.current,
);
const ownerResourceKey = computed(() =>
  selectedAsset.value ? `user:${selectedAsset.value.owner_account_id}` : "",
);
const ownerExplicitlySelected = computed(
  () =>
    Boolean(ownerResourceKey.value) &&
    selectedResources.value.includes(ownerResourceKey.value),
);
const implicitOwnerCount = computed(() =>
  selectedAsset.value && !ownerExplicitlySelected.value ? 1 : 0,
);
const finalPlannedResourceCount = computed(
  () => selectedResources.value.length + implicitOwnerCount.value,
);
const plannedResourceLimitExceeded = computed(
  () => finalPlannedResourceCount.value > MAX_PLANNED_RESOURCES,
);
const isAdmin = computed(() => authStore.userInfo?.role === "admin");
const canOperateGovernance = computed(() =>
  ["admin", "operator"].includes(authStore.userInfo?.role || ""),
);
const selectedAssetId = computed(() => selectedAsset.value?.id);
const governance = computed(() => {
  const assetId = selectedAssetId.value;
  return assetId ? (store.governanceByAssetId[assetId] ?? null) : null;
});
const governanceStatus = computed<OwnedGroupGovernanceState>(
  () =>
    governance.value?.governance_status ||
    selectedAsset.value?.governance_status ||
    "disabled",
);
const governanceLoading = computed(() => {
  const assetId = selectedAssetId.value;
  return assetId ? Boolean(store.governanceLoadingByAssetId[assetId]) : false;
});
const governanceCandidates = computed(() => {
  const assetId = selectedAssetId.value;
  return assetId ? (store.governanceCandidatesByAssetId[assetId] ?? []) : [];
});
const governanceCandidatesLoading = computed(() => {
  const assetId = selectedAssetId.value;
  return assetId
    ? Boolean(store.governanceCandidatesLoadingByAssetId[assetId])
    : false;
});
const selectedGovernanceCandidate = computed(() =>
  governanceCandidates.value.find(
    (candidate) =>
      candidate.guardian_bot_account_id === selectedGuardianBotAccountId.value,
  ),
);
const currentGovernanceCandidate = computed(() =>
  governanceCandidates.value.find(
    (candidate) =>
      candidate.guardian_bot_account_id ===
      governance.value?.guardian_bot_account_id,
  ),
);
const currentGuardianBotAccount = computed(() =>
  botAccounts.value.find(
    (account) => account.id === governance.value?.guardian_bot_account_id,
  ),
);
const governanceBotLabel = computed(
  () =>
    governance.value?.guardian_bot_display_name ||
    currentGovernanceCandidate.value?.display_name ||
    currentGuardianBotAccount.value?.display_name ||
    currentGuardianBotAccount.value?.identifier ||
    (governance.value?.guardian_bot_account_id
      ? "Guardian Bot #" + governance.value.guardian_bot_account_id
      : "-"),
);
const governanceBotUsername = computed(() => {
  const raw =
    governance.value?.guardian_bot_username ||
    currentGovernanceCandidate.value?.username ||
    currentGuardianBotAccount.value?.identifier ||
    "";
  if (!raw) return "-";
  return raw.startsWith("@") ? raw : "@" + raw;
});
const governanceFailure = computed<GovernanceFailure | null>(() => {
  if (governanceActionError.value) return governanceActionError.value;
  if (governance.value?.failure) return governance.value.failure;
  const asset = selectedAsset.value;
  if (
    asset?.governance_last_error_code ||
    asset?.governance_last_error_message
  ) {
    return {
      reason: asset.governance_last_error_code || "governance_failed",
      message: asset.governance_last_error_message || "Guardian 治理状态异常",
      retryable: false,
      missing_permissions: [],
      correlation_id: null,
    };
  }
  return null;
});
const governanceCapabilityRows = computed(() =>
  GOVERNANCE_CAPABILITY_OPTIONS.map((item) => ({
    ...item,
    enabled: Boolean(governance.value?.capabilities[item.key]),
  })),
);
const governanceGrantedPermissions = computed(
  () => governance.value?.permission_probe?.granted_permissions ?? [],
);
const governanceMissingPermissions = computed(
  () => governance.value?.permission_probe?.missing_permissions ?? [],
);
const assetReadyForGovernance = computed(
  () => selectedAsset.value?.status === "ready",
);
const governanceDisabledReason = computed(() => {
  if (selectedAsset.value?.status === "archived") return "资产已归档";
  if (!assetReadyForGovernance.value) {
    return (
      "资产状态为 " +
      (selectedAsset.value?.status || "unknown") +
      "，必须达到 ready"
    );
  }
  if (!canOperateGovernance.value) return "当前角色仅可查看治理状态";
  return "";
});
const inviteLinks = computed(() => store.inviteLinks);
const inviteLinksLoading = computed(() => store.inviteLinksLoading);
const selectedResourceEntries = computed(() =>
  selectedResources.value.map((key) => {
    const [resourceType, rawId] = key.split(":");
    const resourceId = Number(rawId);
    const account =
      resourceType === "user"
        ? resourceOptions.value.find((item) => item.id === resourceId)
        : undefined;
    const profile =
      resourceType === "bot"
        ? botProfileOptions.value.find((item) => item.id === resourceId)
        : undefined;
    return {
      key,
      resource_type: resourceType as OwnedGroupResourceType,
      resource_id: resourceId,
      config: ensureResourceConfig(key),
      is_owner:
        resourceType === "user" &&
        resourceId === selectedAsset.value?.owner_account_id,
      label:
        resourceType === "bot"
          ? profile
            ? botProfileLabel(profile)
            : `Bot #${resourceId}`
          : account
            ? accountOptionLabel(account)
            : `用户 #${resourceId}`,
    };
  }),
);
const operation = computed(() => store.operation);
const operationActive = computed(() =>
  ["queued", "running", "stopping"].includes(operation.value?.status || ""),
);
const isDissolutionOperation = computed(
  () => operation.value?.operation_type === "dissolve",
);
const operationDissolutionNeedsReview = computed(
  () =>
    isAdmin.value &&
    isDissolutionOperation.value &&
    ["unknown", "failed"].includes(operation.value?.status || "") &&
    selectedAsset.value?.status === "needs_attention" &&
    selectedAsset.value?.pending_dissolution_review === true,
);
const assetActive = computed(() =>
  ["prechecking", "creating", "dissolving"].includes(selectedAsset.value?.status || ""),
);
const operationCanPause = computed(
  () =>
    !isDissolutionOperation.value &&
    ["queued", "running"].includes(operation.value?.status || ""),
);
const operationCanResume = computed(
  () => !isDissolutionOperation.value && operation.value?.status === "paused",
);
const operationCanStop = computed(
  () =>
    !isDissolutionOperation.value &&
    ["queued", "running", "paused", "unknown"].includes(
      operation.value?.status || "",
    ),
);
const operationCanRetry = computed(
  () =>
    !isDissolutionOperation.value &&
    ["stopped", "partial_completed", "failed"].includes(
      operation.value?.status || "",
    ),
);
const operationNeedsReconcile = computed(
  () =>
    !isDissolutionOperation.value &&
    ["unknown", "stopping"].includes(operation.value?.status || ""),
);
const assetNeedsReconcile = computed(() =>
  ["needs_attention", "create_failed"].includes(
    selectedAsset.value?.status || "",
  ),
);
const precheckViolations = computed(() => {
  const violations = precheckResult.value?.details?.violations;
  return Array.isArray(violations)
    ? (violations as Array<Record<string, unknown>>)
    : [];
});

const resourceKey = (
  resourceType: OwnedGroupResourceType,
  resourceId: number,
) => `${resourceType}:${resourceId}`;

const createDefaultResourceConfig = (): ResourceAdminConfig => ({
  admin_required: false,
  admin_title: "",
  admin_permissions: Object.fromEntries(
    ADMIN_PERMISSION_OPTIONS.map(({ key }) => [key, false]),
  ),
});

const ensureResourceConfig = (key: string): ResourceAdminConfig => {
  if (!resourceConfigs[key])
    resourceConfigs[key] = createDefaultResourceConfig();
  return resourceConfigs[key];
};

const setAdminRequired = (key: string, value: boolean) => {
  const config = ensureResourceConfig(key);
  config.admin_required = Boolean(value);
  if (
    config.admin_required &&
    !Object.values(config.admin_permissions).some(Boolean)
  ) {
    // A Telegram admin assignment without any right is rejected server-side;
    // choose the least surprising minimal right when the switch is enabled.
    config.admin_permissions.invite_users = true;
  }
  if (!config.admin_required) {
    Object.keys(config.admin_permissions).forEach((permission) => {
      config.admin_permissions[permission] = false;
    });
    config.admin_title = "";
    expandedResourceKeys.value = expandedResourceKeys.value.filter(
      (expandedKey) => expandedKey !== key,
    );
  }
};

const isResourceConfigExpanded = (key: string) =>
  expandedResourceKeys.value.includes(key);

const toggleResourceConfig = (key: string) => {
  expandedResourceKeys.value = isResourceConfigExpanded(key)
    ? expandedResourceKeys.value.filter((expandedKey) => expandedKey !== key)
    : [...expandedResourceKeys.value, key];
};

const isResourceOptionDisabled = (key: string, otherwiseDisabled = false) => {
  if (otherwiseDisabled) return true;
  if (selectedResources.value.includes(key)) return false;
  // At 1,999 non-owner selections, the implicit owner already makes 2,000.
  // Keep the owner selectable: making it explicit replaces the implicit row
  // and therefore does not increase the final plan size.
  if (key === ownerResourceKey.value && !ownerExplicitlySelected.value) {
    return false;
  }
  return finalPlannedResourceCount.value >= MAX_PLANNED_RESOURCES;
};

const validatePlannedResourceLimit = (): boolean => {
  if (!plannedResourceLimitExceeded.value) return true;
  ElMessage.warning(
    `最终计划数（含群主）不能超过 ${MAX_PLANNED_RESOURCES}；当前为 ${finalPlannedResourceCount.value}。请移除至少一个非群主资源，或显式选择群主替代自动计数。`,
  );
  return false;
};

const validateAccountEligibility = (
  accountId: number,
  contextLabel: string,
): boolean => {
  const account = accounts.value.find((item) => item.id === accountId);
  if (!account) {
    ElMessage.warning(contextLabel + "账号不存在或未加载，请刷新后重试");
    return false;
  }
  const reason = accountEligibilityReason(account);
  if (!reason) return true;
  ElMessage.warning(
    contextLabel +
      "账号 " +
      (account.phone || account.identifier || "#" + account.id) +
      " 不可用：" +
      reason,
  );
  return false;
};

const validatePlannedAccountEligibility = (): boolean => {
  if (!selectedAsset.value) return false;
  const accountIds = new Set<number>([selectedAsset.value.owner_account_id]);
  selectedResources.value.forEach((key) => {
    const [resourceType, rawId] = key.split(":");
    const accountId = Number(rawId);
    if (
      resourceType === "user" &&
      Number.isInteger(accountId) &&
      accountId > 0
    ) {
      accountIds.add(accountId);
    }
  });
  for (const accountId of accountIds) {
    if (!validateAccountEligibility(accountId, "计划成员")) {
      precheckResult.value = null;
      return false;
    }
  }
  return true;
};
const setAdminPermission = (
  key: string,
  permission: string,
  value: boolean,
) => {
  const config = ensureResourceConfig(key);
  if (!config.admin_required) return;
  config.admin_permissions[permission] = Boolean(value);
};

const buildResourceSelections = (): OwnedGroupResourceSelection[] =>
  selectedResources.value.flatMap((key) => {
    const [resourceType, rawId] = key.split(":");
    const resourceId = Number(rawId);
    if (
      (resourceType !== "user" && resourceType !== "bot") ||
      !Number.isInteger(resourceId) ||
      resourceId <= 0
    )
      return [];
    const config = ensureResourceConfig(key);
    const isOwner =
      resourceType === "user" &&
      resourceId === selectedAsset.value?.owner_account_id;
    const adminRequired = config.admin_required && !isOwner;
    const adminPermissions = adminRequired
      ? Object.fromEntries(
          Object.entries(config.admin_permissions).filter(
            ([, enabled]) => enabled,
          ),
        )
      : {};
    return [
      {
        resource_type: resourceType as OwnedGroupResourceType,
        resource_id: resourceId,
        admin_required: adminRequired,
        admin_permissions: adminPermissions,
        admin_title: adminRequired
          ? config.admin_title.trim() || undefined
          : undefined,
      },
    ];
  });

const validateResourceAdminConfigs = (): boolean => {
  for (const entry of selectedResourceEntries.value) {
    if (entry.is_owner) continue;
    const config = ensureResourceConfig(entry.key);
    if (!config.admin_required) continue;
    if (!Object.values(config.admin_permissions).some(Boolean)) {
      ElMessage.warning(`${entry.label} 至少需要选择一项管理员权限`);
      return false;
    }
    if (config.admin_title.trim().length > 16) {
      ElMessage.warning(`${entry.label} 的群内管理员头衔不能超过 16 个字符`);
      return false;
    }
  }
  return true;
};

const botProfileLabel = (profile: OwnedBotProfile) => {
  const name =
    profile.display_name || profile.bot_username || `Bot #${profile.id}`;
  return `${name} · ${profile.status}`;
};

const violationLabel = (violation: Record<string, unknown>) => {
  const resourceType =
    typeof violation.resource_type === "string"
      ? violation.resource_type
      : "resource";
  const resourceId =
    typeof violation.resource_id === "number"
      ? `#${violation.resource_id}`
      : "";
  const reason =
    typeof violation.reason === "string" ? violation.reason : "ineligible";
  return `${resourceType} ${resourceId}: ${reason}`.trim();
};

const governanceStatusType = (status: OwnedGroupGovernanceState) => {
  if (status === "managed") return "success";
  if (status === "pending") return "warning";
  if (status === "degraded") return "danger";
  return "info";
};

const governancePermissionLabel = (permission: string) =>
  GOVERNANCE_PERMISSION_LABELS[permission] || permission;

const formatGovernanceTime = (value?: string | null) =>
  value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "-";

const governanceCandidateLabel = (candidate: OwnedGroupGovernanceCandidate) => {
  const name =
    candidate.display_name ||
    candidate.username ||
    "Guardian Bot #" + candidate.guardian_bot_account_id;
  const username = candidate.username
    ? candidate.username.startsWith("@")
      ? candidate.username
      : "@" + candidate.username
    : "无用户名";
  return (
    name + " · " + username + " · account #" + candidate.guardian_bot_account_id
  );
};

const showError = (error: unknown, fallback: string) => {
  const responseData = (error as any)?.response?.data;
  let message = "";
  if (responseData?.detail && typeof responseData.detail === "object") {
    const reason = responseData.detail.reason;
    const details = responseData.detail.details;
    message = [reason, typeof details === "string" ? details : ""]
      .filter(Boolean)
      .join(": ");
  } else {
    message = getApiErrorMessage(responseData);
  }
  errorMessage.value = redactOwnedGroupError(message, fallback);
};

const showBotProfileError = (error: unknown, fallback: string) => {
  const status = (error as any)?.response?.status;
  if (status === 404) {
    botProfileError.value =
      "后端尚未启用自建 Bot profile 接口，暂不能把 Guardian Bot 账号用于自建群；请先完成服务端登记接口。";
    return;
  }
  const responseData = (error as any)?.response?.data;
  botProfileError.value = redactOwnedGroupError(
    getApiErrorMessage(responseData),
    fallback,
  );
};

const showInviteLinkError = (error: unknown, fallback: string) => {
  const responseData = (error as any)?.response?.data;
  const detail = responseData?.detail;
  const message =
    detail && typeof detail === "object" && typeof detail.reason === "string"
      ? detail.reason
      : getApiErrorMessage(responseData);
  inviteLinksError.value = redactOwnedGroupError(message, fallback);
};

const loadInviteLinks = async (assetId = selectedAsset.value?.id) => {
  if (!assetId || selectedAsset.value?.status !== "ready") {
    store.clearInviteLinks();
    inviteLinksLoadedFor.value = null;
    return;
  }
  inviteLinksError.value = "";
  try {
    await store.fetchInviteLinks(assetId);
    if (selectedId.value === assetId) inviteLinksLoadedFor.value = assetId;
  } catch (error) {
    if (selectedId.value === assetId) {
      store.clearInviteLinks();
      inviteLinksLoadedFor.value = null;
      showInviteLinkError(error, "邀请链接加载失败");
    }
  }
};

const copyInviteLink = async (row: OwnedGroupInviteLink) => {
  if (
    !row.available ||
    !row.link ||
    row.group_asset_id !== selectedAsset.value?.id
  ) {
    ElMessage.warning("当前邀请链接不可复制");
    return;
  }
  try {
    if (!navigator.clipboard?.writeText)
      throw new Error("clipboard_unavailable");
    await navigator.clipboard.writeText(row.link);
    ElMessage.success("邀请链接已复制，请按敏感凭据妥善保管");
  } catch {
    ElMessage.warning("浏览器未允许访问剪贴板，请手动选择复制");
  }
};

const revokeInviteLink = async (row: OwnedGroupInviteLink) => {
  if (!isAdmin.value || !selectedAsset.value || row.id === null) return;
  try {
    await ElMessageBox.confirm(
      "撤销后所有持有旧链接的人都将无法继续使用，确认撤销？",
      "撤销邀请链接",
      { type: "warning" },
    );
  } catch {
    return;
  }
  inviteLinkAction.value = `revoke:${row.id}`;
  inviteLinksError.value = "";
  try {
    await store.revokeInviteLink(selectedAsset.value.id, row.id);
    inviteLinksLoadedFor.value = selectedAsset.value.id;
    ElMessage.success("邀请链接已撤销");
  } catch (error) {
    showInviteLinkError(error, "邀请链接撤销失败");
  } finally {
    inviteLinkAction.value = null;
  }
};

const regenerateInviteLink = async () => {
  if (!isAdmin.value || !selectedAsset.value) return;
  try {
    await ElMessageBox.confirm(
      "重新生成会先撤销当前有效链接。新链接生成后，请更新所有分发位置。",
      "重新生成邀请链接",
      { type: "warning" },
    );
  } catch {
    return;
  }
  const assetId = selectedAsset.value.id;
  inviteLinkAction.value = "regenerate";
  inviteLinksError.value = "";
  try {
    await store.regenerateInviteLink(
      assetId,
      selectedAsset.value.invite_mode === "manual_approval",
    );
    if (selectedId.value === assetId) inviteLinksLoadedFor.value = assetId;
    ElMessage.success("新的邀请链接已生成");
  } catch (error) {
    showInviteLinkError(error, "邀请链接重新生成失败");
  } finally {
    inviteLinkAction.value = null;
  }
};

const refreshGovernance = async (
  assetId = selectedAsset.value?.id,
  startPendingPolling = true,
) => {
  if (!assetId) return null;
  try {
    const result = await store.fetchGovernance(assetId);
    governanceActionError.value = null;
    if (result.governance_status === "pending" && startPendingPolling) {
      startPolling();
    }
    return result;
  } catch (error) {
    governanceActionError.value = getOwnedGroupGovernanceFailure(
      error,
      "治理状态加载失败",
    );
    return null;
  }
};

const openGovernanceDialog = async () => {
  if (!selectedAsset.value || !assetReadyForGovernance.value) {
    ElMessage.warning(governanceDisabledReason.value || "资产尚未 ready");
    return;
  }
  if (!canOperateGovernance.value) {
    ElMessage.warning("当前角色仅可查看治理状态");
    return;
  }
  if (governanceStatus.value === "pending") {
    ElMessage.warning("治理操作正在执行，不能选择不同 Bot");
    return;
  }
  if (governanceStatus.value !== "disabled") {
    ElMessage.warning("已接入治理的资产只能重新检测，不能更换 Bot");
    return;
  }
  selectedGuardianBotAccountId.value =
    governance.value?.guardian_bot_account_id ?? undefined;
  governanceActionError.value = null;
  governanceDialogVisible.value = true;
  try {
    const candidates = await store.fetchGovernanceCandidates(
      selectedAsset.value.id,
    );
    if (!selectedGuardianBotAccountId.value && candidates.length === 1) {
      selectedGuardianBotAccountId.value =
        candidates[0].guardian_bot_account_id;
    }
  } catch (error) {
    governanceActionError.value = getOwnedGroupGovernanceFailure(
      error,
      "可用 Guardian Bot 加载失败",
    );
  }
};

const bindGovernance = async () => {
  const assetId = selectedAsset.value?.id;
  if (!assetId || !selectedGuardianBotAccountId.value) {
    ElMessage.warning("请选择 Guardian Bot");
    return;
  }
  governanceActionError.value = null;
  try {
    const result = await store.bindGovernance(
      assetId,
      selectedGuardianBotAccountId.value,
    );
    governanceDialogVisible.value = false;
    actionMessage.value =
      result.governance_status === "managed"
        ? "Guardian 治理接入成功"
        : "Guardian 治理请求已提交，当前状态：" + result.governance_status;
    if (result.governance_status === "managed") {
      ElMessage.success("Guardian 治理接入成功");
    } else {
      ElMessage.warning(actionMessage.value);
    }
    if (result.governance_status === "pending") startPolling();
  } catch (error) {
    governanceActionError.value = getOwnedGroupGovernanceFailure(
      error,
      "Guardian 治理接入失败",
    );
  }
};

const reconcileGovernance = async () => {
  const assetId = selectedAsset.value?.id;
  if (!assetId || !canOperateGovernance.value) return;
  try {
    await ElMessageBox.confirm(
      "将实时检测 Bot 身份、群成员角色和管理员权限，并修复内部绑定；不会自动拉 Bot 入群或授予管理员权限。",
      "重新检测 Guardian 治理",
      { type: "warning", confirmButtonText: "开始检测" },
    );
  } catch {
    return;
  }
  governanceActionError.value = null;
  try {
    const result = await store.reconcileGovernance(assetId);
    actionMessage.value =
      result.governance_status === "managed"
        ? "Guardian 治理检测通过"
        : "Guardian 治理检测完成，当前状态：" + result.governance_status;
    if (result.governance_status === "managed") {
      ElMessage.success("Guardian 治理检测通过");
    } else {
      ElMessage.warning(actionMessage.value);
    }
    if (result.governance_status === "pending") startPolling();
  } catch (error) {
    governanceActionError.value = getOwnedGroupGovernanceFailure(
      error,
      "Guardian 治理重新检测失败",
    );
  }
};

const openGovernancePolicies = () => {
  const asset = selectedAsset.value;
  const status = governance.value;
  const telegramChatId = status?.telegram_chat_id ?? asset?.telegram_chat_id;
  const guardianBotAccountId =
    status?.guardian_bot_account_id ?? asset?.guardian_bot_account_id;
  if (
    !asset ||
    !Number.isSafeInteger(telegramChatId) ||
    telegramChatId === 0 ||
    !guardianBotAccountId
  ) {
    ElMessage.warning("治理关联 ID 尚未就绪，请先重新检测");
    return;
  }
  router.push({
    path: "/guardian/policies",
    query: {
      groupId: String(telegramChatId),
      title: asset.title,
      botId: String(guardianBotAccountId),
      source: "owned_group",
      assetId: String(asset.id),
    },
  });
};

const ownedGroupMessagingDisabledReason = (asset: OwnedGroupAsset) => {
  if (asset.status === "archived") return "资产已归档";
  if (asset.status !== "ready") return "资产必须达到 ready";
  if (!Number.isSafeInteger(asset.core_group_id) || !asset.core_group_id)
    return "核心群映射未就绪";
  return "";
};

const asOwnedGroupAsset = (value: unknown) => value as OwnedGroupAsset;

const canDeleteFailedDraft = (asset: OwnedGroupAsset) =>
  isAdmin.value &&
  asset.status === "needs_attention" &&
  asset.telegram_chat_id == null &&
  asset.core_group_id == null &&
  asset.managed_binding_id == null &&
  asset.guardian_bot_account_id == null &&
  asset.governance_status === "disabled";

const deleteFailedDraft = async (asset: OwnedGroupAsset) => {
  if (!canDeleteFailedDraft(asset)) return;
  try {
    await ElMessageBox.confirm(
      "请先确认已核实 Telegram 中没有创建该群。此操作只删除 Vanguard 本地失败草稿，不会调用 Telegram，也无法用于删除真实群。是否继续？",
      "删除失败草稿",
      {
        type: "warning",
        confirmButtonText: "已核实未建群，删除草稿",
        cancelButtonText: "取消",
      },
    );
  } catch {
    return;
  }

  deletingDraftId.value = asset.id;
  try {
    await store.deleteFailedDraft(asset.id);
    if (selectedId.value === asset.id) {
      selectedId.value = null;
      selectedResources.value = [];
      expandedResourceKeys.value = [];
      Object.keys(resourceConfigs).forEach(
        (key) => delete resourceConfigs[key],
      );
      precheckResult.value = null;
      actionMessage.value = "";
      stopPolling();
    }
    ElMessage.success("本地失败草稿已删除");
  } catch (error) {
    showError(error, "删除失败草稿失败");
  } finally {
    deletingDraftId.value = null;
  };
};
const canDissolveGroup = (asset: OwnedGroupAsset) =>
  isAdmin.value &&
  asset.status === "ready" &&
  Number.isSafeInteger(asset.telegram_chat_id) &&
  asset.telegram_chat_id !== 0;

const queueDissolution = async (asset: OwnedGroupAsset) => {
  if (!canDissolveGroup(asset)) return;
  const confirmation = "DISSOLVE " + asset.id;
  let confirmedValue: string;
  try {
    const prompt = await ElMessageBox.prompt(
      "此操作会先将资产置为解散中，再由后台单线程任务发起 Telegram 解散。解散后群和历史内容无法恢复；如果远端结果不确定，系统会转人工核验，不会自动重试。",
      "解散 Telegram 群",
      {
        type: "error",
        confirmButtonText: "确认排队解散",
        cancelButtonText: "取消",
        inputPlaceholder: confirmation,
        inputValidator: (value) =>
          value?.trim().toUpperCase() === confirmation ||
          "请输入确认短语 " + confirmation,
      },
    );
    confirmedValue = prompt.value.trim().toUpperCase();
  } catch {
    return;
  }

  dissolvingId.value = asset.id;
  errorMessage.value = "";
  try {
    await store.queueDissolution(asset.id, confirmedValue);
    if (selectedId.value !== asset.id) {
      selectedId.value = asset.id;
      selectedResources.value = [];
      expandedResourceKeys.value = [];
      Object.keys(resourceConfigs).forEach(
        (key) => delete resourceConfigs[key],
      );
      precheckResult.value = null;
    }
    actionMessage.value =
      "群 #" + asset.id + " 的解散任务已入队，正在等待单线程执行";
    startPolling();
    ElMessage.success("解散群任务已入队");
  } catch (error) {
    showError(error, "解散群排队失败");
  } finally {
    dissolvingId.value = null;
  }
};

const canResolveDissolution = (asset: OwnedGroupAsset) =>
  isAdmin.value &&
  asset.status === "needs_attention" &&
  asset.pending_dissolution_review === true;

const resolveDissolution = async (asset: OwnedGroupAsset) => {
  if (!canResolveDissolution(asset)) return;
  const archivedPhrase = "CONFIRM DISSOLVED " + asset.id;
  const existsPhrase = "CONFIRM EXISTS " + asset.id;
  let promptValue: string;
  try {
    const prompt = await ElMessageBox.prompt(
      "请先在 Telegram 中核实该群的真实状态，再输入对应确认短语：输入 " +
        archivedPhrase +
        " 表示群已解散（资产归档，不可恢复）；输入 " +
        existsPhrase +
        " 表示群仍存在（资产恢复为可用，可重新发起解散）。",
      "人工核验解散结果",
      {
        type: "warning",
        confirmButtonText: "提交核验结论",
        cancelButtonText: "取消",
        inputPlaceholder: archivedPhrase + " 或 " + existsPhrase,
        inputValidator: (value) => {
          const normalized = value?.trim().toUpperCase() ?? "";
          if (normalized === archivedPhrase || normalized === existsPhrase) {
            return true;
          }
          return (
            "请输入 " + archivedPhrase + " 或 " + existsPhrase
          );
        },
      },
    );
    promptValue = prompt.value.trim().toUpperCase();
  } catch {
    return;
  }

  const outcome =
    promptValue === archivedPhrase ? "archived" : "still_exists";
  resolvingDissolutionId.value = asset.id;
  errorMessage.value = "";
  try {
    await store.resolveDissolution(asset.id, outcome, promptValue);
    if (selectedId.value !== asset.id) {
      selectedId.value = asset.id;
      selectedResources.value = [];
      expandedResourceKeys.value = [];
      Object.keys(resourceConfigs).forEach(
        (key) => delete resourceConfigs[key],
      );
      precheckResult.value = null;
    }
    actionMessage.value =
      outcome === "archived"
        ? "已确认群组解散，资产 #" + asset.id + " 归档"
        : "已确认群仍存在，资产 #" + asset.id + " 恢复为可用";
    ElMessage.success(actionMessage.value);
  } catch (error) {
    showError(error, "人工核验提交失败");
  } finally {
    resolvingDissolutionId.value = null;
  }
};

const openOperationsCenter = (asset: OwnedGroupAsset) => {
  if (!Number.isSafeInteger(asset.id) || asset.id <= 0) return;
  router.push(`/owned-groups/${asset.id}/operations`);
};

const openOwnedGroupMessaging = (asset: OwnedGroupAsset) => {
  const reason = ownedGroupMessagingDisabledReason(asset);
  if (reason) {
    ElMessage.warning(reason);
    return;
  }
  router.push(`/owned-groups/${asset.id}/messaging`);
};

const viewGovernanceFailure = () => {
  const failure = governanceFailure.value;
  if (!failure) {
    ElMessage.info("暂无治理失败记录");
    return;
  }
  const lines = [
    failure.message,
    "Reason: " + failure.reason,
    failure.missing_permissions?.length
      ? "缺失权限：" +
        failure.missing_permissions.map(governancePermissionLabel).join("、")
      : "",
    failure.correlation_id ? "Correlation ID: " + failure.correlation_id : "",
  ].filter(Boolean);
  ElMessageBox.alert(lines.join("\n"), "Guardian 治理失败原因", {
    confirmButtonText: "知道了",
  });
};

const applyRouteAssetSelection = async () => {
  const rawValue = Array.isArray(route.query.assetId)
    ? route.query.assetId[0]
    : route.query.assetId;
  if (rawValue === undefined || rawValue === null || rawValue === "") {
    return false;
  }
  const assetId = Number(rawValue);
  if (!Number.isSafeInteger(assetId) || assetId <= 0) return false;
  let asset = store.list.find((item) => item.id === assetId);
  if (!asset) {
    try {
      asset = await store.fetchAsset(assetId);
    } catch (error) {
      showError(error, "指定的自建群资产加载失败");
      return false;
    }
  }
  if (selectedId.value !== assetId) {
    selectAsset(asset);
  } else {
    await refreshGovernance(assetId);
  }
  return true;
};

const stopPolling = () => {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = undefined;
};

const poll = async () => {
  try {
    if (operation.value) await store.refreshOperation();
    if (selectedId.value && assetActive.value)
      await store.fetchAsset(selectedId.value);
    if (selectedId.value && governanceStatus.value === "pending") {
      await refreshGovernance(selectedId.value, false);
    }
    if (
      selectedId.value &&
      selectedAsset.value?.status === "ready" &&
      inviteLinksLoadedFor.value !== selectedId.value
    )
      await loadInviteLinks(selectedId.value);
    if (
      !operationActive.value &&
      !assetActive.value &&
      governanceStatus.value !== "pending"
    )
      stopPolling();
  } catch (error) {
    showError(error, "刷新操作状态失败");
  }
};

const startPolling = () => {
  stopPolling();
  pollTimer = setInterval(() => void poll(), 3000);
};

const load = async () => {
  errorMessage.value = "";
  try {
    await Promise.all([
      store.fetchList(),
      loadAccountsByType("promoter").then((response) => {
        accounts.value = response;
      }),
      loadAccountsByType("guardian_bot").then((response) => {
        botAccounts.value = response;
      }),
    ]);
    botProfileError.value = "";
    try {
      await store.fetchBotProfiles();
    } catch (error) {
      showBotProfileError(error, "自建 Bot profile 列表加载失败");
    }
    if (selectedId.value && !selectedAsset.value) selectedId.value = null;
    const selectedFromRoute = await applyRouteAssetSelection();
    if (!selectedFromRoute && selectedId.value) {
      await refreshGovernance(selectedId.value);
    }
  } catch (error) {
    showError(error, "自建群资产加载失败");
  }
};

const openBotRegistration = () => {
  if (!isAdmin.value) {
    ElMessage.warning("只有管理员可以登记 Bot profile");
    return;
  }
  botForm.owner_account_id =
    draft.owner_account_id || ownerOptions.value[0]?.id;
  botForm.account_id = undefined;
  botForm.bot_token = "";
  botProfileError.value = "";
  botRegistrationVisible.value = true;
};

const registerBotProfile = async () => {
  if (!isAdmin.value) {
    ElMessage.warning("只有管理员可以登记 Bot profile");
    return;
  }
  if (!botForm.owner_account_id || !botForm.account_id) {
    ElMessage.warning("请选择归属用户和 Bot 账号");
    return;
  }
  if (!validateAccountEligibility(botForm.owner_account_id, "归属用户")) return;
  registeringBot.value = true;
  botProfileError.value = "";
  try {
    const profile = await store.registerBotProfile({
      owner_account_id: botForm.owner_account_id,
      account_id: botForm.account_id,
      bot_token: botForm.bot_token.trim(),
    });
    actionMessage.value = `Bot profile #${profile.id} 已登记，当前状态：${profile.status}`;
    botRegistrationVisible.value = false;
    ElMessage.success("Bot profile 已登记；请等待验证通过后再选择");
  } catch (error) {
    showBotProfileError(error, "Bot profile 登记失败");
  } finally {
    // Never retain a Bot token in component state after the request settles.
    botForm.bot_token = "";
    registeringBot.value = false;
  }
};

const verifyBotProfile = async (profile: OwnedBotProfile) => {
  if (!isAdmin.value) {
    ElMessage.warning("只有管理员可以验证 Bot profile");
    return;
  }
  verifyingBotId.value = profile.id;
  botProfileError.value = "";
  try {
    const result = await store.verifyBotProfile(profile.id);
    actionMessage.value = `Bot profile #${result.id} 验证结果：${result.status}`;
    ElMessage.success(
      result.status === "verified" || result.status === "active"
        ? "Bot Token 验证通过"
        : "验证请求已提交",
    );
  } catch (error) {
    showBotProfileError(error, "Bot profile 验证失败");
  } finally {
    verifyingBotId.value = null;
  }
};

const toggleBotProfile = async (profile: OwnedBotProfile, enabled: boolean) => {
  if (!isAdmin.value) {
    ElMessage.warning("只有管理员可以修改 Bot profile");
    return;
  }
  botProfileError.value = "";
  try {
    await store.setBotProfileEnabled(profile.id, enabled);
  } catch (error) {
    showBotProfileError(error, "Bot profile 状态更新失败");
  }
};

const createDraft = async () => {
  if (
    !draft.internal_name.trim() ||
    !draft.title.trim() ||
    !draft.owner_account_id
  )
    return ElMessage.warning("请填写内部名称、群标题并选择群主账号");
  if (!validateAccountEligibility(draft.owner_account_id, "群主")) return;
  if (
    draft.visibility === "public" &&
    !/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(draft.telegram_username.trim())
  )
    return ElMessage.warning(
      "公开群用户名需为 5-32 位字母、数字或下划线，并以字母开头",
    );
  creating.value = true;
  errorMessage.value = "";
  try {
    const asset = await store.createDraft({
      ...draft,
      owner_account_id: draft.owner_account_id,
      telegram_username:
        draft.visibility === "public"
          ? draft.telegram_username.trim()
          : undefined,
    });
    selectedId.value = asset.id;
    store.select(asset);
    selectedResources.value = [];
    expandedResourceKeys.value = [];
    Object.keys(resourceConfigs).forEach((key) => delete resourceConfigs[key]);
    reconcileChatId.value = "";
    reconcileUsername.value = "";
    precheckResult.value = null;
    actionMessage.value =
      "草稿 #" + asset.id + " 已创建，当前状态：" + asset.status;
    ElMessage.success("自建群草稿已创建");
  } catch (error) {
    showError(error, "创建草稿失败");
  } finally {
    creating.value = false;
  }
};

const reconcileAsset = async () => {
  if (!selectedAsset.value || !assetNeedsReconcile.value) return;
  if (!isAdmin.value) {
    ElMessage.warning("只有管理员可以绑定并对账 Telegram 群");
    return;
  }
  const rawChatId = reconcileChatId.value.trim();
  const rawUsername = reconcileUsername.value.trim();
  let telegramChatId: number | undefined;
  if (rawChatId) {
    if (!/^-?\d+$/.test(rawChatId)) {
      ElMessage.warning("Telegram chat ID 必须是整数");
      return;
    }
    telegramChatId = Number(rawChatId);
    if (!Number.isSafeInteger(telegramChatId) || telegramChatId === 0) {
      ElMessage.warning("Telegram chat ID 无效");
      return;
    }
  }
  let telegramUsername: string | undefined;
  if (rawUsername) {
    telegramUsername = rawUsername.startsWith("@")
      ? rawUsername.slice(1)
      : rawUsername;
    if (!/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(telegramUsername)) {
      ElMessage.warning(
        "公开群用户名需为 5-32 位字母、数字或下划线，并以字母开头",
      );
      return;
    }
  }
  try {
    await ElMessageBox.confirm(
      telegramChatId === undefined && telegramUsername === undefined
        ? "将使用系统已保存的 chat ID 核验群主权限、超级群类型和访问配置；此操作不会创建新群。"
        : `将把已有群${telegramChatId === undefined ? "" : `（chat ID ${telegramChatId}）`}按候选用户名${telegramUsername ? ` @${telegramUsername}` : ""}进行核验；此操作不会创建新群。`,
      "恢复并对账已有群",
      { type: "warning", confirmButtonText: "确认对账" },
    );
  } catch {
    return;
  }
  const assetId = selectedAsset.value.id;
  reconcilingAsset.value = true;
  errorMessage.value = "";
  try {
    const result = await store.reconcileAsset(
      assetId,
      telegramChatId,
      telegramUsername,
    );
    if (result.status === "ready") {
      reconcileChatId.value = "";
      reconcileUsername.value = "";
      actionMessage.value = "已有 Telegram 群验证通过，资产已恢复为 ready";
      await loadInviteLinks(assetId);
      ElMessage.success("资产对账完成");
    } else {
      actionMessage.value = `资产仍需处理：${result.reason_code || result.status}`;
      ElMessage.warning("未能确认已有群，系统不会自动重试建群");
    }
  } catch (error) {
    showError(error, "资产对账失败");
  } finally {
    reconcilingAsset.value = false;
  }
};

const precheck = async () => {
  if (!selectedAsset.value) return ElMessage.warning("请先选择一个资产");
  if (operationActive.value)
    return ElMessage.warning(
      "当前成员编排仍在执行，请完成或停止后再提交下一批",
    );
  if (!validatePlannedResourceLimit()) return;
  if (!validatePlannedAccountEligibility()) return;
  const resources = buildResourceSelections();
  if (!resources.length)
    return ElMessage.warning(
      "请先选择至少一个用户或已验证 Bot，再执行资源预检查",
    );
  if (!validateResourceAdminConfigs()) return;
  prechecking.value = true;
  errorMessage.value = "";
  try {
    const dryRun = await store.precheckOperation(selectedAsset.value.id, {
      resources,
    });
    precheckResult.value = dryRun;
    if (!dryRun.allowed) {
      actionMessage.value = `资源预检查未通过：${dryRun.reason}`;
      ElMessage.warning("资源预检查未通过，请修正后重试");
      return;
    }
    if (selectedAsset.value.status === "ready") {
      actionMessage.value =
        "资源预检查通过；群资产已经 READY，可直接提交成员操作";
    } else {
      await store.precheck(selectedAsset.value.id);
      actionMessage.value = "资源预检查通过，群创建预检查已排队";
      startPolling();
    }
  } catch (error) {
    showError(error, "预检查失败");
  } finally {
    prechecking.value = false;
  }
};

const submit = async () => {
  if (!selectedAsset.value) return ElMessage.warning("请先选择一个资产");
  if (operationActive.value)
    return ElMessage.warning(
      "当前成员编排仍在执行，请完成或停止后再提交下一批",
    );
  if (!validatePlannedResourceLimit()) return;
  if (!validatePlannedAccountEligibility()) return;
  const resources = buildResourceSelections();
  if (!resources.length) return ElMessage.warning("请选择至少一个计划成员");
  if (!validateResourceAdminConfigs()) return;
  if (!precheckResult.value?.allowed)
    return ElMessage.warning("请先完成所选资源的严格预检查");
  submitting.value = true;
  errorMessage.value = "";
  try {
    const result = await store.submitOperation(selectedAsset.value.id, {
      resources,
    });
    if (result) {
      actionMessage.value =
        "操作 #" + result.id + " 已提交，状态：" + result.status;
      selectedResources.value = [];
      expandedResourceKeys.value = [];
      Object.keys(resourceConfigs).forEach(
        (key) => delete resourceConfigs[key],
      );
      precheckResult.value = null;
      startPolling();
    }
    ElMessage.success("操作已提交");
  } catch (error) {
    showError(error, "提交失败：资产尚未 READY 或资源未通过预检查");
  } finally {
    submitting.value = false;
  }
};

const control = async (
  action: "pause" | "resume" | "stop" | "retry" | "reconcile",
) => {
  if (!operation.value) return;
  if (action === "stop") {
    try {
      await ElMessageBox.confirm(
        "停止后未执行项目将不会继续处理，确认停止？",
        "确认停止",
        { type: "warning" },
      );
    } catch {
      return;
    }
  }
  controlling.value = true;
  errorMessage.value = "";
  try {
    await store.controlOperation(action);
    actionMessage.value = "操作控制已提交";
    startPolling();
  } catch (error) {
    showError(error, "操作控制失败");
  } finally {
    controlling.value = false;
  }
};

const selectAsset = (asset: OwnedGroupAsset) => {
  selectedId.value = asset.id;
  store.select(asset);
  selectedResources.value = [];
  expandedResourceKeys.value = [];
  Object.keys(resourceConfigs).forEach((key) => delete resourceConfigs[key]);
  inviteLinksLoadedFor.value = null;
  inviteLinksError.value = "";
  precheckResult.value = null;
  actionMessage.value = "";
  reconcileChatId.value = "";
  reconcileUsername.value = "";
  stopPolling();
  if (asset.status === "ready") void loadInviteLinks(asset.id);
  void refreshGovernance(asset.id);
};

const statusType = (status: OwnedGroupOperationStatus | string) => {
  if (["completed", "member_verified", "admin_verified"].includes(status))
    return "success";
  if (["failed", "unknown", "create_failed"].includes(status)) return "danger";
  if (["paused", "stopping", "needs_attention", "dissolving"].includes(status))
    return "warning";
  return "info";
};

// A dry-run is tied to the exact selection snapshot.  Changing the selected
// resources invalidates the previous result and forces a fresh server check.
watch(
  selectedResources,
  (keys) => {
    const selected = new Set(keys);
    keys.forEach((key) => ensureResourceConfig(key));
    Object.keys(resourceConfigs).forEach((key) => {
      if (!selected.has(key)) delete resourceConfigs[key];
    });
    expandedResourceKeys.value = expandedResourceKeys.value.filter((key) =>
      selected.has(key),
    );
    precheckResult.value = null;
  },
  { deep: true },
);

watch(
  resourceConfigs,
  () => {
    precheckResult.value = null;
  },
  { deep: true },
);

watch(
  () => route.query.assetId,
  () => {
    void applyRouteAssetSelection();
  },
);

onMounted(load);
onBeforeUnmount(() => {
  stopPolling();
  store.clearInviteLinks();
});
</script>

<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h2 class="page-title">自建群编排</h2>
        <p class="page-desc">
          创建自有 Telegram 群草稿，预检查并提交成员编排操作。
        </p>
      </div>
      <el-button @click="load">刷新</el-button>
    </div>
    <el-alert
      v-if="errorMessage"
      type="error"
      show-icon
      :closable="false"
      :title="errorMessage"
    />
    <el-alert
      v-if="actionMessage"
      type="info"
      show-icon
      :closable="false"
      :title="actionMessage"
    />
    <div class="grid">
      <el-card class="draft-card">
        <template #header>创建草稿</template>
        <el-form label-width="100px">
          <el-form-item label="内部名称"
            ><el-input
              v-model="draft.internal_name"
              placeholder="例如 ops-group-2026-01"
          /></el-form-item>
          <el-form-item label="群标题"
            ><el-input v-model="draft.title"
          /></el-form-item>
          <el-form-item label="群简介"
            ><el-input v-model="draft.about" type="textarea" :rows="3"
          /></el-form-item>
          <el-form-item label="群主账号">
            <el-select
              v-model="draft.owner_account_id"
              filterable
              placeholder="选择已授权用户账号"
              style="width: 100%"
            >
              <el-option
                v-for="account in promoterOptions"
                :key="account.id"
                :label="ownedGroupOwnerOptionLabel(account)"
                :value="account.id"
                :disabled="Boolean(accountEligibilityReason(account))"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="可见性"
            ><el-radio-group v-model="draft.visibility"
              ><el-radio-button label="public">公开</el-radio-button
              ><el-radio-button label="private"
                >私有</el-radio-button
              ></el-radio-group
            ></el-form-item
          >
          <el-form-item v-if="draft.visibility === 'public'" label="公开用户名"
            ><el-input v-model="draft.telegram_username" placeholder="不含 @"
          /></el-form-item>
          <el-button type="primary" :loading="creating" @click="createDraft"
            >创建草稿</el-button
          >
        </el-form>
      </el-card>
      <el-card class="asset-card">
        <template #header>资产状态与操作</template>
        <el-empty v-if="!store.list.length" description="暂无自建群资产" />
        <el-table
          v-else
          :data="store.list"
          size="small"
          highlight-current-row
          @row-click="selectAsset"
        >
          <el-table-column prop="id" label="ID" width="65" /><el-table-column
            prop="title"
            label="群标题"
            min-width="130"
          /><el-table-column label="状态" width="130"
            ><template #default="{ row }"
              ><el-tag :type="statusType(row.status)">{{
                row.status
              }}</el-tag></template
            ></el-table-column
          ><el-table-column prop="visibility" label="可见性" width="80" />
          <el-table-column label="群运营" width="300" fixed="right">
            <template #default="{ row }">
              <el-button
                text
                type="primary"
                @click.stop="openOperationsCenter(asOwnedGroupAsset(row))"
                >运营中心</el-button
              >
              <el-tooltip
                :disabled="
                  !ownedGroupMessagingDisabledReason(asOwnedGroupAsset(row))
                "
                :content="
                  ownedGroupMessagingDisabledReason(asOwnedGroupAsset(row))
                "
              >
                <span>
                  <el-button
                    text
                    type="primary"
                    :disabled="
                      Boolean(
                        ownedGroupMessagingDisabledReason(
                          asOwnedGroupAsset(row),
                        ),
                      )
                    "
                    @click.stop="
                      openOwnedGroupMessaging(asOwnedGroupAsset(row))
                    "
                    >群内消息</el-button
                  >
                </span>
              </el-tooltip>
              <el-button
                v-if="canDissolveGroup(asOwnedGroupAsset(row))"
                text
                type="danger"
                :loading="dissolvingId === row.id"
                @click.stop="queueDissolution(asOwnedGroupAsset(row))"
              >
                解散群
              </el-button>
              <el-button
                v-if="canResolveDissolution(asOwnedGroupAsset(row))"
                text
                type="warning"
                :loading="resolvingDissolutionId === row.id"
                @click.stop="resolveDissolution(asOwnedGroupAsset(row))"
              >
                人工核验
              </el-button>
              <el-button
                v-if="canDeleteFailedDraft(asOwnedGroupAsset(row))"
                text
                type="danger"
                :loading="deletingDraftId === row.id"
                @click.stop="deleteFailedDraft(asOwnedGroupAsset(row))"
              >
                删除草稿
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="selectedAsset" class="operation-panel">
          <p>
            <strong>当前：</strong>{{ selectedAsset.title }}（#{{
              selectedAsset.id
            }}）
            <el-button
              type="primary"
              plain
              @click="openOperationsCenter(selectedAsset)"
              >运营中心</el-button
            >
          </p>
          <el-card shadow="never" class="governance-card">
            <template #header>
              <div class="governance-header">
                <div>
                  <strong>Guardian 治理</strong>
                  <el-tag
                    class="governance-status-tag"
                    :type="governanceStatusType(governanceStatus)"
                  >
                    {{ GOVERNANCE_STATUS_LABELS[governanceStatus] }}
                  </el-tag>
                </div>
                <el-button
                  text
                  :loading="governanceLoading"
                  @click="refreshGovernance()"
                >
                  刷新状态
                </el-button>
              </div>
            </template>

            <el-skeleton
              v-if="governanceLoading && !governance"
              animated
              :rows="4"
            />
            <template v-else>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="Guardian Bot">
                  {{ governanceBotLabel }}
                </el-descriptions-item>
                <el-descriptions-item label="用户名">
                  {{ governanceBotUsername }}
                </el-descriptions-item>
                <el-descriptions-item label="Account ID">
                  {{ governance?.guardian_bot_account_id ?? "-" }}
                </el-descriptions-item>
                <el-descriptions-item label="Bot 当前角色">
                  {{ governance?.bot_role || "-" }}
                </el-descriptions-item>
                <el-descriptions-item label="最近检测时间">
                  {{
                    formatGovernanceTime(governance?.governance_last_checked_at)
                  }}
                </el-descriptions-item>
                <el-descriptions-item label="绑定状态">
                  {{ governance?.binding_status || "-" }}
                </el-descriptions-item>
              </el-descriptions>

              <el-alert
                v-if="governance?.stale_pending"
                type="warning"
                show-icon
                :closable="false"
                title="治理操作已 pending 超过 2 分钟，请执行重新检测；不要更换 Bot 重复接入。"
              />

              <div class="governance-section">
                <strong>管理员权限</strong>
                <div class="governance-permissions">
                  <span class="governance-section-label">已授予</span>
                  <el-tag
                    v-for="permission in governanceGrantedPermissions"
                    :key="'granted:' + permission"
                    size="small"
                    type="success"
                    effect="plain"
                  >
                    {{ governancePermissionLabel(permission) }}
                  </el-tag>
                  <span v-if="!governanceGrantedPermissions.length">-</span>
                </div>
                <div class="governance-permissions">
                  <span class="governance-section-label">缺失</span>
                  <el-tag
                    v-for="permission in governanceMissingPermissions"
                    :key="'missing:' + permission"
                    size="small"
                    type="danger"
                    effect="plain"
                  >
                    {{ governancePermissionLabel(permission) }}
                  </el-tag>
                  <span v-if="!governanceMissingPermissions.length">无</span>
                </div>
              </div>

              <div class="governance-section">
                <strong>可用能力</strong>
                <div class="governance-capabilities">
                  <div
                    v-for="capability in governanceCapabilityRows"
                    :key="capability.key"
                    class="governance-capability"
                  >
                    <el-tag
                      :type="capability.enabled ? 'success' : 'info'"
                      effect="plain"
                      size="small"
                    >
                      {{ capability.label }} ·
                      {{ capability.enabled ? "可用" : "不可用" }}
                    </el-tag>
                  </div>
                </div>
              </div>

              <el-alert
                v-if="governanceFailure"
                type="error"
                show-icon
                :closable="false"
                :title="governanceFailure.message"
                :description="'Reason: ' + governanceFailure.reason"
              />

              <div class="governance-actions">
                <template v-if="!assetReadyForGovernance">
                  <el-button type="primary" disabled>接入治理</el-button>
                  <small class="poll-hint">{{
                    governanceDisabledReason
                  }}</small>
                </template>
                <template v-else-if="governanceStatus === 'disabled'">
                  <el-button
                    type="primary"
                    :disabled="!canOperateGovernance"
                    @click="openGovernanceDialog"
                  >
                    接入治理
                  </el-button>
                  <small v-if="governanceDisabledReason" class="poll-hint">
                    {{ governanceDisabledReason }}
                  </small>
                </template>
                <template v-else-if="governanceStatus === 'pending'">
                  <el-button type="primary" loading disabled>
                    治理处理中
                  </el-button>
                  <el-button
                    :loading="governanceLoading"
                    @click="refreshGovernance()"
                  >
                    刷新状态
                  </el-button>
                  <el-button
                    v-if="governance?.stale_pending"
                    :loading="governanceLoading"
                    :disabled="!canOperateGovernance"
                    @click="reconcileGovernance"
                  >
                    重新检测
                  </el-button>
                </template>
                <template v-else-if="governanceStatus === 'managed'">
                  <el-button
                    type="primary"
                    :disabled="!canOperateGovernance"
                    @click="openGovernancePolicies"
                  >
                    治理设置
                  </el-button>
                  <el-button
                    :loading="governanceLoading"
                    :disabled="!canOperateGovernance"
                    @click="reconcileGovernance"
                  >
                    重新检测
                  </el-button>
                </template>
                <template v-else>
                  <el-button type="danger" plain @click="viewGovernanceFailure">
                    查看失败原因
                  </el-button>
                  <el-button
                    :loading="governanceLoading"
                    :disabled="!canOperateGovernance"
                    @click="reconcileGovernance"
                  >
                    重新检测
                  </el-button>
                </template>
              </div>
            </template>
          </el-card>
          <div v-if="assetNeedsReconcile" class="asset-reconcile-panel">
            <el-alert
              type="warning"
              :closable="false"
              show-icon
              title="上一次建群结果无法确认。普通预检查不会盲目重建；请先核验 Telegram 中已存在的群。"
            />
            <template v-if="isAdmin">
              <el-input
                v-model="reconcileChatId"
                clearable
                placeholder="候选 Telegram chat ID；系统已保存时可留空"
              >
                <template #append>
                  <el-button :loading="reconcilingAsset" @click="reconcileAsset"
                    >恢复并对账</el-button
                  >
                </template>
              </el-input>
              <el-input
                v-if="selectedAsset.visibility === 'public'"
                v-model="reconcileUsername"
                clearable
                class="reconcile-username-input"
                placeholder="可选：替换失败的公开群用户名，如 owned_group_2"
              />
              <small class="poll-hint"
                >只验证已有群及群主权限，并补齐用户名/邀请链接；不会调用新建群接口。</small
              >
            </template>
            <small v-else class="poll-hint"
              >绑定候选 chat ID 需要管理员权限。</small
            >
          </div>
          <el-button
            :loading="prechecking"
            :disabled="selectedAsset.status === 'archived' || operationActive"
            @click="precheck"
            >资源预检查并排队</el-button
          >
          <el-form-item label="计划成员">
            <el-select
              v-model="selectedResources"
              multiple
              :multiple-limit="MAX_PLANNED_RESOURCES"
              filterable
              collapse-tags
              placeholder="选择用户账号或已验证 Bot"
              style="width: 100%"
            >
              <el-option-group label="用户账号">
                <el-option
                  v-for="account in resourceOptions"
                  :key="resourceKey('user', account.id)"
                  :label="accountOptionLabel(account)"
                  :value="resourceKey('user', account.id)"
                  :disabled="
                    isResourceOptionDisabled(
                      resourceKey('user', account.id),
                      Boolean(accountEligibilityReason(account)),
                    )
                  "
                />
              </el-option-group>
              <el-option-group
                v-if="botProfileOptions.length"
                label="已登记 Bot profile"
              >
                <el-option
                  v-for="profile in botProfileOptions"
                  :key="resourceKey('bot', profile.id)"
                  :label="botProfileLabel(profile)"
                  :value="resourceKey('bot', profile.id)"
                  :disabled="
                    isResourceOptionDisabled(
                      resourceKey('bot', profile.id),
                      profile.status !== 'verified' &&
                        profile.status !== 'active',
                    )
                  "
                />
              </el-option-group>
            </el-select>
          </el-form-item>
          <div
            class="planned-resource-count"
            :class="{ 'is-over-limit': plannedResourceLimitExceeded }"
            data-testid="planned-resource-count"
          >
            <strong>
              最终计划数（含群主） {{ finalPlannedResourceCount }}/{{
                MAX_PLANNED_RESOURCES
              }}
            </strong>
            <small v-if="implicitOwnerCount">
              群主未显式选择，系统会自动计入 1 个名额。
            </small>
          </div>
          <div
            v-if="selectedResourceEntries.length"
            class="resource-admin-list"
          >
            <div
              v-for="entry in selectedResourceEntries"
              :key="entry.key"
              class="resource-admin-item"
            >
              <div class="resource-admin-header">
                <span>{{ entry.label }}</span>
                <el-tag v-if="entry.is_owner" type="info" size="small"
                  >群主已自动计入</el-tag
                >
                <el-switch
                  v-else
                  :model-value="entry.config.admin_required"
                  active-text="设为管理员"
                  @change="setAdminRequired(entry.key, Boolean($event))"
                />
                <el-button
                  v-if="!entry.is_owner && entry.config.admin_required"
                  text
                  size="small"
                  @click="toggleResourceConfig(entry.key)"
                >
                  {{
                    isResourceConfigExpanded(entry.key)
                      ? "收起权限"
                      : "配置权限"
                  }}
                </el-button>
              </div>
              <template
                v-if="
                  !entry.is_owner &&
                  entry.config.admin_required &&
                  isResourceConfigExpanded(entry.key)
                "
              >
                <el-input
                  v-model="entry.config.admin_title"
                  maxlength="16"
                  show-word-limit
                  clearable
                  placeholder="可选：群内管理员头衔（最多 16 字符）"
                />
                <div class="permission-grid">
                  <el-checkbox
                    v-for="permission in ADMIN_PERMISSION_OPTIONS"
                    :key="permission.key"
                    :model-value="
                      Boolean(entry.config.admin_permissions[permission.key])
                    "
                    @change="
                      setAdminPermission(
                        entry.key,
                        permission.key,
                        Boolean($event),
                      )
                    "
                    >{{ permission.label }}</el-checkbox
                  >
                </div>
              </template>
            </div>
          </div>
          <el-alert
            v-if="!botProfileOptions.length"
            type="info"
            :closable="false"
            title="暂无可用的已登记 Bot；如需使用 Bot，请先在下方登记并完成验证。"
          />
          <el-alert
            v-if="precheckResult"
            :type="precheckResult.allowed ? 'success' : 'error'"
            :closable="false"
            show-icon
            :title="
              precheckResult.allowed
                ? '资源预检查通过'
                : `资源预检查未通过：${precheckResult.reason}`
            "
          >
            <template v-if="precheckViolations.length" #default>
              <div class="violation-list">
                <span
                  v-for="(violation, index) in precheckViolations"
                  :key="index"
                  class="violation-item"
                  >{{ violationLabel(violation) }}</span
                >
              </div>
            </template>
          </el-alert>
          <el-button
            type="primary"
            :loading="submitting"
            :disabled="selectedAsset.status !== 'ready' || operationActive"
            @click="submit"
            >提交成员操作</el-button
          >
          <el-alert
            v-if="selectedAsset.status !== 'ready'"
            type="warning"
            :closable="false"
            title="资产必须先通过预检查并达到 ready，才能提交成员操作。"
          />
          <div v-if="operation" class="operation-status">
            <div class="status-header">
              <strong>操作 #{{ operation.id }}</strong
              ><el-tag :type="statusType(operation.status)">{{
                operation.status
              }}</el-tag
              ><el-button text :loading="store.loading" @click="poll"
                >刷新</el-button
              >
            </div>
            <el-progress
              :percentage="
                operation.planned_count
                  ? Math.min(
                      100,
                      Math.round(
                        ((operation.completed_count +
                          operation.skipped_count +
                          operation.failed_count) /
                          operation.planned_count) *
                          100,
                      ),
                    )
                  : 0
              "
            />
            <p class="counts">
              已完成 {{ operation.completed_count }} · 跳过
              {{ operation.skipped_count }} · 失败
              {{ operation.failed_count }} / {{ operation.planned_count }}
            </p>
            <p v-if="operation.last_error" class="last-error">
              {{ operation.last_error }}
            </p>
            <el-button-group>
              <el-button
                v-if="operationCanPause"
                :loading="controlling"
                @click="control('pause')"
                >暂停</el-button
              >
              <el-button
                v-if="operationCanResume"
                :loading="controlling"
                @click="control('resume')"
                >恢复</el-button
              >
              <el-button
                v-if="operationCanStop"
                :loading="controlling"
                type="danger"
                plain
                @click="control('stop')"
                >停止</el-button
              >
              <el-button
                v-if="operationCanRetry"
                :loading="controlling"
                @click="control('retry')"
                >重试</el-button
              >
              <el-button
                v-if="operationNeedsReconcile"
                :loading="controlling"
                @click="control('reconcile')"
                >执行对账</el-button
              >
            </el-button-group>
            <small v-if="operationActive" class="poll-hint">
              {{
                isDissolutionOperation
                  ? "解散任务执行中，页面每 3 秒自动刷新；远端结果不确定时将转人工核验，不会自动重试。"
                  : "执行中，页面每 3 秒自动刷新；可随时暂停或停止。"
              }}
            </small>
            <div
              v-if="operationDissolutionNeedsReview"
              class="poll-hint"
              style="margin-top: 8px"
            >
              <el-alert
                type="warning"
                :closable="false"
                show-icon
                title="解散结果未确认：请在 Telegram 中核实该群状态后，点击人工确认提交结论。"
              />
              <el-button
                type="warning"
                size="small"
                :loading="resolvingDissolutionId === selectedAsset.id"
                @click="resolveDissolution(asOwnedGroupAsset(selectedAsset))"
                >人工确认解散结果</el-button
              >
            </div>
          </div>
          <div v-if="selectedAsset.status === 'ready'" class="invite-panel">
            <div class="invite-header">
              <strong>邀请链接</strong>
              <div>
                <el-button
                  text
                  :loading="inviteLinksLoading"
                  @click="loadInviteLinks(selectedAsset.id)"
                  >刷新</el-button
                >
                <el-button
                  v-if="isAdmin && selectedAsset.visibility === 'private'"
                  type="warning"
                  plain
                  size="small"
                  :loading="inviteLinkAction === 'regenerate'"
                  @click="regenerateInviteLink"
                  >重新生成</el-button
                >
              </div>
            </div>
            <el-alert
              type="warning"
              :closable="false"
              show-icon
              title="私有邀请链接属于 bearer 凭据；仅复制给授权对象，页面切换后会清除明文。撤销和重新生成仅管理员可执行。"
            />
            <el-alert
              v-if="inviteLinksError"
              type="error"
              :closable="false"
              :title="inviteLinksError"
            />
            <el-empty
              v-if="
                inviteLinksLoadedFor === selectedAsset.id &&
                !inviteLinksLoading &&
                !inviteLinks.length
              "
              description="暂无可用邀请链接"
            />
            <div
              v-for="link in inviteLinks"
              :key="`${link.link_type}:${link.id ?? 'public'}`"
              class="invite-row"
            >
              <div class="invite-row-meta">
                <el-tag size="small">{{ link.link_type }}</el-tag>
                <span>{{ link.status }}</span>
              </div>
              <el-input :model-value="link.link || '链接当前不可用'" readonly>
                <template #append>
                  <el-button
                    :disabled="!link.available || !link.link"
                    @click="copyInviteLink(link as OwnedGroupInviteLink)"
                    >复制</el-button
                  >
                </template>
              </el-input>
              <el-button
                v-if="isAdmin && link.id !== null && link.is_active"
                type="danger"
                plain
                size="small"
                :loading="inviteLinkAction === `revoke:${link.id}`"
                @click="revokeInviteLink(link as OwnedGroupInviteLink)"
                >撤销</el-button
              >
            </div>
          </div>
        </div>
      </el-card>
    </div>
    <el-card class="bot-card">
      <template #header>
        <div class="card-header">
          <span>自建 Bot profile</span>
          <el-button
            v-if="isAdmin"
            type="primary"
            size="small"
            @click="openBotRegistration"
            >登记 Bot</el-button
          >
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="Bot Token 只会通过 HTTPS 发送一次；页面、列表和审计响应均不会回显 Token。不能用 Guardian Bot profile ID 替代自建 Bot profile。"
      />
      <el-alert
        v-if="botProfileError"
        class="bot-error"
        type="warning"
        :closable="false"
        show-icon
        :title="botProfileError"
      />
      <el-empty
        v-if="!botProfiles.length && !store.botProfilesLoading"
        description="暂无自建 Bot profile"
      />
      <el-table
        v-else
        v-loading="store.botProfilesLoading"
        :data="botProfiles"
        size="small"
        border
      >
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="Bot" min-width="180"
          ><template #default="{ row }">{{
            row.display_name || row.bot_username || `Bot #${row.id}`
          }}</template></el-table-column
        >
        <el-table-column prop="bot_username" label="用户名" min-width="150" />
        <el-table-column prop="account_id" label="账号 ID" width="90" />
        <el-table-column label="状态" width="150"
          ><template #default="{ row }"
            ><el-tag :type="statusType(row.status)">{{
              row.status
            }}</el-tag></template
          ></el-table-column
        >
        <el-table-column label="启用" width="90"
          ><template #default="{ row }"
            ><el-switch
              :model-value="row.enabled"
              :disabled="!isAdmin"
              @change="
                toggleBotProfile(row as OwnedBotProfile, Boolean($event))
              " /></template
        ></el-table-column>
        <el-table-column label="操作" width="100"
          ><template #default="{ row }"
            ><el-button
              v-if="
                isAdmin && row.status !== 'verified' && row.status !== 'active'
              "
              text
              size="small"
              :loading="verifyingBotId === row.id"
              @click="verifyBotProfile(row as OwnedBotProfile)"
              >验证</el-button
            ></template
          ></el-table-column
        >
      </el-table>
    </el-card>

    <el-dialog
      v-model="governanceDialogVisible"
      title="接入 Guardian 治理"
      width="680px"
      :close-on-click-modal="false"
    >
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="本操作只执行实时身份与权限探针，不会自动拉 Bot 入群或授予管理员权限。"
      />
      <el-form label-width="120px" class="governance-bind-form">
        <el-form-item label="目标自建群">
          <el-input
            :model-value="
              selectedAsset
                ? selectedAsset.title + ' (#' + selectedAsset.id + ')'
                : '-'
            "
            disabled
          />
        </el-form-item>
        <el-form-item label="Guardian Bot" required>
          <el-select
            v-model="selectedGuardianBotAccountId"
            filterable
            :loading="governanceCandidatesLoading"
            placeholder="选择符合归属、Profile、健康与风险条件的 Bot"
            style="width: 100%"
          >
            <el-option
              v-for="candidate in governanceCandidates"
              :key="candidate.guardian_bot_account_id"
              :label="governanceCandidateLabel(candidate)"
              :value="candidate.guardian_bot_account_id"
            >
              <div class="governance-candidate-option">
                <strong>{{
                  candidate.display_name || candidate.username || "Guardian Bot"
                }}</strong>
                <span>{{
                  candidate.username
                    ? candidate.username.startsWith("@")
                      ? candidate.username
                      : "@" + candidate.username
                    : "无用户名"
                }}</span>
                <span>account #{{ candidate.guardian_bot_account_id }}</span>
                <el-tag size="small" effect="plain">
                  Owned: {{ candidate.owned_profile_status }}
                </el-tag>
                <el-tag
                  size="small"
                  effect="plain"
                  :type="
                    candidate.guardian_health_status === 'healthy'
                      ? 'success'
                      : 'warning'
                  "
                >
                  Guardian: {{ candidate.guardian_health_status }}
                </el-tag>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
      </el-form>
      <el-alert
        v-if="
          selectedGovernanceCandidate &&
          !selectedGovernanceCandidate.local_is_admin
        "
        type="warning"
        :closable="false"
        show-icon
        :title="
          '本地成员记录尚未核验为管理员（' +
          (selectedGovernanceCandidate.local_membership_status || 'unknown') +
          '）；仍可发起实时权限探针。'
        "
      />
      <el-alert
        v-if="!governanceCandidatesLoading && !governanceCandidates.length"
        type="info"
        :closable="false"
        title="没有符合当前群主归属及双 Profile 条件的 Guardian Bot。"
      />
      <el-alert
        v-if="governanceActionError"
        type="error"
        :closable="false"
        show-icon
        :title="governanceActionError.message"
        :description="
          governanceActionError.missing_permissions?.length
            ? '缺失权限：' +
              governanceActionError.missing_permissions
                .map(governancePermissionLabel)
                .join('、')
            : 'Reason: ' + governanceActionError.reason
        "
      />
      <template #footer>
        <el-button @click="governanceDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="governanceLoading"
          :disabled="
            !selectedGuardianBotAccountId || governanceCandidatesLoading
          "
          @click="bindGovernance"
        >
          确认接入
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="botRegistrationVisible"
      title="登记自建 Bot profile"
      width="520px"
      :close-on-click-modal="false"
      @closed="botForm.bot_token = ''"
    >
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="请先在 BotFather 创建 Bot。Token 仅用于本次登记和服务端加密托管，提交后无法再次查看。"
      />
      <el-form label-width="110px" class="bot-form">
        <el-form-item label="归属用户" required>
          <el-select
            v-model="botForm.owner_account_id"
            filterable
            placeholder="选择推广用户账号"
            style="width: 100%"
          >
            <el-option
              v-for="account in promoterOptions"
              :key="account.id"
              :label="accountOptionLabel(account)"
              :value="account.id"
              :disabled="Boolean(accountEligibilityReason(account))"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="Bot 账号" required>
          <el-select
            v-model="botForm.account_id"
            filterable
            placeholder="选择已登记的 guardian_bot 账号"
            style="width: 100%"
          >
            <el-option
              v-for="account in botAccounts.filter((item) => item.is_active)"
              :key="account.id"
              :label="accountOptionLabel(account)"
              :value="account.id"
            />
          </el-select>
          <small v-if="!botAccounts.length" class="form-hint"
            >暂无可用 Bot 账号，请先在“Bot账号”页面创建账号。</small
          >
        </el-form-item>
        <el-form-item label="BotFather Token">
          <el-input
            v-model="botForm.bot_token"
            type="password"
            autocomplete="new-password"
            placeholder="可留空：若该账号已有服务端托管 Token 将自动复用"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="botRegistrationVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="registeringBot"
          :disabled="!botAccounts.length"
          @click="registerBotProfile"
          >安全登记</el-button
        >
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.page-shell {
  display: grid;
  gap: 16px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}
.page-title {
  margin: 0;
  font-size: 20px;
}
.page-desc {
  margin: 6px 0 0;
  color: #606266;
}
.grid {
  display: grid;
  grid-template-columns: minmax(360px, 1fr) minmax(440px, 1.4fr);
  gap: 16px;
}
.operation-panel {
  margin-top: 16px;
  display: grid;
  gap: 12px;
}
.operation-status {
  display: grid;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.asset-reconcile-panel,
.resource-admin-list,
.invite-panel {
  display: grid;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.reconcile-username-input {
  margin-top: 2px;
}
.resource-admin-item {
  display: grid;
  gap: 10px;
  padding: 10px;
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
}
.planned-resource-count {
  display: grid;
  gap: 3px;
  margin: -8px 0 12px 110px;
  color: var(--el-text-color-secondary);
}
.planned-resource-count.is-over-limit {
  color: var(--el-color-danger);
}
.resource-admin-header,
.invite-header,
.invite-row-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.permission-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px 12px;
}
.invite-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: end;
}
.invite-row-meta {
  grid-column: 1 / -1;
  justify-content: flex-start;
  color: var(--el-text-color-secondary);
}
.status-header {
  display: flex;
  align-items: center;
  gap: 10px;
}
.status-header .el-button {
  margin-left: auto;
}
.counts,
.poll-hint,
.form-hint {
  color: var(--el-text-color-secondary);
  margin: 0;
}
.last-error {
  color: var(--el-color-danger);
  margin: 0;
  white-space: pre-wrap;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.bot-card {
  overflow: hidden;
}
.bot-error {
  margin-top: 10px;
}
.bot-form {
  margin-top: 16px;
}
.violation-list {
  display: grid;
  gap: 4px;
}
.violation-item {
  color: var(--el-color-danger);
}
.governance-card :deep(.el-card__body) {
  display: grid;
  gap: 12px;
}
.governance-header,
.governance-actions,
.governance-permissions,
.governance-candidate-option {
  display: flex;
  align-items: center;
  gap: 8px;
}
.governance-header {
  justify-content: space-between;
}
.governance-status-tag {
  margin-left: 8px;
}
.governance-section {
  display: grid;
  gap: 8px;
}
.governance-permissions {
  flex-wrap: wrap;
}
.governance-section-label {
  min-width: 46px;
  color: var(--el-text-color-secondary);
}
.governance-capabilities {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
}
.governance-actions {
  flex-wrap: wrap;
}
.governance-bind-form {
  margin-top: 16px;
}
.governance-candidate-option {
  width: 100%;
  white-space: nowrap;
}
.governance-candidate-option > span:not(.el-tag) {
  color: var(--el-text-color-secondary);
}
@media (max-width: 900px) {
  .grid {
    grid-template-columns: 1fr;
  }
  .permission-grid,
  .invite-row {
    grid-template-columns: 1fr;
  }
  .planned-resource-count {
    margin-left: 0;
  }
  .governance-capabilities {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
