<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Connection,
  Document,
  Plus,
  Refresh,
  Select,
  Setting,
  Timer,
  VideoPlay,
} from "@element-plus/icons-vue";
import { useRouter } from 'vue-router'
import { accountsApi } from '@/api/accounts'
import { groupsApi, type Group } from '@/api/groups'
import {
  automationApi,
  type AccountAdBinding,
  type AccountOperationConfig,
  type AccountOperationMode,
  type AdDeliveryExecutionSettings,
  type AdDeliveryThrottleSettings,
  type AdFailurePolicy,
  type AdDynamicStatus,
  type AutoJoinSchedulerConfig,
  type AdCampaign,
  type AdCreative,
  type GroupAdProfile,
  type AutomationRunResult,
} from '@/api/automation'
import { createDefaultAdDeliveryExecution, createDefaultAdDeliveryThrottle, createDefaultAdFailurePolicy, createDefaultAutoJoinScheduler } from '@/config/automationDefaults'
import { DEFAULT_GROUP_SEARCH_KEYWORD_TYPES, GROUP_SEARCH_KEYWORD_TYPE_OPTIONS } from '@/api/keywords'

import ClientListPagination from '@/components/ClientListPagination.vue'
import AdOnlyRecommendationPanel from '@/components/AdOnlyRecommendationPanel.vue'
import { useClientPagination } from '@/utils/clientPagination'
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
const creatives = ref<AdCreative[]>([])
const campaigns = ref<AdCampaign[]>([])
const bindings = ref<AccountAdBinding[]>([])
const targetGroups = ref<Group[]>([])
let groupProfilesRefreshTimer: number | null = null
const groupAdProfiles = ref<GroupAdProfile[]>([])
const dynamicStatuses = ref<AdDynamicStatus[]>([])
const accounts = ref<AccountOption[]>([])
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

const autoJoinForm = reactive({
  max_accounts: 10,
  keywords_per_account: 10,
  max_groups_per_keyword: 20,
  dry_run: true,
})

const schedulerConfigForm = reactive(createDefaultAutoJoinScheduler())

const adRunForm = reactive({
  max_deliveries: 20,
  dry_run: true,
})

const adFailurePolicyForm = reactive(createDefaultAdFailurePolicy())


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
  delivery_policy: 'growth' as 'growth' | 'ad_only',
  send_mode: 'after_join' as 'after_join' | 'interval' | 'scheduled',
  target_group_levels: ['A'],
  target_group_ids: [] as number[],
  start_at: '',
  end_at: '',
  min_wait_after_join_minutes: 60,
  interval_minutes: 180,
})

