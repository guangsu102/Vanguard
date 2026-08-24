<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CircleCheck,
  Close,
  Connection,
  Document,
  Plus,
  Refresh,
  Select,
  Setting,
  Timer,
  UserFilled,
  VideoPlay,
  WarningFilled,
} from "@element-plus/icons-vue";
import { useRouter } from 'vue-router'
import { accountsApi } from '@/api/accounts'
import { groupsApi, type Group } from '@/api/groups'
import {
  automationApi,
  type AccountAdBinding,
  type AccountOperationConfig,
  type AccountOperationMode,
  type AccountRiskGuardSettings,
  type AccountWarmupPolicySettings,
  type AdDeliveryExecutionSettings,
  type AdDeliveryThrottleSettings,
  type AdFailurePolicy,
  type AdDynamicStatus,
  type AutoJoinSchedulerConfig,
  type AdCampaign,
  type AdCreative,
  type AutoJoinVerificationLog,
  type GroupFailoverStatus,
  type GroupFailoverTask,
  type GroupAdProfile,
  type AutomationRunResult,
} from '@/api/automation'
import { createDefaultAdDeliveryExecution, createDefaultAdDeliveryThrottle, createDefaultAdFailurePolicy, createDefaultAutoJoinScheduler, createDefaultRiskGuard, createDefaultWarmupPolicy } from '@/config/automationDefaults'
import { DEFAULT_GROUP_SEARCH_KEYWORD_TYPES, GROUP_SEARCH_KEYWORD_TYPE_OPTIONS } from '@/api/keywords'

type AccountOption = {
  id: number
  identifier?: string
  display_name?: string
  phone?: string
  session_name?: string
  status?: string
  is_active?: boolean
}

type CreativePoolSummary = {
  account_count: number
  pool_size: number
  created_count: number
  creative_ids: number[]
}

type CampaignBindingStats = {
  accountIds: number[];
  creativeIds: number[];
  bindingCount: number;
  enabledBindingCount: number;
};

type BindingGroup = {
  key: string;
  accountId: number;
  campaignId: number;
  bindings: AccountAdBinding[];
  creativeIds: number[];
  enabledCount: number;
  priority: number;
};

type PolicySummaryItem = {
  label: string
  value: string | number
  type?: 'success' | 'info' | 'warning' | 'danger' | 'primary'
}

const loading = ref(false)
const running = ref('')
const activeTab = ref('join')
const router = useRouter()
const lastResult = ref<AutomationRunResult | null>(null)
const autoJoinAttempts = ref<any[]>([])
const autoJoinVerificationLogs = ref<AutoJoinVerificationLog[]>([])
const groupFailoverTasks = ref<GroupFailoverTask[]>([])
const groupFailoverSummary = ref<Partial<Record<GroupFailoverStatus, number>>>({})
const groupFailoverTotal = ref(0)
const groupFailoverStatusFilter = ref<GroupFailoverStatus | "">("")

const deliveryLogs = ref<any[]>([])
const creatives = ref<AdCreative[]>([])
const campaigns = ref<AdCampaign[]>([])
const bindings = ref<AccountAdBinding[]>([])
const targetGroups = ref<Group[]>([])
let groupProfilesRefreshTimer: number | null = null
const groupAdProfiles = ref<GroupAdProfile[]>([])
const groupPolicyProbeRunning = ref<number | null>(null)
const dynamicStatuses = ref<AdDynamicStatus[]>([])
const accounts = ref<AccountOption[]>([])
const failoverTargetAccounts = computed(() =>
  accounts.value.filter(
    (account) => account.is_active && account.status !== 'banned' && account.status !== 'error',
  ),
)
const adBindingAccounts = computed(() =>
  accounts.value.filter(
    (account) => account.is_active !== false && !['banned', 'error'].includes(account.status || ''),
  ),
)
const creativePoolStatus = ref<CreativePoolSummary | null>(null)
const accountConfigLoading = ref(false)
const savingAccountConfig = ref(false)
const schedulerConfigLoading = ref(false)
const failurePolicyLoading = ref(false)
const riskGuardLoading = ref(false)
const warmupPolicyLoading = ref(false)
const adExecutionLoading = ref(false)
const adThrottleLoading = ref(false)
const selectedAccountId = ref<number>()
const batchAccountIds = ref<number[]>([])
const savingBatchAccountConfig = ref(false)
const editingCreativeId = ref<number | null>(null)
const savingCreative = ref(false)
const editingCampaignId = ref<number | null>(null)
const savingCampaign = ref(false)
const targetGroupDialogVisible = ref(false)
const savingTargetGroup = ref(false)
const deliveryLogLoading = ref(false)
const deliveryLogFilters = reactive({
  account_id: undefined as number | undefined,
  campaign_id: undefined as number | undefined,
  status: '',
  time_range: [] as string[],
})
const deliveryLogPagination = reactive({
  page: 1,
  page_size: 50,
  total: 0,
})

const autoJoinForm = reactive({
  max_accounts: 10,
  keywords_per_account: 10,
  max_groups_per_keyword: 20,
  dry_run: true,
})

const groupFailoverForm = reactive({
  max_tasks: 20,
  dry_run: false,
  target_account_ids: [] as number[],
})

const groupFailoverStatusOptions: Array<{ label: string; value: GroupFailoverStatus }> = [
  { label: '\u5f85\u63a5\u7ba1', value: 'queued' },
  { label: '\u63a5\u7ba1\u4e2d', value: 'joining' },
  { label: '\u7b49\u5f85\u91cd\u8bd5', value: 'retry' },
  { label: '\u5df2\u6062\u590d', value: 'succeeded' },
  { label: '\u9700\u4eba\u5de5\u5904\u7406', value: 'manual_required' },
  { label: '\u5931\u8d25', value: 'failed' },
  { label: '\u5df2\u53d6\u6d88', value: 'cancelled' },
]

const schedulerConfigForm = reactive(createDefaultAutoJoinScheduler())

const adRunForm = reactive({
  max_deliveries: 20,
  dry_run: true,
})

const adFailurePolicyForm = reactive(createDefaultAdFailurePolicy())

const riskActionOptions = [
  { label: '搜群', value: 'search' },
  { label: '加群', value: 'join' },
  { label: '私聊', value: 'private_message' },
  { label: '群消息', value: 'group_message' },
  { label: '审核', value: 'moderation' },
  { label: '广告投放', value: 'ad_delivery' },
  { label: '资料', value: 'profile_update' },
  { label: '回应表情', value: 'reaction' },
  { label: '转发', value: 'forward' },
  { label: '置顶', value: 'pin' },
  { label: '机器人消息', value: 'bot_message' },
  { label: '机器人置顶', value: 'bot_pin' },
  { label: '创建频道', value: 'channel_create' },
]

const defaultRiskActions = () => createDefaultRiskGuard().actions

const accountRiskGuardForm = reactive(createDefaultRiskGuard())

const warmupTierOptions = [
  { label: '未知', value: 'unknown' },
  { label: '1个月', value: 'month_1' },
  { label: '3-6个月', value: 'month_3_6' },
  { label: '1年', value: 'year_1' },
  { label: '2年', value: 'year_2' },
  { label: '3年以上', value: 'year_3_plus' },
]

const warmupStageOptions = [
  { label: '观察', value: 'observe' },
  { label: '起步', value: 'seed' },
  { label: '低频', value: 'soft' },
  { label: '提量', value: 'ramp' },
  { label: '正常', value: 'normal' },
  { label: '冷却', value: 'cooldown' },
]

const defaultWarmupTiers = (): AccountWarmupPolicySettings['tiers'] => createDefaultWarmupPolicy().tiers

const defaultWarmupStages = (): AccountWarmupPolicySettings['stages'] => createDefaultWarmupPolicy().stages

const accountWarmupPolicyForm = reactive(createDefaultWarmupPolicy())

const adDeliveryExecutionForm = reactive(createDefaultAdDeliveryExecution())

const adDeliveryThrottleForm = reactive(createDefaultAdDeliveryThrottle())

const keywordReplenishForm = reactive({
  auto_approve: true,
})

const creativeForm = reactive({
  name: '',
  content: '',
  creative_type: 'text' as 'text' | 'image' | 'mixed',
  media_url: '',
  link_url: '',
  weight: 100,
  enabled: true,
})

const emptyCreativeForm = () => ({
  name: '',
  content: '',
  creative_type: 'text' as 'text' | 'image' | 'mixed',
  media_url: '',
  link_url: '',
  weight: 100,
  enabled: true,
})

const campaignForm = reactive({
  name: '',
  enabled: false,
  status: 'draft',
  send_mode: 'after_join' as 'after_join' | 'interval' | 'scheduled',
  target_group_levels: ['A'],
  target_group_ids: [] as number[],
  start_at: '',
  end_at: '',
  min_wait_after_join_minutes: 60,
  interval_minutes: 180,
  max_sends_per_group_per_day: 1,
  max_sends_per_account_per_day: 500,
})

const emptyCampaignForm = () => ({
  name: '',
  enabled: false,
  status: 'draft',
  send_mode: 'after_join' as 'after_join' | 'interval' | 'scheduled',
  target_group_levels: ['A'],
  target_group_ids: [] as number[],
  start_at: '',
  end_at: '',
  min_wait_after_join_minutes: 60,
  interval_minutes: 180,
  max_sends_per_group_per_day: 1,
  max_sends_per_account_per_day: 500,
})

const scheduledTimesText = ref('')

const targetGroupForm = reactive({
  groupLink: '',
  accountId: undefined as number | undefined,
})

const bindingForm = reactive({
  account_ids: [] as number[],
  ad_campaign_id: undefined as number | undefined,
  creative_ids: [] as number[],
  enabled: true,
  priority: 0,
})

const adWorkspaceView = ref("campaigns");
const campaignDrawerVisible = ref(false);
const creativeDrawerVisible = ref(false);
const bindingDrawerVisible = ref(false);
const adRunDialogVisible = ref(false);
const campaignFilters = reactive({
  query: "",
  status: "",
});
const bindingFilters = reactive({
  account_id: undefined as number | undefined,
  campaign_id: undefined as number | undefined,
  status: "",
});

const groupPolicyFilters = reactive({
  mode: "",
  tier: "",
});

const accountConfigForm = reactive({
  operation_mode: 'growth' as AccountOperationMode,
  enabled: true,
  auto_join_enabled: false,
  auto_ads_enabled: true,
  max_groups_per_day: 100,
  max_groups_total: 100,
  join_interval_min_seconds: 60,
  join_interval_max_seconds: 900,
  max_messages_per_day: 500,
  message_interval_seconds: 300,
  quiet_hours_start: '',
  quiet_hours_end: '',
  keyword_types: [...DEFAULT_GROUP_SEARCH_KEYWORD_TYPES] as string[],
  keyword_auto_replenish_enabled: true,
  keyword_replenish_requires_review: false,
  risk_level: 'normal',
  business_stage: 'new',
  next_join_after: '',
})

const keywordTypeOptions = GROUP_SEARCH_KEYWORD_TYPE_OPTIONS

const isAdOnlyAccount = computed(() => accountConfigForm.operation_mode === 'ad_only')

watch(() => accountConfigForm.operation_mode, (mode) => {
  if (mode === 'ad_only') {
    accountConfigForm.auto_join_enabled = false
    accountConfigForm.keyword_auto_replenish_enabled = false
  }
})

const deliveryStatusOptions = [
  { label: '成功', value: 'success' },
  { label: '失败', value: 'failed' },
  { label: '跳过', value: 'skipped' },
  { label: '待处理', value: 'pending' },
]

const resultSummary = computed(() => {
  if (!lastResult.value) return []
  if (lastResult.value.queued) {
    return [
      { label: '状态', value: lastResult.value.status || 'queued' },
      { label: '任务', value: lastResult.value.task_name || '-' },
      { label: 'ID', value: lastResult.value.task_id || '-' },
    ]
  }
  return [
    { label: '处理', value: lastResult.value.processed },
    { label: '成功', value: lastResult.value.succeeded },
    { label: '创建', value: lastResult.value.created },
    { label: '跳过', value: lastResult.value.skipped },
    { label: '失败', value: lastResult.value.failed },
  ]
})

const booleanTagType = (value: boolean): PolicySummaryItem['type'] => (value ? 'success' : 'info')
const booleanText = (value: boolean) => (value ? '开启' : '关闭')

const goGrowthConfig = (config: 'join' | 'warmup' | 'risk' | 'ads' | 'group-ai' | 'asset') => {
  router.push({ path: '/growth-dashboard', query: { config } })
}

const schedulerSummary = computed<PolicySummaryItem[]>(() => [
  { label: '定时扫描', value: booleanText(schedulerConfigForm.enabled), type: booleanTagType(schedulerConfigForm.enabled) },
  { label: '扫描间隔', value: `${schedulerConfigForm.scan_interval_minutes} 分钟` },
  { label: '入群验证', value: booleanText(schedulerConfigForm.join_verification.enabled), type: booleanTagType(schedulerConfigForm.join_verification.enabled) },
  { label: 'AI识别', value: booleanText(schedulerConfigForm.join_verification.ai_enabled), type: booleanTagType(schedulerConfigForm.join_verification.ai_enabled) },
  { label: '可信阈值', value: schedulerConfigForm.join_verification.confidence_threshold },
  { label: '标题黑名单', value: booleanText(schedulerConfigForm.search_filter.title_blacklist_enabled), type: booleanTagType(schedulerConfigForm.search_filter.title_blacklist_enabled) },
  { label: '满群清理', value: booleanText(schedulerConfigForm.group_capacity_cleanup.enabled), type: booleanTagType(schedulerConfigForm.group_capacity_cleanup.enabled) },
])

const riskSummary = computed<PolicySummaryItem[]>(() => [
  { label: '风控开关', value: booleanText(accountRiskGuardForm.enabled), type: booleanTagType(accountRiskGuardForm.enabled) },
  { label: '单号总日额度', value: accountRiskGuardForm.global_daily_limit },
  { label: '共享群写日额度', value: accountRiskGuardForm.group_write_daily_limit },
  { label: '单号加群额度', value: accountRiskGuardForm.actions.join?.daily_limit ?? '-' },
  { label: '单号广告额度', value: accountRiskGuardForm.actions.ad_delivery?.daily_limit ?? '-' },
  { label: '单号群消息额度', value: accountRiskGuardForm.actions.group_message?.daily_limit ?? '-' },
])

const warmupSummary = computed<PolicySummaryItem[]>(() => [
  { label: '暖号策略', value: booleanText(accountWarmupPolicyForm.enabled), type: booleanTagType(accountWarmupPolicyForm.enabled) },
  { label: '默认天数', value: `${accountWarmupPolicyForm.default_warmup_days} 天` },
  { label: '最短天数', value: `${accountWarmupPolicyForm.minimum_warmup_days} 天` },
  { label: '观察阶段加群', value: accountWarmupPolicyForm.stages.observe?.join_multiplier ?? '-' },
  { label: '提量阶段广告', value: accountWarmupPolicyForm.stages.ramp?.ad_multiplier ?? '-' },
])

const activeCampaignCount = computed(
  () => campaigns.value.filter((item) => item.enabled).length,
);
const enabledCreativeCount = computed(
  () => creatives.value.filter((item) => item.enabled).length,
);
const enabledBindingCount = computed(
  () => bindings.value.filter((item) => item.enabled).length,
);
const adSuccess24h = computed(() =>
  dynamicStatuses.value.reduce(
    (total, item) => total + Number(item.success_24h || 0),
    0,
  ),
);
const adFailed24h = computed(() =>
  dynamicStatuses.value.reduce(
    (total, item) => total + Number(item.failed_24h || 0),
    0,
  ),
);
const adSuccessRate24h = computed(() => {
  const total = adSuccess24h.value + adFailed24h.value;
  return total ? Math.round((adSuccess24h.value / total) * 100) : 0;
});
const allowedGroupPolicyModes = new Set([
  "soft_ad_trial",
  "soft_ad_allowed",
  "high_volume_ad_allowed",
]);
const adAllowedGroupCount = computed(
  () =>
    groupAdProfiles.value.filter(
      (profile) =>
        allowedGroupPolicyModes.has(profile.ad_policy_mode) &&
        Number(profile.daily_capacity || 0) > 0,
    ).length,
);
const pendingGroupPolicyCount = computed(
  () =>
    groupAdProfiles.value.filter((profile) =>
      ["unknown", "unknown_probe", "approval_required"].includes(profile.ad_policy_mode),
    ).length,
);
const forbiddenGroupCount = computed(
  () =>
    groupAdProfiles.value.filter(
      (profile) => profile.ad_policy_mode === "forbidden",
    ).length,
);
const groupDailyCapacityTotal = computed(() =>
  groupAdProfiles.value.reduce(
    (total, profile) => total + Number(profile.daily_capacity || 0),
    0,
  ),
);
const unboundCampaignCount = computed(
  () =>
    campaigns.value.filter(
      (campaign) => campaign.enabled && !campaignBindingStats.value.has(campaign.id),
    ).length,
);
const blockedAdAccountCount = computed(
  () => adReadinessRows.value.filter((item) => !item.ready).length,
);
const adReadinessScore = computed(() => {
  const checks = [
    activeCampaignCount.value > 0,
    readyAdAccountCount.value > 0,
    enabledCreativeCount.value > 0,
    enabledBindingCount.value > 0,
    adAllowedGroupCount.value > 0,
  ];
  return checks.filter(Boolean).length * 20;
});
const filteredGroupAdProfiles = computed(() =>
  [...groupAdProfiles.value]
    .filter(
      (profile) =>
        !groupPolicyFilters.mode ||
        profile.ad_policy_mode === groupPolicyFilters.mode,
    )
    .filter(
      (profile) =>
        !groupPolicyFilters.tier || profile.ad_tier === groupPolicyFilters.tier,
    )
    .sort(
      (left, right) =>
        Number(right.daily_capacity || 0) - Number(left.daily_capacity || 0) ||
        right.id - left.id,
    ),
);
const campaignBindingStats = computed(() => {
  const grouped = new Map<
    number,
    {
      accountIds: Set<number>;
      creativeIds: Set<number>;
      bindingCount: number;
      enabledBindingCount: number;
    }
  >();

  for (const binding of bindings.value) {
    const current = grouped.get(binding.ad_campaign_id) || {
      accountIds: new Set<number>(),
      creativeIds: new Set<number>(),
      bindingCount: 0,
      enabledBindingCount: 0,
    };
    current.accountIds.add(binding.account_id);
    if (binding.creative_id) current.creativeIds.add(binding.creative_id);
    current.bindingCount += 1;
    if (binding.enabled) current.enabledBindingCount += 1;
    grouped.set(binding.ad_campaign_id, current);
  }

  const result = new Map<number, CampaignBindingStats>();
  grouped.forEach((item, campaignId) => {
    result.set(campaignId, {
      accountIds: [...item.accountIds],
      creativeIds: [...item.creativeIds],
      bindingCount: item.bindingCount,
      enabledBindingCount: item.enabledBindingCount,
    });
  });
  return result;
});

