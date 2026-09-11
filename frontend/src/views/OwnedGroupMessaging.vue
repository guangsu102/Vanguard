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
import { acquisitionApi, type KeywordTrigger } from "@/api/acquisition";
import {
  getOwnedGroupMessagingError,
  type OwnedGroupMessageCategory,
  type OwnedGroupMessageExecutionFilters,
  type OwnedGroupMessageExecutionStatus,
  type OwnedGroupMessageExecutionSummary,
  type OwnedGroupMessageManualExecutionInput,
  type OwnedGroupMessageMode,
  type OwnedGroupMessagePolicy,
  type OwnedGroupMessagePolicyCreateInput,
  type OwnedGroupMessagePreviewInput,
  type OwnedGroupMessageTemplate,
  type OwnedGroupMessageTemplateInput,
  type OwnedGroupMessageTriggerConfig,
  type OwnedGroupTemplateMessageType,
} from "@/api/ownedGroupMessaging";
import { useAuthStore } from "@/stores/auth";
import { useOwnedGroupMessagingStore } from "@/stores/ownedGroupMessaging";

const route = useRoute();
const router = useRouter();
const authStore = useAuthStore();
const store = useOwnedGroupMessagingStore();

const activeTab = ref("policies");
const pageError = ref("");
const keywordTriggers = ref<KeywordTrigger[]>([]);
const now = ref(dayjs());
let clockTimer: ReturnType<typeof setInterval> | undefined;

const STATUS_LABELS: Record<OwnedGroupMessageExecutionStatus, string> = {
  queued: "排队中",
  generating: "生成中",
  pending_review: "待审核",
  ready_to_send: "待发送",
  sending: "发送中",
  sent: "已发送",
  skipped: "已跳过",
  failed: "失败",
  rejected: "已拒绝",
  expired: "已过期",
  cancelled: "已取消",
};

const ERROR_LABELS: Record<string, string> = {
  POLICY_DAILY_LIMIT_REACHED: "策略今日额度已用完",
  POLICY_DAILY_LIMIT_ZERO: "策略每日额度为 0",
  GROUP_COOLDOWN_ACTIVE: "群级冷却中",
  DRY_RUN_ENABLED: "当前为演练模式，未发送",
  DUPLICATE_CONTENT: "同群去重窗口内已有相同内容",
  TARGET_MAPPING_INVALID: "群双 ID 或治理绑定不一致",
  GOVERNANCE_NOT_MANAGED: "Guardian 治理未就绪",
  ACCOUNT_NOT_ELIGIBLE: "推广账号当前不可用",
  ACCOUNT_MODE_NOT_ALLOWED: "仅允许 growth 推广账号",
  CONTENT_SAFETY_BLOCKED: "内容安全检查未通过",
  OWNED_GROUP_PROMOTION_DISABLED: "群内广告模式已关闭",
  TELEGRAM_SEND_OUTCOME_UNKNOWN: "Telegram 发送结果未知，未自动重试",
};

const CATEGORY_LABELS: Record<OwnedGroupMessageCategory, string> = {
  community: "普通消息",
  promotion: "群内广告",
};

const TRIGGER_LABELS = {
  scheduled: "定时",
  keyword: "关键词",
  reply: "回复",
  manual: "手动",
} as const;

const TEMPLATE_TYPE_LABELS: Record<OwnedGroupTemplateMessageType, string> = {
  interaction: "互动",
  qa: "问答",
  share: "分享",
  guide: "引导",
};

const WEEKDAY_OPTIONS = [
  { label: "周一", value: 1 },
  { label: "周二", value: 2 },
  { label: "周三", value: 3 },
  { label: "周四", value: 4 },
  { label: "周五", value: 5 },
  { label: "周六", value: 6 },
  { label: "周日", value: 7 },
];

const TEMPLATE_VARIABLES = new Set([
  "group_name",
  "account_name",
  "user_name",
  "current_date",
  "current_time",
  "promotion_url",
  "promotion_cta",
]);

const assetId = computed(() => {
  const raw = Array.isArray(route.params.assetId)
    ? route.params.assetId[0]
    : route.params.assetId;
  const parsed = Number(raw);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
});

const isAdmin = computed(() => authStore.userInfo?.role === "admin");
const summary = computed(() => store.summary);
const runtimeStatus = computed(() => summary.value?.messaging_status ?? {});
const runtimeLimits = computed(() => summary.value?.runtime_limits ?? {});
const summaryStats = computed(() => summary.value?.stats ?? {});
const assetTitle = computed(
  () =>
    store.asset?.title ||
    summary.value?.title ||
    `自建群 #${assetId.value ?? "-"}`,
);
const assetStatus = computed(
  () => store.asset?.status || summary.value?.status || "unknown",
);
const governanceStatus = computed(
  () =>
    summary.value?.governance_status ||
    store.asset?.governance_status ||
    "disabled",
);
const coreGroupId = computed(
  () => summary.value?.core_group_id ?? store.asset?.core_group_id ?? null,
);
const telegramChatId = computed(
  () =>
    summary.value?.telegram_chat_id ?? store.asset?.telegram_chat_id ?? null,
);
const staticEnabled = computed(
  () => runtimeStatus.value.static_enabled === true,
);
const runtimeEnabled = computed(
  () =>
    (runtimeStatus.value.runtime_enabled ?? runtimeStatus.value.enabled) ===
    true,
);
const dryRun = computed(() => runtimeStatus.value.dry_run !== false);
const groupSentToday = computed(
  () =>
    summaryStats.value.group_sent_today ??
    summaryStats.value.sent_today ??
    store.policies[0]?.group_sent_today ??
    0,
);
const globalGroupDailyLimit = computed(
  () =>
    runtimeLimits.value.global_group_daily_limit ??
    runtimeLimits.value.global_max_per_group_per_day ??
    null,
);
const communitySentToday = computed(
  () =>
    summaryStats.value.community_sent_today ??
    store.policies.reduce(
      (total, policy) => total + (policy.community_sent_today ?? 0),
      0,
    ),
);
const promotionSentToday = computed(
  () =>
    summaryStats.value.promotion_sent_today ??
    store.policies.reduce(
      (total, policy) => total + (policy.promotion_sent_today ?? 0),
      0,
    ),
);
const lastSentAt = computed(() => {
  if (summaryStats.value.last_sent_at) return summaryStats.value.last_sent_at;
  return store.policies.reduce<string | null>((latest, policy) => {
    if (!policy.last_sent_at) return latest;
    if (!latest || dayjs(policy.last_sent_at).isAfter(dayjs(latest))) {
      return policy.last_sent_at;
    }
    return latest;
  }, null);
});

const sendBlockers = computed(() => {
  const reasons: string[] = [];
  if (assetStatus.value === "archived") reasons.push("资产已归档");
  else if (assetStatus.value !== "ready")
    reasons.push(`资产状态为 ${assetStatus.value}，必须达到 ready`);
  if (!Number.isSafeInteger(coreGroupId.value) || !coreGroupId.value)
    reasons.push("核心群映射未就绪");
  if (!Number.isSafeInteger(telegramChatId.value) || !telegramChatId.value)
    reasons.push("Telegram Chat ID 未就绪");
  if (governanceStatus.value !== "managed")
    reasons.push("Guardian 治理未就绪，当前只能查看、预配置和预览");
  if (!staticEnabled.value) reasons.push("静态消息开关已关闭");
  if (!runtimeEnabled.value) reasons.push("运行消息开关已关闭");
  if (dryRun.value) reasons.push("当前为 dry-run，实际发送会被阻断");
  if (runtimeStatus.value.can_send === false)
    reasons.push("服务端当前判定不可发送");
  return reasons;
});

const canEnablePolicy = computed(
  () =>
    assetStatus.value === "ready" &&
    Boolean(coreGroupId.value) &&
    Boolean(telegramChatId.value) &&
    governanceStatus.value === "managed" &&
    staticEnabled.value &&
    runtimeEnabled.value,
);

const formatTime = (value?: string | null) =>
  value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "-";

const statusLabel = (status: OwnedGroupMessageExecutionStatus | string) =>
  STATUS_LABELS[status as OwnedGroupMessageExecutionStatus] || status;

const statusType = (status: OwnedGroupMessageExecutionStatus | string) => {
  if (status === "sent") return "success";
  if (["failed", "rejected"].includes(status)) return "danger";
  if (["pending_review", "ready_to_send", "sending"].includes(status))
    return "warning";
  if (["skipped", "expired", "cancelled"].includes(status)) return "info";
  return "primary";
};

const errorLabel = (code?: string | null) =>
  code ? ERROR_LABELS[code] || code : "-";

const categoryLabel = (category: OwnedGroupMessageCategory) =>
  CATEGORY_LABELS[category];

const modeLabel = (mode: OwnedGroupMessageMode) =>
  mode === "ai" ? "AI" : mode === "template" ? "模板" : "关闭";

const policyPersonaName = (policy: OwnedGroupMessagePolicy) => {
  if (!policy.persona) return "未记录";
  if (policy.persona.name) return policy.persona.name;
  return policy.persona.configured ? "配置异常" : "中性默认";
};

const policyPersonaMeta = (policy: OwnedGroupMessagePolicy) => {
  if (!policy.persona) return "历史策略 · revision 未记录";
  const source = policy.persona.configured ? "账号配置" : "中性默认";
  return `${source} · v${policy.persona.revision}`;
};

const policyPersonaStatus = (policy: OwnedGroupMessagePolicy) => {
  if (!policy.persona) return { label: "未记录", type: "info" as const };
  if (!policy.persona.applicable) {
    return { label: "当前不适用", type: "info" as const };
  }
  if (policy.mode !== "ai" && policy.promotion_config.mode !== "ai") {
    return { label: "策略未使用", type: "info" as const };
  }
  if (!policy.persona.effective_enabled) {
    return { label: "功能关闭", type: "warning" as const };
  }
  return { label: "已应用", type: "success" as const };
};

const EXECUTION_PERSONA_SOURCE_LABELS = {
  configured: "账号配置",
  neutral_default: "中性默认",
  feature_disabled_default: "功能关闭默认",
  legacy_default: "历史默认",
  legacy_untracked: "历史未记录",
} as const;

const executionPersonaName = (execution: OwnedGroupMessageExecutionSummary) =>
  execution.mode_snapshot !== "ai"
    ? "不适用"
    : execution.persona?.name ||
      (execution.persona?.source === "neutral_default" ? "中性默认" : "未记录");

const executionPersonaMeta = (execution: OwnedGroupMessageExecutionSummary) => {
  if (execution.mode_snapshot !== "ai") return "模板执行 · Persona 未使用";
  const persona = execution.persona;
  if (!persona) return "历史未记录 · revision 未记录";
  const source = EXECUTION_PERSONA_SOURCE_LABELS[persona.source];
  const revision = persona.revision === null ? "revision 未记录" : `v${persona.revision}`;
  return `${source} · ${revision}`;
};