const emptyCampaignForm = () => ({
  name: '',
  enabled: false,
  status: 'draft',
  delivery_policy: 'growth' as 'growth' | 'ad_only',
  send_mode: 'after_join' as 'after_join' | 'interval' | 'scheduled',
  target_group_levels: ['A'],
  target_group_ids: [] as number[],
  start_at: '',
  end_at: '',
  min_wait_after_join_minutes: 60,
  interval_minutes: 180,
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
const accountConfigDialogVisible = ref(false)
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


const accountConfigForm = reactive({
  operation_mode: 'growth' as AccountOperationMode,
  enabled: true,
  auto_join_enabled: false,
  auto_ads_enabled: true,
  max_groups_per_day: 100,
  max_groups_total: 100,
  join_interval_min_seconds: 60,
  join_interval_max_seconds: 900,
  max_messages_per_day: null as number | null,
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
  router.push({ path: '/growth-settings', query: { config } })
}

const goGroupAdPolicy = () => {
  router.push({ path: '/groups', query: { tab: 'ad-policy' } })
}

const goAccountOperations = () => {
  router.push({ path: '/accounts', query: { tab: 'operations' } })
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
        Number(profile.ad_policy_confidence || 0) >= 80,
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

const unboundCampaignCount = computed(
  () =>
    campaigns.value.filter(
      (campaign) => campaign.enabled && !campaignBindingStats.value.has(campaign.id),
    ).length,
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
const campaignPaginationSource = computed(() => filteredCampaigns.value)
const creativePaginationSource = computed(() => creatives.value)
const bindingPaginationSource = computed(() => filteredBindingGroups.value)
const {
  page: campaignPage,
  pageSize: campaignPageSize,
  total: campaignTotal,
  rows: pagedCampaigns,
} = useClientPagination(campaignPaginationSource, 10)
const {
  page: creativePage,
  pageSize: creativePageSize,
  total: creativeTotal,
  rows: pagedCreatives,
} = useClientPagination(creativePaginationSource, 10)
const {
  page: bindingPage,
  pageSize: bindingPageSize,
  total: bindingTotal,
  rows: pagedBindingGroups,
} = useClientPagination(bindingPaginationSource, 10)

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
  if (campaign.delivery_policy !== "ad_only") return "群全局冷却 24 小时";
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


const accountMap = computed(() => {
  return new Map(accounts.value.map((item) => [item.id, item]))
})

const targetGroupMap = computed(() => new Map(targetGroups.value.map((item) => [item.id, item])))
const accountOperationModeMap = computed(() =>
  new Map(dynamicStatuses.value.map((item) => [item.account_id, item.operation_mode])),
)
const targetGroupJoinAccounts = computed(() =>
  accounts.value.filter(
    (account) =>
      account.is_active !== false
      && !['banned', 'error'].includes(account.status || '')
      && accountOperationModeMap.value.get(account.id) !== 'ad_only',
  ),
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


const fillAdDeliveryExecutionForm = (config: AdDeliveryExecutionSettings) => {
  Object.assign(adDeliveryExecutionForm, config)
}

const fillAdDeliveryThrottleForm = (config: AdDeliveryThrottleSettings) => {
  Object.assign(adDeliveryThrottleForm, config)
}

const loadAccounts = async () => {
  const payload = await accountsApi.list({ limit: 100, account_type: 'promoter' })
  accounts.value = payload.list
  const availableAccounts = adBindingAccounts.value
  if (!availableAccounts.some((account) => account.id === selectedAccountId.value)) {
    selectedAccountId.value = availableAccounts[0]?.id
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
    delivery_policy: campaign.delivery_policy || 'growth',
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

const openAccountConfig = async (accountId?: number) => {
  const fallbackAccount = adBindingAccounts.value[0]
  const nextAccount = adBindingAccounts.value.find((account) => account.id === accountId) || fallbackAccount
  if (!nextAccount) {
    ElMessage.warning('当前没有可配置的推广账号')
    return
  }
  accountConfigDialogVisible.value = true
  if (selectedAccountId.value === nextAccount.id) {
    await loadAccountConfig(nextAccount.id)
  } else {
    selectedAccountId.value = nextAccount.id
  }
}

const saveAccountConfig = async () => {
  const selectedAccount = adBindingAccounts.value.find((account) => account.id === selectedAccountId.value)
  if (!selectedAccount) {
    ElMessage.warning('只能配置正常、启用中的推广账号')
    return
  }
  if (!validateAccountConfigForm()) return

  savingAccountConfig.value = true
  try {
    const response = await automationApi.updateAccountOperationConfig(selectedAccount.id, accountConfigPayload())
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
  batchAccountIds.value = adBindingAccounts.value.map((item) => item.id)
}

const clearBatchAccounts = () => {
  batchAccountIds.value = []
}

const refreshData = async () => {
  loading.value = true
  try {
    const [creativesRes, campaignsRes, bindingsRes, groupProfilesRes] = await Promise.all([
      automationApi.getCreatives({ page_size: 50 }),
      automationApi.getCampaigns({ page_size: 50 }),
      automationApi.getBindings(),
      automationApi.getGroupAdProfiles(),
    ])
    const dynamicStatusRes = await automationApi.getAdDynamicStatus()
    creatives.value = creativesRes.data.data
    campaigns.value = campaignsRes.data.data
    bindings.value = bindingsRes.data.data
    groupAdProfiles.value = groupProfilesRes.data.data
    dynamicStatuses.value = dynamicStatusRes.data.data
    creativePoolStatus.value = null
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
  if (campaignForm.delivery_policy === "ad_only" && !campaignForm.target_group_ids.length) {
    ElMessage.warning("Ad-only 活动必须指定目标群");
    return;
  }
  if (
    campaignForm.delivery_policy === "ad_only"
    && !["interval", "scheduled"].includes(campaignForm.send_mode)
  ) {
    ElMessage.warning("Ad-only 活动必须设置固定间隔或每日定时");
    return;
  }
  if (
    campaignForm.delivery_policy === "ad_only"
    && campaignForm.send_mode === "scheduled"
    && !scheduledTimes.length
  ) {
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
    ElMessage.success(`已加入并添加 ${groupName}`);
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

const openDeliveryFromAds = (campaignId?: number, accountId?: number) => {
  router.push({
    path: '/growth-logs',
    query: {
      tab: 'delivery',
      campaign_id: campaignId,
      account_id: accountId,
    },
  })
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

watch(() => campaignForm.delivery_policy, (policy) => {
  if (policy === 'ad_only' && campaignForm.send_mode === 'after_join') {
    campaignForm.send_mode = 'interval'
  }
})

watch(selectedAccountId, async (accountId) => {
  if (!accountId) return
  const selectedAccount = adBindingAccounts.value.find((account) => account.id === accountId)
  if (!selectedAccount) return
  bindingForm.account_ids = [accountId]
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

        </div>
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
                  :class="{ active: adWorkspaceView === 'bindings' }"
                  @click="adWorkspaceView = 'bindings'"
                >
                  <span class="flow-step-icon flow-green"><Connection /></span>
                  <span>
                    <small>02 / 账号绑定</small>
                    <strong>{{ enabledBindingCount }} 条生效</strong>
                    <em>{{ bindingGroups.length }} 组绑定</em>
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
                    <small>03 / 素材</small>
                    <strong>{{ enabledCreativeCount }} 个启用</strong>
                    <em>{{ creatives.length }} 个总素材</em>
                  </span>
                </button>
                <button
                  type="button"
                  class="ad-flow-step"
                  @click="goGroupAdPolicy"
                >
                  <span class="flow-step-icon flow-amber"><Connection /></span>
                  <span>
                    <small>04 / 群池资格</small>
                    <strong>{{ adAllowedGroupCount }} 个可投</strong>
                    <em>{{ pendingGroupPolicyCount }} 个待确认</em>
                  </span>
                </button>
                <button
                  type="button"
                  class="ad-flow-step"
                  :class="{ active: adWorkspaceView === 'handovers' }"
                  @click="adWorkspaceView = 'handovers'"
                >
                  <span class="flow-step-icon flow-amber"><Connection /></span>
                  <span>
                    <small>05 / 专用交接</small>
                    <strong>候选审批</strong>
                    <em>可恢复交接</em>
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
                <span class="ad-readiness-links">
                  <el-button link type="primary" @click="goAccountOperations">推广账号</el-button>
                  <el-button link type="primary" @click="goGroupAdPolicy">群池资格</el-button>
                </span>
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
              <span>广告许可群</span>
              <strong>{{ adAllowedGroupCount }}</strong>
              <small>{{ pendingGroupPolicyCount }} 个待确认</small>
            </div>
            <div>
              <span>调度间隔</span>
              <strong>{{ adDeliveryExecutionForm.dispatcher_interval_seconds }} 秒</strong>
              <small>同账号串行，跨账号并行</small>
            </div>
            <div>
              <span>Growth 群全局冷却</span>
              <strong>{{ Math.round(adDeliveryExecutionForm.growth_group_global_cooldown_seconds / 3600) }} 小时</strong>
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
                  :data="pagedCampaigns"
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
                        <strong>{{ row.delivery_policy === 'ad_only' ? 'Ad-only' : 'Growth' }}</strong>
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
                <ClientListPagination
                  v-model:page="campaignPage"
                  v-model:page-size="campaignPageSize"
                  :total="campaignTotal"
                />
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
                  :data="pagedCreatives"
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
                <ClientListPagination
                  v-model:page="creativePage"
                  v-model:page-size="creativePageSize"
                  :total="creativeTotal"
                />
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
                    <el-button @click="openAccountConfig()">
                      <el-icon><Setting /></el-icon>
                      账号配置
                    </el-button>

                <el-table
                  :data="pagedBindingGroups"
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
                  <el-table-column label="操作" width="310" fixed="right">
                    <template #default="{ row }">
                      <el-button link type="primary" @click="openAccountConfig(row.accountId)">
                        账号配置
                      </el-button>
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
                <ClientListPagination
                  v-model:page="bindingPage"
                  v-model:page-size="bindingPageSize"
                  :total="bindingTotal"
                />
              </section>
            </el-tab-pane>

            <el-tab-pane name="handovers" lazy>
              <template #label>
                <span class="ad-tab-label"><Connection />专用交接</span>
              </template>
              <AdOnlyRecommendationPanel />
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
              <el-form-item label="投放策略">
                <el-segmented
                  v-model="campaignForm.delivery_policy"
                  :disabled="Boolean(editingCampaignId && campaignForm.enabled)"
                  :options="[
                    { label: 'Growth', value: 'growth' },
                    { label: 'Ad-only', value: 'ad_only' },
                  ]"
                />
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
                v-if="campaignForm.delivery_policy === 'growth' && !campaignForm.target_group_ids.length"
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

              <template v-if="campaignForm.delivery_policy === 'ad_only'">
                <div class="drawer-section-title">发送节奏</div>
                <el-form-item label="发送模式">
                  <el-segmented
                    v-model="campaignForm.send_mode"
                    :options="[
                      { label: '固定间隔', value: 'interval' },
                      { label: '每日定时', value: 'scheduled' },
                    ]"
                  />
                </el-form-item>
                <el-form-item
                  v-if="campaignForm.send_mode === 'interval'"
                  label="每群发送间隔"
                >
                  <el-input-number
                    v-model="campaignForm.interval_minutes"
                    :min="50"
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
              </template>
              <div class="drawer-section-title">有效期</div>
              <div class="drawer-form-grid">

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
            v-model="accountConfigDialogVisible"
            title="账号运营配置"
            width="min(920px, 96vw)"
            destroy-on-close
          >
            <div v-loading="accountConfigLoading">
              <div class="account-toolbar">
                <el-select
                  v-model="selectedAccountId"
                  filterable
                  placeholder="选择可用推广账号"
                  class="account-select"
                >
                  <el-option
                    v-for="item in adBindingAccounts"
                    :key="item.id"
                    :label="accountLabel(item.id)"
                    :value="item.id"
                  />
                </el-select>
                <el-button :icon="Select" @click="loadAccountConfig(selectedAccountId)">读取配置</el-button>
              </div>

              <el-form label-position="top" class="account-config-form">
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
                  <el-form-item v-if="!isAdOnlyAccount" label="每日最大加群数">
                    <el-input-number v-model="accountConfigForm.max_groups_per_day" :min="0" :max="1000" />
                  </el-form-item>
                  <el-form-item v-if="!isAdOnlyAccount" label="账号总群上限">
                    <el-input-number v-model="accountConfigForm.max_groups_total" :min="0" :max="10000" />
                  </el-form-item>
                  <el-form-item v-if="!isAdOnlyAccount" label="加群最小间隔(秒)">
                    <el-input-number v-model="accountConfigForm.join_interval_min_seconds" :min="60" :max="86400" />
                  </el-form-item>
                  <el-form-item v-if="!isAdOnlyAccount" label="加群最大间隔(秒)">
                    <el-input-number v-model="accountConfigForm.join_interval_max_seconds" :min="60" :max="86400" />
                  </el-form-item>
                  <el-form-item label="每日出站消息硬上限">
                    <el-input-number
                      v-model="accountConfigForm.max_messages_per_day"
                      :min="1"
                      :max="20000"
                      clearable
                      placeholder="留空使用配置中心默认值"
                    />
                  </el-form-item>
                  <el-form-item label="消息发送间隔(秒)">
                    <el-input-number v-model="accountConfigForm.message_interval_seconds" :min="1" :max="86400" />
                  </el-form-item>
                  <el-form-item v-if="!isAdOnlyAccount" label="关键词不足自动补充">
                    <el-switch v-model="accountConfigForm.keyword_auto_replenish_enabled" />
                  </el-form-item>
                  <el-form-item v-if="!isAdOnlyAccount" label="补充后需要审核">
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
                      <span v-if="selectedDynamicStatus" class="muted-text">
                        分数 {{ selectedDynamicStatus.risk_score }}
                      </span>
                    </div>
                  </el-form-item>
                  <el-form-item label="业务状态">
                    <el-tag :type="businessStageType(accountConfigForm.business_stage)">
                      {{ businessStageText(accountConfigForm.business_stage) }}
                    </el-tag>
                  </el-form-item>
                </div>

                <el-form-item v-if="!isAdOnlyAccount" label="搜群关键词类型">
                  <el-checkbox-group v-model="accountConfigForm.keyword_types">
                    <el-checkbox v-for="item in keywordTypeOptions" :key="item.value" :value="item.value">
                      {{ item.label }}
                    </el-checkbox>
                  </el-checkbox-group>
                </el-form-item>
                <el-form-item label="下次允许加群时间">
                  <el-tag type="info">{{ accountConfigForm.next_join_after || '未设置' }}</el-tag>
                </el-form-item>
              </el-form>

              <el-divider content-position="left">批量套用</el-divider>
              <div class="batch-toolbar">
                <el-select
                  v-model="batchAccountIds"
                  multiple
                  filterable
                  collapse-tags
                  collapse-tags-tooltip
                  placeholder="选择可用推广账号"
                  class="batch-account-select"
                >
                  <el-option
                    v-for="item in adBindingAccounts"
                    :key="item.id"
                    :label="accountLabel(item.id)"
                    :value="item.id"
                  />
                </el-select>
                <el-button @click="selectAllBatchAccounts">全选</el-button>
                <el-button @click="clearBatchAccounts">清空</el-button>
                <el-button :loading="savingBatchAccountConfig" @click="saveBatchAccountConfig">批量套用</el-button>
              </div>
            </div>
            <template #footer>
              <el-button @click="accountConfigDialogVisible = false">取消</el-button>
              <el-button type="primary" :loading="savingAccountConfig" @click="saveAccountConfig">保存配置</el-button>
            </template>
          </el-dialog>

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

    </el-tabs>

    <el-dialog v-model="targetGroupDialogVisible" title="通过链接加入群" width="520px">
      <el-form label-width="110px">
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
              v-for="item in targetGroupJoinAccounts"
              :key="item.id"
              :label="accountLabel(item.id)"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="targetGroupDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingTargetGroup" @click="saveTargetGroup">
          加入并添加
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
  grid-template-columns: repeat(5, minmax(0, 1fr));
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