const filteredCampaigns = computed(() => {
  const keyword = campaignFilters.query.trim().toLowerCase();
  return [...campaigns.value]
    .filter((campaign) => {
      const matchesKeyword =
        !keyword || campaign.name.toLowerCase().includes(keyword);
      const matchesStatus =
        !campaignFilters.status ||
        (campaignFilters.status === "active" && campaign.enabled) ||
        (campaignFilters.status === "paused" && !campaign.enabled) ||
        campaign.status === campaignFilters.status;
      return matchesKeyword && matchesStatus;
    })
    .sort(
      (left, right) =>
        Number(right.enabled) - Number(left.enabled) || right.id - left.id,
    );
});

const creativeBindingCounts = computed(() => {
  const counts = new Map<number, number>();
  for (const binding of bindings.value) {
    if (!binding.creative_id) continue;
    counts.set(binding.creative_id, (counts.get(binding.creative_id) || 0) + 1);
  }
  return counts;
});

const bindingGroups = computed<BindingGroup[]>(() => {
  const grouped = new Map<string, AccountAdBinding[]>();
  for (const binding of bindings.value) {
    const key = `${binding.account_id}:${binding.ad_campaign_id}`;
    grouped.set(key, [...(grouped.get(key) || []), binding]);
  }
  return [...grouped.entries()].map(([key, groupBindings]) => ({
    key,
    accountId: groupBindings[0].account_id,
    campaignId: groupBindings[0].ad_campaign_id,
    bindings: groupBindings,
    creativeIds: groupBindings
      .map((binding) => binding.creative_id)
      .filter((creativeId): creativeId is number => Boolean(creativeId)),
    enabledCount: groupBindings.filter((binding) => binding.enabled).length,
    priority: Math.max(
      ...groupBindings.map((binding) => binding.priority || 0),
    ),
  }));
});

const filteredBindingGroups = computed(() =>
  bindingGroups.value
    .filter(
      (group) =>
        !bindingFilters.account_id ||
        group.accountId === bindingFilters.account_id,
    )
    .filter(
      (group) =>
        !bindingFilters.campaign_id ||
        group.campaignId === bindingFilters.campaign_id,
    )
    .filter((group) => {
      if (!bindingFilters.status) return true;
      if (bindingFilters.status === "enabled") return group.enabledCount > 0;
      return group.enabledCount === 0;
    })
    .sort(
      (left, right) =>
        right.enabledCount - left.enabledCount ||
        right.priority - left.priority,
    ),
);

const adReadinessRows = computed(() =>
  accounts.value
    .map((account) => {
      const status = dynamicStatuses.value.find(
        (item) => item.account_id === account.id,
      );
      const accountBindings = bindings.value.filter(
        (binding) => binding.account_id === account.id,
      );
      return {
        account,
        status,
        bindingCount: accountBindings.length,
        enabledBindingCount: accountBindings.filter(
          (binding) => binding.enabled,
        ).length,
        ready:
          account.status !== "banned" &&
          account.status !== "error" &&
          account.is_active !== false &&
          Boolean(status?.delivery_diagnostic?.ad_delivery_allowed),
      };
    })
    .sort(
      (left, right) =>
        Number(right.ready) - Number(left.ready) ||
        right.enabledBindingCount - left.enabledBindingCount,
    ),
);

const readyAdAccountCount = computed(
  () => adReadinessRows.value.filter((item) => item.ready).length,
);
const campaignStats = (campaignId: number): CampaignBindingStats =>
  campaignBindingStats.value.get(campaignId) || {
    accountIds: [],
    creativeIds: [],
    bindingCount: 0,
    enabledBindingCount: 0,
  };

const campaignTargetGroups = (campaign: any) =>
  (campaign.target_group_ids || [])
    .map((groupId: number) => targetGroupMap.value.get(groupId))
    .filter((group: Group | undefined): group is Group => Boolean(group));

const campaignFrequencyText = (campaign: any) => {
  if (campaign.send_mode === "after_join")
    return `入群 ${campaign.min_wait_after_join_minutes} 分钟后`;
  if (campaign.send_mode === "interval")
    return `每 ${campaign.interval_minutes} 分钟`;
  return campaign.scheduled_times?.length
    ? campaign.scheduled_times.join("、")
    : "未设置时点";
};

const campaignWindowText = (campaign: any) => {
  if (!campaign.start_at && !campaign.end_at) return "长期有效";
  return `${formatTimestamp(campaign.start_at)} 至 ${formatTimestamp(campaign.end_at)}`;
};

const accountStatusType = (status?: string) => {
  if (status === "active" || status === "connected") return "success";
  if (status === "banned" || status === "error") return "danger";
  return "info";
};

const accountStatusText = (status?: string) => {
  const labels: Record<string, string> = {
    active: "在线",
    connected: "在线",
    offline: "离线",
    banned: "已封禁",
    error: "异常",
  };
  return labels[status || ""] || status || "未知";
};

const deliveryBlockReason = (status?: AdDynamicStatus) =>
  status?.delivery_diagnostic?.primary_block_label ||
  status?.delivery_diagnostic?.next_action_label ||
  status?.risk_reason ||
  "等待状态评估";

const accountMap = computed(() => {
  return new Map(accounts.value.map((item) => [item.id, item]))
})

const targetGroupMap = computed(() => new Map(targetGroups.value.map((item) => [item.id, item])))
const accountOperationModeMap = computed(() =>
  new Map(dynamicStatuses.value.map((item) => [item.account_id, item.operation_mode])),
)
const targetGroupAccountIsAdOnly = computed(() =>
  targetGroupForm.accountId === selectedAccountId.value
    ? accountConfigForm.operation_mode === 'ad_only'
    : accountOperationModeMap.value.get(targetGroupForm.accountId || 0) === 'ad_only',
)

const selectedDynamicStatus = computed(() =>
  dynamicStatuses.value.find((item) => item.account_id === selectedAccountId.value),
)