const executionPersonaStatus = (execution: OwnedGroupMessageExecutionSummary) => {
  if (execution.mode_snapshot !== "ai") {
    return { label: "不适用", type: "info" as const };
  }
  const source = execution.persona?.source;
  if (source === "configured" || source === "neutral_default") {
    return { label: "已应用", type: "success" as const };
  }
  if (source === "feature_disabled_default") {
    return { label: "功能关闭", type: "warning" as const };
  }
  return { label: "历史兼容", type: "info" as const };
};

const goToAccountPersona = (accountId: number) => {
  if (!isAdmin.value) return;
  router.push({
    name: "Accounts",
    query: { tab: "list", persona_account_id: String(accountId) },
  });
};

const triggerSummary = (policy: OwnedGroupMessagePolicy) => {
  const config = policy.trigger_config;
  return (Object.keys(TRIGGER_LABELS) as Array<keyof typeof TRIGGER_LABELS>)
    .filter((key) => config[key].enabled)
    .map((key) => {
      if (key === "manual") return TRIGGER_LABELS[key];
      return `${TRIGGER_LABELS[key]}·${categoryLabel(config[key].content_category)}`;
    });
};

const asPolicy = (value: unknown) => value as OwnedGroupMessagePolicy;
const asExecution = (value: unknown) =>
  value as OwnedGroupMessageExecutionSummary;
const asTemplate = (value: unknown) => value as OwnedGroupMessageTemplate;
const triggerLabel = (value: string) =>
  TRIGGER_LABELS[value as keyof typeof TRIGGER_LABELS] || value;
const templateTypeLabel = (value: string) =>
  TEMPLATE_TYPE_LABELS[value as OwnedGroupTemplateMessageType] || value;
const templateVariableToken = (value: string) => `{{${value}}}`;

const showError = (error: unknown, fallback: string) => {
  const failure = getOwnedGroupMessagingError(error, fallback);
  pageError.value = failure.message;
  if (failure.code === "EXECUTION_REVISION_CONFLICT") {
    ElMessage.warning("内容已被其他管理员处理，已刷新最新状态");
  } else if (failure.code === "POLICY_REVISION_CONFLICT") {
    ElMessage.warning("策略已被其他管理员修改，正在刷新最新配置");
  } else if (failure.status === 429) {
    const details = failure.details;
    const suffix = details.cooldown_until
      ? `，冷却至 ${formatTime(String(details.cooldown_until))}`
      : details.remaining_today !== undefined
        ? `，剩余额度 ${details.remaining_today}`
        : details.sent_today !== undefined && details.daily_limit !== undefined
          ? `，已发送 ${details.sent_today}/${details.daily_limit}`
          : "";
    ElMessage.warning(failure.message + suffix);
  } else {
    ElMessage.error(failure.message);
  }
  return failure;
};

const loadKeywordTriggers = async () => {
  try {
    const response = await acquisitionApi.getKeywordTriggers({
      enabled: true,
      page: 1,
      page_size: 100,
    });
    const payload = response.data as unknown as Record<string, unknown>;
    const rows = Array.isArray(payload)
      ? payload
      : Array.isArray(payload.data)
        ? payload.data
        : payload.data && typeof payload.data === "object"
          ? (((payload.data as Record<string, unknown>).data as
              unknown[] | undefined) ?? [])
          : [];
    keywordTriggers.value = (
      Array.isArray(rows) ? rows : []
    ) as KeywordTrigger[];
  } catch {
    keywordTriggers.value = [];
  }
};

const loadWorkspace = async () => {
  pageError.value = "";
  if (!assetId.value) {
    pageError.value = "无效的自建群资产 ID";
    return;
  }
  try {
    await Promise.all([
      store.loadWorkspace(assetId.value),
      loadKeywordTriggers(),
    ]);
  } catch (error) {
    showError(error, "群内消息工作区加载失败");
  }
};

const goBack = () => {
  router.push({
    path: "/owned-groups",
    query: assetId.value ? { assetId: String(assetId.value) } : {},
  });
};

const goToOperationsCenter = () => {
  if (assetId.value) router.push(`/owned-groups/${assetId.value}/operations?tab=overview`);
};

// Policy drawer
const policyDrawerVisible = ref(false);
const editingPolicyId = ref<number | null>(null);
const policySnapshot = ref("");

const defaultTriggerConfig = (): OwnedGroupMessageTriggerConfig => ({
  version: 1,
  scheduled: {
    enabled: false,
    timezone: "Asia/Shanghai",
    weekdays: [1, 2, 3, 4, 5, 6, 7],
    times: [],
    jitter_seconds: 0,
    content_category: "community",
  },
  keyword: {
    enabled: false,
    trigger_ids: [],
    reply_to_source: true,
    content_category: "community",
  },
  reply: {
    enabled: false,
    strategy: "directed",
    semantic_min_confidence: 0.75,
    context_messages: 6,
    content_category: "community",
  },
  manual: {
    enabled: true,
    allowed_content_categories: ["community", "promotion"],
  },
  dedupe_window_seconds: 21600,
});

const policyForm = reactive<
  OwnedGroupMessagePolicyCreateInput & { revision: number }
>({
  account_id: 0,
  mode: "off",
  default_template_id: null,
  trigger_config: defaultTriggerConfig(),
  promotion_config: {
    mode: "off",
    default_template_id: null,
    destination_url: null,
    cta_text: null,
  },
  daily_limit: 5,
  cooldown_seconds: 3600,
  allowed_topics: [],
  require_review: true,
  enabled: false,
  revision: 1,
});

const topicInput = ref("");

const resetPolicyForm = () => {
  Object.assign(policyForm, {
    account_id: 0,
    mode: "off",
    default_template_id: null,
    trigger_config: defaultTriggerConfig(),
    promotion_config: {
      mode: "off",
      default_template_id: null,
      destination_url: null,
      cta_text: null,
    },
    daily_limit: 5,
    cooldown_seconds: 3600,
    allowed_topics: [],
    require_review: true,
    enabled: false,
    revision: 1,
  });
  topicInput.value = "";
};

const clonePolicyIntoForm = (policy: OwnedGroupMessagePolicy) => {
  const cloned = JSON.parse(JSON.stringify(policy)) as OwnedGroupMessagePolicy;
  Object.assign(policyForm, {
    account_id: cloned.account_id,
    mode: cloned.mode,
    default_template_id: cloned.default_template_id,
    trigger_config: cloned.trigger_config,
    promotion_config: cloned.promotion_config,
    daily_limit: cloned.daily_limit,
    cooldown_seconds: cloned.cooldown_seconds,
    allowed_topics: cloned.allowed_topics,
    require_review: cloned.require_review,
    enabled: cloned.enabled,
    revision: cloned.revision,
  });
  topicInput.value = cloned.allowed_topics.join("\n");
};

const snapshotPolicy = () => {
  policySnapshot.value = JSON.stringify({
    ...policyForm,
    allowed_topics: topicInput.value,
  });
};

const policyDirty = computed(
  () =>
    policyDrawerVisible.value &&
    policySnapshot.value !==
      JSON.stringify({ ...policyForm, allowed_topics: topicInput.value }),
);

const enabledCommunityTemplates = computed(() =>
  store.templates.filter(
    (item) => item.enabled && item.content_category === "community",
  ),
);
const enabledPromotionTemplates = computed(() =>
  store.templates.filter(
    (item) => item.enabled && item.content_category === "promotion",
  ),
);
const communityTemplateOptions = computed(() =>
  store.templates.filter(
    (item) =>
      item.content_category === "community" &&
      (item.enabled || item.id === policyForm.default_template_id),
  ),
);
const promotionTemplateOptions = computed(() =>
  store.templates.filter(
    (item) =>
      item.content_category === "promotion" &&
      (item.enabled ||
        item.id === policyForm.promotion_config.default_template_id),
  ),
);

const accountLabel = (accountId: number) =>
  store.eligibleAccounts.find((item) => item.account_id === accountId)
    ?.display_name || `账号 #${accountId}`;

const accountEligibility = (accountId: number) =>
  store.eligibleAccounts.find((item) => item.account_id === accountId);

const openCreatePolicy = () => {
  if (!isAdmin.value) return;
  editingPolicyId.value = null;
  resetPolicyForm();
  policyDrawerVisible.value = true;
  snapshotPolicy();
};

const openEditPolicy = (policy: OwnedGroupMessagePolicy) => {
  editingPolicyId.value = policy.id;
  clonePolicyIntoForm(policy);
  policyDrawerVisible.value = true;
  snapshotPolicy();
};

const closePolicyDrawer = async (done?: () => void) => {
  if (policyDirty.value) {
    try {
      await ElMessageBox.confirm("存在未保存修改，确认关闭？", "未保存修改", {
        type: "warning",
      });
    } catch {
      return;
    }
  }
  policyDrawerVisible.value = false;
  done?.();
};

const normalizeTopics = (raw: string) => {
  const seen = new Set<string>();
  const result: string[] = [];
  raw
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((item) => {
      const key = item.toLocaleLowerCase();
      if (!seen.has(key)) {
        seen.add(key);
        result.push(item);
      }
    });
  return result;
};

const selectedModeForCategory = (category: OwnedGroupMessageCategory) =>
  category === "community" ? policyForm.mode : policyForm.promotion_config.mode;

const expectedKeywordAction = (category: OwnedGroupMessageCategory) =>
  selectedModeForCategory(category) === "template"
    ? "reply_template"
    : "reply_ai";

const policyValidationError = (): string | null => {
  const topics = normalizeTopics(topicInput.value);
  if (
    !Number.isSafeInteger(policyForm.account_id) ||
    policyForm.account_id <= 0
  )
    return "请选择推广账号";
  if (policyForm.daily_limit < 0 || policyForm.daily_limit > 100)
    return "每日上限必须在 0–100 之间";
  if (policyForm.cooldown_seconds < 60 || policyForm.cooldown_seconds > 86400)
    return "群级冷却必须在 60–86400 秒之间";
  if (topics.length > 20 || topics.some((item) => item.length > 50))
    return "允许主题最多 20 项，每项 1–50 字";
  const config = policyForm.trigger_config;
  if (config.version !== 1 || config.scheduled.timezone !== "Asia/Shanghai")
    return "当前仅支持 trigger_config v1 和 Asia/Shanghai";
  const weekdays = [...new Set(config.scheduled.weekdays)];
  if (weekdays.some((item) => item < 1 || item > 7))
    return "星期范围必须为 1–7";
  const times = [...new Set(config.scheduled.times.map((item) => item.trim()))];
  if (
    times.length > 12 ||
    times.some((item) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(item))
  )
    return "时间点必须为 HH:MM，且最多 12 个";
  if (config.scheduled.enabled && times.length === 0)
    return "开启定时触发后至少配置一个时间点";
  if (
    config.scheduled.jitter_seconds < 0 ||
    config.scheduled.jitter_seconds > 900
  )
    return "随机延迟必须在 0–900 秒之间";
  if (config.keyword.trigger_ids.length > 50)
    return "关键词触发器最多选择 50 个";
  if (
    config.reply.semantic_min_confidence < 0.5 ||
    config.reply.semantic_min_confidence > 1
  )
    return "语义置信度必须在 0.50–1.00 之间";
  if (config.reply.context_messages < 1 || config.reply.context_messages > 20)
    return "上下文消息数必须在 1–20 之间";
  if (
    config.dedupe_window_seconds < 600 ||
    config.dedupe_window_seconds > 86400
  )
    return "内容去重窗口必须在 600–86400 秒之间";
  if (config.manual.allowed_content_categories.length === 0)
    return "手动触发至少允许一种内容类别";
  for (const key of ["scheduled", "keyword", "reply"] as const) {
    const trigger = config[key];
    if (
      trigger.enabled &&
      trigger.content_category === "community" &&
      policyForm.mode === "off"
    )
      return `${TRIGGER_LABELS[key]}选择普通消息时，普通消息模式不能关闭`;
    if (
      trigger.enabled &&
      trigger.content_category === "promotion" &&
      policyForm.promotion_config.mode === "off"
    )
      return `${TRIGGER_LABELS[key]}选择群内广告时，群内广告模式不能关闭`;
  }
  if (config.reply.enabled && config.reply.strategy === "semantic") {
    if (selectedModeForCategory(config.reply.content_category) !== "ai")
      return "语义回复仅支持对应类别的 AI 模式";
  }
  if (config.keyword.enabled) {
    const expected = expectedKeywordAction(config.keyword.content_category);
    const invalid = config.keyword.trigger_ids.some(
      (id) =>
        keywordTriggers.value.find((item) => item.id === id)?.action !==
        expected,
    );
    if (invalid)
      return `关键词动作必须与当前模式匹配（${expected === "reply_ai" ? "AI 回复" : "模板回复"}）`;
  }
  const aiAutoEnabled =
    (config.scheduled.enabled &&
      selectedModeForCategory(config.scheduled.content_category) === "ai") ||
    (config.reply.enabled &&
      config.reply.strategy === "semantic" &&
      selectedModeForCategory(config.reply.content_category) === "ai");
  if (aiAutoEnabled && topics.length === 0)
    return "AI 定时或语义回复至少需要一个允许主题";
  if (policyForm.mode === "template") {
    if (!policyForm.default_template_id) return "请选择普通消息默认模板";
    if (
      !enabledCommunityTemplates.value.some(
        (item) => item.id === policyForm.default_template_id,
      )
    )
      return "普通消息模板必须为当前资产已启用的 interaction/qa 模板";
  }
  if (policyForm.promotion_config.mode === "template") {
    if (!policyForm.promotion_config.default_template_id)
      return "请选择群内广告默认模板";
    if (
      !enabledPromotionTemplates.value.some(
        (item) => item.id === policyForm.promotion_config.default_template_id,
      )
    )
      return "群内广告模板必须为当前资产已启用的 share/guide 模板";
  }
  if (config.scheduled.enabled) {
    const scheduledTemplateId =
      config.scheduled.content_category === "community"
        ? policyForm.default_template_id
        : policyForm.promotion_config.default_template_id;
    const scheduledTemplate = store.templates.find(
      (item) => item.id === scheduledTemplateId,
    );
    if (
      selectedModeForCategory(config.scheduled.content_category) ===
        "template" &&
      scheduledTemplate?.template_variables.includes("user_name")
    ) {
      return "定时触发没有 user_name，所选模板不能使用该变量";
    }
  }
  const destination = policyForm.promotion_config.destination_url?.trim() || "";
  if (destination) {
    if (destination.length > 512)
      return "群内广告链接必须为不超过 512 字的 HTTPS 地址";
    try {
      const parsed = new URL(destination);
      if (
        parsed.protocol !== "https:" ||
        !parsed.hostname ||
        parsed.username ||
        parsed.password
      )
        return "群内广告链接必须为不含账号密码的 HTTPS 地址";
    } catch {
      return "群内广告链接必须为有效的 HTTPS 地址";
    }
  }
  const cta = policyForm.promotion_config.cta_text?.trim() || "";
  if (cta.length > 100 || /https?:\/\//i.test(cta))
    return "CTA 最多 100 字且不能包含另一个 URL";
  if (policyForm.enabled && !canEnablePolicy.value)
    return sendBlockers.value[0] || "当前不能启用策略";
  return null;
};

const normalizedPolicyInput = (): OwnedGroupMessagePolicyCreateInput => {
  const triggerConfig = JSON.parse(
    JSON.stringify(policyForm.trigger_config),
  ) as OwnedGroupMessageTriggerConfig;
  triggerConfig.scheduled.weekdays = [
    ...new Set(triggerConfig.scheduled.weekdays),
  ].sort((a, b) => a - b);
  triggerConfig.scheduled.times = [
    ...new Set(triggerConfig.scheduled.times.map((item) => item.trim())),
  ].sort();
  triggerConfig.keyword.trigger_ids = [
    ...new Set(triggerConfig.keyword.trigger_ids),
  ].sort((a, b) => a - b);
  triggerConfig.manual.allowed_content_categories = [
    ...new Set(triggerConfig.manual.allowed_content_categories),
  ];
  return {
    account_id: policyForm.account_id,
    mode: policyForm.mode,
    default_template_id:
      policyForm.mode === "template" ? policyForm.default_template_id : null,
    trigger_config: triggerConfig,
    promotion_config: {
      mode: policyForm.promotion_config.mode,
      default_template_id:
        policyForm.promotion_config.mode === "template"
          ? policyForm.promotion_config.default_template_id
          : null,
      destination_url:
        policyForm.promotion_config.destination_url?.trim() || null,
      cta_text: policyForm.promotion_config.cta_text?.trim() || null,
    },
    daily_limit: policyForm.daily_limit,
    cooldown_seconds: policyForm.cooldown_seconds,
    allowed_topics: normalizeTopics(topicInput.value),
    require_review: policyForm.require_review,
    enabled: policyForm.enabled,
  };
};

const savePolicy = async () => {
  if (!isAdmin.value || !assetId.value) return;
  const validation = policyValidationError();
  if (validation) {
    ElMessage.warning(validation);
    return;
  }
  pageError.value = "";
  try {
    const input = normalizedPolicyInput();
    const saved = editingPolicyId.value
      ? await store.replacePolicy(assetId.value, editingPolicyId.value, {
          ...input,
          revision: policyForm.revision,
        })
      : await store.createPolicy(assetId.value, input);
    clonePolicyIntoForm(saved);
    snapshotPolicy();
    policyDrawerVisible.value = false;
    ElMessage.success("策略已保存");
    await Promise.allSettled([
      store.fetchEligibleAccounts(assetId.value),
      store.fetchReviews(assetId.value),
    ]);
  } catch (error) {
    const failure = showError(error, "策略保存失败");
    if (failure.code === "POLICY_REVISION_CONFLICT") {
      await store.fetchPolicies(assetId.value).catch(() => undefined);
      const latest = store.policies.find(
        (item) => item.id === editingPolicyId.value,
      );
      if (latest) {
        clonePolicyIntoForm(latest);
        snapshotPolicy();
      }
    }
  }
};

// Preview and manual execution
const actionDialogVisible = ref(false);
const actionKind = ref<"preview" | "manual">("preview");
const actionPolicy = ref<OwnedGroupMessagePolicy | null>(null);
const actionForm = reactive({
  content_category: "community" as OwnedGroupMessageCategory,
  topic: "",
  instruction: "",
  template_id: null as number | null,
  variables_json: "{}",
  reply_to_message_id: null as number | null,
  scheduled_at: "" as string,
});
const idempotencyKey = ref("");

const makeIdempotencyKey = () => {
  const uuid = globalThis.crypto?.randomUUID?.();
  return `owned-message-${assetId.value}-${uuid || `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
};

const modeForPolicyCategory = (
  policy: OwnedGroupMessagePolicy,
  category: OwnedGroupMessageCategory,
) => (category === "community" ? policy.mode : policy.promotion_config.mode);

const actionTemplates = computed(() => {
  const policy = actionPolicy.value;
  if (!policy) return [];
  return store.templates.filter(
    (item) =>
      item.enabled && item.content_category === actionForm.content_category,
  );
});

const openMessageAction = (
  policy: OwnedGroupMessagePolicy,
  kind: "preview" | "manual",
) => {
  if (kind === "manual" && !isAdmin.value) return;
  actionPolicy.value = policy;
  actionKind.value = kind;
  actionForm.content_category =
    policy.mode !== "off" ? "community" : "promotion";
  actionForm.topic = "";
  actionForm.instruction = "";
  actionForm.template_id = null;
  actionForm.variables_json = "{}";
  actionForm.reply_to_message_id = null;
  actionForm.scheduled_at = "";
  store.previewResult = null;
  idempotencyKey.value = makeIdempotencyKey();
  actionDialogVisible.value = true;
};

const parseVariables = (): Record<string, string> | null => {
  try {
    const value = JSON.parse(actionForm.variables_json || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value))
      throw new Error();
    if (Object.values(value).some((item) => typeof item !== "string"))
      throw new Error();
    const variables = value as Record<string, string>;
    const unknown = Object.keys(variables).filter(
      (item) => !TEMPLATE_VARIABLES.has(item),
    );
    if (unknown.length) {
      ElMessage.warning(`不支持的模板变量：${unknown.join("、")}`);
      return null;
    }
    const injected = Object.keys(variables).filter((item) =>
      ["promotion_url", "promotion_cta"].includes(item),
    );
    if (injected.length) {
      ElMessage.warning(
        `${injected.join("、")} 只能由当前策略的群内广告配置注入`,
      );
      return null;
    }
    return variables;
  } catch {
    ElMessage.warning("模板变量必须为字符串键值 JSON 对象");
    return null;
  }
};

const validateMessageAction = (): string | null => {
  const policy = actionPolicy.value;
  if (!policy) return "未选择策略";
  const mode = modeForPolicyCategory(policy, actionForm.content_category);
  if (mode === "off")
    return `${categoryLabel(actionForm.content_category)}模式已关闭`;
  if (
    actionKind.value === "manual" &&
    (!policy.trigger_config.manual.enabled ||
      !policy.trigger_config.manual.allowed_content_categories.includes(
        actionForm.content_category,
      ))
  )
    return "该策略未允许此类手动触发";
  if (actionForm.topic && !policy.allowed_topics.includes(actionForm.topic))
    return "主题必须来自策略允许主题";
  if (actionForm.instruction.trim().length > 1000)
    return "补充指令不能超过 1000 字";
  if (mode === "template") {
    const templateId =
      actionForm.content_category === "promotion"
        ? policy.promotion_config.default_template_id
        : actionForm.template_id || policy.default_template_id;
    if (
      !templateId ||
      !actionTemplates.value.some((item) => item.id === templateId)
    )
      return "请选择当前资产中已启用且类别匹配的模板";
    if (
      actionForm.content_category === "promotion" &&
      actionForm.template_id &&
      actionForm.template_id !== policy.promotion_config.default_template_id
    )
      return "群内广告只能使用策略默认模板";
  }
  if (actionForm.scheduled_at) {
    const scheduled = dayjs(actionForm.scheduled_at);
    if (!scheduled.isValid() || scheduled.isBefore(dayjs()))
      return "预约时间必须晚于当前时间";
    if (scheduled.isAfter(dayjs().add(7, "day")))
      return "手动预约最长只能到未来 7 天";
  }
  return null;
};