const parseScheduledTimes = () => {
  return scheduledTimesText.value
    .split(/[,\n，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

const accountLabel = (accountId?: number) => {
  if (!accountId) return '-'
  const account = accountMap.value.get(accountId)
  if (!account) return `#${accountId}`
  return (account as any).display_name || (account as any).identifier || account.phone || account.session_name || `#${accountId}`
}

const targetGroupLabel = (groupId: number) => {
  const group = targetGroupMap.value.get(groupId)
  if (!group) return `#${groupId}`
  const identity = group.username ? `@${group.username.replace(/^@/, '')}` : group.chatId
  const owner = group.adDeliveryAccountId ? ` · 专用 ${accountLabel(group.adDeliveryAccountId)}` : ''
  return `${group.title || identity} · ${identity}${owner}`
}

const campaignTargetLabel = (campaign: any) => {
  if (campaign.target_group_ids?.length) {
    return campaign.target_group_ids.map(targetGroupLabel).join('、')
  }
  return `等级 ${campaign.target_group_levels?.join('/') || '-'}`
}

const fillAccountConfigForm = (config: AccountOperationConfig) => {
  Object.assign(accountConfigForm, {
    operation_mode: config.operation_mode || 'growth',
    enabled: config.enabled,
    auto_join_enabled: config.auto_join_enabled,
    auto_ads_enabled: config.auto_ads_enabled,
    max_groups_per_day: config.max_groups_per_day,
    max_groups_total: config.max_groups_total,
    join_interval_min_seconds: config.join_interval_min_seconds,
    join_interval_max_seconds: config.join_interval_max_seconds,
    max_messages_per_day: config.max_messages_per_day,
    message_interval_seconds: config.message_interval_seconds,
    quiet_hours_start: config.quiet_hours_start || '',
    quiet_hours_end: config.quiet_hours_end || '',
    keyword_types: config.keyword_types?.length ? config.keyword_types : [...DEFAULT_GROUP_SEARCH_KEYWORD_TYPES],
    keyword_auto_replenish_enabled: config.keyword_auto_replenish_enabled ?? true,
    keyword_replenish_requires_review: config.keyword_replenish_requires_review ?? false,
    risk_level: config.risk_level || 'normal',
    business_stage: config.business_stage || 'new',
    next_join_after: config.next_join_after || '',
  })
}

const fillSchedulerConfigForm = (config: AutoJoinSchedulerConfig) => {
  Object.assign(schedulerConfigForm, {
    enabled: config.enabled,
    scan_interval_minutes: config.scan_interval_minutes,
    search_filter: {
      ...schedulerConfigForm.search_filter,
      ...(config.search_filter || {}),
      title_blacklist: config.search_filter?.title_blacklist || [],
    },
    join_verification: {
      ...schedulerConfigForm.join_verification,
      ...(config.join_verification || {}),
    },
    group_capacity_cleanup: {
      ...schedulerConfigForm.group_capacity_cleanup,
      ...(config.group_capacity_cleanup || {}),
    },
  })
}

const fillAdFailurePolicyForm = (config: AdFailurePolicy) => {
  Object.assign(adFailurePolicyForm, {
    enabled: config.enabled,
    leave_on_group_control_failure: config.leave_on_group_control_failure,
    group_control_failure_limit: config.group_control_failure_limit,
    group_control_failure_window_hours: config.group_control_failure_window_hours,
    levels: config.levels?.length ? config.levels : ['B'],
  })
}

const fillAccountRiskGuardForm = (config: AccountRiskGuardSettings) => {
  accountRiskGuardForm.enabled = config.enabled
  accountRiskGuardForm.global_daily_limit = config.global_daily_limit
  accountRiskGuardForm.group_write_daily_limit = config.group_write_daily_limit
  accountRiskGuardForm.redis_fail_closed = config.redis_fail_closed
  const actions = defaultRiskActions()
  for (const item of riskActionOptions) {
    actions[item.value] = {
      ...actions[item.value],
      ...(config.actions?.[item.value] || {}),
    }
  }
  accountRiskGuardForm.actions = actions
  accountRiskGuardForm.level_thresholds = {
    ...accountRiskGuardForm.level_thresholds,
    ...(config.level_thresholds || {}),
  }
  accountRiskGuardForm.level_budget_multipliers = {
    ...accountRiskGuardForm.level_budget_multipliers,
    ...(config.level_budget_multipliers || {}),
  }
  accountRiskGuardForm.risk_score_deltas = {
    ...accountRiskGuardForm.risk_score_deltas,
    ...(config.risk_score_deltas || {}),
  }
  accountRiskGuardForm.lifecycle = {
    ...accountRiskGuardForm.lifecycle,
    ...(config.lifecycle || {}),
  }
  accountRiskGuardForm.group_write_forbidden = {
    ...accountRiskGuardForm.group_write_forbidden,
    ...(config.group_write_forbidden || {}),
  }
  accountRiskGuardForm.retention = {
    ...accountRiskGuardForm.retention,
    ...(config.retention || {}),
  }
}

const fillAccountWarmupPolicyForm = (config: AccountWarmupPolicySettings) => {
  accountWarmupPolicyForm.enabled = config.enabled
  accountWarmupPolicyForm.default_warmup_days = config.default_warmup_days
  accountWarmupPolicyForm.minimum_warmup_days = config.minimum_warmup_days
  accountWarmupPolicyForm.user_initiated_private_message_multiplier =
    config.user_initiated_private_message_multiplier

  const tiers = defaultWarmupTiers()
  for (const item of warmupTierOptions) {
    tiers[item.value] = {
      ...tiers[item.value],
      ...(config.tiers?.[item.value] || {}),
    }
  }
  accountWarmupPolicyForm.tiers = tiers

  const stages = defaultWarmupStages()
  for (const item of warmupStageOptions) {
    stages[item.value] = {
      ...stages[item.value],
      ...(config.stages?.[item.value] || {}),
    }
  }
  accountWarmupPolicyForm.stages = stages
}

const fillAdDeliveryExecutionForm = (config: AdDeliveryExecutionSettings) => {
  Object.assign(adDeliveryExecutionForm, config)
}

const fillAdDeliveryThrottleForm = (config: AdDeliveryThrottleSettings) => {
  Object.assign(adDeliveryThrottleForm, config)
}

const loadAccounts = async () => {
  const payload = await accountsApi.list({ limit: 100, account_type: 'promoter' })
  accounts.value = payload.list
  if (!selectedAccountId.value && accounts.value.length > 0) {
    selectedAccountId.value = accounts.value[0].id
  }
  const bindableAccountIds = new Set(adBindingAccounts.value.map((account) => account.id))
  bindingForm.account_ids = bindingForm.account_ids.filter((accountId) =>
    bindableAccountIds.has(accountId),
  )
  if (!bindingForm.account_ids.length && selectedAccountId.value) {
    const preferredAccount = adBindingAccounts.value.find(
      (account) => account.id === selectedAccountId.value,
    )
    const fallbackAccount = preferredAccount || adBindingAccounts.value[0]
    bindingForm.account_ids = fallbackAccount ? [fallbackAccount.id] : []
  }
}

const loadTargetGroups = async () => {
  const response = await groupsApi.list({ page: 1, pageSize: 200, status: 'active' })
  targetGroups.value = response.data.data.filter(
    (group: Group) => group.status === 'active' && group.accountCount > 0,
  )
}

const loadAccountConfig = async (accountId?: number) => {
  if (!accountId) return
  accountConfigLoading.value = true
  try {
    const response = await automationApi.getAccountOperationConfig(accountId)
    fillAccountConfigForm(response.data.data)
  } finally {
    accountConfigLoading.value = false
  }
}

const accountConfigPayload = () => ({
  operation_mode: accountConfigForm.operation_mode,
  enabled: accountConfigForm.enabled,
  auto_join_enabled: accountConfigForm.auto_join_enabled,
  auto_ads_enabled: accountConfigForm.auto_ads_enabled,
  max_groups_per_day: accountConfigForm.max_groups_per_day,
  max_groups_total: accountConfigForm.max_groups_total,
  join_interval_min_seconds: accountConfigForm.join_interval_min_seconds,
  join_interval_max_seconds: accountConfigForm.join_interval_max_seconds,
  max_messages_per_day: accountConfigForm.max_messages_per_day,
  message_interval_seconds: accountConfigForm.message_interval_seconds,
  quiet_hours_start: accountConfigForm.quiet_hours_start || undefined,
  quiet_hours_end: accountConfigForm.quiet_hours_end || undefined,
  keyword_types: accountConfigForm.keyword_types,
  keyword_auto_replenish_enabled: accountConfigForm.keyword_auto_replenish_enabled,
  keyword_replenish_requires_review: accountConfigForm.keyword_replenish_requires_review,
})

const validateAccountConfigForm = () => {
  if (accountConfigForm.join_interval_max_seconds < accountConfigForm.join_interval_min_seconds) {
    ElMessage.warning('最大加群间隔不能小于最小间隔')
    return false
  }
  return true
}

const loadSchedulerConfig = async () => {
  schedulerConfigLoading.value = true
  try {
    const response = await automationApi.getAutoJoinSchedulerConfig()
    fillSchedulerConfigForm(response.data.data)
  } finally {
    schedulerConfigLoading.value = false
  }
}

const loadAdFailurePolicy = async () => {
  failurePolicyLoading.value = true
  try {
    const response = await automationApi.getAdFailurePolicy()
    fillAdFailurePolicyForm(response.data.data)
  } finally {
    failurePolicyLoading.value = false
  }
}

const loadAccountRiskGuard = async () => {
  riskGuardLoading.value = true
  try {
    const response = await automationApi.getAccountRiskGuard()
    fillAccountRiskGuardForm(response.data.data)
  } finally {
    riskGuardLoading.value = false
  }
}

const loadAccountWarmupPolicy = async () => {
  warmupPolicyLoading.value = true
  try {
    const response = await automationApi.getAccountWarmupPolicy()
    fillAccountWarmupPolicyForm(response.data.data)
  } finally {
    warmupPolicyLoading.value = false
  }
}

const loadAdDeliveryExecution = async () => {
  adExecutionLoading.value = true
  try {
    const response = await automationApi.getAdDeliveryExecution()
    fillAdDeliveryExecutionForm(response.data.data)
  } finally {
    adExecutionLoading.value = false
  }
}

const loadAdDeliveryThrottle = async () => {
  adThrottleLoading.value = true
  try {
    const response = await automationApi.getAdDeliveryThrottle()
    fillAdDeliveryThrottleForm(response.data.data)
  } finally {
    adThrottleLoading.value = false
  }
}

const selectedBindingCreatives = computed(() => {
  if (!bindingForm.creative_ids.length) return []
  const map = new Map(creatives.value.map((item) => [item.id, item]))
  return bindingForm.creative_ids.map((id) => map.get(id)).filter(Boolean) as AdCreative[]
})


const creativeById = (creativeId?: number) => {
  if (!creativeId) return undefined
  return creatives.value.find((item) => item.id === creativeId)
}

const creativePreview = (content?: string, maxLength = 72) => {
  const normalized = (content || '').replace(/\s+/g, ' ').trim()
  if (!normalized) return '-'
  return normalized.length > maxLength ? `${normalized.slice(0, maxLength)}...` : normalized
}

const sendModeText = (mode?: string) => {
  const labels: Record<string, string> = {
    after_join: '入群后',
    interval: '间隔',
    scheduled: '定时',
  }
  return labels[mode || ''] || mode || '-'
}

const resetCreativeForm = () => {
  Object.assign(creativeForm, emptyCreativeForm());
  editingCreativeId.value = null;
};

const openCreateCreative = () => {
  resetCreativeForm();
  creativeDrawerVisible.value = true;
};

const editCreative = (creative: any) => {
  editingCreativeId.value = creative.id;
  Object.assign(creativeForm, {
    name: creative.name,
    content: creative.content,
    creative_type: creative.creative_type,
    media_url: creative.media_url || "",
    link_url: creative.link_url || "",
    weight: creative.weight,
    enabled: creative.enabled,
  });
  creativeDrawerVisible.value = true;
};

const resetCampaignForm = () => {
  Object.assign(campaignForm, emptyCampaignForm());
  scheduledTimesText.value = "";
  editingCampaignId.value = null;
};

const openCreateCampaign = () => {
  resetCampaignForm();
  campaignDrawerVisible.value = true;
};

const editCampaign = (campaign: any) => {
  editingCampaignId.value = campaign.id;
  Object.assign(campaignForm, {
    name: campaign.name,
    enabled: campaign.enabled,
    status: campaign.status,
    send_mode: campaign.send_mode,
    target_group_levels: campaign.target_group_levels?.length
      ? campaign.target_group_levels
      : ["A"],
    target_group_ids: (campaign.target_group_ids || []).filter(
      (groupId: number) => targetGroupMap.value.has(groupId),
    ),
    start_at: campaign.start_at || "",
    end_at: campaign.end_at || "",
    min_wait_after_join_minutes: campaign.min_wait_after_join_minutes,
    interval_minutes: campaign.interval_minutes,
    max_sends_per_group_per_day: campaign.max_sends_per_group_per_day,
    max_sends_per_account_per_day: campaign.max_sends_per_account_per_day,
  });
  scheduledTimesText.value = campaign.scheduled_times?.join(",") || "";
  campaignDrawerVisible.value = true;
};

const resetBindingForm = (campaignId?: number) => {
  const preferredAccount = adBindingAccounts.value.find(
    (account) => account.id === selectedAccountId.value,
  );
  const fallbackAccount = preferredAccount || adBindingAccounts.value[0];
  Object.assign(bindingForm, {
    account_ids: fallbackAccount ? [fallbackAccount.id] : [],
    ad_campaign_id: campaignId,
    creative_ids: [],
    enabled: true,
    priority: 0,
  });
  creativePoolStatus.value = null;
};

const openBindingDrawer = (campaign?: any) => {
  resetBindingForm(campaign?.id);
  bindingDrawerVisible.value = true;
};

const saveAccountConfig = async () => {
  if (!selectedAccountId.value) {
    ElMessage.warning('请先选择账号')
    return
  }
  if (!validateAccountConfigForm()) return

  savingAccountConfig.value = true
  try {
    const response = await automationApi.updateAccountOperationConfig(selectedAccountId.value, accountConfigPayload())
    fillAccountConfigForm(response.data.data)
    ElMessage.success('账号自动化配置已保存')
  } finally {
    savingAccountConfig.value = false
  }
}

const saveBatchAccountConfig = async () => {
  if (!batchAccountIds.value.length) {
    ElMessage.warning('请选择要批量套用的账号')
    return
  }
  if (!validateAccountConfigForm()) return

  await ElMessageBox.confirm(
    `确认将当前账号配置套用到 ${batchAccountIds.value.length} 个账号？`,
    '批量套用配置',
    { type: 'warning' },
  )
  savingBatchAccountConfig.value = true
  try {
    const response = await automationApi.updateAccountOperationConfigsBatch({
      account_ids: batchAccountIds.value,
      config: accountConfigPayload(),
    })
    const result = response.data.data
    ElMessage.success(`已更新 ${result.updated_count} 个账号，跳过 ${result.skipped_count} 个`)
    if (selectedAccountId.value) {
      await loadAccountConfig(selectedAccountId.value)
    }
  } finally {
    savingBatchAccountConfig.value = false
  }
}

const selectAllBatchAccounts = () => {
  batchAccountIds.value = accounts.value.map((item) => item.id)
}

const clearBatchAccounts = () => {
  batchAccountIds.value = []
}

const loadDeliveryLogs = async () => {
  deliveryLogLoading.value = true
  try {
    const response = await automationApi.getDeliveryLogs({
      account_id: deliveryLogFilters.account_id,
      campaign_id: deliveryLogFilters.campaign_id,
      status: deliveryLogFilters.status || undefined,
      start_at: deliveryLogFilters.time_range?.[0],
      end_at: deliveryLogFilters.time_range?.[1],
      page: deliveryLogPagination.page,
      page_size: deliveryLogPagination.page_size,
    })
    deliveryLogs.value = response.data.data
    deliveryLogPagination.total = response.data.total || 0
    deliveryLogPagination.page = response.data.page || deliveryLogPagination.page
    deliveryLogPagination.page_size = response.data.page_size || deliveryLogPagination.page_size
  } finally {
    deliveryLogLoading.value = false
  }
}

const searchDeliveryLogs = async () => {
  deliveryLogPagination.page = 1
  await loadDeliveryLogs()
}

const resetDeliveryLogFilters = async () => {
  deliveryLogFilters.account_id = undefined
  deliveryLogFilters.campaign_id = undefined
  deliveryLogFilters.status = ''
  deliveryLogFilters.time_range = []
  deliveryLogPagination.page = 1
  await loadDeliveryLogs()
}

const handleDeliveryPageChange = async (page: number) => {
  deliveryLogPagination.page = page
  await loadDeliveryLogs()
}

const handleDeliveryPageSizeChange = async (pageSize: number) => {
  deliveryLogPagination.page_size = pageSize
  deliveryLogPagination.page = 1
  await loadDeliveryLogs()
}

const loadGroupFailovers = async () => {
  const response = await automationApi.getGroupFailoverTasks({
    status: groupFailoverStatusFilter.value || undefined,
    page_size: 100,
  })
  groupFailoverTasks.value = response.data.data
  groupFailoverSummary.value = response.data.summary || {}
  groupFailoverTotal.value = (['queued', 'joining', 'retry', 'manual_required', 'failed'] as GroupFailoverStatus[])
    .reduce((total, status) => total + (groupFailoverSummary.value[status] || 0), 0)
}

const refreshData = async () => {
  loading.value = true
  try {
    const [attemptsRes, verificationLogsRes, creativesRes, campaignsRes, bindingsRes, groupProfilesRes] = await Promise.all([
      automationApi.getAutoJoinAttempts({ limit: 30 }),
      automationApi.getAutoJoinVerificationLogs({ limit: 30 }),
      automationApi.getCreatives({ page_size: 50 }),
      automationApi.getCampaigns({ page_size: 50 }),
      automationApi.getBindings(),
      automationApi.getGroupAdProfiles(),
    ])
    const dynamicStatusRes = await automationApi.getAdDynamicStatus()
    autoJoinAttempts.value = attemptsRes.data.data
    autoJoinVerificationLogs.value = verificationLogsRes.data.data
    creatives.value = creativesRes.data.data
    campaigns.value = campaignsRes.data.data
    bindings.value = bindingsRes.data.data
    groupAdProfiles.value = groupProfilesRes.data.data
    await loadGroupFailovers()
    dynamicStatuses.value = dynamicStatusRes.data.data
    creativePoolStatus.value = null
    await loadDeliveryLogs()
  } finally {
    loading.value = false
  }
}

const refreshGroupAdProfiles = async () => {
  if (loading.value || document.visibilityState === 'hidden') return
  try {
    const response = await automationApi.getGroupAdProfiles()
    groupAdProfiles.value = response.data.data
  } catch (error) {
    console.error('Failed to refresh group advertisement profiles:', error)
  }
}

const refreshPage = async () => {
  loading.value = true;
  try {
    await Promise.all([
      loadAccounts(),
      loadTargetGroups(),
      refreshData(),
      loadSchedulerConfig(),
      loadAdFailurePolicy(),
      loadAccountRiskGuard(),
      loadAccountWarmupPolicy(),
      loadAdDeliveryExecution(),
      loadAdDeliveryThrottle(),
    ]);
    if (selectedAccountId.value) {
      await loadAccountConfig(selectedAccountId.value);
    }
  } finally {
    loading.value = false;
  }
};

const refreshAdWorkspace = async () => {
  loading.value = true;
  try {
    await Promise.all([
      loadAccounts(),
      loadTargetGroups(),
      refreshData(),
      loadAdFailurePolicy(),
      loadAdDeliveryExecution(),
      loadAdDeliveryThrottle(),
    ]);
  } finally {
    loading.value = false;
  }
};

const runTask = async (name: string, fn: () => Promise<any>) => {
  running.value = name
  try {
    const response = await fn()
    lastResult.value = response.data.data
    ElMessage.success(lastResult.value?.queued ? '任务已下发' : '任务已执行')
    await refreshData()
    if (selectedAccountId.value) {
      await loadAccountConfig(selectedAccountId.value)
    }
  } finally {
    running.value = ''
  }
}

const runKeywordReplenish = () => {
  runTask('keywords', () => automationApi.replenishKeywords({ auto_approve: keywordReplenishForm.auto_approve }))
}

const runAutoJoin = () => {
  runTask('autoJoin', () => automationApi.runAutoJoin({ ...autoJoinForm }))
}

const runGroupFailover = () => {
  runTask('groupFailover', () =>
    automationApi.runGroupFailover({
      max_tasks: groupFailoverForm.max_tasks,
      dry_run: groupFailoverForm.dry_run,
    }),
  )
}

const assignSelectedGroupFailover = async () => {
  const targetAccountIds = [...groupFailoverForm.target_account_ids]
  if (!targetAccountIds.length) {
    ElMessage.warning('请先选择接管账号')
    return
  }
  try {
    await ElMessageBox.confirm(
      '将待接管群组分配给 ' + targetAccountIds.length + ' 个所选账号，并按负载均衡逐群接管，确认继续？',
      '一键分配接管账号',
      { type: 'warning', confirmButtonText: '确认分配', cancelButtonText: '取消' },
    )
    await runTask('groupFailoverAssign', () =>
      automationApi.runGroupFailover({
        max_tasks: groupFailoverForm.max_tasks,
        dry_run: groupFailoverForm.dry_run,
        target_account_ids: targetAccountIds,
      }),
    )
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      console.error('Failed to assign failover accounts:', error)
    }
  }
}

const retryGroupFailover = async (task: any) => {
  await automationApi.retryGroupFailoverTask(task.id)
  ElMessage.success('\u6062\u590d\u4efb\u52a1\u5df2\u91cd\u65b0\u6392\u961f')
  await loadGroupFailovers()
}

const cancelGroupFailover = async (task: any) => {
  await ElMessageBox.confirm(
    `\u786e\u8ba4\u53d6\u6d88\u300c${task.group_title || task.telegram_group_id}\u300d\u7684\u6062\u590d\u4efb\u52a1\uff1f`,
    '\u53d6\u6d88\u6062\u590d',
    { type: 'warning' },
  )
  await automationApi.cancelGroupFailoverTask(task.id)
  ElMessage.success('\u6062\u590d\u4efb\u52a1\u5df2\u53d6\u6d88')
  await loadGroupFailovers()
}

const groupFailoverStatusText = (value: GroupFailoverStatus) =>
  groupFailoverStatusOptions.find((item) => item.value === value)?.label || value

const groupFailoverStatusType = (value: GroupFailoverStatus) => {
  if (value === 'succeeded') return 'success'
  if (value === 'failed') return 'danger'
  if (value === 'retry' || value === 'manual_required') return 'warning'
  if (value === 'joining') return 'primary'
  return 'info'
}

const formatTimestamp = (value?: string) => (value ? value.replace('T', ' ').slice(0, 19) : '-')

const runAds = async () => {
  await runTask("ads", () => automationApi.runAds({ ...adRunForm }));
  adRunDialogVisible.value = false;
};

const saveCreative = async () => {
  if (!creativeForm.name || !creativeForm.content) {
    ElMessage.warning("请填写广告名称和内容");
    return;
  }
  savingCreative.value = true;
  try {
    if (editingCreativeId.value) {
      await automationApi.updateCreative(editingCreativeId.value, {
        ...creativeForm,
      });
      ElMessage.success("广告素材已更新");
    } else {
      await automationApi.createCreative({ ...creativeForm });
      ElMessage.success("广告素材已创建");
    }
    creativeDrawerVisible.value = false;
    resetCreativeForm();
    await refreshData();
  } finally {
    savingCreative.value = false;
  }
};

const toggleCreative = async (creative: any) => {
  await automationApi.updateCreative(creative.id, {
    enabled: !creative.enabled,
  });
  ElMessage.success(creative.enabled ? "素材已停用" : "素材已启用");
  await refreshData();
};

const deleteCreative = async (creative: any) => {
  await ElMessageBox.confirm(
    `确认删除素材「${creative.name}」？相关绑定会失去该素材。`,
    "删除素材",
    {
      type: "warning",
    },
  );
  await automationApi.deleteCreative(creative.id);
  if (editingCreativeId.value === creative.id) resetCreativeForm();
  ElMessage.success("广告素材已删除");
  await refreshData();
};

const cleanupInvalidCreatives = async () => {
  const response = await automationApi.cleanupInvalidCreatives();
  const count = response.data.data.disabled_count;
  ElMessage.success(
    count > 0 ? `已停用 ${count} 条异常素材` : "未发现异常素材",
  );
  await refreshData();
};

const ensureCreativePool = async () => {
  if (!bindingForm.account_ids.length || !bindingForm.ad_campaign_id) {
    ElMessage.warning("请先选择账号和广告计划");
    return;
  }

  const poolStatuses = [];
  for (const accountId of bindingForm.account_ids) {
    const response = await automationApi.ensureCreativePool({
      account_id: accountId,
      ad_campaign_id: bindingForm.ad_campaign_id,
      min_pool_size: 3,
      generate_count: 3,
    });
    poolStatuses.push(response.data.data);
  }

  const summary: CreativePoolSummary = {
    account_count: poolStatuses.length,
    pool_size: Math.min(...poolStatuses.map((item) => item.pool_size)),
    created_count: poolStatuses.reduce(
      (total, item) => total + item.created_count,
      0,
    ),
    creative_ids: poolStatuses.flatMap((item) => item.creative_ids),
  };
  await refreshData();
  creativePoolStatus.value = summary;
  ElMessage.success(
    `已检查 ${summary.account_count} 个账号的素材池，共新增 ${summary.created_count} 条`,
  );
};

const saveCampaign = async () => {
  if (!campaignForm.name) {
    ElMessage.warning("请填写广告计划名称");
    return;
  }
  const scheduledTimes = parseScheduledTimes();
  if (campaignForm.send_mode === "scheduled" && !scheduledTimes.length) {
    ElMessage.warning("请至少填写一个定时时点");
    return;
  }
  const payload = {
    ...campaignForm,
    start_at: campaignForm.start_at || undefined,
    end_at: campaignForm.end_at || undefined,
    scheduled_times: scheduledTimes,
  };
  savingCampaign.value = true;
  try {
    if (editingCampaignId.value) {
      await automationApi.updateCampaign(editingCampaignId.value, payload);
      ElMessage.success("广告计划已更新");
    } else {
      await automationApi.createCampaign(payload);
      ElMessage.success("广告计划已创建");
    }
    campaignDrawerVisible.value = false;
    resetCampaignForm();
    await refreshData();
  } finally {
    savingCampaign.value = false;
  }
};

const openTargetGroupDialog = () => {
  Object.assign(targetGroupForm, {
    groupLink: "",
    accountId: selectedAccountId.value,
  });
  targetGroupDialogVisible.value = true;
};

const saveTargetGroup = async () => {
  const groupLink = targetGroupForm.groupLink.trim();
  if (!groupLink) {
    ElMessage.warning("请输入 Telegram 群链接");
    return;
  }
  if (!targetGroupForm.accountId) {
    ElMessage.warning("请选择执行入群的推广账号");
    return;
  }

  savingTargetGroup.value = true;
  try {
    const response = await groupsApi.joinByLink({
      groupLink,
      accountId: targetGroupForm.accountId,
    });
    const group = response.data.data;
    await loadTargetGroups();
    if (!campaignForm.target_group_ids.includes(group.id)) {
      campaignForm.target_group_ids.push(group.id);
    }
    targetGroupDialogVisible.value = false;
    const groupName = group.title || group.username || group.chatId;
    if (targetGroupAccountIsAdOnly.value) {
      if (group.adDeliveryAccountId === targetGroupForm.accountId) {
        ElMessage.success(`已由专用账号接管 ${groupName}，普通账号退群处理已执行`);
      } else {
        ElMessage.warning(`已加入 ${groupName}，但广告许可未达到接管条件，未执行移交和退群`);
      }
    } else {
      ElMessage.success(`已加入并添加 ${groupName}`);
    }
  } finally {
    savingTargetGroup.value = false;
  }
};

const toggleCampaign = async (campaign: any) => {
  await automationApi.updateCampaign(campaign.id, {
    enabled: !campaign.enabled,
    status: campaign.enabled ? "paused" : "active",
  });
  ElMessage.success(campaign.enabled ? "广告计划已停止" : "广告计划已启动");
  await refreshData();
};

const deleteCampaign = async (campaign: any) => {
  await ElMessageBox.confirm(
    `确认删除计划「${campaign.name}」？计划下的绑定也会删除。`,
    "删除计划",
    {
      type: "warning",
    },
  );
  await automationApi.deleteCampaign(campaign.id);
  if (editingCampaignId.value === campaign.id) resetCampaignForm();
  ElMessage.success("广告计划已删除");
  await refreshData();
};

const createBinding = async () => {
  if (!bindingForm.account_ids.length || !bindingForm.ad_campaign_id) {
    ElMessage.warning("请选择账号和广告计划");
    return;
  }
  if (!bindingForm.creative_ids.length) {
    ElMessage.warning("请至少选择一个素材");
    return;
  }
  const response = await automationApi.createBindingsBatch({
    account_ids: bindingForm.account_ids,
    ad_campaign_id: bindingForm.ad_campaign_id,
    creative_ids: bindingForm.creative_ids,
    enabled: bindingForm.enabled,
    priority: bindingForm.priority,
  });
  const expectedCount =
    bindingForm.account_ids.length * bindingForm.creative_ids.length;
  const createdCount = response.data.data.length;
  const existingCount = expectedCount - createdCount;
  ElMessage.success(
    existingCount > 0
      ? `已创建 ${createdCount} 条绑定，跳过 ${existingCount} 条已有绑定`
      : `已为 ${bindingForm.account_ids.length} 个账号创建 ${createdCount} 条绑定`,
  );
  Object.assign(bindingForm, {
    account_ids: selectedAccountId.value ? [selectedAccountId.value] : [],
    ad_campaign_id: undefined,
    creative_ids: [],
    enabled: true,
    priority: 0,
  });
  bindingDrawerVisible.value = false;
  await refreshData();
};

const toggleBinding = async (binding: AccountAdBinding) => {
  await automationApi.updateBinding(binding.id, { enabled: !binding.enabled });
  ElMessage.success(binding.enabled ? "绑定已停用" : "绑定已启用");
  await refreshData();
};

const deleteBinding = async (binding: AccountAdBinding) => {
  await ElMessageBox.confirm("确认删除这条账号广告绑定？", "删除绑定", {
    type: "warning",
  });
  await automationApi.deleteBinding(binding.id);
  ElMessage.success("绑定已删除");
  await refreshData();
};

const toggleBindingGroup = async (group: any) => {
  const shouldEnable = group.enabledCount === 0;
  await Promise.all(
    group.bindings.map((binding: AccountAdBinding) =>
      automationApi.updateBinding(binding.id, { enabled: shouldEnable }),
    ),
  );
  ElMessage.success(shouldEnable ? "账号计划绑定已启用" : "账号计划绑定已停用");
  await refreshData();
};

const deleteBindingGroup = async (group: any) => {
  await ElMessageBox.confirm(
    `确认删除 ${accountLabel(group.accountId)} 在该计划下的 ${group.bindings.length} 条素材绑定？`,
    "删除账号计划绑定",
    { type: "warning" },
  );
  await Promise.all(
    group.bindings.map((binding: AccountAdBinding) =>
      automationApi.deleteBinding(binding.id),
    ),
  );
  ElMessage.success("账号计划绑定已删除");
  await refreshData();
};

const openAccountFromAds = (accountId: number) => {
  selectedAccountId.value = accountId;
  activeTab.value = "accounts";
};

const openDeliveryFromAds = (campaignId?: number, accountId?: number) => {
  deliveryLogFilters.campaign_id = campaignId;
  deliveryLogFilters.account_id = accountId;
  deliveryLogPagination.page = 1;
  activeTab.value = "delivery";
  loadDeliveryLogs();
};

const statusType = (status: string) => {
  if (status === 'success') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'skipped') return 'warning'
  return 'info'
}

const tierType = (tier: string) => {
  if (tier === 'hot') return 'success'
  if (tier === 'normal') return 'primary'
  if (tier === 'conservative') return 'warning'
  if (tier === 'cooldown') return 'warning'
  if (tier === 'paused') return 'danger'
  return 'info'
}

const tierText = (tier: string) => {
  const labels: Record<string, string> = {
    hot: '高频',
    normal: '正常',
    conservative: '保守',
    cooldown: '冷却',
    paused: '暂停',
  }
  return labels[tier] || tier || '-'
}

const groupPolicyType = (mode?: string) => {
  if (mode === "forbidden") return "danger";
  if (mode === "unknown" || mode === "unknown_probe" || mode === "approval_required") return "warning";
  if (mode === "soft_ad_trial") return "info";
  if (mode === "soft_ad_allowed" || mode === "high_volume_ad_allowed") return "success";
  return "info";
};

const groupPolicyText = (mode?: string) => {
  const labels: Record<string, string> = {
    forbidden: "禁止投放",
    unknown: "许可未知",
    unknown_probe: "广告检测中",
    approval_required: "待人工审批",
    soft_ad_trial: "试投中",
    soft_ad_allowed: "允许软广",
    high_volume_ad_allowed: "高量许可",
  };
  return labels[mode || ""] || mode || "-";
};

const triggerGroupAdPolicyProbe = async (profile: any) => {
  if (groupPolicyProbeRunning.value !== null) return
  try {
    await ElMessageBox.confirm(
      "确认向「" + (profile.group_title || profile.telegram_group_id) + "」发送 1 条无链接广告检测？检测消息会观察 24 小时，若被删除将标记为禁止投放并退出该群。",
      "发送广告检测",
      { type: "warning", confirmButtonText: "发送广告探针", cancelButtonText: "取消" },
    )
  } catch (error) {
    if (error === "cancel" || error === "close") return
    throw error
  }

  groupPolicyProbeRunning.value = profile.group_id
  try {
    await automationApi.triggerGroupAdPolicyProbe(profile.group_id)
    ElMessage.success("广告检测已发送，等待 24 小时结果")
    await refreshData()
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    ElMessage.error(detail ? "检测未发送：" + detail : "广告检测发送失败")
  } finally {
    groupPolicyProbeRunning.value = null
  }
}

const groupTierType = (tier?: string) => {
  if (tier === "blocked") return "danger";
  if (tier === "observing") return "warning";
  if (tier === "trial") return "info";
  if (tier === "validated" || tier === "stable") return "success";
  if (tier === "high" || tier === "premium") return "primary";
  return "info";
};

const groupTierText = (tier?: string) => {
  const labels: Record<string, string> = {
    blocked: "封禁",
    observing: "观察",
    trial: "试投",
    validated: "已验证",
    stable: "稳定",
    low: "低量",
    medium: "中量",
    high: "高量",
    premium: "优质",
  };
  return labels[tier || ""] || tier || "-";
};
const businessStageType = (stage: string) => {
  if (stage === 'hot') return 'success'
  if (stage === 'normal') return 'primary'
  if (stage === 'new') return 'info'
  if (stage === 'cooldown') return 'warning'
  return 'info'
}

const businessStageText = (stage: string) => {
  const labels: Record<string, string> = {
    new: '新号',
    normal: '正常',
    hot: '热号',
    cooldown: '冷却',
  }
  return labels[stage] || stage || '-'
}

const riskLevelType = (level?: string) => {
  if (level === 'normal') return 'success'
  if (level === 'watch' || level === 'limited') return 'warning'
  if (level === 'frozen' || level === 'quarantined') return 'danger'
  return 'info'
}

const riskLevelText = (level?: string) => {
  const labels: Record<string, string> = {
    normal: '正常',
    watch: '观察',
    limited: '限流',
    frozen: '冻结',
    quarantined: '隔离',
  }
  return labels[level || ''] || level || '-'
}

const warmupStageType = (stage: string) => {
  if (stage === 'normal') return 'success'
  if (stage === 'ramp' || stage === 'soft') return 'warning'
  if (stage === 'cooldown') return 'danger'
  return 'info'
}

const warmupStageText = (stage: string) => {
  const labels: Record<string, string> = {
    observe: '观察',
    seed: '起步',
    soft: '低频',
    ramp: '提量',
    normal: '正常',
    cooldown: '冷却',
  }
  return labels[stage] || stage || '-'
}

const verificationSuccessType = (success?: boolean) => {
  if (success === true) return 'success'
  if (success === false) return 'danger'
  return 'info'
}

const verificationSuccessText = (success?: boolean) => {
  if (success === true) return '动作成功'
  if (success === false) return '动作失败'
  return '未确认'
}

const verificationSourceText = (source?: string) => {
  if (source === 'ai') return 'AI'
  if (source === 'local') return '规则'
  if (source === 'fallback') return '兜底'
  return '未知'
}

const verificationActionText = (action?: string) => {
  const labels: Record<string, string> = {
    click_button: '点击按钮',
    send_answer: '发送答案',
    wait: '等待审批',
    leave: '退出',
    manual: '人工',
    skip: '跳过',
    ai_timeout: 'AI超时',
  }
  return labels[action || ''] || action || '-'
}

watch(selectedAccountId, async (accountId) => {
  if (!accountId) return
  const selectedAccount = accounts.value.find((account) => account.id === accountId)
  bindingForm.account_ids = selectedAccount?.status === 'banned' ? [] : [accountId]
  await loadAccountConfig(accountId)
})

onMounted(refreshPage)

onMounted(() => {
  groupProfilesRefreshTimer = window.setInterval(refreshGroupAdProfiles, 30_000)
})

onBeforeUnmount(() => {
  if (groupProfilesRefreshTimer !== null) {
    window.clearInterval(groupProfilesRefreshTimer)
    groupProfilesRefreshTimer = null
  }
})
</script>

<template>
  <div class="automation-page">
    <div class="page-header">
      <h2 class="page-title">自动化管理</h2>
      <el-button :loading="loading" @click="refreshPage">
        <el-icon><Refresh /></el-icon>
        刷新
      </el-button>
    </div>

    <el-row :gutter="16" class="summary-row">
      <el-col v-for="item in resultSummary" :key="item.label" :span="4">
        <el-card shadow="never" class="metric-card">
          <div class="metric-label">{{ item.label }}</div>
          <div class="metric-value">{{ item.value }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-tabs v-model="activeTab" class="automation-tabs">
      <el-tab-pane label="加群" name="join">
        <div class="control-grid">
          <el-card shadow="never">
            <template #header>搜群关键词补充</template>
            <el-form label-width="120px">
              <el-form-item label="补充后状态">
                <el-switch
                  v-model="keywordReplenishForm.auto_approve"
                  active-text="免审核直接启用"
                  inactive-text="进入搜群词审核"
                />
              </el-form-item>
              <el-button type="primary" :loading="running === 'keywords'" @click="runKeywordReplenish">
                <el-icon><VideoPlay /></el-icon>
                执行搜群补词
              </el-button>
            </el-form>
          </el-card>

          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span>自动加群</span>
                <div>
                  <el-button :loading="schedulerConfigLoading" @click="loadSchedulerConfig">
                    <el-icon><Refresh /></el-icon>
                    读取配置
                  </el-button>
                  <el-button type="primary" @click="goGrowthConfig('join')">
                    <el-icon><Setting /></el-icon>
                    去增长驾驶舱配置
                  </el-button>
                </div>
              </div>
            </template>
            <div v-loading="schedulerConfigLoading">
              <div class="single-entry-note">
                入群扫描、群检测、标题黑名单、满群清理等全局策略已收口到增长驾驶舱。
              </div>
              <div class="policy-summary">
                <div v-for="item in schedulerSummary" :key="item.label" class="policy-summary-item">
                  <span class="policy-label">{{ item.label }}</span>
                  <el-tag v-if="item.type" :type="item.type" effect="plain">{{ item.value }}</el-tag>
                  <span v-else class="policy-value">{{ item.value }}</span>
                </div>
              </div>
              <el-divider />
              <el-form label-width="120px">
              <el-form-item label="账号数">
                <el-input-number v-model="autoJoinForm.max_accounts" :min="1" :max="100" />
              </el-form-item>
              <el-form-item label="每号关键词">
                <el-input-number v-model="autoJoinForm.keywords_per_account" :min="1" :max="50" />
              </el-form-item>
              <el-form-item label="每词群数">
                <el-input-number v-model="autoJoinForm.max_groups_per_keyword" :min="1" :max="50" />
              </el-form-item>
              <el-form-item label="Dry Run">
                <el-switch v-model="autoJoinForm.dry_run" />
              </el-form-item>
              <el-button type="primary" :loading="running === 'autoJoin'" @click="runAutoJoin">
                <el-icon><VideoPlay /></el-icon>
                执行加群任务
              </el-button>
              </el-form>
            </div>
          </el-card>

        <el-card shadow="never" class="failover-card">
          <template #header>
            <div class="card-header">
              <div class="failover-heading">
                <span>&#x5C01;&#x53F7;&#x7FA4;&#x8D44;&#x6E90;&#x6062;&#x590D;</span>
                <el-tag type="warning" effect="plain">&#x5F85;&#x5904;&#x7406; {{ groupFailoverTotal }}</el-tag>
                <el-tag type="success" effect="plain">&#x5DF2;&#x6062;&#x590D; {{ groupFailoverSummary.succeeded || 0 }}</el-tag>
                <el-tag v-if="groupFailoverSummary.manual_required" type="warning" effect="plain">
                  &#x4EBA;&#x5DE5; {{ groupFailoverSummary.manual_required }}
                </el-tag>
              </div>
              <el-select
                v-model="groupFailoverStatusFilter"
                clearable
                placeholder="&#x5168;&#x90E8;&#x72B6;&#x6001;"
                class="status-filter"
                @change="loadGroupFailovers"
              >
                <el-option
                  v-for="item in groupFailoverStatusOptions"
                  :key="item.value"
                  :label="item.label"
                  :value="item.value"
                />
              </el-select>
            </div>
          </template>

          <el-form inline class="failover-toolbar">
            <el-form-item label="接管账号">
              <el-select
                v-model="groupFailoverForm.target_account_ids"
                multiple
                filterable
                clearable
                collapse-tags
                collapse-tags-tooltip
                placeholder="留空自动均衡"
                style="width: 320px"
              >
                <el-option
                  v-for="account in failoverTargetAccounts"
                  :key="account.id"
                  :label="account.display_name || account.phone || account.session_name || account.identifier"
                  :value="account.id"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="单轮任务数">
              <el-input-number v-model="groupFailoverForm.max_tasks" :min="1" :max="100" />
            </el-form-item>
            <el-form-item label="Dry Run">
              <el-switch v-model="groupFailoverForm.dry_run" />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="running === 'groupFailover'"
                @click="runGroupFailover"
              >
                <el-icon><VideoPlay /></el-icon>
                执行恢复扫描
              </el-button>
              <el-button
                type="warning"
                :loading="running === 'groupFailoverAssign'"
                :disabled="!groupFailoverForm.target_account_ids.length"
                @click="assignSelectedGroupFailover"
              >
                <el-icon><Select /></el-icon>
                一键分配所选账号
              </el-button>
            </el-form-item>
          </el-form>

          <el-table :data="groupFailoverTasks" stripe max-height="480">
            <el-table-column label="&#x7FA4;" min-width="180">
              <template #default="{ row }">
                <div>{{ row.group_title || row.telegram_group_id }}</div>
                <small v-if="row.group_username">@{{ row.group_username }}</small>
              </template>
            </el-table-column>
            <el-table-column label="&#x6E90;&#x8D26;&#x53F7;" min-width="150">
              <template #default="{ row }">{{ row.source_account_label || `#${row.source_account_id}` }}</template>
            </el-table-column>
            <el-table-column label="&#x63A5;&#x7BA1;&#x8D26;&#x53F7;" min-width="150">
              <template #default="{ row }">{{ row.target_account_label || (row.target_account_id ? `#${row.target_account_id}` : "-") }}</template>
            </el-table-column>
            <el-table-column label="&#x72B6;&#x6001;" width="120">
              <template #default="{ row }">
                <el-tag :type="groupFailoverStatusType(row.status)" effect="plain">
                  {{ groupFailoverStatusText(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="reason" label="&#x539F;&#x56E0;" min-width="170" show-overflow-tooltip />
            <el-table-column prop="attempt_count" label="&#x5C1D;&#x8BD5;" width="70" />
            <el-table-column label="&#x4E0B;&#x6B21;&#x5904;&#x7406;" width="180">
              <template #default="{ row }">{{ formatTimestamp(row.next_retry_at) }}</template>
            </el-table-column>
            <el-table-column label="&#x64CD;&#x4F5C;" width="150" fixed="right">
              <template #default="{ row }">
                <el-button
                  v-if="!['succeeded', 'cancelled', 'joining'].includes(row.status)"
                  link
                  type="primary"
                  @click="retryGroupFailover(row)"
                >
                  <el-icon><Refresh /></el-icon>
                  &#x91CD;&#x8BD5;
                </el-button>
                <el-button
                  v-if="!['succeeded', 'cancelled'].includes(row.status)"
                  link
                  type="danger"
                  @click="cancelGroupFailover(row)"
                >
                  <el-icon><Close /></el-icon>
                  &#x53D6;&#x6D88;
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        </div>
      </el-tab-pane>

      <el-tab-pane label="账号" name="accounts">
        <el-card shadow="never" class="dynamic-status-card">
          <template #header>
            <div class="card-header">
              <span>动态频率状态</span>
              <el-button :loading="loading" @click="refreshData">
                <el-icon><Refresh /></el-icon>
                刷新状态
              </el-button>
            </div>
          </template>
          <el-table :data="dynamicStatuses" size="small" border>
            <el-table-column label="账号" min-width="150">
              <template #default="{ row }">{{ row.account_label || accountLabel(row.account_id) }}</template>
            </el-table-column>
            <el-table-column label="档位" width="90">
              <template #default="{ row }">
                <el-tag :type="tierType(row.tier)">{{ tierText(row.tier) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="业务态" width="90">
              <template #default="{ row }">
                <el-tag :type="businessStageType(row.business_stage)">{{ businessStageText(row.business_stage) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="暖号" width="120">
              <template #default="{ row }">
                <el-tag :type="warmupStageType(row.warmup_stage)" effect="plain">{{ warmupStageText(row.warmup_stage) }}</el-tag>
                <div class="muted-text">{{ row.managed_age_days }}天 / 余{{ row.warmup_remaining_days }}天</div>
              </template>
            </el-table-column>
            <el-table-column prop="health_score" label="健康分" width="90" />
            <el-table-column label="风险" min-width="150">
              <template #default="{ row }">
                <el-tag :type="row.risk_level === 'normal' ? 'success' : 'warning'">{{ row.risk_level }}</el-tag>
                <span class="muted-text"> {{ row.risk_score }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="dynamic_daily_limit" label="广告日额" width="100" />
            <el-table-column prop="probe_based_daily_limit" label="Probe额" width="90" />
            <el-table-column prop="dynamic_run_limit" label="单轮额" width="90" />
            <el-table-column prop="join_dynamic_daily_limit" label="加群日额" width="100" />
            <el-table-column label="探测" min-width="220">
              <template #default="{ row }">
                <el-tag type="success">成功6h {{ row.recent_probe_success_6h }}</el-tag>
                <el-tag type="warning">失败6h {{ row.recent_probe_failed_6h }}</el-tag>
                <el-tag type="primary">可投 {{ row.ad_eligible_groups }}</el-tag>
                <el-tag type="info">待探 {{ row.pending_probe_groups }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="倍率" min-width="170">
              <template #default="{ row }">
                Probe {{ row.probe_factor }} / 暖 {{ row.warmup_action_multiplier }} / 时 {{ row.time_window_multiplier }}
              </template>
            </el-table-column>
            <el-table-column label="质量" min-width="170">
              <template #default="{ row }">
                写 {{ row.writable_rate }} / 探 {{ row.probe_success_rate_24h }} / 群 {{ row.average_group_quality_score }}
              </template>
            </el-table-column>
            <el-table-column label="24h发送" min-width="150">
              <template #default="{ row }">
                成功 {{ row.success_24h }} / 失败 {{ row.failed_24h }}
              </template>
            </el-table-column>
            <el-table-column label="群控/账号/临时" min-width="150">
              <template #default="{ row }">
                {{ row.group_control_failed_24h }} / {{ row.account_failed_24h }} / {{ row.transient_failed_24h }}
              </template>
            </el-table-column>
            <el-table-column label="最近原因" min-width="260">
              <template #default="{ row }">
                <span class="error-text">{{ row.risk_reason || row.recent_errors?.[0]?.error || '-' }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never" class="risk-guard-card">
          <template #header>
            <div class="card-header">
              <span>全局账号风控（单号额度）</span>
              <div>
                <el-button :loading="riskGuardLoading" @click="loadAccountRiskGuard">
                  <el-icon><Refresh /></el-icon>
                  读取配置
                </el-button>
                <el-button type="primary" @click="goGrowthConfig('risk')">
                  <el-icon><Setting /></el-icon>
                  去增长驾驶舱配置
                </el-button>
              </div>
            </div>
          </template>

          <div v-loading="riskGuardLoading">
            <div class="single-entry-note">
              统一配置所有账号的单号动作额度、冷却、Redis 异常策略；额度按账号独立计数。
            </div>
            <div class="policy-summary">
              <div v-for="item in riskSummary" :key="item.label" class="policy-summary-item">
                <span class="policy-label">{{ item.label }}</span>
                <el-tag v-if="item.type" :type="item.type" effect="plain">{{ item.value }}</el-tag>
                <span v-else class="policy-value">{{ item.value }}</span>
              </div>
            </div>
          </div>
        </el-card>

        <el-card shadow="never" class="warmup-policy-card">
          <template #header>
            <div class="card-header">
              <span>托管暖号策略</span>
              <div>
                <el-button :loading="warmupPolicyLoading" @click="loadAccountWarmupPolicy">
                  <el-icon><Refresh /></el-icon>
                  读取配置
                </el-button>
                <el-button type="primary" @click="goGrowthConfig('warmup')">
                  <el-icon><Setting /></el-icon>
                  去增长驾驶舱配置
                </el-button>
              </div>
            </div>
          </template>

          <div v-loading="warmupPolicyLoading">
            <div class="single-entry-note">
              暖号天数、阶段倍率、主动私聊限制统一在增长驾驶舱维护。
            </div>
            <div class="policy-summary">
              <div v-for="item in warmupSummary" :key="item.label" class="policy-summary-item">
                <span class="policy-label">{{ item.label }}</span>
                <el-tag v-if="item.type" :type="item.type" effect="plain">{{ item.value }}</el-tag>
                <span v-else class="policy-value">{{ item.value }}</span>
              </div>
            </div>
          </div>
        </el-card>

        <el-card shadow="never">
          <div class="account-toolbar">
            <el-select v-model="selectedAccountId" placeholder="选择账号" filterable class="account-select">
              <el-option
                v-for="item in accounts"
                :key="item.id"
                :label="accountLabel(item.id)"
                :value="item.id"
              />
            </el-select>
            <el-button :loading="accountConfigLoading" @click="loadAccountConfig(selectedAccountId)">
              <el-icon><Select /></el-icon>
              读取配置
            </el-button>
            <el-button type="primary" :loading="savingAccountConfig" @click="saveAccountConfig">
              <el-icon><Setting /></el-icon>
              保存配置
            </el-button>
          </div>

          <div class="batch-toolbar">
            <el-select
              v-model="batchAccountIds"
              multiple
              filterable
              collapse-tags
              collapse-tags-tooltip
              placeholder="选择要套用当前配置的账号"
              class="batch-account-select"
            >
              <el-option
                v-for="item in accounts"
                :key="item.id"
                :label="accountLabel(item.id)"
                :value="item.id"
              />
            </el-select>
            <el-button @click="selectAllBatchAccounts">全选账号</el-button>
            <el-button @click="clearBatchAccounts">清空</el-button>
            <el-button type="primary" :loading="savingBatchAccountConfig" @click="saveBatchAccountConfig">
              <el-icon><Setting /></el-icon>
              批量套用当前配置
            </el-button>
          </div>

          <el-form v-loading="accountConfigLoading" label-width="150px" class="account-config-form">
            <div class="account-config-grid">
              <el-form-item label="账号模式">
                <el-select v-model="accountConfigForm.operation_mode">
                  <el-option label="增长运营" value="growth" />
                  <el-option label="手动投放专用" value="ad_only" />
                </el-select>
              </el-form-item>
              <el-form-item label="启用配置">
                <el-switch v-model="accountConfigForm.enabled" />
              </el-form-item>
              <el-form-item label="自动加群">
                <el-switch v-model="accountConfigForm.auto_join_enabled" :disabled="isAdOnlyAccount" />
              </el-form-item>
              <el-form-item :label="isAdOnlyAccount ? '指定群广告投放' : '账号自动投放广告'">
                <el-switch v-model="accountConfigForm.auto_ads_enabled" />
              </el-form-item>
              <el-form-item label="每日最大加群数">
                <el-input-number v-model="accountConfigForm.max_groups_per_day" :min="0" :max="1000" />
              </el-form-item>
              <el-form-item label="账号总群上限">
                <el-input-number v-model="accountConfigForm.max_groups_total" :min="0" :max="10000" />
              </el-form-item>
              <el-form-item label="加群最小间隔(秒)">
                <el-input-number v-model="accountConfigForm.join_interval_min_seconds" :min="60" :max="86400" />
              </el-form-item>
              <el-form-item label="加群最大间隔(秒)">
                <el-input-number v-model="accountConfigForm.join_interval_max_seconds" :min="60" :max="86400" />
              </el-form-item>
              <el-form-item label="每日消息上限">
                <el-input-number v-model="accountConfigForm.max_messages_per_day" :min="0" :max="20000" />
              </el-form-item>
              <el-form-item label="消息发送间隔(秒)">
                <el-input-number v-model="accountConfigForm.message_interval_seconds" :min="1" :max="86400" />
              </el-form-item>
              <el-form-item label="关键词不足自动补充">
                <el-switch v-model="accountConfigForm.keyword_auto_replenish_enabled" :disabled="isAdOnlyAccount" />
              </el-form-item>
              <el-form-item label="补充后需要审核">
                <el-switch v-model="accountConfigForm.keyword_replenish_requires_review" />
              </el-form-item>
              <el-form-item label="免打扰开始">
                <el-input v-model="accountConfigForm.quiet_hours_start" placeholder="例如 23:00" />
              </el-form-item>
              <el-form-item label="免打扰结束">
                <el-input v-model="accountConfigForm.quiet_hours_end" placeholder="例如 08:00" />
              </el-form-item>
              <el-form-item label="风控等级">
                <div class="readonly-status">
                  <el-tag :type="riskLevelType(selectedDynamicStatus?.risk_level || accountConfigForm.risk_level)">
                    {{ riskLevelText(selectedDynamicStatus?.risk_level || accountConfigForm.risk_level) }}
                  </el-tag>
                  <span v-if="selectedDynamicStatus" class="muted-text">分数 {{ selectedDynamicStatus.risk_score }}</span>
                </div>
              </el-form-item>
              <el-form-item label="业务状态">
                <el-tag :type="businessStageType(accountConfigForm.business_stage)">
                  {{ businessStageText(accountConfigForm.business_stage) }}
                </el-tag>
              </el-form-item>
            </div>

            <el-form-item label="搜群关键词类型">
              <el-checkbox-group v-model="accountConfigForm.keyword_types">
                <el-checkbox v-for="item in keywordTypeOptions" :key="item.value" :value="item.value">
                  {{ item.label }}
                </el-checkbox>
              </el-checkbox-group>
            </el-form-item>

            <el-form-item label="关键词来源说明">
              <div class="form-helper">
                搜群关键词类型与关键词管理中的“加群搜索词”完全一致。关键词不足时，若已开启自动补充且设置为免审核，系统会自动补充并立即供加群任务使用。
              </div>
            </el-form-item>

            <el-form-item label="下次允许加群时间">
              <el-tag type="info">{{ accountConfigForm.next_join_after || '未设置' }}</el-tag>
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="广告" name="ads">
        <div v-loading="loading" class="ad-workbench">
                    <section class="ad-command-deck">
            <div class="ad-command-main">
              <div class="ad-command-topline">
                <div class="ad-command-title">
                  <div class="ad-command-kicker">
                    <span
                      class="ad-status-dot"
                      :class="adDeliveryExecutionForm.enabled ? 'is-live' : 'is-off'"
                    ></span>
                    AD DELIVERY CONTROL
                  </div>
                  <h3>
                    {{
                      adDeliveryExecutionForm.enabled
                        ? "自动投放运行中"
                        : "自动投放已关闭"
                    }}
                  </h3>
                  <p>从计划到账号、群资格和素材，一次看清每个投放环节。</p>
                </div>
                <div class="ad-header-actions">
                  <el-button :loading="loading" @click="refreshAdWorkspace">
                    <el-icon><Refresh /></el-icon>
                    刷新
                  </el-button>
                  <el-button @click="openDeliveryFromAds()">
                    <el-icon><Document /></el-icon>
                    发送记录
                  </el-button>
                  <el-button @click="goGrowthConfig('ads')">
                    <el-icon><Setting /></el-icon>
                    策略
                  </el-button>
                  <el-button type="primary" @click="adRunDialogVisible = true">
                    <el-icon><VideoPlay /></el-icon>
                    手动执行
                  </el-button>
                </div>
              </div>

              <div class="ad-flow-grid" aria-label="广告投放流程">
                <button
                  type="button"
                  class="ad-flow-step"
                  :class="{ active: adWorkspaceView === 'campaigns' }"
                  @click="adWorkspaceView = 'campaigns'"
                >
                  <span class="flow-step-icon flow-blue"><Timer /></span>
                  <span>
                    <small>01 / 计划</small>
                    <strong>{{ activeCampaignCount }} 个运行</strong>
                    <em>{{ campaigns.length }} 个总计划</em>
                  </span>
                </button>
                <button
                  type="button"
                  class="ad-flow-step"
                  :class="{ active: adWorkspaceView === 'accounts' }"
                  @click="adWorkspaceView = 'accounts'"
                >
                  <span class="flow-step-icon flow-green"><UserFilled /></span>
                  <span>
                    <small>02 / 账号</small>
                    <strong>{{ readyAdAccountCount }} 个可投</strong>
                    <em>{{ blockedAdAccountCount }} 个需处理</em>
                  </span>
                </button>
                <button
                  type="button"
                  class="ad-flow-step"
                  :class="{ active: adWorkspaceView === 'groups' }"
                  @click="adWorkspaceView = 'groups'"
                >
                  <span class="flow-step-icon flow-amber"><Connection /></span>
                  <span>
                    <small>03 / 群资格</small>
                    <strong>{{ adAllowedGroupCount }} 个可投</strong>
                    <em>{{ pendingGroupPolicyCount }} 个待确认</em>
                  </span>
                </button>
                <button
                  type="button"
                  class="ad-flow-step"
                  :class="{ active: adWorkspaceView === 'creatives' }"
                  @click="adWorkspaceView = 'creatives'"
                >
                  <span class="flow-step-icon flow-violet"><Document /></span>
                  <span>
                    <small>04 / 素材</small>
                    <strong>{{ enabledCreativeCount }} 个启用</strong>
                    <em>{{ enabledBindingCount }} 条绑定生效</em>
                  </span>
                </button>
              </div>
            </div>

            <aside class="ad-readiness-panel">
              <div class="ad-readiness-heading">
                <span>今日投放就绪度</span>
                <strong>{{ adReadinessScore }}%</strong>
              </div>
              <div class="ad-readiness-track">
                <span :style="{ width: adReadinessScore + '%' }"></span>
              </div>
              <ul class="ad-readiness-list">
                <li :class="{ ready: activeCampaignCount > 0 }">
                  <span>运行计划</span><b>{{ activeCampaignCount }}</b>
                </li>
                <li :class="{ ready: readyAdAccountCount > 0 }">
                  <span>可投账号</span><b>{{ readyAdAccountCount }}</b>
                </li>
                <li :class="{ ready: adAllowedGroupCount > 0 }">
                  <span>群广告许可</span><b>{{ adAllowedGroupCount }}</b>
                </li>
                <li :class="{ ready: enabledBindingCount > 0 }">
                  <span>生效绑定</span><b>{{ enabledBindingCount }}</b>
                </li>
              </ul>
              <div class="ad-readiness-footnote">
                <span v-if="forbiddenGroupCount">{{ forbiddenGroupCount }} 个群已禁止广告</span>
                <span v-else-if="unboundCampaignCount">{{ unboundCampaignCount }} 个运行计划尚未绑定账号</span>
                <span v-else>当前没有高优先级阻断</span>
                <el-button link type="primary" @click="adWorkspaceView = 'groups'">查看资格</el-button>
              </div>
            </aside>
          </section>

          <section class="ad-status-ribbon">
            <div>
              <span>24 小时发送</span>
              <strong>{{ adSuccess24h }} / {{ adFailed24h }}</strong>
              <small>成功率 {{ adSuccessRate24h }}%</small>
            </div>
            <div>
              <span>群每日容量</span>
              <strong>{{ groupDailyCapacityTotal }}</strong>
              <small>{{ adAllowedGroupCount }} 个群合计</small>
            </div>
            <div>
              <span>调度间隔</span>
              <strong>{{ adDeliveryExecutionForm.dispatcher_interval_seconds }} 秒</strong>
              <small>串行投放</small>
            </div>
            <div>
              <span>同群计划冷却</span>
              <strong>{{ adDeliveryExecutionForm.group_campaign_cooldown_minutes }} 分钟</strong>
              <small>失败退群 {{ adFailurePolicyForm.leave_on_group_control_failure ? "开启" : "关闭" }}</small>
            </div>
          </section>
<el-tabs v-model="adWorkspaceView" class="ad-workspace-tabs">
            <el-tab-pane name="campaigns">
              <template #label>
                <span class="ad-tab-label"
                  ><Timer />投放计划 <b>{{ campaigns.length }}</b></span
                >
              </template>

              <section class="ad-data-section">
                <div class="ad-section-toolbar">
                  <div>
                    <h4>投放计划</h4>
                    <p>按计划查看目标群、账号覆盖、素材池和发送频率</p>
                  </div>
                  <div class="ad-toolbar-controls">
                    <el-input
                      v-model="campaignFilters.query"
                      clearable
                      placeholder="搜索计划名称"
                      class="ad-search-control"
                    />
                    <el-select
                      v-model="campaignFilters.status"
                      clearable
                      placeholder="全部状态"
                      class="ad-status-control"
                    >
                      <el-option label="运行中" value="active" />
                      <el-option label="已停止" value="paused" />
                      <el-option label="草稿" value="draft" />
                    </el-select>
                    <el-button type="primary" @click="openCreateCampaign">
                      <el-icon><Plus /></el-icon>
                      新建计划
                    </el-button>
                  </div>
                </div>

                <el-table
                  :data="filteredCampaigns"
                  row-key="id"
                  class="ad-primary-table"
                >
                  <el-table-column type="expand" width="44">
                    <template #default="{ row }">
                      <div class="campaign-detail-grid">
                        <div class="campaign-detail-block">
                          <span class="detail-label">目标群</span>
                          <div
                            v-if="campaignTargetGroups(row).length"
                            class="detail-tag-list"
                          >
                            <el-tag
                              v-for="group in campaignTargetGroups(row)"
                              :key="group.id"
                              type="info"
                              effect="plain"
                            >
                              {{ group.title || group.chatId }} ·
                              {{ group.level }}级 ·
                              {{ group.accountCount }}个账号在群
                            </el-tag>
                          </div>
                          <strong v-else
                            >按等级
                            {{
                              row.target_group_levels?.join("/") || "-"
                            }}
                            自动匹配</strong
                          >
                        </div>
                        <div class="campaign-detail-block">
                          <span class="detail-label">投放账号</span>
                          <div
                            v-if="campaignStats(row.id).accountIds.length"
                            class="detail-tag-list"
                          >
                            <el-tag
                              v-for="accountId in campaignStats(row.id)
                                .accountIds"
                              :key="accountId"
                              :type="
                                accountStatusType(
                                  accountMap.get(accountId)?.status,
                                )
                              "
                              effect="plain"
                            >
                              {{ accountLabel(accountId) }} ·
                              {{
                                accountStatusText(
                                  accountMap.get(accountId)?.status,
                                )
                              }}
                            </el-tag>
                          </div>
                          <strong v-else class="text-warning"
                            >尚未绑定账号</strong
                          >
                        </div>
                        <div class="campaign-detail-block">
                          <span class="detail-label">素材池</span>
                          <div
                            v-if="campaignStats(row.id).creativeIds.length"
                            class="detail-tag-list"
                          >
                            <el-tag
                              v-for="creativeId in campaignStats(row.id)
                                .creativeIds"
                              :key="creativeId"
                              type="success"
                              effect="plain"
                            >
                              {{ creativeById(creativeId)?.name || creativeId }}
                            </el-tag>
                          </div>
                          <strong v-else class="text-warning"
                            >尚未绑定素材</strong
                          >
                        </div>
                        <div class="campaign-detail-block">
                          <span class="detail-label">生效窗口</span>
                          <strong>{{ campaignWindowText(row) }}</strong>
                          <small
                            >单群每日 {{ row.max_sends_per_group_per_day }} 次 ·
                            单号每日
                            {{ row.max_sends_per_account_per_day }} 次</small
                          >
                        </div>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="计划" min-width="180">
                    <template #default="{ row }">
                      <div class="primary-cell">
                        <strong>{{ row.name }}</strong>
                        <small
                          >#{{ row.id }} · {{ row.status || "draft" }}</small
                        >
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="状态" width="96">
                    <template #default="{ row }">
                      <el-tag
                        :type="row.enabled ? 'success' : 'info'"
                        effect="plain"
                      >
                        {{ row.enabled ? "运行中" : "已停止" }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="目标" min-width="180">
                    <template #default="{ row }">
                      <div class="primary-cell">
                        <strong v-if="row.target_group_ids?.length"
                          >指定 {{ row.target_group_ids.length }} 个群</strong
                        >
                        <strong v-else
                          >等级
                          {{
                            row.target_group_levels?.join("/") || "-"
                          }}</strong
                        >
                        <small>{{ campaignTargetLabel(row) }}</small>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="发送频率" min-width="150">
                    <template #default="{ row }">
                      <div class="primary-cell">
                        <strong>{{ sendModeText(row.send_mode) }}</strong>
                        <small>{{ campaignFrequencyText(row) }}</small>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column
                    label="账号 / 素材"
                    width="140"
                    align="center"
                  >
                    <template #default="{ row }">
                      <strong
                        >{{ campaignStats(row.id).accountIds.length }} /
                        {{ campaignStats(row.id).creativeIds.length }}</strong
                      >
                      <small class="table-subline"
                        >{{
                          campaignStats(row.id).enabledBindingCount
                        }}
                        条生效</small
                      >
                    </template>
                  </el-table-column>
                  <el-table-column label="操作" width="286" fixed="right">
                    <template #default="{ row }">
                      <el-button
                        link
                        type="primary"
                        @click="openBindingDrawer(row)"
                        >绑定</el-button
                      >
                      <el-button
                        link
                        :type="row.enabled ? 'warning' : 'success'"
                        @click="toggleCampaign(row)"
                      >
                        {{ row.enabled ? "停止" : "启动" }}
                      </el-button>
                      <el-button link type="primary" @click="editCampaign(row)"
                        >编辑</el-button
                      >
                      <el-button link @click="openDeliveryFromAds(row.id)"
                        >记录</el-button
                      >
                      <el-button link type="danger" @click="deleteCampaign(row)"
                        >删除</el-button
                      >
                    </template>
                  </el-table-column>
                </el-table>
              </section>
            </el-tab-pane>

            <el-tab-pane name="creatives">
              <template #label>
                <span class="ad-tab-label"
                  ><Document />素材库 <b>{{ creatives.length }}</b></span
                >
              </template>

              <section class="ad-data-section">
                <div class="ad-section-toolbar">
                  <div>
                    <h4>广告素材库</h4>
                    <p>素材正文、媒体、链接、权重和绑定覆盖</p>
                  </div>
                  <div class="ad-toolbar-controls">
                    <el-button @click="cleanupInvalidCreatives"
                      >清理异常素材</el-button
                    >
                    <el-button type="primary" @click="openCreateCreative">
                      <el-icon><Plus /></el-icon>
                      新建素材
                    </el-button>
                  </div>
                </div>

                <el-table
                  :data="creatives"
                  row-key="id"
                  class="ad-primary-table"
                >
                  <el-table-column label="素材" min-width="180">
                    <template #default="{ row }">
                      <div class="primary-cell">
                        <strong>{{ row.name }}</strong>
                        <small>#{{ row.id }} · {{ row.creative_type }}</small>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="广告文案" min-width="320">
                    <template #default="{ row }">
                      <div class="creative-preview-cell">
                        <span class="creative-preview-text">{{
                          creativePreview(row.content, 120)
                        }}</span>
                        <el-popover
                          v-if="row.content"
                          placement="top-start"
                          width="460"
                          trigger="click"
                        >
                          <template #reference
                            ><el-button link type="primary"
                              >全文</el-button
                            ></template
                          >
                          <pre class="creative-full-text">{{
                            row.content
                          }}</pre>
                        </el-popover>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="媒体 / 链接" min-width="190">
                    <template #default="{ row }">
                      <div class="primary-cell">
                        <strong>{{
                          row.media_url ? "含媒体" : "纯文本"
                        }}</strong>
                        <small>{{
                          row.link_url || row.media_url || "-"
                        }}</small>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column
                    prop="weight"
                    label="权重"
                    width="80"
                    align="center"
                  />
                  <el-table-column label="绑定" width="80" align="center">
                    <template #default="{ row }">{{
                      creativeBindingCounts.get(row.id) || 0
                    }}</template>
                  </el-table-column>
                  <el-table-column label="状态" width="90">
                    <template #default="{ row }">
                      <el-tag
                        :type="row.enabled ? 'success' : 'info'"
                        effect="plain"
                        >{{ row.enabled ? "启用" : "停用" }}</el-tag
                      >
                    </template>
                  </el-table-column>
                  <el-table-column label="更新时间" width="170">
                    <template #default="{ row }">{{
                      formatTimestamp(row.updated_at)
                    }}</template>
                  </el-table-column>
                  <el-table-column label="操作" width="178" fixed="right">
                    <template #default="{ row }">
                      <el-button link type="primary" @click="editCreative(row)"
                        >编辑</el-button
                      >
                      <el-button
                        link
                        :type="row.enabled ? 'warning' : 'success'"
                        @click="toggleCreative(row)"
                      >
                        {{ row.enabled ? "停用" : "启用" }}
                      </el-button>
                      <el-button link type="danger" @click="deleteCreative(row)"
                        >删除</el-button
                      >
                    </template>
                  </el-table-column>
                </el-table>
              </section>
            </el-tab-pane>

            <el-tab-pane name="bindings">
              <template #label>
                <span class="ad-tab-label"
                  ><Connection />账号绑定
                  <b>{{ bindingGroups.length }}</b></span
                >
              </template>

              <section class="ad-data-section">
                <div class="ad-section-toolbar">
                  <div>
                    <h4>账号与计划绑定</h4>
                    <p>每行代表一个账号在一个计划下的完整素材池</p>
                  </div>
                  <div class="ad-toolbar-controls">
                    <el-select
                      v-model="bindingFilters.account_id"
                      clearable
                      filterable
                      placeholder="全部账号"
                      class="ad-filter-control"
                    >
                      <el-option
                        v-for="account in accounts"
                        :key="account.id"
                        :label="accountLabel(account.id)"
                        :value="account.id"
                      />
                    </el-select>
                    <el-select
                      v-model="bindingFilters.campaign_id"
                      clearable
                      filterable
                      placeholder="全部计划"
                      class="ad-filter-control"
                    >
                      <el-option
                        v-for="campaign in campaigns"
                        :key="campaign.id"
                        :label="campaign.name"
                        :value="campaign.id"
                      />
                    </el-select>
                    <el-select
                      v-model="bindingFilters.status"
                      clearable
                      placeholder="全部状态"
                      class="ad-status-control"
                    >
                      <el-option label="已启用" value="enabled" />
                      <el-option label="已停用" value="disabled" />
                    </el-select>
                    <el-button type="primary" @click="openBindingDrawer()">
                      <el-icon><Plus /></el-icon>
                      新建绑定
                    </el-button>
                  </div>
                </div>

                <el-table
                  :data="filteredBindingGroups"
                  row-key="key"
                  class="ad-primary-table"
                >
                  <el-table-column type="expand" width="44">
                    <template #default="{ row }">
                      <div class="binding-detail-list">
                        <div
                          v-for="binding in row.bindings"
                          :key="binding.id"
                          class="binding-detail-row"
                        >
                          <div>
                            <strong>{{
                              creativeById(binding.creative_id)?.name ||
                              binding.creative_id ||
                              "未指定素材"
                            }}</strong>
                            <small>{{
                              creativePreview(
                                creativeById(binding.creative_id)?.content,
                                90,
                              )
                            }}</small>
                          </div>
                          <el-tag
                            :type="binding.enabled ? 'success' : 'info'"
                            effect="plain"
                          >
                            {{ binding.enabled ? "生效" : "停用" }}
                          </el-tag>
                          <span>优先级 {{ binding.priority }}</span>
                          <el-button
                            link
                            :type="binding.enabled ? 'warning' : 'success'"
                            @click="toggleBinding(binding)"
                          >
                            {{ binding.enabled ? "停用" : "启用" }}
                          </el-button>
                          <el-button
                            link
                            type="danger"
                            @click="deleteBinding(binding)"
                            >删除</el-button
                          >
                        </div>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="账号" min-width="190">
                    <template #default="{ row }">
                      <div class="primary-cell">
                        <strong>{{ accountLabel(row.accountId) }}</strong>
                        <small>
                          {{
                            accountStatusText(
                              accountMap.get(row.accountId)?.status,
                            )
                          }}
                          · ID {{ row.accountId }}
                        </small>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="计划" min-width="180">
                    <template #default="{ row }">
                      {{
                        campaigns.find(
                          (campaign) => campaign.id === row.campaignId,
                        )?.name || row.campaignId
                      }}
                    </template>
                  </el-table-column>
                  <el-table-column label="素材池" min-width="260">
                    <template #default="{ row }">
                      <div class="detail-tag-list compact-tags">
                        <el-tag
                          v-for="creativeId in row.creativeIds"
                          :key="creativeId"
                          type="info"
                          effect="plain"
                        >
                          {{ creativeById(creativeId)?.name || creativeId }}
                        </el-tag>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="生效" width="100" align="center">
                    <template #default="{ row }"
                      >{{ row.enabledCount }} /
                      {{ row.bindings.length }}</template
                    >
                  </el-table-column>
                  <el-table-column
                    prop="priority"
                    label="优先级"
                    width="90"
                    align="center"
                  />
                  <el-table-column label="操作" width="236" fixed="right">
                    <template #default="{ row }">
                      <el-button
                        link
                        :type="row.enabledCount ? 'warning' : 'success'"
                        @click="toggleBindingGroup(row)"
                      >
                        {{ row.enabledCount ? "全部停用" : "全部启用" }}
                      </el-button>
                      <el-button
                        link
                        @click="
                          openDeliveryFromAds(row.campaignId, row.accountId)
                        "
                        >记录</el-button
                      >
                      <el-button
                        link
                        type="danger"
                        @click="deleteBindingGroup(row)"
                        >删除整组</el-button
                      >
                    </template>
                  </el-table-column>
                </el-table>
              </section>
            </el-tab-pane>

            <el-tab-pane name="groups">
              <template #label>
                <span class="ad-tab-label"
                  ><Connection />群资格 <b>{{ groupAdProfiles.length }}</b></span
                >
              </template>

              <section class="ad-data-section">
                <div class="ad-section-toolbar">
                  <div>
                    <h4>群广告资格</h4>
                    <p>许可决定能不能投，档位决定每天能投多少；这里展示投放前的最终资格。</p>
                  </div>
                  <div class="ad-toolbar-controls">
                    <el-select
                      v-model="groupPolicyFilters.mode"
                      clearable
                      placeholder="全部许可"
                      class="ad-filter-control"
                    >
                      <el-option label="许可未知" value="unknown" />
                      <el-option label="广告检测中" value="unknown_probe" />
                      <el-option label="待人工审批" value="approval_required" />
                      <el-option label="试投中" value="soft_ad_trial" />
                      <el-option label="允许软广" value="soft_ad_allowed" />
                      <el-option label="高量许可" value="high_volume_ad_allowed" />
                      <el-option label="禁止投放" value="forbidden" />
                    </el-select>
                    <el-select
                      v-model="groupPolicyFilters.tier"
                      clearable
                      placeholder="全部档位"
                      class="ad-filter-control"
                    >
                      <el-option label="观察" value="observing" />
                      <el-option label="试投" value="trial" />
                      <el-option label="已验证" value="validated" />
                      <el-option label="稳定" value="stable" />
                      <el-option label="高量" value="high" />
                      <el-option label="优质" value="premium" />
                      <el-option label="封禁" value="blocked" />
                    </el-select>
                    <el-button @click="goGrowthConfig('ads')">
                      <el-icon><Setting /></el-icon>
                      调整策略
                    </el-button>
                  </div>
                </div>

                <div class="group-policy-note">
                  <span><CircleCheck />允许投放 {{ adAllowedGroupCount }}</span>
                  <span><WarningFilled />待确认 {{ pendingGroupPolicyCount }}</span>
                  <span><Close />禁止投放 {{ forbiddenGroupCount }}</span>
                  <strong>合计日容量 {{ groupDailyCapacityTotal }}</strong>
                </div>

                <el-table
                  :data="filteredGroupAdProfiles"
                  row-key="id"
                  class="ad-primary-table group-policy-table"
                >
                  <el-table-column label="群组" min-width="220">
                    <template #default="{ row }">
                      <div class="primary-cell">
                        <strong>{{ row.group_title || `群组 #${row.telegram_group_id}` }}</strong>
                        <small>ID {{ row.telegram_group_id }} · {{ row.group_level || "未分级" }}级 · {{ row.group_status || "-" }}</small>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="广告许可" width="140">
                    <template #default="{ row }">
                      <el-tag :type="groupPolicyType(row.ad_policy_mode)" effect="plain">
                        {{ groupPolicyText(row.ad_policy_mode) }}
                      </el-tag>
                      <small class="table-subline">置信度 {{ row.ad_policy_confidence }} · {{ row.ad_policy_source || "-" }}</small>
                    </template>
                  </el-table-column>
                  <el-table-column label="档位" width="110">
                    <template #default="{ row }">
                      <el-tag :type="groupTierType(row.ad_tier)" effect="plain">{{ groupTierText(row.ad_tier) }}</el-tag>
                      <small class="table-subline">{{ row.daily_capacity || 0 }} 条 / 天</small>
                    </template>
                  </el-table-column>
                  <el-table-column label="存活 / 删除" width="120" align="center">
                    <template #default="{ row }">
                      <strong>{{ row.survival_count || 0 }} / {{ row.deleted_count || 0 }}</strong>
                      <small class="table-subline">24h样本 {{ row.metrics?.completed_samples || 0 }}</small>
                    </template>
                  </el-table-column>
                  <el-table-column label="最近状态" min-width="170">
                    <template #default="{ row }">
                      <span v-if="row.ad_policy_probe_status === 'sent'" class="muted-text">
                        检测已发送，等待 24 小时存活
                      </span>
                      <span v-else-if="row.ad_policy_probe_status === 'failed'" class="error-text">
                        检测失败<span v-if="row.ad_policy_probe_error">：{{ row.ad_policy_probe_error }}</span>
                      </span>
                      <span v-else-if="row.ad_policy_probe_status === 'survived'" class="muted-text">
                        检测通过 {{ formatTimestamp(row.ad_policy_probe_at) }}
                      </span>
                      <span v-else-if="row.blocked_reason" class="error-text">{{ row.blocked_reason }}</span>
                      <span v-else-if="row.paused_until" class="muted-text">暂停至 {{ formatTimestamp(row.paused_until) }}</span>
                      <span v-else class="muted-text">最近验证 {{ formatTimestamp(row.ad_policy_verified_at) }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="操作" width="130" fixed="right">
                    <template #default="{ row }">
                      <el-button
                        v-if="row.ad_policy_mode === 'unknown'"
                        link
                        type="primary"
                        :loading="groupPolicyProbeRunning === row.group_id"
                        @click="triggerGroupAdPolicyProbe(row)"
                      >
                        <el-icon><VideoPlay /></el-icon>
                        发送广告探针
                      </el-button>
                      <el-tag v-else-if="row.ad_policy_mode === 'unknown_probe'" type="warning" effect="plain">
                        检测中
                      </el-tag>
                      <el-button v-else link type="primary" @click="goGrowthConfig('ads')">人工调整</el-button>
                    </template>
                  </el-table-column>
                </el-table>
              </section>
            </el-tab-pane>
            <el-tab-pane name="accounts">
              <template #label>
                <span class="ad-tab-label"
                  ><UserFilled />账号状态 <b>{{ accounts.length }}</b></span
                >
              </template>

              <section class="ad-data-section">
                <div class="ad-section-toolbar">
                  <div>
                    <h4>账号投放状态</h4>
                    <p>账号可用性、暖号、风控、群资格与近 24 小时结果</p>
                  </div>
                </div>

                <el-table
                  :data="adReadinessRows"
                  row-key="account.id"
                  class="ad-primary-table"
                >
                  <el-table-column label="账号" min-width="190">
                    <template #default="{ row }">
                      <div class="primary-cell">
                        <strong>{{ accountLabel(row.account.id) }}</strong>
                        <small>ID {{ row.account.id }}</small>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="账号状态" width="105">
                    <template #default="{ row }">
                      <el-tag
                        :type="accountStatusType(row.account.status)"
                        effect="plain"
                      >
                        {{ accountStatusText(row.account.status) }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="投放状态" min-width="150">
                    <template #default="{ row }">
                      <div class="primary-cell">
                        <el-tag
                          :type="row.ready ? 'success' : 'warning'"
                          effect="plain"
                        >
                          {{ row.ready ? "可投放" : "暂不可投" }}
                        </el-tag>
                        <small v-if="!row.ready">{{
                          deliveryBlockReason(row.status)
                        }}</small>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column
                    label="健康 / 风险"
                    width="125"
                    align="center"
                  >
                    <template #default="{ row }">
                      <strong
                        >{{ row.status?.health_score ?? "-" }} /
                        {{ row.status?.risk_score ?? "-" }}</strong
                      >
                      <small class="table-subline">{{
                        riskLevelText(row.status?.risk_level)
                      }}</small>
                    </template>
                  </el-table-column>
                  <el-table-column label="暖号阶段" width="120">
                    <template #default="{ row }">
                      <el-tag
                        :type="warmupStageType(row.status?.warmup_stage || '')"
                        effect="plain"
                      >
                        {{ warmupStageText(row.status?.warmup_stage || "") }}
                      </el-tag>
                      <small class="table-subline"
                        >余
                        {{ row.status?.warmup_remaining_days ?? "-" }} 天</small
                      >
                    </template>
                  </el-table-column>
                  <el-table-column label="额度" width="130" align="center">
                    <template #default="{ row }">
                      <strong
                        >{{ row.status?.dynamic_daily_limit ?? 0 }} / 日</strong
                      >
                      <small class="table-subline"
                        >单轮 {{ row.status?.dynamic_run_limit ?? 0 }}</small
                      >
                    </template>
                  </el-table-column>
                  <el-table-column label="群资格" width="135" align="center">
                    <template #default="{ row }">
                      <strong
                        >{{ row.status?.ad_eligible_groups ?? 0 }} 可投</strong
                      >
                      <small class="table-subline"
                        >{{
                          row.status?.pending_probe_groups ?? 0
                        }}
                        待探测</small
                      >
                    </template>
                  </el-table-column>
                  <el-table-column label="绑定" width="100" align="center">
                    <template #default="{ row }"
                      >{{ row.enabledBindingCount }} /
                      {{ row.bindingCount }}</template
                    >
                  </el-table-column>
                  <el-table-column label="24 小时" width="120" align="center">
                    <template #default="{ row }">
                      <strong
                        >{{ row.status?.success_24h ?? 0 }} /
                        {{ row.status?.failed_24h ?? 0 }}</strong
                      >
                      <small class="table-subline">成功 / 失败</small>
                    </template>
                  </el-table-column>
                  <el-table-column label="操作" width="150" fixed="right">
                    <template #default="{ row }">
                      <el-button
                        link
                        type="primary"
                        @click="openAccountFromAds(row.account.id)"
                        >配置</el-button
                      >
                      <el-button
                        link
                        @click="openDeliveryFromAds(undefined, row.account.id)"
                        >记录</el-button
                      >
                    </template>
                  </el-table-column>
                </el-table>
              </section>
            </el-tab-pane>
          </el-tabs>

          <el-drawer
            v-model="campaignDrawerVisible"
            :title="editingCampaignId ? '编辑投放计划' : '新建投放计划'"
            size="min(660px, 100vw)"
            class="ad-form-drawer"
            destroy-on-close
          >
            <el-form label-position="top" class="ad-drawer-form">
              <div class="drawer-section-title">基础信息</div>
              <div class="drawer-form-grid">
                <el-form-item label="计划名称" required>
                  <el-input
                    v-model="campaignForm.name"
                    placeholder="例如：核心群软广告"
                  />
                </el-form-item>
                <el-form-item label="计划状态">
                  <el-select v-model="campaignForm.status">
                    <el-option label="草稿" value="draft" />
                    <el-option label="运行中" value="active" />
                    <el-option label="已暂停" value="paused" />
                  </el-select>
                </el-form-item>
              </div>
              <el-form-item label="启用计划">
                <el-switch v-model="campaignForm.enabled" />
              </el-form-item>

              <div class="drawer-section-title">目标群</div>
              <el-form-item label="指定群">
                <div class="target-group-control">
                  <el-select
                    v-model="campaignForm.target_group_ids"
                    multiple
                    filterable
                    collapse-tags
                    collapse-tags-tooltip
                    clearable
                    placeholder="选择当前仍有账号加入的活跃群"
                  >
                    <el-option
                      v-for="group in targetGroups"
                      :key="group.id"
                      :label="targetGroupLabel(group.id)"
                      :value="group.id"
                    >
                      <div class="rich-option">
                        <span>{{ group.title || group.chatId }}</span>
                        <small
                          >{{ group.level }}级 · {{ group.memberCount }} 人 ·
                          {{ group.accountCount }} 个账号在群</small
                        >
                      </div>
                    </el-option>
                  </el-select>
                  <el-button @click="openTargetGroupDialog"
                    ><el-icon><Plus /></el-icon>添加群</el-button
                  >
                </div>
              </el-form-item>
              <el-form-item
                v-if="!campaignForm.target_group_ids.length"
                label="目标等级"
              >
                <el-checkbox-group v-model="campaignForm.target_group_levels">
                  <el-checkbox-button value="A">A级群</el-checkbox-button>
                  <el-checkbox-button value="B">B级群</el-checkbox-button>
                  <el-checkbox-button value="C">C级群</el-checkbox-button>
                </el-checkbox-group>
              </el-form-item>
              <div
                v-if="campaignForm.target_group_ids.length"
                class="selected-target-summary"
              >
                已选 {{ campaignForm.target_group_ids.length }} 个群，覆盖
                {{
                  campaignForm.target_group_ids.reduce(
                    (total, id) =>
                      total + (targetGroupMap.get(id)?.accountCount || 0),
                    0,
                  )
                }}
                个账号群席位
              </div>

              <div class="drawer-section-title">发送节奏</div>
              <el-form-item label="发送模式">
                <el-segmented
                  v-model="campaignForm.send_mode"
                  :options="[
                    { label: '入群后', value: 'after_join' },
                    { label: '固定间隔', value: 'interval' },
                    { label: '每日定时', value: 'scheduled' },
                  ]"
                />
              </el-form-item>
              <el-form-item
                v-if="campaignForm.send_mode === 'after_join'"
                label="入群后等待"
              >
                <el-input-number
                  v-model="campaignForm.min_wait_after_join_minutes"
                  :min="0"
                  :max="43200"
                />
                <span class="input-suffix">分钟</span>
              </el-form-item>
              <el-form-item
                v-if="campaignForm.send_mode === 'interval'"
                label="每群发送间隔"
              >
                <el-input-number
                  v-model="campaignForm.interval_minutes"
                  :min="1"
                  :max="43200"
                />
                <span class="input-suffix">分钟</span>
              </el-form-item>
              <el-form-item
                v-if="campaignForm.send_mode === 'scheduled'"
                label="每日发送时点"
                required
              >
                <el-input
                  v-model="scheduledTimesText"
                  placeholder="09:00, 14:30, 21:00"
                />
              </el-form-item>

              <div class="drawer-section-title">额度与有效期</div>
              <div class="drawer-form-grid">
                <el-form-item label="单群每日上限">
                  <el-input-number
                    v-model="campaignForm.max_sends_per_group_per_day"
                    :min="0"
                  />
                </el-form-item>
                <el-form-item label="单号每日上限">
                  <el-input-number
                    v-model="campaignForm.max_sends_per_account_per_day"
                    :min="0"
                  />
                </el-form-item>
                <el-form-item label="开始时间">
                  <el-date-picker
                    v-model="campaignForm.start_at"
                    type="datetime"
                    value-format="YYYY-MM-DDTHH:mm:ss"
                    placeholder="立即生效"
                  />
                </el-form-item>
                <el-form-item label="结束时间">
                  <el-date-picker
                    v-model="campaignForm.end_at"
                    type="datetime"
                    value-format="YYYY-MM-DDTHH:mm:ss"
                    placeholder="长期有效"
                  />
                </el-form-item>
              </div>
            </el-form>
            <template #footer>
              <el-button @click="campaignDrawerVisible = false">取消</el-button>
              <el-button
                type="primary"
                :loading="savingCampaign"
                @click="saveCampaign"
              >
                {{ editingCampaignId ? "保存计划" : "创建计划" }}
              </el-button>
            </template>
          </el-drawer>

          <el-drawer
            v-model="creativeDrawerVisible"
            :title="editingCreativeId ? '编辑广告素材' : '新建广告素材'"
            size="min(560px, 100vw)"
            class="ad-form-drawer"
            destroy-on-close
          >
            <el-form label-position="top" class="ad-drawer-form">
              <div class="drawer-form-grid">
                <el-form-item label="素材名称" required>
                  <el-input v-model="creativeForm.name" />
                </el-form-item>
                <el-form-item label="素材类型">
                  <el-select v-model="creativeForm.creative_type">
                    <el-option label="文本" value="text" />
                    <el-option label="图片" value="image" />
                    <el-option label="图文" value="mixed" />
                  </el-select>
                </el-form-item>
              </div>
              <el-form-item label="广告文案" required>
                <el-input
                  v-model="creativeForm.content"
                  type="textarea"
                  :rows="10"
                  maxlength="4096"
                  show-word-limit
                />
              </el-form-item>
              <el-form-item label="媒体地址">
                <el-input
                  v-model="creativeForm.media_url"
                  placeholder="图片 URL 或 Telegram file_id"
                />
              </el-form-item>
              <el-form-item label="跳转链接">
                <el-input v-model="creativeForm.link_url" />
              </el-form-item>
              <div class="drawer-form-grid">
                <el-form-item label="轮换权重">
                  <el-input-number
                    v-model="creativeForm.weight"
                    :min="0"
                    :max="10000"
                  />
                </el-form-item>
                <el-form-item label="启用素材">
                  <el-switch v-model="creativeForm.enabled" />
                </el-form-item>
              </div>
            </el-form>
            <template #footer>
              <el-button @click="creativeDrawerVisible = false">取消</el-button>
              <el-button
                type="primary"
                :loading="savingCreative"
                @click="saveCreative"
              >
                {{ editingCreativeId ? "保存素材" : "创建素材" }}
              </el-button>
            </template>
          </el-drawer>

          <el-drawer
            v-model="bindingDrawerVisible"
            title="配置账号与素材池"
            size="min(620px, 100vw)"
            class="ad-form-drawer"
            destroy-on-close
          >
            <el-form label-position="top" class="ad-drawer-form">
              <el-form-item label="广告计划" required>
                <el-select
                  v-model="bindingForm.ad_campaign_id"
                  filterable
                  @change="bindingForm.creative_ids = []"
                >
                  <el-option
                    v-for="campaign in campaigns"
                    :key="campaign.id"
                    :label="campaign.name"
                    :value="campaign.id"
                  >
                    <div class="rich-option">
                      <span>{{ campaign.name }}</span>
                      <small
                        >{{ sendModeText(campaign.send_mode) }} ·
                        {{ campaignFrequencyText(campaign) }}</small
                      >
                    </div>
                  </el-option>
                </el-select>
              </el-form-item>
              <el-form-item label="投放账号" required>
                <el-select
                  v-model="bindingForm.account_ids"
                  multiple
                  filterable
                  collapse-tags
                  collapse-tags-tooltip
                  clearable
                  placeholder="选择一个或多个可用账号"
                >
                  <el-option
                    v-for="account in adBindingAccounts"
                    :key="account.id"
                    :label="accountLabel(account.id)"
                    :value="account.id"
                  >
                    <div class="rich-option">
                      <span>{{ accountLabel(account.id) }}</span>
                      <small
                        >{{ accountStatusText(account.status) }} · ID
                        {{ account.id }}</small
                      >
                    </div>
                  </el-option>
                </el-select>
              </el-form-item>
              <el-form-item label="素材池" required>
                <el-select
                  v-model="bindingForm.creative_ids"
                  multiple
                  filterable
                  collapse-tags
                  collapse-tags-tooltip
                  clearable
                  placeholder="选择用于轮换发送的素材"
                >
                  <el-option
                    v-for="creative in creatives"
                    :key="creative.id"
                    :label="creative.name"
                    :value="creative.id"
                    :disabled="!creative.enabled"
                  >
                    <div class="rich-option">
                      <span>{{ creative.name }}</span>
                      <small
                        >{{ creative.creative_type }} · 权重
                        {{ creative.weight }} ·
                        {{ creativePreview(creative.content, 36) }}</small
                      >
                    </div>
                  </el-option>
                </el-select>
              </el-form-item>
              <div class="binding-pool-summary">
                <div>
                  <span>所选账号</span
                  ><strong>{{ bindingForm.account_ids.length }}</strong>
                </div>
                <div>
                  <span>所选素材</span
                  ><strong>{{ bindingForm.creative_ids.length }}</strong>
                </div>
                <div>
                  <span>将创建绑定</span
                  ><strong>{{
                    bindingForm.account_ids.length *
                    bindingForm.creative_ids.length
                  }}</strong>
                </div>
              </div>
              <div
                v-if="selectedBindingCreatives.length"
                class="selected-creative-list"
              >
                <div
                  v-for="creative in selectedBindingCreatives"
                  :key="creative.id"
                >
                  <strong>{{ creative.name }}</strong>
                  <span>{{ creativePreview(creative.content, 88) }}</span>
                </div>
              </div>
              <div class="drawer-form-grid">
                <el-form-item label="优先级">
                  <el-input-number
                    v-model="bindingForm.priority"
                    :min="0"
                    :max="10000"
                  />
                </el-form-item>
                <el-form-item label="立即启用">
                  <el-switch v-model="bindingForm.enabled" />
                </el-form-item>
              </div>
              <div class="drawer-secondary-action">
                <el-button
                  :disabled="
                    !bindingForm.account_ids.length ||
                    !bindingForm.ad_campaign_id
                  "
                  @click="ensureCreativePool"
                >
                  自动补齐每个账号的素材池
                </el-button>
                <span v-if="creativePoolStatus">
                  已检查 {{ creativePoolStatus.account_count }} 个账号，新建
                  {{ creativePoolStatus.created_count }} 条素材
                </span>
              </div>
            </el-form>
            <template #footer>
              <el-button @click="bindingDrawerVisible = false">取消</el-button>
              <el-button type="primary" @click="createBinding"
                >创建绑定</el-button
              >
            </template>
          </el-drawer>

          <el-dialog
            v-model="adRunDialogVisible"
            title="手动执行广告投放"
            width="min(480px, 94vw)"
          >
            <el-form label-position="top">
              <el-form-item label="本次最大发送数">
                <el-input-number
                  v-model="adRunForm.max_deliveries"
                  :min="1"
                  :max="10000"
                />
              </el-form-item>
              <el-form-item label="执行模式">
                <el-segmented
                  v-model="adRunForm.dry_run"
                  :options="[
                    { label: '仅预演', value: true },
                    { label: '实际发送', value: false },
                  ]"
                />
              </el-form-item>
            </el-form>
            <template #footer>
              <el-button @click="adRunDialogVisible = false">取消</el-button>
              <el-button
                type="primary"
                :loading="running === 'ads'"
                @click="runAds"
              >
                <el-icon><VideoPlay /></el-icon>
                {{ adRunForm.dry_run ? "执行预演" : "确认发送" }}
              </el-button>
            </template>
          </el-dialog>
        </div>
      </el-tab-pane>

      <el-tab-pane label="日志" name="logs">
        <el-card shadow="never" class="log-card">
          <template #header>AI处理记录（入群验证）</template>
          <el-table :data="autoJoinVerificationLogs" height="320">
            <el-table-column label="账号" min-width="140">
              <template #default="{ row }">{{ accountLabel(row.account_id) }}</template>
            </el-table-column>
            <el-table-column prop="group_title" label="群组" min-width="200" show-overflow-tooltip />
            <el-table-column label="来源" width="90">
              <template #default="{ row }">
                <el-tag :type="row.source === 'ai' ? 'success' : 'info'">{{ verificationSourceText(row.source) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="动作" width="110">
              <template #default="{ row }">{{ verificationActionText(row.action) }}</template>
            </el-table-column>
            <el-table-column label="结果" width="100">
              <template #default="{ row }">
                <el-tag :type="verificationSuccessType(row.success)">{{ verificationSuccessText(row.success) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最终" width="100">
              <template #default="{ row }">
                <el-tag :type="row.audit_passed ? 'success' : 'warning'">{{ row.audit_passed ? '通过' : '未通过' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="置信度" width="90">
              <template #default="{ row }">{{ row.confidence ?? '-' }}</template>
            </el-table-column>
            <el-table-column prop="button_text" label="按钮/答案" min-width="120" show-overflow-tooltip>
              <template #default="{ row }">{{ row.button_text || row.answer || '-' }}</template>
            </el-table-column>
            <el-table-column label="复查" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">
                {{ row.post_action_final_permission_reason || row.post_action_status || '-' }}
              </template>
            </el-table-column>
            <el-table-column prop="reason" label="原因" min-width="160" show-overflow-tooltip />
            <el-table-column prop="updated_at" label="时间" width="180" />
          </el-table>
        </el-card>

        <el-card shadow="never" class="log-card">
          <template #header>最近加群记录</template>
          <el-table :data="autoJoinAttempts" height="320">
            <el-table-column label="账号" min-width="140">
              <template #default="{ row }">{{ accountLabel(row.account_id) }}</template>
            </el-table-column>
            <el-table-column prop="group_title" label="群组" min-width="180" />
            <el-table-column prop="source_keyword" label="关键词" width="140" />
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="statusType(row.status)">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="reason" label="原因" min-width="160" />
            <el-table-column prop="attempted_at" label="时间" width="180" />
          </el-table>
        </el-card>

      </el-tab-pane>

      <el-tab-pane label="广告发送记录" name="delivery">
        <el-card shadow="never" class="log-card">
          <template #header>
            <div class="card-header">
              <span>广告发送记录</span>
              <el-button :loading="deliveryLogLoading" @click="loadDeliveryLogs">
                <el-icon><Refresh /></el-icon>
                刷新
              </el-button>
            </div>
          </template>

          <div class="delivery-filter-bar">
            <el-select v-model="deliveryLogFilters.account_id" clearable filterable placeholder="账号" class="filter-control">
              <el-option
                v-for="item in accounts"
                :key="item.id"
                :label="accountLabel(item.id)"
                :value="item.id"
              />
            </el-select>
            <el-select v-model="deliveryLogFilters.campaign_id" clearable filterable placeholder="广告计划" class="filter-control">
              <el-option v-for="item in campaigns" :key="item.id" :label="item.name" :value="item.id" />
            </el-select>
            <el-select v-model="deliveryLogFilters.status" clearable placeholder="状态" class="filter-control">
              <el-option
                v-for="item in deliveryStatusOptions"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
            <el-date-picker
              v-model="deliveryLogFilters.time_range"
              type="datetimerange"
              start-placeholder="开始时间"
              end-placeholder="结束时间"
              value-format="YYYY-MM-DDTHH:mm:ss"
              class="time-range-control"
            />
            <el-button type="primary" :loading="deliveryLogLoading" @click="searchDeliveryLogs">
              <el-icon><Select /></el-icon>
              筛选
            </el-button>
            <el-button @click="resetDeliveryLogFilters">重置</el-button>
          </div>

          <el-table v-loading="deliveryLogLoading" :data="deliveryLogs" height="520">
            <el-table-column label="账号" min-width="140">
              <template #default="{ row }">{{ accountLabel(row.account_id) }}</template>
            </el-table-column>
            <el-table-column label="群名称" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">{{ row.group_title || row.group_username || '-' }}</template>
            </el-table-column>
            <el-table-column prop="telegram_group_id" label="群ID" min-width="140" />
            <el-table-column label="计划" min-width="140">
              <template #default="{ row }">{{ campaigns.find((item) => item.id === row.ad_campaign_id)?.name || row.ad_campaign_id }}</template>
            </el-table-column>
            <el-table-column label="素材" min-width="180">
              <template #default="{ row }">
                <el-popover v-if="creativeById(row.creative_id)?.content" placement="top-start" width="460" trigger="click">
                  <template #reference>
                    <el-button link type="primary">{{ creativeById(row.creative_id)?.name || row.creative_id }}</el-button>
                  </template>
                  <pre class="creative-full-text">{{ creativeById(row.creative_id)?.content }}</pre>
                </el-popover>
                <span v-else>{{ creativeById(row.creative_id)?.name || row.creative_id || '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="statusType(row.status)">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="telegram_message_id" label="消息ID" width="120" />
            <el-table-column prop="error" label="错误" min-width="220" show-overflow-tooltip />
            <el-table-column prop="sent_at" label="发送时间" width="180" />
            <el-table-column prop="created_at" label="创建时间" width="180" />
          </el-table>

          <div class="pagination-bar">
            <el-pagination
              v-model:current-page="deliveryLogPagination.page"
              v-model:page-size="deliveryLogPagination.page_size"
              :total="deliveryLogPagination.total"
              :page-sizes="[20, 50, 100, 200]"
              layout="total, sizes, prev, pager, next, jumper"
              @current-change="handleDeliveryPageChange"
              @size-change="handleDeliveryPageSizeChange"
            />
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="targetGroupDialogVisible" title="通过链接加入群" width="520px">
      <el-form label-width="110px">
        <el-alert
          v-if="targetGroupAccountIsAdOnly"
          title="接管成功后，该群中的普通账号将自动退群"
          type="warning"
          :closable="false"
          show-icon
          class="handover-alert"
        />
        <el-form-item label="群链接" required>
          <el-input
            v-model="targetGroupForm.groupLink"
            placeholder="例如 https://t.me/group_name 或 https://t.me/+invite"
            clearable
          />
        </el-form-item>
        <el-form-item label="推广账号" required>
          <el-select v-model="targetGroupForm.accountId" filterable placeholder="选择执行入群的账号">
            <el-option
              v-for="item in accounts"
              :key="item.id"
              :label="accountLabel(item.id)"
              :value="item.id"
              :disabled="!item.is_active || ['error', 'banned'].includes(item.status || '')"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="targetGroupDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingTargetGroup" @click="saveTargetGroup">
          {{ targetGroupAccountIsAdOnly ? '接管并添加' : '加入并添加' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.handover-alert {
  margin-bottom: 16px;
}

.automation-page {
  padding: 0;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.page-title {
  margin: 0;
  color: #303133;
  font-size: 20px;
  font-weight: 600;
}

.summary-row {
  margin-bottom: 16px;
}

.metric-card {
  .metric-label {
    color: #909399;
    font-size: 13px;
  }

  .metric-value {
    margin-top: 8px;
    color: #303133;
    font-size: 24px;
    font-weight: 700;
    overflow-wrap: anywhere;
  }
}

.automation-tabs {
  padding: 16px;
  background: #fff;
  border-radius: 8px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.single-entry-note {
  margin-bottom: 12px;
  padding: 10px 12px;
  color: #606266;
  font-size: 13px;
  line-height: 1.6;
  background: #f5f7fa;
  border: 1px solid #ebeef5;
  border-radius: 6px;
}

.policy-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(160px, 1fr));
  gap: 10px;
}

.policy-summary-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
}

.policy-label {
  flex: 0 0 auto;
  color: #909399;
  font-size: 13px;
}

.policy-value {
  min-width: 0;
  overflow: hidden;
  color: #303133;
  font-weight: 600;
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.control-grid,
.config-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(280px, 1fr));
  gap: 16px;
}

.account-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
}

.account-select {
  width: min(320px, 100%);
}

.dynamic-status-card {
  margin-bottom: 16px;
}

.risk-guard-card,
.warmup-policy-card {
  margin-bottom: 16px;
}

.warmup-stage-table {
  margin-top: 16px;
}

.muted-text {
  color: #909399;
}

.error-text {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  color: #606266;
  text-overflow: ellipsis;
  vertical-align: bottom;
  white-space: nowrap;
}

.batch-toolbar,
.delivery-filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-bottom: 16px;
}

.batch-account-select {
  width: min(520px, 100%);
}

.filter-control {
  width: 180px;
}

.time-range-control {
  width: 360px;
}

.account-config-form {
  margin-top: 8px;
}

.account-config-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(260px, 1fr));
  gap: 0 16px;
}

.binding-table {
  margin-top: 16px;
}

.campaign-table {
  margin-top: 16px;
}

.creative-table {
  margin-top: 16px;
}

.creative-preview-cell {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
}

.creative-preview-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.creative-full-text {
  max-height: 360px;
  margin: 0;
  overflow: auto;
  color: #303133;
  font-family: inherit;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.clickable-tag {
  cursor: pointer;
}

.form-helper {
  color: #606266;
  line-height: 1.6;
}

.inline-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.target-group-control {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  width: 100%;
}

.log-card + .log-card {
  margin-top: 16px;
}

.pagination-bar {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
  overflow-x: auto;
}

.failover-card {
  grid-column: 1 / -1;
}

.failover-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.status-filter {
  width: 160px;
}

.failover-toolbar {
  margin-bottom: 8px;
}

@media (max-width: 1200px) {
  .control-grid,
  .config-grid,
  .account-config-grid,
  .policy-summary {
    grid-template-columns: 1fr;
  }

  .filter-control,
  .time-range-control,
  .batch-account-select {
    width: 100%;
  }

  .target-group-control {
    grid-template-columns: 1fr;
  }

  .pagination-bar {
    justify-content: flex-start;
  }
}
.ad-workbench {
  min-width: 0;
  color: #1f2937;
}

.ad-command-deck {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 292px;
  margin-top: 12px;
  overflow: hidden;
  color: #f8fafc;
  background: #171a1e;
  border: 1px solid #2d3339;
  border-radius: 8px;
}

.ad-command-main {
  min-width: 0;
  padding: 24px;
}

.ad-command-topline {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}

.ad-command-title {
  min-width: 0;

  h3 {
    margin: 8px 0 0;
    color: #fff;
    font-size: 24px;
    font-weight: 700;
    line-height: 1.2;
  }

  p {
    margin: 8px 0 0;
    color: #aab2ba;
    font-size: 13px;
    line-height: 1.5;
  }
}

.ad-command-kicker {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #9ba4ad;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.ad-status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #69737d;
}

.ad-status-dot.is-live {
  background: #51cf66;
  box-shadow: 0 0 0 4px rgb(81 207 102 / 12%);
}

.ad-status-dot.is-off {
  background: #ff8787;
}

.ad-command-deck .ad-header-actions {
  :deep(.el-button) {
    color: #dbe2e8;
    background: transparent;
    border-color: #46505a;
  }

  :deep(.el-button:hover) {
    color: #fff;
    border-color: #8b99a6;
  }

  :deep(.el-button--primary) {
    color: #101315;
    background: #69db7c;
    border-color: #69db7c;
  }
}

.ad-flow-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin-top: 28px;
  padding-top: 18px;
  border-top: 1px solid #2d3339;
}

.ad-flow-step {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  min-width: 0;
  padding: 10px;
  color: #b9c2ca;
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.16s ease, border-color 0.16s ease;

  &:hover,
  &.active {
    color: #f8fafc;
    background: #22282e;
    border-color: #3e4851;
  }

  small,
  strong,
  em {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  small {
    color: #89939d;
    font-size: 10px;
    font-weight: 700;
  }

  strong {
    margin-top: 4px;
    color: #f8fafc;
    font-size: 13px;
  }

  em {
    margin-top: 3px;
    color: #89939d;
    font-size: 11px;
    font-style: normal;
  }
}

.flow-step-icon {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border-radius: 6px;

  svg {
    width: 17px;
    height: 17px;
  }
}

.flow-blue {
  color: #74c0fc;
  background: #1b3a54;
}

.flow-green {
  color: #8ce99a;
  background: #23452d;
}

.flow-amber {
  color: #ffd43b;
  background: #4b3d1b;
}

.flow-violet {
  color: #d0bfff;
  background: #3b3152;
}

.ad-readiness-panel {
  display: flex;
  min-width: 0;
  flex-direction: column;
  padding: 22px 20px;
  color: #25313a;
  background: #f8fafc;
  border-left: 1px solid #2d3339;
}

.ad-readiness-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;

  span {
    color: #66727d;
    font-size: 12px;
    font-weight: 600;
  }

  strong {
    color: #1f2933;
    font-size: 26px;
    line-height: 1;
  }
}

.ad-readiness-track {
  height: 6px;
  margin: 14px 0 18px;
  overflow: hidden;
  background: #e2e8ee;
  border-radius: 99px;

  span {
    display: block;
    height: 100%;
    background: #37b24d;
    border-radius: inherit;
    transition: width 0.2s ease;
  }
}

.ad-readiness-list {
  display: grid;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;

  li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    color: #89939d;
    font-size: 12px;
  }

  li.ready {
    color: #2f9e44;
  }

  b {
    color: #25313a;
    font-size: 13px;
  }
}

.ad-readiness-footnote {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: auto;
  padding-top: 18px;
  color: #89939d;
  font-size: 11px;
  line-height: 1.4;
  border-top: 1px solid #e2e8ee;
}

.ad-status-ribbon {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px;
  margin-top: 12px;
  overflow: hidden;
  background: #dce2e7;
  border: 1px solid #dce2e7;
  border-radius: 6px;

  > div {
    min-width: 0;
    padding: 12px 14px;
    background: #fff;
  }

  span,
  strong,
  small {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  span {
    color: #75808a;
    font-size: 11px;
  }

  strong {
    margin-top: 4px;
    color: #1f2933;
    font-size: 17px;
  }

  small {
    margin-top: 3px;
    color: #9aa4ad;
    font-size: 11px;
  }
}

.group-policy-note {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 16px;
  margin: -2px 0 12px;
  padding: 9px 12px;
  color: #6b7680;
  font-size: 12px;
  background: #f7f9fb;
  border: 1px solid #e1e6eb;
  border-radius: 6px;

  span,
  strong {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  strong {
    margin-left: auto;
    color: #1f2933;
  }

  svg {
    width: 14px;
    height: 14px;
  }
}
.ad-workbench-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 4px 0 18px;
  border-bottom: 1px solid #e5e7eb;
}

.ad-workbench-heading {
  min-width: 0;

  h3 {
    margin: 0;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 0;
  }

  p {
    margin: 6px 0 0;
    color: #6b7280;
    font-size: 13px;
    line-height: 1.5;
  }
}

.ad-title-line,
.ad-header-actions,
.ad-toolbar-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.ad-header-actions {
  flex: 0 0 auto;
  justify-content: flex-end;

  :deep(.el-button + .el-button) {
    margin-left: 0;
  }
}

.ad-metrics-band {
  display: grid;
  grid-template-columns: repeat(5, minmax(150px, 1fr));
  margin-top: 16px;
  overflow: hidden;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
}

.ad-metric-item {
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr);
  gap: 12px;
  align-items: center;
  min-height: 104px;
  padding: 16px;
  border-right: 1px solid #e5e7eb;

  &:last-child {
    border-right: 0;
  }

  span,
  small {
    display: block;
  }

  span {
    color: #6b7280;
    font-size: 12px;
  }

  strong {
    display: block;
    margin: 3px 0;
    color: #111827;
    font-size: 22px;
    font-weight: 700;
    line-height: 1.2;
    overflow-wrap: anywhere;
  }

  small {
    overflow: hidden;
    color: #9ca3af;
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.ad-metric-icon {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border-radius: 6px;

  svg {
    width: 19px;
    height: 19px;
  }
}

.metric-green {
  color: #087f5b;
  background: #e6fcf5;
}

.metric-blue {
  color: #1864ab;
  background: #e7f5ff;
}

.metric-cyan {
  color: #0b7285;
  background: #e3fafc;
}

.metric-amber {
  color: #a15c00;
  background: #fff4e6;
}

.ad-alert-band {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 10px 12px;
  color: #8a4b08;
  font-size: 13px;
  line-height: 1.5;
  background: #fff8e6;
  border: 1px solid #f1d29a;
  border-radius: 6px;

  span {
    min-width: 0;
  }

  .el-button {
    flex: 0 0 auto;
    margin-left: auto;
  }
}

.ad-policy-strip {
  display: grid;
  grid-template-columns: repeat(5, minmax(120px, 1fr));
  gap: 1px;
  margin-top: 12px;
  overflow: hidden;
  background: #e5e7eb;
  border: 1px solid #e5e7eb;
  border-radius: 6px;

  > div {
    min-width: 0;
    padding: 10px 12px;
    background: #f8fafc;
  }

  span,
  strong {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  span {
    color: #6b7280;
    font-size: 11px;
  }

  strong {
    margin-top: 3px;
    color: #374151;
    font-size: 13px;
  }
}

.ad-workspace-tabs {
  margin-top: 18px;

  :deep(> .el-tabs__header) {
    margin-bottom: 0;
  }

  :deep(> .el-tabs__content) {
    overflow: visible;
  }
}

.ad-tab-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;

  svg {
    width: 15px;
    height: 15px;
  }

  b {
    min-width: 20px;
    padding: 1px 6px;
    color: #6b7280;
    font-size: 11px;
    font-weight: 600;
    line-height: 18px;
    text-align: center;
    background: #f1f3f5;
    border-radius: 10px;
  }
}

.ad-data-section {
  min-width: 0;
  padding-top: 16px;
}

.ad-section-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;

  h4 {
    margin: 0;
    color: #111827;
    font-size: 15px;
    font-weight: 700;
  }

  p {
    margin: 4px 0 0;
    color: #6b7280;
    font-size: 12px;
    line-height: 1.5;
  }
}

.ad-toolbar-controls {
  flex: 0 1 auto;
  justify-content: flex-end;

  :deep(.el-button + .el-button) {
    margin-left: 0;
  }
}

.ad-search-control {
  width: 220px;
}

.ad-filter-control {
  width: 180px;
}

.ad-status-control {
  width: 130px;
}

.ad-primary-table {
  width: 100%;
  border-top: 1px solid #e5e7eb;

  :deep(th.el-table__cell) {
    height: 42px;
    color: #6b7280;
    font-size: 12px;
    font-weight: 600;
    background: #f8fafc;
  }

  :deep(td.el-table__cell) {
    padding: 10px 0;
  }

  :deep(.el-table__expanded-cell) {
    padding: 0 !important;
    background: #f8fafc;
  }
}

.primary-cell {
  min-width: 0;

  strong,
  small {
    display: block;
  }

  strong {
    overflow: hidden;
    color: #1f2937;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  small {
    margin-top: 3px;
    overflow: hidden;
    color: #9ca3af;
    font-size: 11px;
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.table-subline {
  display: block;
  margin-top: 3px;
  color: #9ca3af;
  font-size: 11px;
  line-height: 1.3;
}

.campaign-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  background: #e5e7eb;
}

.campaign-detail-block {
  min-width: 0;
  min-height: 96px;
  padding: 14px 18px;
  background: #f8fafc;

  strong,
  small {
    display: block;
  }

  strong {
    color: #374151;
    font-size: 13px;
  }

  small {
    margin-top: 6px;
    color: #6b7280;
    font-size: 11px;
  }
}

.detail-label {
  display: block;
  margin-bottom: 8px;
  color: #9ca3af;
  font-size: 11px;
  font-weight: 600;
}

.detail-tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;

  :deep(.el-tag) {
    max-width: 100%;
  }
}

.compact-tags {
  max-height: 58px;
  overflow: hidden;
}

.text-warning {
  color: #a15c00 !important;
}

.binding-detail-list {
  padding: 8px 18px;
}

.binding-detail-row {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) 72px 90px auto auto;
  gap: 12px;
  align-items: center;
  min-height: 48px;
  padding: 6px 0;
  border-bottom: 1px solid #e5e7eb;

  &:last-child {
    border-bottom: 0;
  }

  strong,
  small {
    display: block;
  }

  small {
    margin-top: 2px;
    overflow: hidden;
    color: #6b7280;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.ad-drawer-form {
  padding: 0 2px 20px;

  :deep(.el-select),
  :deep(.el-date-editor.el-input),
  :deep(.el-date-editor.el-input__wrapper) {
    width: 100%;
  }
}

.drawer-section-title {
  margin: 8px 0 14px;
  padding-bottom: 8px;
  color: #374151;
  font-size: 13px;
  font-weight: 700;
  border-bottom: 1px solid #e5e7eb;

  &:not(:first-child) {
    margin-top: 24px;
  }
}

.drawer-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 16px;
}

.input-suffix {
  margin-left: 8px;
  color: #6b7280;
  font-size: 12px;
}

.selected-target-summary {
  padding: 9px 11px;
  color: #0b7285;
  font-size: 12px;
  background: #e3fafc;
  border: 1px solid #bee3e8;
  border-radius: 6px;
}

.rich-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  width: 100%;

  span,
  small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  span {
    min-width: 0;
  }

  small {
    flex: 0 1 auto;
    color: #9ca3af;
    font-size: 11px;
    text-align: right;
  }
}

.binding-pool-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px;
  margin: 6px 0 16px;
  overflow: hidden;
  background: #dfe3e8;
  border: 1px solid #dfe3e8;
  border-radius: 6px;

  > div {
    padding: 12px;
    background: #f8fafc;
  }

  span,
  strong {
    display: block;
    text-align: center;
  }

  span {
    color: #6b7280;
    font-size: 11px;
  }

  strong {
    margin-top: 4px;
    color: #111827;
    font-size: 18px;
  }
}

.selected-creative-list {
  margin-bottom: 16px;
  border-top: 1px solid #e5e7eb;

  > div {
    display: grid;
    grid-template-columns: 140px minmax(0, 1fr);
    gap: 12px;
    padding: 9px 0;
    border-bottom: 1px solid #e5e7eb;
  }

  strong,
  span {
    overflow: hidden;
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  span {
    color: #6b7280;
  }
}

.drawer-secondary-action {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding-top: 12px;
  border-top: 1px solid #e5e7eb;

  span {
    color: #087f5b;
    font-size: 12px;
  }
}

@media (max-width: 1200px) {
  .ad-command-deck {
    grid-template-columns: 1fr;
  }

  .ad-readiness-panel {
    border-top: 1px solid #2d3339;
    border-left: 0;
  }
}
@media (max-width: 1500px) {
  .ad-metrics-band {
    grid-template-columns: repeat(3, minmax(160px, 1fr));
  }

  .ad-metric-item:nth-child(3) {
    border-right: 0;
  }

  .ad-metric-item:nth-child(n + 4) {
    border-top: 1px solid #e5e7eb;
  }
}

@media (max-width: 1100px) {
  .ad-command-topline,
  .ad-workbench-header,
  .ad-section-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .ad-header-actions,
  .ad-toolbar-controls {
    justify-content: flex-start;
  }

  .ad-policy-strip {
    grid-template-columns: repeat(3, minmax(120px, 1fr));
  }

  .ad-flow-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .ad-header-actions .el-button {
    flex: 1 1 calc(50% - 8px);
    margin: 0;
  }

  .ad-metrics-band {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .ad-metric-item,
  .ad-metric-item:nth-child(3) {
    border-right: 1px solid #e5e7eb;
  }

  .ad-metric-item:nth-child(even) {
    border-right: 0;
  }

  .ad-metric-item:nth-child(n + 3) {
    border-top: 1px solid #e5e7eb;
  }

  .ad-command-main,  .ad-readiness-panel,  .ad-flow-grid,  .ad-policy-strip,
  .campaign-detail-grid,
  .drawer-form-grid {
    grid-template-columns: 1fr;
  }

  .ad-toolbar-controls,
  .ad-search-control,
  .ad-filter-control,
  .ad-status-control {
    width: 100%;
  }

  .ad-toolbar-controls > * {
    flex: 1 1 100%;
    width: 100%;
  }

  .binding-detail-row {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .binding-pool-summary {
    grid-template-columns: repeat(3, minmax(80px, 1fr));
  }

  .rich-option small {
    display: none;
  }
}

@media (max-width: 480px) {
  .ad-metrics-band {
    grid-template-columns: 1fr;
  }

  .ad-metric-item,
  .ad-metric-item:nth-child(3) {
    min-height: 88px;
    border-right: 0;
    border-top: 1px solid #e5e7eb;
  }

  .ad-metric-item:first-child {
    border-top: 0;
  }

  .ad-alert-band {
    align-items: flex-start;
    flex-wrap: wrap;

    .el-button {
      margin-left: 22px;
    }
  }

  .binding-pool-summary {
    grid-template-columns: 1fr;
  }

  .selected-creative-list > div {
    grid-template-columns: 1fr;
    gap: 3px;
  }
}
</style>