const previewPolicy = async () => {
  if (!assetId.value || !actionPolicy.value) return;
  const validation = validateMessageAction();
  if (validation) return void ElMessage.warning(validation);
  const variables = parseVariables();
  if (!variables) return;
  const input: OwnedGroupMessagePreviewInput = {
    trigger_type: "manual",
    content_category: actionForm.content_category,
    topic: actionForm.topic || null,
    instruction: actionForm.instruction.trim() || null,
    template_id: actionForm.template_id,
    variables,
  };
  try {
    await store.previewPolicy(assetId.value, actionPolicy.value.id, input);
    ElMessage.success("预览已生成，未创建执行任务");
  } catch (error) {
    showError(error, "预览生成失败");
  }
};

const createManualExecution = async () => {
  if (!isAdmin.value || !assetId.value || !actionPolicy.value) return;
  const validation = validateMessageAction();
  if (validation) return void ElMessage.warning(validation);
  const variables = parseVariables();
  if (!variables) return;
  if (idempotencyKey.value.length < 8 || idempotencyKey.value.length > 128)
    return void ElMessage.warning("幂等键长度必须为 8–128 字符");
  const input: OwnedGroupMessageManualExecutionInput = {
    trigger_type: "manual",
    content_category: actionForm.content_category,
    topic: actionForm.topic || null,
    instruction: actionForm.instruction.trim() || null,
    template_id: actionForm.template_id,
    variables,
    reply_to_message_id: actionForm.reply_to_message_id,
    scheduled_at: actionForm.scheduled_at
      ? dayjs(actionForm.scheduled_at).toISOString()
      : null,
  };
  try {
    const accepted = await store.createManualExecution(
      assetId.value,
      actionPolicy.value.id,
      input,
      idempotencyKey.value,
    );
    actionDialogVisible.value = false;
    ElMessage.success(
      accepted.status === "pending_review"
        ? "已进入待审核队列"
        : `已进入队列（${statusLabel(accepted.status)}）`,
    );
  } catch (error) {
    showError(error, "手动执行创建失败");
  }
};

// Review queue
const reviewDialogVisible = ref(false);
const reviewItem = ref<OwnedGroupMessageExecutionSummary | null>(null);
const reviewMode = ref<"approve" | "edit" | "reject">("approve");
const reviewContent = ref("");
const rejectReason = ref("");
const reviewLoading = ref(false);
let reviewLoadGeneration = 0;

const reviewCountdown = (item: OwnedGroupMessageExecutionSummary) => {
  if (!item.review_expires_at) return "-";
  const seconds = dayjs(item.review_expires_at).diff(now.value, "second");
  if (seconds <= 0) return "已到期";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours}小时${minutes}分`;
};

const reviewExpired = computed(
  () =>
    Boolean(reviewItem.value?.review_expires_at) &&
    dayjs(reviewItem.value?.review_expires_at).isBefore(
      now.value.add(1, "second"),
    ),
);

const openReview = async (
  item: OwnedGroupMessageExecutionSummary,
  mode: "approve" | "edit" | "reject",
) => {
  if (!isAdmin.value || !assetId.value) return;
  const selectedAssetId = assetId.value;
  const generation = ++reviewLoadGeneration;
  reviewItem.value = item;
  reviewMode.value = mode;
  reviewContent.value = "";
  rejectReason.value = "";
  reviewLoading.value = true;
  reviewDialogVisible.value = true;
  try {
    const detail = await store.fetchExecutionDetail(selectedAssetId, item.id);
    if (
      generation !== reviewLoadGeneration ||
      assetId.value !== selectedAssetId ||
      !reviewDialogVisible.value
    ) {
      return;
    }
    reviewItem.value = detail;
    reviewContent.value = detail.content || "";
    if (detail.status !== "pending_review") {
      ElMessage.warning(
        `该执行当前为${statusLabel(detail.status)}，不能再审核`,
      );
      reviewDialogVisible.value = false;
      await store.fetchReviews(selectedAssetId).catch(() => undefined);
    }
  } catch (error) {
    if (
      generation === reviewLoadGeneration &&
      assetId.value === selectedAssetId
    ) {
      reviewDialogVisible.value = false;
      showError(error, "审核内容加载失败");
    }
  } finally {
    if (
      generation === reviewLoadGeneration &&
      assetId.value === selectedAssetId
    ) {
      reviewLoading.value = false;
    }
  }
};

const submitReview = async () => {
  if (!assetId.value || !reviewItem.value || !isAdmin.value) return;
  if (reviewLoading.value) return;
  if (reviewItem.value.status !== "pending_review") {
    return void ElMessage.warning("当前执行状态已变化，请刷新审核队列");
  }
  if (reviewExpired.value) {
    ElMessage.warning("审核内容已过期，已刷新审核队列");
    reviewDialogVisible.value = false;
    await store.fetchReviews(assetId.value).catch(() => undefined);
    return;
  }
  if (
    reviewMode.value === "edit" &&
    (reviewContent.value.trim().length < 1 ||
      reviewContent.value.trim().length > 4096)
  )
    return void ElMessage.warning("修改后的内容需为 1–4096 字");
  if (
    reviewMode.value === "reject" &&
    (rejectReason.value.trim().length < 1 ||
      rejectReason.value.trim().length > 500)
  )
    return void ElMessage.warning("拒绝原因需为 1–500 字");
  try {
    if (reviewMode.value === "reject") {
      await store.rejectExecution(
        assetId.value,
        reviewItem.value.id,
        reviewItem.value.revision,
        rejectReason.value,
      );
      ElMessage.success("已拒绝");
    } else {
      await store.approveExecution(
        assetId.value,
        reviewItem.value.id,
        reviewItem.value.revision,
        reviewMode.value === "edit" ? reviewContent.value.trim() : null,
      );
      ElMessage.success(
        reviewMode.value === "edit" ? "修改后已通过" : "已通过审核",
      );
    }
    reviewDialogVisible.value = false;
  } catch (error) {
    const failure = showError(error, "审核操作失败");
    if (failure.code === "EXECUTION_REVISION_CONFLICT") {
      await Promise.allSettled([
        store.fetchReviews(assetId.value),
        store.fetchExecutionDetail(assetId.value, reviewItem.value.id),
      ]);
      reviewDialogVisible.value = false;
    }
  }
};

// Execution history
const historyFilters = reactive<OwnedGroupMessageExecutionFilters>({
  page: 1,
  page_size: 20,
});
const historyDateRange = ref<[Date, Date] | null>(null);
const executionDialogVisible = ref(false);

const applyHistoryFilters = async () => {
  if (!assetId.value) return;
  historyFilters.created_from = historyDateRange.value?.[0]
    ? dayjs(historyDateRange.value[0]).startOf("day").toISOString()
    : undefined;
  historyFilters.created_to = historyDateRange.value?.[1]
    ? dayjs(historyDateRange.value[1]).endOf("day").toISOString()
    : undefined;
  try {
    await store.fetchHistory(assetId.value, { ...historyFilters });
  } catch (error) {
    showError(error, "执行历史加载失败");
  }
};

const resetHistoryFilters = () => {
  Object.assign(historyFilters, {
    policy_id: undefined,
    account_id: undefined,
    trigger_type: "",
    content_category: "",
    status: "",
    created_from: undefined,
    created_to: undefined,
    page: 1,
    page_size: 20,
  });
  historyDateRange.value = null;
  void applyHistoryFilters();
};

const changeHistoryPage = (page: number) => {
  historyFilters.page = page;
  void applyHistoryFilters();
};

const openExecutionDetail = async (item: OwnedGroupMessageExecutionSummary) => {
  if (!assetId.value) return;
  executionDialogVisible.value = true;
  try {
    await store.fetchExecutionDetail(assetId.value, item.id);
  } catch (error) {
    executionDialogVisible.value = false;
    showError(error, "执行详情加载失败");
  }
};

// Owned-group template management
const templateFilters = reactive({
  content_category: "" as OwnedGroupMessageCategory | "",
  message_type: "" as OwnedGroupTemplateMessageType | "",
  enabled: "" as "" | "true" | "false",
});
const filteredTemplates = computed(() =>
  store.templates.filter((item) => {
    if (
      templateFilters.content_category &&
      item.content_category !== templateFilters.content_category
    )
      return false;
    if (
      templateFilters.message_type &&
      item.message_type !== templateFilters.message_type
    )
      return false;
    if (
      templateFilters.enabled &&
      item.enabled !== (templateFilters.enabled === "true")
    )
      return false;
    return true;
  }),
);

const templateDrawerVisible = ref(false);
const editingTemplateId = ref<number | null>(null);
const templatePreviewVisible = ref(false);
const templatePreviewContent = ref("");
const templateSnapshot = ref("");
const templateForm = reactive({
  name: "",
  content: "",
  content_category: "community" as OwnedGroupMessageCategory,
  message_type: "interaction" as OwnedGroupTemplateMessageType,
  enabled: true,
});

const templateMessageTypeOptions = computed(() =>
  templateForm.content_category === "community"
    ? (["interaction", "qa"] as OwnedGroupTemplateMessageType[])
    : (["share", "guide"] as OwnedGroupTemplateMessageType[]),
);

const extractTemplateVariables = (content: string) => {
  const variables: string[] = [];
  const seen = new Set<string>();
  const pattern = /\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(content))) {
    const name = match[1];
    if (!seen.has(name)) {
      seen.add(name);
      variables.push(name);
    }
  }
  return variables;
};

const templateVariables = computed(() =>
  extractTemplateVariables(templateForm.content),
);

const templateValidationError = (): string | null => {
  const name = templateForm.name.trim();
  const content = templateForm.content;
  if (name.length < 1 || name.length > 100) return "模板名称需为 1–100 字";
  if (content.length < 1 || content.length > 5000)
    return "模板内容需为 1–5000 字";
  const withoutValid = content.replace(/\{\{\s*[a-z_][a-z0-9_]*\s*\}\}/g, "");
  if (withoutValid.includes("{{") || withoutValid.includes("}}"))
    return "模板存在未闭合或非法占位符";
  const invalid = templateVariables.value.filter(
    (item) => !TEMPLATE_VARIABLES.has(item),
  );
  if (invalid.length) return `不支持的模板变量：${invalid.join("、")}`;
  if (templateVariables.value.includes("register_link"))
    return "阶段 2 模板禁止使用 register_link";
  if (
    templateForm.content_category === "community" &&
    templateVariables.value.some((item) =>
      ["promotion_url", "promotion_cta"].includes(item),
    )
  )
    return "普通消息模板不能使用 promotion_url 或 promotion_cta";
  if (!templateMessageTypeOptions.value.includes(templateForm.message_type))
    return "模板类型与内容类别不匹配";
  return null;
};

const resetTemplateForm = () => {
  Object.assign(templateForm, {
    name: "",
    content: "",
    content_category: "community",
    message_type: "interaction",
    enabled: true,
  });
};

const snapshotTemplate = () => {
  templateSnapshot.value = JSON.stringify(templateForm);
};
const templateDirty = computed(
  () =>
    templateDrawerVisible.value &&
    templateSnapshot.value !== JSON.stringify(templateForm),
);

const openCreateTemplate = () => {
  if (!isAdmin.value) return;
  editingTemplateId.value = null;
  resetTemplateForm();
  templateDrawerVisible.value = true;
  snapshotTemplate();
};

const openEditTemplate = (template: OwnedGroupMessageTemplate) => {
  editingTemplateId.value = template.id;
  Object.assign(templateForm, {
    name: template.name,
    content: template.content,
    content_category: template.content_category,
    message_type: template.message_type,
    enabled: template.enabled,
  });
  templateDrawerVisible.value = true;
  snapshotTemplate();
};

const previewTemplate = (template?: OwnedGroupMessageTemplate) => {
  templatePreviewContent.value = template?.content ?? templateForm.content;
  templatePreviewVisible.value = true;
};

const closeTemplateDrawer = async (done?: () => void) => {
  if (templateDirty.value) {
    try {
      await ElMessageBox.confirm("存在未保存修改，确认关闭？", "未保存修改", {
        type: "warning",
      });
    } catch {
      return;
    }
  }
  templateDrawerVisible.value = false;
  done?.();
};

const saveTemplate = async () => {
  if (!assetId.value || !isAdmin.value) return;
  const validation = templateValidationError();
  if (validation) return void ElMessage.warning(validation);
  const existing = editingTemplateId.value
    ? store.templates.find((item) => item.id === editingTemplateId.value)
    : null;
  if (existing?.enabled && !templateForm.enabled) {
    try {
      await ElMessageBox.confirm(
        "已引用该模板的策略不会被自动改写，但之后新执行将被阻断",
        "停用群内模板",
        { type: "warning" },
      );
    } catch {
      return;
    }
  }
  const input: OwnedGroupMessageTemplateInput = {
    name: templateForm.name.trim(),
    content: templateForm.content,
    message_type: templateForm.message_type,
    template_variables: templateVariables.value,
    enabled: templateForm.enabled,
  };
  try {
    const saved = editingTemplateId.value
      ? await store.updateTemplate(
          assetId.value,
          editingTemplateId.value,
          input,
        )
      : await store.createTemplate(assetId.value, input);
    editingTemplateId.value = saved.id;
    templateDrawerVisible.value = false;
    ElMessage.success("群内模板已保存");
  } catch (error) {
    showError(error, "群内模板保存失败");
  }
};

watch(
  () => templateForm.content_category,
  (category) => {
    const options =
      category === "community" ? ["interaction", "qa"] : ["share", "guide"];
    if (!options.includes(templateForm.message_type)) {
      templateForm.message_type = options[0] as OwnedGroupTemplateMessageType;
    }
  },
);

watch(
  () => route.params.assetId,
  () => {
    policyDrawerVisible.value = false;
    editingPolicyId.value = null;
    resetPolicyForm();
    actionDialogVisible.value = false;
    actionPolicy.value = null;
    store.previewResult = null;
    reviewLoadGeneration += 1;
    reviewLoading.value = false;
    reviewDialogVisible.value = false;
    reviewItem.value = null;
    executionDialogVisible.value = false;
    templateDrawerVisible.value = false;
    editingTemplateId.value = null;
    resetTemplateForm();
    templatePreviewVisible.value = false;
    templatePreviewContent.value = "";
    Object.assign(historyFilters, {
      policy_id: undefined,
      account_id: undefined,
      trigger_type: undefined,
      content_category: undefined,
      status: undefined,
      created_from: undefined,
      created_to: undefined,
      page: 1,
      page_size: 20,
    });
    historyDateRange.value = null;
    Object.assign(templateFilters, {
      content_category: "",
      message_type: "",
      enabled: "",
    });
    activeTab.value = "policies";
    void loadWorkspace();
  },
);

onMounted(() => {
  void loadWorkspace();
  clockTimer = setInterval(() => {
    now.value = dayjs();
  }, 60000);
});

onBeforeUnmount(() => {
  if (clockTimer) clearInterval(clockTimer);
  store.reset();
});
</script>

<template>
  <div class="messaging-page">
    <div class="page-header">
      <div>
        <el-button text @click="goBack">← 返回自建群</el-button>
        <el-button v-if="assetId" text @click="goToOperationsCenter">返回群运营中心</el-button>
        <h2>群内消息 · {{ assetTitle }}</h2>
        <p>按自建群和推广账号配置 AI、模板、审核与执行记录。</p>
      </div>
      <el-button :loading="store.loading.workspace" @click="loadWorkspace"
        >刷新</el-button
      >
    </div>

    <el-alert
      v-if="pageError"
      type="error"
      show-icon
      :closable="false"
      :title="pageError"
      class="page-alert"
    />
    <el-alert
      v-for="reason in sendBlockers"
      :key="reason"
      :type="reason.includes('dry-run') ? 'info' : 'warning'"
      show-icon
      :closable="false"
      :title="reason"
      class="page-alert"
    />
    <el-alert
      v-if="!isAdmin"
      type="info"
      show-icon
      :closable="false"
      title="当前角色为只读：可查看策略、预览内容和执行历史，不能修改策略、创建任务或审核。"
      class="page-alert"
    />

    <el-card
      shadow="never"
      class="summary-card"
      v-loading="store.loading.workspace"
    >
      <el-descriptions :column="4" border>
        <el-descriptions-item label="群名称">{{
          assetTitle
        }}</el-descriptions-item>
        <el-descriptions-item label="Telegram Chat ID">{{
          telegramChatId ?? "-"
        }}</el-descriptions-item>
        <el-descriptions-item label="core_group_id">{{
          coreGroupId ?? "-"
        }}</el-descriptions-item>
        <el-descriptions-item label="治理状态">
          <el-tag
            :type="governanceStatus === 'managed' ? 'success' : 'warning'"
            >{{ governanceStatus }}</el-tag
          >
        </el-descriptions-item>
        <el-descriptions-item label="静态开关">{{
          staticEnabled ? "开启" : "关闭"
        }}</el-descriptions-item>
        <el-descriptions-item label="运行开关">{{
          runtimeEnabled ? "开启" : "关闭"
        }}</el-descriptions-item>
        <el-descriptions-item label="执行模式">{{
          dryRun ? "Dry-run" : "实际发送"
        }}</el-descriptions-item>
        <el-descriptions-item label="服务端发送资格">{{
          runtimeStatus.can_send === true ? "可发送" : "不可发送"
        }}</el-descriptions-item>
        <el-descriptions-item label="今日群发送">
          {{ groupSentToday }} / {{ globalGroupDailyLimit ?? "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="普通消息">{{
          communitySentToday
        }}</el-descriptions-item>
        <el-descriptions-item label="群内广告">{{
          promotionSentToday
        }}</el-descriptions-item>
        <el-descriptions-item label="最近发送">{{
          formatTime(lastSentAt)
        }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card shadow="never" class="workspace-card">
      <el-tabs v-model="activeTab">
        <el-tab-pane label="账号策略" name="policies">
          <div class="toolbar">
            <div class="toolbar-note">
              推广账号资格由服务端按成员、风险及 growth 模式统一判定。
            </div>
            <el-button v-if="isAdmin" type="primary" @click="openCreatePolicy"
              >新建策略</el-button
            >
          </div>
          <el-table
            :data="store.policies"
            v-loading="store.loading.policies"
            row-key="id"
          >
            <el-table-column label="推广账号" min-width="150">
              <template #default="{ row }">
                <div>
                  {{ row.account_display_name || accountLabel(row.account_id) }}
                </div>
                <small>#{{ row.account_id }}</small>
                <small
                  v-if="accountEligibility(row.account_id)"
                  class="account-health"
                >
                  {{ accountEligibility(row.account_id)?.status }} ·
                  {{ accountEligibility(row.account_id)?.risk_level }} ·
                  {{ accountEligibility(row.account_id)?.membership_status }}
                </small>
              </template>
            </el-table-column>
            <el-table-column label="账号资格" width="130">
              <template #default="{ row }">
                <el-tag
                  :type="row.account_eligible === false ? 'danger' : 'success'"
                >
                  {{ row.account_eligible === false ? "不可用" : "可用" }}
                </el-tag>
                <el-tooltip
                  v-if="row.account_blocking_reasons?.length"
                  :content="row.account_blocking_reasons.join('；')"
                  ><span class="reason-link">查看原因</span></el-tooltip
                >
              </template>
            </el-table-column>
            <el-table-column label="AI 性格" min-width="210">
              <template #default="{ row }">
                <div class="persona-summary">
                  <div class="persona-summary__name">
                    {{ policyPersonaName(asPolicy(row)) }}
                  </div>
                  <small>{{ policyPersonaMeta(asPolicy(row)) }}</small>
                  <div class="persona-summary__actions">
                    <el-tag
                      size="small"
                      :type="policyPersonaStatus(asPolicy(row)).type"
                    >
                      {{ policyPersonaStatus(asPolicy(row)).label }}
                    </el-tag>
                    <el-button
                      v-if="isAdmin"
                      type="primary"
                      link
                      size="small"
                      @click="goToAccountPersona(row.account_id)"
                    >
                      Persona 设置
                    </el-button>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="普通/广告模式" min-width="150">
              <template #default="{ row }">
                <el-tag>{{ modeLabel(row.mode) }}</el-tag>
                <el-tag type="warning"
                  >广告·{{ modeLabel(row.promotion_config.mode) }}</el-tag
                >
              </template>
            </el-table-column>
            <el-table-column label="触发" min-width="190">
              <template #default="{ row }">
                <el-tag
                  v-for="item in triggerSummary(asPolicy(row))"
                  :key="item"
                  size="small"
                  >{{ item }}</el-tag
                >
                <span v-if="!triggerSummary(asPolicy(row)).length">-</span>
              </template>
            </el-table-column>
            <el-table-column label="今日/上限" width="105">
              <template #default="{ row }"
                >{{ row.sent_today || 0 }} / {{ row.daily_limit }}</template
              >
            </el-table-column>
            <el-table-column label="冷却至" width="155">
              <template #default="{ row }">{{
                formatTime(row.cooldown_until)
              }}</template>
            </el-table-column>
            <el-table-column
              label="待审核"
              width="80"
              prop="pending_review_count"
            />
            <el-table-column label="状态" width="90">
              <template #default="{ row }"
                ><el-tag :type="row.enabled ? 'success' : 'info'">{{
                  row.enabled ? "启用" : "停用"
                }}</el-tag></template
              >
            </el-table-column>
            <el-table-column label="操作" min-width="250" fixed="right">
              <template #default="{ row }">
                <el-button text @click="openEditPolicy(asPolicy(row))">{{
                  isAdmin ? "编辑" : "查看"
                }}</el-button>
                <el-button
                  text
                  @click="openMessageAction(asPolicy(row), 'preview')"
                  >预览</el-button
                >
                <el-button
                  v-if="isAdmin"
                  text
                  @click="openMessageAction(asPolicy(row), 'manual')"
                  >手动触发</el-button
                >
                <el-button
                  text
                  @click="
                    Object.assign(historyFilters, {
                      policy_id: row.id,
                      page: 1,
                    });
                    activeTab = 'history';
                    applyHistoryFilters();
                  "
                  >查看历史</el-button
                >
              </template>
            </el-table-column>
          </el-table>

          <el-divider content-position="left">可选推广账号</el-divider>
          <el-table
            :data="store.eligibleAccounts"
            size="small"
            v-loading="store.loading.eligible"
          >
            <el-table-column prop="display_name" label="账号" />
            <el-table-column prop="status" label="在线状态" width="100" />
            <el-table-column prop="risk_level" label="风险" width="100" />
            <el-table-column
              prop="membership_status"
              label="成员状态"
              min-width="150"
            />
            <el-table-column label="资格" width="100">
              <template #default="{ row }"
                ><el-tag :type="row.eligible ? 'success' : 'danger'">{{
                  row.eligible ? "可选" : "不可选"
                }}</el-tag></template
              >
            </el-table-column>
            <el-table-column label="阻断原因" min-width="260">
              <template #default="{ row }">{{
                row.blocking_reasons?.join("；") || "-"
              }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane :label="`审核队列 (${store.reviewTotal})`" name="reviews">
          <div class="toolbar">
            <span>按创建时间升序，promotion 始终强制审核。</span
            ><el-button @click="assetId && store.fetchReviews(assetId)"
              >刷新</el-button
            >
          </div>
          <el-table
            :data="store.reviewItems"
            v-loading="store.loading.reviews"
            row-key="id"
          >
            <el-table-column label="群" min-width="140"
              ><template #default>{{ assetTitle }}</template></el-table-column
            >
            <el-table-column
              prop="account_display_name"
              label="账号"
              min-width="120"
            />
            <el-table-column label="AI 性格" min-width="190">
              <template #default="{ row }">
                <div class="persona-summary">
                  <div class="persona-summary__name">
                    {{ executionPersonaName(asExecution(row)) }}
                  </div>
                  <small>{{ executionPersonaMeta(asExecution(row)) }}</small>
                  <el-tag
                    size="small"
                    :type="executionPersonaStatus(asExecution(row)).type"
                  >
                    {{ executionPersonaStatus(asExecution(row)).label }}
                  </el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="触发/类别" width="150">
              <template #default="{ row }"
                >{{ triggerLabel(row.trigger_type) }} ·
                {{ categoryLabel(row.content_category) }}</template
              >
            </el-table-column>
            <el-table-column prop="topic" label="主题" min-width="120" />
            <el-table-column label="内容" min-width="280">
              <template #default="{ row }"
                ><div class="content-preview">
                  {{ row.content || row.content_summary || "-" }}
                </div></template
              >
            </el-table-column>
            <el-table-column label="创建时间" width="160"
              ><template #default="{ row }">{{
                formatTime(row.created_at)
              }}</template></el-table-column
            >
            <el-table-column label="过期倒计时" width="120"
              ><template #default="{ row }">{{
                reviewCountdown(asExecution(row))
              }}</template></el-table-column
            >
            <el-table-column
              v-if="isAdmin"
              label="审核"
              width="235"
              fixed="right"
            >
              <template #default="{ row }">
                <el-button
                  text
                  type="success"
                  @click="openReview(asExecution(row), 'approve')"
                  >通过</el-button
                >
                <el-button text @click="openReview(asExecution(row), 'edit')"
                  >修改后通过</el-button
                >
                <el-button
                  text
                  type="danger"
                  @click="openReview(asExecution(row), 'reject')"
                  >拒绝</el-button
                >
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="执行历史" name="history">
          <div class="filters">
            <el-select
              v-model="historyFilters.account_id"
              clearable
              placeholder="账号"
            >
              <el-option
                v-for="item in store.eligibleAccounts"
                :key="item.account_id"
                :label="item.display_name"
                :value="item.account_id"
              />
            </el-select>
            <el-select
              v-model="historyFilters.trigger_type"
              clearable
              placeholder="触发类型"
            >
              <el-option
                v-for="(label, value) in TRIGGER_LABELS"
                :key="value"
                :label="label"
                :value="value"
              />
            </el-select>
            <el-select
              v-model="historyFilters.content_category"
              clearable
              placeholder="内容类别"
            >
              <el-option label="普通消息" value="community" /><el-option
                label="群内广告"
                value="promotion"
              />
            </el-select>
            <el-select
              v-model="historyFilters.status"
              clearable
              placeholder="状态"
            >
              <el-option
                v-for="(label, value) in STATUS_LABELS"
                :key="value"
                :label="label"
                :value="value"
              />
            </el-select>
            <el-date-picker
              v-model="historyDateRange"
              type="daterange"
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
            />
            <el-button
              type="primary"
              @click="
                historyFilters.page = 1;
                applyHistoryFilters();
              "
              >筛选</el-button
            >
            <el-button @click="resetHistoryFilters">重置</el-button>
          </div>
          <el-table
            :data="store.history"
            v-loading="store.loading.history"
            row-key="id"
          >
            <el-table-column prop="id" label="执行 ID" width="100" />
            <el-table-column label="账号" min-width="120"
              ><template #default="{ row }">{{
                row.account_display_name || accountLabel(row.account_id)
              }}</template></el-table-column
            >
            <el-table-column label="AI 性格" min-width="190">
              <template #default="{ row }">
                <div class="persona-summary">
                  <div class="persona-summary__name">
                    {{ executionPersonaName(asExecution(row)) }}
                  </div>
                  <small>{{ executionPersonaMeta(asExecution(row)) }}</small>
                  <el-tag
                    size="small"
                    :type="executionPersonaStatus(asExecution(row)).type"
                  >
                    {{ executionPersonaStatus(asExecution(row)).label }}
                  </el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="触发/类别" width="155"
              ><template #default="{ row }"
                >{{ triggerLabel(row.trigger_type) }} ·
                {{ categoryLabel(row.content_category) }}</template
              ></el-table-column
            >
            <el-table-column label="内容摘要" min-width="240"
              ><template #default="{ row }"
                ><div class="content-preview">
                  {{ row.content_summary || row.content || "-" }}
                </div></template
              ></el-table-column
            >
            <el-table-column label="状态" width="100"
              ><template #default="{ row }"
                ><el-tag :type="statusType(row.status)">{{
                  statusLabel(row.status)
                }}</el-tag></template
              ></el-table-column
            >
            <el-table-column label="原因" min-width="180"
              ><template #default="{ row }">{{
                errorLabel(row.error_code)
              }}</template></el-table-column
            >
            <el-table-column label="Telegram message_id" width="165"
              ><template #default="{ row }">{{
                row.status === "sent" ? row.telegram_message_id || "-" : "-"
              }}</template></el-table-column
            >
            <el-table-column label="时间" width="160"
              ><template #default="{ row }">{{
                formatTime(row.sent_at || row.created_at)
              }}</template></el-table-column
            >
            <el-table-column label="操作" width="90" fixed="right"
              ><template #default="{ row }"
                ><el-button text @click="openExecutionDetail(asExecution(row))"
                  >详情</el-button
                ></template
              ></el-table-column
            >
          </el-table>
          <el-pagination
            layout="total, prev, pager, next"
            :total="store.historyTotal"
            :page-size="historyFilters.page_size"
            :current-page="historyFilters.page"
            @current-change="changeHistoryPage"
          />
        </el-tab-pane>

        <el-tab-pane label="群内模板" name="templates">
          <div class="toolbar">
            <div class="filters compact">
              <el-select
                v-model="templateFilters.content_category"
                clearable
                placeholder="内容类别"
                ><el-option label="普通消息" value="community" /><el-option
                  label="群内广告"
                  value="promotion"
              /></el-select>
              <el-select
                v-model="templateFilters.message_type"
                clearable
                placeholder="模板类型"
                ><el-option
                  v-for="(label, value) in TEMPLATE_TYPE_LABELS"
                  :key="value"
                  :label="label"
                  :value="value"
              /></el-select>
              <el-select
                v-model="templateFilters.enabled"
                clearable
                placeholder="启用状态"
                ><el-option label="启用" value="true" /><el-option
                  label="停用"
                  value="false"
              /></el-select>
            </div>
            <el-button v-if="isAdmin" type="primary" @click="openCreateTemplate"
              >新建群内模板</el-button
            >
          </div>
          <el-table
            :data="filteredTemplates"
            v-loading="store.loading.templates"
            row-key="id"
          >
            <el-table-column prop="name" label="名称" min-width="160" />
            <el-table-column label="类别" width="110"
              ><template #default="{ row }">{{
                categoryLabel(row.content_category)
              }}</template></el-table-column
            >
            <el-table-column label="类型" width="90"
              ><template #default="{ row }">{{
                templateTypeLabel(row.message_type)
              }}</template></el-table-column
            >
            <el-table-column label="内容摘要" min-width="280"
              ><template #default="{ row }"
                ><div class="content-preview">{{ row.content }}</div></template
              ></el-table-column
            >
            <el-table-column label="变量" min-width="180"
              ><template #default="{ row }">{{
                row.template_variables?.join("、") || "-"
              }}</template></el-table-column
            >
            <el-table-column label="状态" width="90"
              ><template #default="{ row }"
                ><el-tag :type="row.enabled ? 'success' : 'info'">{{
                  row.enabled ? "启用" : "停用"
                }}</el-tag></template
              ></el-table-column
            >
            <el-table-column label="更新时间" width="160"
              ><template #default="{ row }">{{
                formatTime(row.updated_at)
              }}</template></el-table-column
            >
            <el-table-column label="操作" width="145" fixed="right"
              ><template #default="{ row }"
                ><el-button text @click="openEditTemplate(asTemplate(row))">{{
                  isAdmin ? "编辑" : "查看"
                }}</el-button
                ><el-button text @click="previewTemplate(asTemplate(row))"
                  >预览</el-button
                ></template
              ></el-table-column
            >
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <el-drawer
      v-model="policyDrawerVisible"
      :title="
        editingPolicyId
          ? isAdmin
            ? '编辑账号策略'
            : '查看账号策略'
          : '新建账号策略'
      "
      size="720px"
      :before-close="closePolicyDrawer"
    >
      <el-form label-width="150px" :disabled="!isAdmin">
        <el-divider content-position="left">账号与普通消息</el-divider>
        <el-form-item label="推广账号" required>
          <el-select
            v-model="policyForm.account_id"
            filterable
            :disabled="Boolean(editingPolicyId) || !isAdmin"
            style="width: 100%"
          >
            <el-option
              v-for="account in store.eligibleAccounts"
              :key="account.account_id"
              :value="account.account_id"
              :label="`${account.display_name} · ${account.eligible ? '可选' : account.blocking_reasons.join('；')}`"
              :disabled="!account.eligible"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="普通消息模式"
          ><el-radio-group v-model="policyForm.mode"
            ><el-radio-button label="ai">AI</el-radio-button
            ><el-radio-button label="template">模板</el-radio-button
            ><el-radio-button label="off">关闭</el-radio-button></el-radio-group
          ></el-form-item
        >
        <el-form-item
          v-if="policyForm.mode === 'template'"
          label="普通消息模板"
          required
          ><el-select
            v-model="policyForm.default_template_id"
            style="width: 100%"
            ><el-option
              v-for="item in communityTemplateOptions"
              :key="item.id"
              :label="`${item.name} · ${TEMPLATE_TYPE_LABELS[item.message_type]}${item.enabled ? '' : ' · 已停用'}`"
              :value="item.id"
              :disabled="!item.enabled" /></el-select
        ></el-form-item>

        <el-divider content-position="left">定时触发</el-divider>
        <el-form-item label="启用"
          ><el-switch v-model="policyForm.trigger_config.scheduled.enabled"
        /></el-form-item>
        <template v-if="policyForm.trigger_config.scheduled.enabled">
          <el-form-item label="内容类别"
            ><el-radio-group
              v-model="policyForm.trigger_config.scheduled.content_category"
              ><el-radio label="community">普通消息</el-radio
              ><el-radio label="promotion">群内广告</el-radio></el-radio-group
            ></el-form-item
          >
          <el-form-item label="星期"
            ><el-checkbox-group
              v-model="policyForm.trigger_config.scheduled.weekdays"
              ><el-checkbox
                v-for="item in WEEKDAY_OPTIONS"
                :key="item.value"
                :label="item.value"
                >{{ item.label }}</el-checkbox
              ></el-checkbox-group
            ></el-form-item
          >
          <el-form-item label="时间点"
            ><el-select
              v-model="policyForm.trigger_config.scheduled.times"
              multiple
              allow-create
              filterable
              default-first-option
              placeholder="HH:MM"
              style="width: 100%"
          /></el-form-item>
          <el-form-item label="随机延迟（秒）"
            ><el-input-number
              v-model="policyForm.trigger_config.scheduled.jitter_seconds"
              :min="0"
              :max="900"
          /></el-form-item>
        </template>

        <el-divider content-position="left">关键词触发</el-divider>
        <el-form-item label="启用"
          ><el-switch v-model="policyForm.trigger_config.keyword.enabled"
        /></el-form-item>
        <template v-if="policyForm.trigger_config.keyword.enabled">
          <el-form-item label="内容类别"
            ><el-radio-group
              v-model="policyForm.trigger_config.keyword.content_category"
              ><el-radio label="community">普通消息</el-radio
              ><el-radio label="promotion">群内广告</el-radio></el-radio-group
            ></el-form-item
          >
          <el-form-item label="关键词规则"
            ><el-select
              v-model="policyForm.trigger_config.keyword.trigger_ids"
              multiple
              filterable
              style="width: 100%"
              ><el-option
                v-for="trigger in keywordTriggers"
                :key="trigger.id"
                :label="`${trigger.keyword_text} · ${trigger.action}`"
                :value="trigger.id"
                :disabled="
                  trigger.action !==
                  expectedKeywordAction(
                    policyForm.trigger_config.keyword.content_category,
                  )
                " /></el-select
          ></el-form-item>
          <el-form-item label="回复原消息"
            ><el-switch
              v-model="policyForm.trigger_config.keyword.reply_to_source"
          /></el-form-item>
        </template>

        <el-divider content-position="left">回复触发</el-divider>
        <el-form-item label="启用"
          ><el-switch v-model="policyForm.trigger_config.reply.enabled"
        /></el-form-item>
        <template v-if="policyForm.trigger_config.reply.enabled">
          <el-form-item label="内容类别"
            ><el-radio-group
              v-model="policyForm.trigger_config.reply.content_category"
              ><el-radio label="community">普通消息</el-radio
              ><el-radio label="promotion">群内广告</el-radio></el-radio-group
            ></el-form-item
          >
          <el-form-item label="策略"
            ><el-radio-group v-model="policyForm.trigger_config.reply.strategy"
              ><el-radio label="directed">定向回复</el-radio
              ><el-radio
                label="semantic"
                :disabled="
                  selectedModeForCategory(
                    policyForm.trigger_config.reply.content_category,
                  ) !== 'ai'
                "
                >语义回复</el-radio
              ></el-radio-group
            ></el-form-item
          >
          <el-form-item
            v-if="policyForm.trigger_config.reply.strategy === 'semantic'"
            label="语义置信度"
            ><el-input-number
              v-model="policyForm.trigger_config.reply.semantic_min_confidence"
              :min="0.5"
              :max="1"
              :step="0.01"
          /></el-form-item>
          <el-form-item label="上下文条数"
            ><el-input-number
              v-model="policyForm.trigger_config.reply.context_messages"
              :min="1"
              :max="20"
          /></el-form-item>
        </template>

        <el-divider content-position="left">手动触发</el-divider>
        <el-form-item label="启用"
          ><el-switch v-model="policyForm.trigger_config.manual.enabled"
        /></el-form-item>
        <el-form-item label="允许类别"
          ><el-checkbox-group
            v-model="
              policyForm.trigger_config.manual.allowed_content_categories
            "
            ><el-checkbox label="community">普通消息</el-checkbox
            ><el-checkbox label="promotion"
              >群内广告</el-checkbox
            ></el-checkbox-group
          ></el-form-item
        >

        <el-divider content-position="left">群内广告（独立配置）</el-divider>
        <el-form-item label="广告模式"
          ><el-radio-group v-model="policyForm.promotion_config.mode"
            ><el-radio-button label="ai">AI</el-radio-button
            ><el-radio-button label="template">模板</el-radio-button
            ><el-radio-button label="off">关闭</el-radio-button></el-radio-group
          ></el-form-item
        >
        <el-form-item
          v-if="policyForm.promotion_config.mode === 'template'"
          label="广告默认模板"
          required
          ><el-select
            v-model="policyForm.promotion_config.default_template_id"
            style="width: 100%"
            ><el-option
              v-for="item in promotionTemplateOptions"
              :key="item.id"
              :label="`${item.name} · ${TEMPLATE_TYPE_LABELS[item.message_type]}${item.enabled ? '' : ' · 已停用'}`"
              :value="item.id"
              :disabled="!item.enabled" /></el-select
        ></el-form-item>
        <el-form-item label="HTTPS 推广链接"
          ><el-input
            v-model="policyForm.promotion_config.destination_url"
            placeholder="可空；只允许 HTTPS"
        /></el-form-item>
        <el-form-item label="CTA"
          ><el-input
            v-model="policyForm.promotion_config.cta_text"
            maxlength="100"
            show-word-limit
        /></el-form-item>
        <el-alert
          type="warning"
          :closable="false"
          title="群内广告始终强制人工审核，且不读取增长中心广告计划、素材、额度或开关。"
        />

        <el-divider content-position="left">额度与安全</el-divider>
        <el-form-item label="每日上限"
          ><el-input-number
            v-model="policyForm.daily_limit"
            :min="0"
            :max="100"
        /></el-form-item>
        <el-form-item label="群级冷却（秒）"
          ><el-input-number
            v-model="policyForm.cooldown_seconds"
            :min="60"
            :max="86400"
        /></el-form-item>
        <el-form-item label="去重窗口（秒）"
          ><el-input-number
            v-model="policyForm.trigger_config.dedupe_window_seconds"
            :min="600"
            :max="86400"
        /></el-form-item>
        <el-form-item label="允许主题"
          ><el-input
            v-model="topicInput"
            type="textarea"
            :rows="4"
            placeholder="每行一个，最多 20 项"
        /></el-form-item>
        <el-form-item label="人工审核"
          ><el-switch v-model="policyForm.require_review"
        /></el-form-item>
        <el-form-item label="启用策略"
          ><el-switch v-model="policyForm.enabled" :disabled="!canEnablePolicy"
        /></el-form-item>
      </el-form>
      <template #footer
        ><el-button @click="closePolicyDrawer()">关闭</el-button
        ><el-button
          v-if="isAdmin"
          type="primary"
          :loading="store.loading.mutation"
          @click="savePolicy"
          >保存</el-button
        ></template
      >
    </el-drawer>

    <el-dialog
      v-model="actionDialogVisible"
      :title="actionKind === 'preview' ? '生成预览' : '创建手动执行'"
      width="620px"
    >
      <el-form label-width="120px">
        <el-form-item label="内容类别"
          ><el-radio-group v-model="actionForm.content_category"
            ><el-radio label="community">普通消息</el-radio
            ><el-radio label="promotion">群内广告</el-radio></el-radio-group
          ></el-form-item
        >
        <el-form-item v-if="actionPolicy?.allowed_topics?.length" label="主题"
          ><el-select v-model="actionForm.topic" clearable
            ><el-option
              v-for="topic in actionPolicy.allowed_topics"
              :key="topic"
              :label="topic"
              :value="topic" /></el-select
        ></el-form-item>
        <el-form-item label="补充指令"
          ><el-input
            v-model="actionForm.instruction"
            type="textarea"
            :rows="3"
            maxlength="1000"
            show-word-limit
        /></el-form-item>
        <el-form-item
          v-if="
            actionPolicy &&
            modeForPolicyCategory(actionPolicy, actionForm.content_category) ===
              'template'
          "
          label="模板"
          ><el-select
            v-model="actionForm.template_id"
            :disabled="actionForm.content_category === 'promotion'"
            style="width: 100%"
            ><el-option
              v-for="item in actionTemplates"
              :key="item.id"
              :label="item.name"
              :value="item.id" /></el-select
        ></el-form-item>
        <el-form-item label="模板变量 JSON"
          ><el-input
            v-model="actionForm.variables_json"
            type="textarea"
            :rows="3"
        /></el-form-item>
        <template v-if="actionKind === 'manual'">
          <el-form-item label="回复消息 ID"
            ><el-input-number v-model="actionForm.reply_to_message_id" :min="1"
          /></el-form-item>
          <el-form-item label="预约时间"
            ><el-date-picker
              v-model="actionForm.scheduled_at"
              type="datetime"
              placeholder="为空立即入队"
          /></el-form-item>
        </template>
        <el-card v-if="store.previewResult" shadow="never" class="preview-card">
          <div class="preview-meta">
            <el-tag>{{
              categoryLabel(store.previewResult.content_category)
            }}</el-tag
            ><span>{{ store.previewResult.message_purpose }}</span
            ><span>{{ store.previewResult.content.length }} 字</span
            ><el-tag
              :type="
                store.previewResult.would_require_review ? 'warning' : 'success'
              "
              >{{
                store.previewResult.would_require_review
                  ? "需要审核"
                  : "无需审核"
              }}</el-tag
            >
          </div>
          <pre>{{ store.previewResult.content }}</pre>
          <p v-if="store.previewResult.warnings.length">
            安全提示：{{ store.previewResult.warnings.join("；") }}
          </p>
        </el-card>
      </el-form>
      <template #footer
        ><el-button @click="actionDialogVisible = false">关闭</el-button
        ><el-button :loading="store.loading.preview" @click="previewPolicy"
          >生成预览</el-button
        ><el-button
          v-if="actionKind === 'manual' && isAdmin"
          type="primary"
          :loading="store.loading.mutation"
          @click="createManualExecution"
          >进入队列</el-button
        ></template
      >
    </el-dialog>

    <el-dialog
      v-model="reviewDialogVisible"
      :title="
        reviewMode === 'reject'
          ? '拒绝内容'
          : reviewMode === 'edit'
            ? '修改后通过'
            : '审核通过'
      "
      width="640px"
    >
      <el-skeleton v-if="reviewLoading" :rows="5" animated />
      <template v-else>
        <p>执行 #{{ reviewItem?.id }} · revision {{ reviewItem?.revision }}</p>
        <el-alert
          type="warning"
          :closable="false"
          :title="
            reviewExpired
              ? '该审核已过期，不能提交'
              : '提交时会校验 revision；若其他管理员已处理，将刷新最新状态且不会覆盖。'
          "
        />
        <el-input
          v-if="reviewMode === 'edit'"
          v-model="reviewContent"
          type="textarea"
          :rows="8"
          maxlength="4096"
          show-word-limit
        />
        <div v-else-if="reviewMode === 'approve'" class="review-content">
          {{ reviewItem?.content || "-" }}
        </div>
        <el-input
          v-else
          v-model="rejectReason"
          type="textarea"
          :rows="4"
          maxlength="500"
          show-word-limit
          placeholder="请输入拒绝原因"
        />
      </template>
      <template #footer
        ><el-button @click="reviewDialogVisible = false">取消</el-button
        ><el-button
          :type="reviewMode === 'reject' ? 'danger' : 'primary'"
          :loading="store.loading.mutation || reviewLoading"
          :disabled="
            reviewLoading ||
            reviewExpired ||
            reviewItem?.status !== 'pending_review'
          "
          @click="submitReview"
          >确认</el-button
        ></template
      >
    </el-dialog>

    <el-dialog v-model="executionDialogVisible" title="执行详情" width="760px">
      <el-skeleton v-if="store.loading.detail" :rows="6" animated />
      <template v-else-if="store.executionDetail">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="执行 ID">{{
            store.executionDetail.id
          }}</el-descriptions-item>
          <el-descriptions-item label="状态"
            ><el-tag :type="statusType(store.executionDetail.status)">{{
              statusLabel(store.executionDetail.status)
            }}</el-tag></el-descriptions-item
          >
          <el-descriptions-item label="asset/core/chat"
            >{{ store.executionDetail.owned_group_asset_id }} /
            {{ store.executionDetail.core_group_id }} /
            {{ store.executionDetail.telegram_chat_id }}</el-descriptions-item
          >
          <el-descriptions-item label="策略/账号"
            >{{ store.executionDetail.policy_id }} /
            {{ store.executionDetail.account_id }}</el-descriptions-item
          >
          <el-descriptions-item label="AI 性格">
            <div class="persona-summary">
              <div class="persona-summary__name">
                {{ executionPersonaName(store.executionDetail) }}
              </div>
              <small>{{ executionPersonaMeta(store.executionDetail) }}</small>
              <div class="persona-summary__actions">
                <el-tag
                  size="small"
                  :type="executionPersonaStatus(store.executionDetail).type"
                >
                  {{ executionPersonaStatus(store.executionDetail).label }}
                </el-tag>
                <el-button
                  v-if="isAdmin"
                  type="primary"
                  link
                  size="small"
                  @click="goToAccountPersona(store.executionDetail.account_id)"
                >
                  Persona 设置
                </el-button>
              </div>
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="触发/类别"
            >{{ TRIGGER_LABELS[store.executionDetail.trigger_type] }} /
            {{
              categoryLabel(store.executionDetail.content_category)
            }}</el-descriptions-item
          >
          <el-descriptions-item label="重试次数">{{
            store.executionDetail.attempt_count
          }}</el-descriptions-item>
          <el-descriptions-item label="content_hash" :span="2">{{
            store.executionDetail.content_hash || "-"
          }}</el-descriptions-item>
          <el-descriptions-item label="错误码">{{
            errorLabel(store.executionDetail.error_code)
          }}</el-descriptions-item>
          <el-descriptions-item label="Telegram message_id">{{
            store.executionDetail.status === "sent"
              ? store.executionDetail.telegram_message_id || "-"
              : "-"
          }}</el-descriptions-item>
          <el-descriptions-item label="内容" :span="2">
            <pre>{{
              store.executionDetail.content ||
              store.executionDetail.content_summary ||
              "-"
            }}</pre>
          </el-descriptions-item>
          <el-descriptions-item
            :label="isAdmin ? '失败详情' : '脱敏失败摘要'"
            :span="2"
            >{{
              store.executionDetail.error_message || "-"
            }}</el-descriptions-item
          >
          <el-descriptions-item v-if="isAdmin" label="审计事件" :span="2">{{
            store.executionDetail.audit_event_ids?.join(", ") || "-"
          }}</el-descriptions-item>
        </el-descriptions>
        <el-timeline class="timeline">
          <el-timeline-item
            v-for="(item, index) in store.executionDetail.timeline || []"
            :key="`${item.status}-${index}`"
            :timestamp="formatTime(item.at)"
            >{{ statusLabel(item.status)
            }}<span v-if="item.note"> · {{ item.note }}</span></el-timeline-item
          >
        </el-timeline>
      </template>
    </el-dialog>

    <el-drawer
      v-model="templateDrawerVisible"
      :title="
        editingTemplateId
          ? isAdmin
            ? '编辑群内模板'
            : '查看群内模板'
          : '新建群内模板'
      "
      size="620px"
      :before-close="closeTemplateDrawer"
    >
      <el-form label-width="110px" :disabled="!isAdmin">
        <el-form-item label="名称" required
          ><el-input
            v-model="templateForm.name"
            maxlength="100"
            show-word-limit
        /></el-form-item>
        <el-form-item label="内容类别" required
          ><el-radio-group v-model="templateForm.content_category"
            ><el-radio label="community">普通消息</el-radio
            ><el-radio label="promotion">群内广告</el-radio></el-radio-group
          ></el-form-item
        >
        <el-form-item label="消息类型" required
          ><el-select v-model="templateForm.message_type"
            ><el-option
              v-for="type in templateMessageTypeOptions"
              :key="type"
              :label="TEMPLATE_TYPE_LABELS[type]"
              :value="type" /></el-select
        ></el-form-item>
        <el-form-item label="内容" required
          ><el-input
            v-model="templateForm.content"
            type="textarea"
            :rows="12"
            maxlength="5000"
            show-word-limit
        /></el-form-item>
        <el-form-item label="识别变量"
          ><div class="variable-list">
            <el-tag v-for="variable in templateVariables" :key="variable">{{
              templateVariableToken(variable)
            }}</el-tag
            ><span v-if="!templateVariables.length">无</span>
          </div></el-form-item
        >
        <el-form-item label="启用"
          ><el-switch v-model="templateForm.enabled"
        /></el-form-item>
        <el-alert
          type="info"
          :closable="false"
          title="模板仅属于当前自建群；不会进入增长中心模板缓存，也不能跨资产复制。"
        />
      </el-form>
      <template #footer
        ><el-button @click="closeTemplateDrawer()">关闭</el-button
        ><el-button @click="previewTemplate()">内容预览</el-button
        ><el-button
          v-if="isAdmin"
          type="primary"
          :loading="store.loading.mutation"
          @click="saveTemplate"
          >保存</el-button
        ></template
      >
    </el-drawer>

    <el-dialog
      v-model="templatePreviewVisible"
      title="模板内容预览"
      width="560px"
    >
      <pre class="template-preview">{{ templatePreviewContent }}</pre>
      <template #footer
        ><el-button @click="templatePreviewVisible = false"
          >关闭</el-button
        ></template
      ></el-dialog
    >
  </div>
</template>

<style scoped>
.messaging-page {
  padding: 20px;
}
.page-header,
.toolbar,
.filters,
.preview-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}
.page-header {
  justify-content: space-between;
  margin-bottom: 16px;
}
.page-header h2 {
  margin: 4px 0;
}
.page-header p,
.toolbar-note {
  color: var(--el-text-color-secondary);
  margin: 0;
}
.page-alert {
  margin-bottom: 10px;
}
.summary-card {
  margin-bottom: 16px;
}
.workspace-card {
  min-height: 480px;
}
.toolbar {
  justify-content: space-between;
  margin-bottom: 14px;
}
.filters {
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.filters > .el-select {
  width: 150px;
}
.filters.compact {
  margin: 0;
}
.reason-link {
  margin-left: 6px;
  color: var(--el-color-primary);
  cursor: help;
  font-size: 12px;
}
.account-health {
  display: block;
  margin-top: 3px;
  color: var(--el-text-color-secondary);
}
.persona-summary {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.persona-summary__name {
  font-weight: 500;
}
.persona-summary small {
  color: var(--el-text-color-secondary);
}
.persona-summary__actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.content-preview {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}
.preview-card {
  margin-top: 14px;
}
.preview-meta {
  margin-bottom: 10px;
}
.preview-card pre,
.review-content,
.template-preview,
.el-descriptions pre {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
}
.review-content {
  padding: 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
}
.timeline {
  margin-top: 24px;
}
.variable-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.el-pagination {
  margin-top: 16px;
  justify-content: flex-end;
}
@media (max-width: 900px) {
  .messaging-page {
    padding: 12px;
  }
  .page-header {
    align-items: flex-start;
  }
}
</style>
