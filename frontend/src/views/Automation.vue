<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Close, Delete, Edit, Plus, Refresh, Select, Setting, VideoPause, VideoPlay } from '@element-plus/icons-vue'
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
  type AutomationRunResult,
} from '@/api/automation'
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
const dynamicStatuses = ref<AdDynamicStatus[]>([])
const accounts = ref<AccountOption[]>([])
const failoverTargetAccounts = computed(() =>
  accounts.value.filter(
    (account) => account.is_active && account.status !== 'banned' && account.status !== 'error',
  ),
)
const adBindingAccounts = computed(() =>
  accounts.value.filter((account) => account.status !== 'banned'),
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

const schedulerConfigForm = reactive({
  enabled: true,
  scan_interval_minutes: 1,
  search_filter: {
    title_blacklist_enabled: true,
    title_blacklist: [] as string[],
  },
  join_verification: {
    enabled: true,
    ai_enabled: true,
    confidence_threshold: 0.72,
    post_action_wait_seconds: 8,
    post_action_recheck_attempts: 3,
    post_action_extra_wait_seconds: 12,
    message_limit: 20,
    ai_timeout_seconds: 45,
    action_timeout_seconds: 5,
    pending_sync_min_age_seconds: 120,
    pending_sync_limit: 5,
    unknown_challenge_action: 'leave' as 'leave' | 'manual' | 'wait' | 'skip',
    allow_button_clicks: true,
    allow_text_answers: true,
    answer_profile: '中文用户，主要为了学习交流、找资料、行业沟通。',
  },
  group_capacity_cleanup: {
    enabled: false,
    no_conversion_days: 30,
    min_join_age_days: 30,
    max_cleanup_per_run: 15,
  },
})

const adRunForm = reactive({
  max_deliveries: 20,
  dry_run: true,
})

const adFailurePolicyForm = reactive<AdFailurePolicy>({
  enabled: true,
  leave_on_group_control_failure: true,
  group_control_failure_limit: 3,
  group_control_failure_window_hours: 24,
  levels: ['B'],
})

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

const defaultRiskActions = () =>
  Object.fromEntries(
    riskActionOptions.map((item) => [
      item.value,
      {
        daily_limit:
          item.value === 'search'
            ? 100
            : item.value === 'join'
              ? 6
              : item.value === 'group_message'
                ? 4
                : item.value === 'ad_delivery'
                  ? 5
                  : 1,
        cooldown_seconds:
          item.value === 'search'
            ? 30
            : item.value === 'join' || item.value === 'group_message'
              ? 7200
              : item.value === 'ad_probe' || item.value === 'channel_create'
                ? 86400
                : item.value === 'ai_warmup'
                  ? 21600
                  : item.value === 'ad_delivery'
                    ? 9000
                    : 0,
      },
    ]),
  )

const accountRiskGuardForm = reactive<AccountRiskGuardSettings>({
  enabled: true,
  global_daily_limit: 30,
  group_write_daily_limit: 8,
  redis_fail_closed: null,
  actions: defaultRiskActions(),
  level_thresholds: { watch: 20, limited: 45, frozen: 70, quarantined: 90 },
  level_budget_multipliers: { normal: 1, watch: 0.7, limited: 0.45, frozen: 0, quarantined: 0 },
  risk_score_deltas: {
    group_write_forbidden: 4,
    platform_group_write_forbidden: 12,
    flood_wait: 15,
    peer_flood: 35,
    account_banned: 50,
    account_restricted: 50,
    generic_failure: 5,
    block: 1,
  },
  lifecycle: {
    default_freeze_seconds: 3600,
    flood_wait_buffer_seconds: 60,
    peer_flood_freeze_seconds: 86400,
    account_restricted_freeze_seconds: 86400,
    group_write_forbidden_freeze_seconds: 43200,
    recovery_seconds: 86400,
    post_freeze_score_cap: 69,
    manual_clear_score_cap: 44,
    decay_interval_hours: 24,
    decay_points_per_interval: 8,
    new_account_days: 3,
    new_account_multiplier: 0.3,
    recovery_multiplier: 0.5,
    healthy_account_days: 14,
    healthy_account_multiplier: 1,
    max_budget_multiplier: 1,
  },
  group_write_forbidden: {
    freeze_window_hours: 2,
    freeze_distinct_groups: 5,
    quarantine_window_hours: 24,
    quarantine_distinct_groups: 10,
  },
  retention: {
    low_value_detail_retention_days: 14,
    high_value_detail_retention_days: 90,
    daily_stat_retention_days: 370,
  },
})

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

const defaultWarmupTiers = (): AccountWarmupPolicySettings['tiers'] => ({
  unknown: { warmup_days: 15 },
  month_1: { warmup_days: 18 },
  month_3_6: { warmup_days: 12 },
  year_1: { warmup_days: 9 },
  year_2: { warmup_days: 7 },
  year_3_plus: { warmup_days: 7 },
})

const defaultWarmupStages = (): AccountWarmupPolicySettings['stages'] => ({
  observe: {
    limit_multiplier: 0.08,
    join_multiplier: 0,
    ad_multiplier: 0,
    run_multiplier: 0,
    probe_multiplier: 0.1,
    private_message_multiplier: 0,
    group_message_multiplier: 0.05,
    profile_update_multiplier: 0.2,
    allow_proactive_private_message: false,
  },
  seed: {
    limit_multiplier: 0.15,
    join_multiplier: 0.15,
    ad_multiplier: 0,
    run_multiplier: 0,
    probe_multiplier: 0.25,
    private_message_multiplier: 0,
    group_message_multiplier: 0.15,
    profile_update_multiplier: 0.5,
    allow_proactive_private_message: false,
  },
  soft: {
    limit_multiplier: 0.35,
    join_multiplier: 0.35,
    ad_multiplier: 0.25,
    run_multiplier: 0.25,
    probe_multiplier: 0.45,
    private_message_multiplier: 0.1,
    group_message_multiplier: 0.35,
    profile_update_multiplier: 0.75,
    allow_proactive_private_message: false,
  },
  ramp: {
    limit_multiplier: 0.65,
    join_multiplier: 0.65,
    ad_multiplier: 0.65,
    run_multiplier: 0.65,
    probe_multiplier: 0.75,
    private_message_multiplier: 0.25,
    group_message_multiplier: 0.65,
    profile_update_multiplier: 1,
    allow_proactive_private_message: false,
  },
  normal: {
    limit_multiplier: 1,
    join_multiplier: 1,
    ad_multiplier: 1,
    run_multiplier: 1,
    probe_multiplier: 1,
    private_message_multiplier: 1,
    group_message_multiplier: 1,
    profile_update_multiplier: 1,
    allow_proactive_private_message: true,
  },
  cooldown: {
    limit_multiplier: 0,
    join_multiplier: 0,
    ad_multiplier: 0,
    run_multiplier: 0,
    probe_multiplier: 0,
    private_message_multiplier: 0,
    group_message_multiplier: 0,
    profile_update_multiplier: 0,
    allow_proactive_private_message: false,
  },
})

const accountWarmupPolicyForm = reactive<AccountWarmupPolicySettings>({
  enabled: true,
  default_warmup_days: 15,
  minimum_warmup_days: 5,
  user_initiated_private_message_multiplier: 1,
  tiers: defaultWarmupTiers(),
  stages: defaultWarmupStages(),
})

const adDeliveryExecutionForm = reactive<AdDeliveryExecutionSettings>({
  enabled: true,
  dispatcher_interval_seconds: 60,
  max_deliveries_per_run: 20,
  max_deliveries_per_account_per_run: 5,
  group_campaign_cooldown_minutes: 180,
  stop_account_after_success: false,
  stop_account_after_failure: true,
})

const adDeliveryThrottleForm = reactive<AdDeliveryThrottleSettings>({
  enabled: true,
  delivery_interval_seconds: 0,
  batch_window_seconds: 180,
  batch_size_min: 200,
  batch_size_max: 200,
  cooldown_min_seconds: 0,
  cooldown_max_seconds: 0,
})

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

const adPolicySummary = computed<PolicySummaryItem[]>(() => [
  { label: '自动投放', value: booleanText(adDeliveryExecutionForm.enabled), type: booleanTagType(adDeliveryExecutionForm.enabled) },
  { label: '执行间隔', value: `${adDeliveryExecutionForm.dispatcher_interval_seconds} 秒` },
  { label: '单轮上限', value: adDeliveryExecutionForm.max_deliveries_per_run },
  { label: '单号单轮', value: adDeliveryExecutionForm.max_deliveries_per_account_per_run },
  { label: '节流策略', value: booleanText(adDeliveryThrottleForm.enabled), type: booleanTagType(adDeliveryThrottleForm.enabled) },
  { label: '发送间隔', value: `${adDeliveryThrottleForm.delivery_interval_seconds} 秒` },
  { label: '失败策略', value: booleanText(adFailurePolicyForm.enabled), type: booleanTagType(adFailurePolicyForm.enabled) },
  { label: '失败退群', value: booleanText(adFailurePolicyForm.leave_on_group_control_failure), type: booleanTagType(adFailurePolicyForm.leave_on_group_control_failure) },
  { label: '失败阈值', value: `${adFailurePolicyForm.group_control_failure_limit} 次/${adFailurePolicyForm.group_control_failure_window_hours} 小时` },
])

const adPolicyLoading = computed(() => adExecutionLoading.value || adThrottleLoading.value || failurePolicyLoading.value)

const accountMap = computed(() => {
  return new Map(accounts.value.map((item) => [item.id, item]))
})

const targetGroupMap = computed(() => new Map(targetGroups.value.map((item) => [item.id, item])))

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
  return `${group.title || identity} · ${identity}`
}

const campaignTargetLabel = (campaign: AdCampaign) => {
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
    (group) => group.status === 'active' && group.accountCount > 0,
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

const creativePoolCount = computed(() => creatives.value.filter((item) => item.enabled).length)

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
  Object.assign(creativeForm, emptyCreativeForm())
  editingCreativeId.value = null
}

const editCreative = (creative: AdCreative) => {
  editingCreativeId.value = creative.id
  Object.assign(creativeForm, {
    name: creative.name,
    content: creative.content,
    creative_type: creative.creative_type,
    media_url: creative.media_url || '',
    link_url: creative.link_url || '',
    weight: creative.weight,
    enabled: creative.enabled,
  })
}

const resetCampaignForm = () => {
  Object.assign(campaignForm, emptyCampaignForm())
  scheduledTimesText.value = ''
  editingCampaignId.value = null
}

const editCampaign = (campaign: AdCampaign) => {
  editingCampaignId.value = campaign.id
  Object.assign(campaignForm, {
    name: campaign.name,
    enabled: campaign.enabled,
    status: campaign.status,
    send_mode: campaign.send_mode,
    target_group_levels: campaign.target_group_levels?.length ? campaign.target_group_levels : ['A'],
    target_group_ids: (campaign.target_group_ids || []).filter((groupId) =>
      targetGroupMap.value.has(groupId),
    ),
    start_at: campaign.start_at || '',
    end_at: campaign.end_at || '',
    min_wait_after_join_minutes: campaign.min_wait_after_join_minutes,
    interval_minutes: campaign.interval_minutes,
    max_sends_per_group_per_day: campaign.max_sends_per_group_per_day,
    max_sends_per_account_per_day: campaign.max_sends_per_account_per_day,
  })
  scheduledTimesText.value = campaign.scheduled_times?.join(',') || ''
}

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
    const [attemptsRes, verificationLogsRes, creativesRes, campaignsRes, bindingsRes] = await Promise.all([
      automationApi.getAutoJoinAttempts({ limit: 30 }),
      automationApi.getAutoJoinVerificationLogs({ limit: 30 }),
      automationApi.getCreatives({ page_size: 50 }),
      automationApi.getCampaigns({ page_size: 50 }),
      automationApi.getBindings(),
    ])
    const dynamicStatusRes = await automationApi.getAdDynamicStatus()
    autoJoinAttempts.value = attemptsRes.data.data
    autoJoinVerificationLogs.value = verificationLogsRes.data.data
    creatives.value = creativesRes.data.data
    campaigns.value = campaignsRes.data.data
    bindings.value = bindingsRes.data.data
    await loadGroupFailovers()
    dynamicStatuses.value = dynamicStatusRes.data.data
    creativePoolStatus.value = null
    await loadDeliveryLogs()
  } finally {
    loading.value = false
  }
}

const refreshPage = async () => {
  loading.value = true
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
    ])
    if (selectedAccountId.value) {
      await loadAccountConfig(selectedAccountId.value)
    }
  } finally {
    loading.value = false
  }
}

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

const runAds = () => {
  runTask('ads', () => automationApi.runAds({ ...adRunForm }))
}

const saveCreative = async () => {
  if (!creativeForm.name || !creativeForm.content) {
    ElMessage.warning('请填写广告名称和内容')
    return
  }
  savingCreative.value = true
  try {
    if (editingCreativeId.value) {
      await automationApi.updateCreative(editingCreativeId.value, { ...creativeForm })
      ElMessage.success('广告素材已更新')
    } else {
      await automationApi.createCreative({ ...creativeForm })
      ElMessage.success('广告素材已创建')
    }
    resetCreativeForm()
    await refreshData()
  } finally {
    savingCreative.value = false
  }
}

const toggleCreative = async (creative: AdCreative) => {
  await automationApi.updateCreative(creative.id, { enabled: !creative.enabled })
  ElMessage.success(creative.enabled ? '素材已停用' : '素材已启用')
  await refreshData()
}

const deleteCreative = async (creative: AdCreative) => {
  await ElMessageBox.confirm(`确认删除素材「${creative.name}」？相关绑定会失去该素材。`, '删除素材', {
    type: 'warning',
  })
  await automationApi.deleteCreative(creative.id)
  if (editingCreativeId.value === creative.id) resetCreativeForm()
  ElMessage.success('广告素材已删除')
  await refreshData()
}

const cleanupInvalidCreatives = async () => {
  const response = await automationApi.cleanupInvalidCreatives()
  const count = response.data.data.disabled_count
  ElMessage.success(count > 0 ? `已停用 ${count} 条异常素材` : '未发现异常素材')
  await refreshData()
}

const ensureCreativePool = async () => {
  if (!bindingForm.account_ids.length || !bindingForm.ad_campaign_id) {
    ElMessage.warning('请先选择账号和广告计划')
    return
  }

  const poolStatuses = []
  for (const accountId of bindingForm.account_ids) {
    const response = await automationApi.ensureCreativePool({
      account_id: accountId,
      ad_campaign_id: bindingForm.ad_campaign_id,
      min_pool_size: 3,
      generate_count: 3,
    })
    poolStatuses.push(response.data.data)
  }

  const summary: CreativePoolSummary = {
    account_count: poolStatuses.length,
    pool_size: Math.min(...poolStatuses.map((item) => item.pool_size)),
    created_count: poolStatuses.reduce((total, item) => total + item.created_count, 0),
    creative_ids: poolStatuses.flatMap((item) => item.creative_ids),
  }
  await refreshData()
  creativePoolStatus.value = summary
  ElMessage.success(`已检查 ${summary.account_count} 个账号的素材池，共新增 ${summary.created_count} 条`)
}

const saveCampaign = async () => {
  if (!campaignForm.name) {
    ElMessage.warning('请填写广告计划名称')
    return
  }
  const scheduledTimes = parseScheduledTimes()
  if (campaignForm.send_mode === 'scheduled' && !scheduledTimes.length) {
    ElMessage.warning('请至少填写一个定时时点')
    return
  }
  const payload = {
    ...campaignForm,
    start_at: campaignForm.start_at || undefined,
    end_at: campaignForm.end_at || undefined,
    scheduled_times: scheduledTimes,
  }
  savingCampaign.value = true
  try {
    if (editingCampaignId.value) {
      await automationApi.updateCampaign(editingCampaignId.value, payload)
      ElMessage.success('广告计划已更新')
    } else {
      await automationApi.createCampaign(payload)
      ElMessage.success('广告计划已创建')
    }
    resetCampaignForm()
    await refreshData()
  } finally {
    savingCampaign.value = false
  }
}

const openTargetGroupDialog = () => {
  Object.assign(targetGroupForm, {
    groupLink: '',
    accountId: selectedAccountId.value,
  })
  targetGroupDialogVisible.value = true
}

const saveTargetGroup = async () => {
  const groupLink = targetGroupForm.groupLink.trim()
  if (!groupLink) {
    ElMessage.warning('请输入 Telegram 群链接')
    return
  }
  if (!targetGroupForm.accountId) {
    ElMessage.warning('请选择执行入群的推广账号')
    return
  }

  savingTargetGroup.value = true
  try {
    const response = await groupsApi.joinByLink({
      groupLink,
      accountId: targetGroupForm.accountId,
    })
    const group = response.data.data
    await loadTargetGroups()
    if (!campaignForm.target_group_ids.includes(group.id)) {
      campaignForm.target_group_ids.push(group.id)
    }
    targetGroupDialogVisible.value = false
    ElMessage.success(`已加入并添加 ${group.title || group.username || group.chatId}`)
  } finally {
    savingTargetGroup.value = false
  }
}

const toggleCampaign = async (campaign: AdCampaign) => {
  await automationApi.updateCampaign(campaign.id, {
    enabled: !campaign.enabled,
    status: campaign.enabled ? 'paused' : 'active',
  })
  ElMessage.success(campaign.enabled ? '广告计划已停止' : '广告计划已启动')
  await refreshData()
}

const deleteCampaign = async (campaign: AdCampaign) => {
  await ElMessageBox.confirm(`确认删除计划「${campaign.name}」？计划下的绑定也会删除。`, '删除计划', {
    type: 'warning',
  })
  await automationApi.deleteCampaign(campaign.id)
  if (editingCampaignId.value === campaign.id) resetCampaignForm()
  ElMessage.success('广告计划已删除')
  await refreshData()
}

const createBinding = async () => {
  if (!bindingForm.account_ids.length || !bindingForm.ad_campaign_id) {
    ElMessage.warning('请选择账号和广告计划')
    return
  }
  if (!bindingForm.creative_ids.length) {
    ElMessage.warning('请至少选择一个素材')
    return
  }
  const response = await automationApi.createBindingsBatch({
    account_ids: bindingForm.account_ids,
    ad_campaign_id: bindingForm.ad_campaign_id,
    creative_ids: bindingForm.creative_ids,
    enabled: bindingForm.enabled,
    priority: bindingForm.priority,
  })
  const expectedCount = bindingForm.account_ids.length * bindingForm.creative_ids.length
  const createdCount = response.data.data.length
  const existingCount = expectedCount - createdCount
  ElMessage.success(
    existingCount > 0
      ? `已创建 ${createdCount} 条绑定，跳过 ${existingCount} 条已有绑定`
      : `已为 ${bindingForm.account_ids.length} 个账号创建 ${createdCount} 条绑定`,
  )
  Object.assign(bindingForm, {
    account_ids: selectedAccountId.value ? [selectedAccountId.value] : [],
    ad_campaign_id: undefined,
    creative_ids: [],
    enabled: true,
    priority: 0,
  })
  await refreshData()
}

const toggleBinding = async (binding: AccountAdBinding) => {
  await automationApi.updateBinding(binding.id, { enabled: !binding.enabled })
  ElMessage.success(binding.enabled ? '绑定已停用' : '绑定已启用')
  await refreshData()
}

const deleteBinding = async (binding: AccountAdBinding) => {
  await ElMessageBox.confirm('确认删除这条账号广告绑定？', '删除绑定', {
    type: 'warning',
  })
  await automationApi.deleteBinding(binding.id)
  ElMessage.success('绑定已删除')
  await refreshData()
}

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
                  <el-option label="广告专用" value="ad_only" />
                </el-select>
              </el-form-item>
              <el-form-item label="启用配置">
                <el-switch v-model="accountConfigForm.enabled" />
              </el-form-item>
              <el-form-item label="自动加群">
                <el-switch v-model="accountConfigForm.auto_join_enabled" :disabled="isAdOnlyAccount" />
              </el-form-item>
              <el-form-item label="自动广告">
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
        <div class="config-grid">
          <el-card shadow="never">
            <template #header>广告投放</template>
            <el-form label-width="140px">
              <el-form-item label="发送上限">
                <el-input-number v-model="adRunForm.max_deliveries" :min="1" :max="10000" />
              </el-form-item>
              <el-form-item label="Dry Run">
                <el-switch v-model="adRunForm.dry_run" />
              </el-form-item>
              <el-button type="primary" :loading="running === 'ads'" @click="runAds">
                <el-icon><VideoPlay /></el-icon>
                执行投放任务
              </el-button>
            </el-form>
          </el-card>

          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span>广告全局策略</span>
                <div>
                  <el-button :loading="adPolicyLoading" @click="refreshData">
                    <el-icon><Refresh /></el-icon>
                    读取配置
                  </el-button>
                  <el-button type="primary" @click="goGrowthConfig('ads')">
                    <el-icon><Setting /></el-icon>
                    去增长驾驶舱配置
                  </el-button>
                </div>
              </div>
            </template>
            <div v-loading="adPolicyLoading">
              <div class="single-entry-note">
                广告执行、节流、动态容量和失败退群策略统一在增长驾驶舱维护。
              </div>
              <div class="policy-summary">
                <div v-for="item in adPolicySummary" :key="item.label" class="policy-summary-item">
                  <span class="policy-label">{{ item.label }}</span>
                  <el-tag v-if="item.type" :type="item.type" effect="plain">{{ item.value }}</el-tag>
                  <span v-else class="policy-value">{{ item.value }}</span>
                </div>
              </div>
            </div>
          </el-card>

          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span>{{ editingCreativeId ? '编辑广告素材' : '广告素材' }}</span>
                <el-button v-if="editingCreativeId" link type="primary" @click="resetCreativeForm">
                  <el-icon><Close /></el-icon>
                  取消编辑
                </el-button>
              </div>
            </template>
            <el-form label-width="88px">
              <el-form-item label="名称"><el-input v-model="creativeForm.name" /></el-form-item>
              <el-form-item label="类型">
                <el-select v-model="creativeForm.creative_type">
                  <el-option label="文本" value="text" />
                  <el-option label="图片" value="image" />
                  <el-option label="图文" value="mixed" />
                </el-select>
              </el-form-item>
              <el-form-item label="内容">
                <el-input v-model="creativeForm.content" type="textarea" :rows="4" />
              </el-form-item>
              <el-form-item label="媒体地址">
                <el-input v-model="creativeForm.media_url" placeholder="图片 URL 或 Telegram file_id" />
              </el-form-item>
              <el-form-item label="链接"><el-input v-model="creativeForm.link_url" /></el-form-item>
              <el-form-item label="权重"><el-input-number v-model="creativeForm.weight" :min="0" /></el-form-item>
              <el-form-item label="启用"><el-switch v-model="creativeForm.enabled" /></el-form-item>
              <el-button type="primary" :loading="savingCreative" @click="saveCreative">
                <el-icon><Plus v-if="!editingCreativeId" /><Edit v-else /></el-icon>
                {{ editingCreativeId ? '保存素材' : '创建素材' }}
              </el-button>
              <el-button @click="cleanupInvalidCreatives">
                清理异常素材
              </el-button>
            </el-form>

            <el-table :data="creatives" class="creative-table" size="small" max-height="320">
              <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
              <el-table-column prop="creative_type" label="类型" width="80" />
              <el-table-column label="广告文案" min-width="260">
                <template #default="{ row }">
                  <div class="creative-preview-cell">
                    <span class="creative-preview-text">{{ creativePreview(row.content) }}</span>
                    <el-popover v-if="row.content" placement="top-start" width="460" trigger="click">
                      <template #reference>
                        <el-button link type="primary" size="small">查看</el-button>
                      </template>
                      <pre class="creative-full-text">{{ row.content }}</pre>
                    </el-popover>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="link_url" label="链接" min-width="150" show-overflow-tooltip />
              <el-table-column prop="weight" label="权重" width="80" />
              <el-table-column label="启用" width="80">
                <template #default="{ row }">
                  <el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '是' : '否' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="190" fixed="right">
                <template #default="{ row }">
                  <el-button link type="primary" size="small" @click="editCreative(row)">
                    <el-icon><Edit /></el-icon>
                    编辑
                  </el-button>
                  <el-button link :type="row.enabled ? 'warning' : 'success'" size="small" @click="toggleCreative(row)">
                    {{ row.enabled ? '停用' : '启用' }}
                  </el-button>
                  <el-button link type="danger" size="small" @click="deleteCreative(row)">
                    <el-icon><Delete /></el-icon>
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-card>

          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span>{{ editingCampaignId ? '编辑广告计划' : '广告计划' }}</span>
                <el-button v-if="editingCampaignId" link type="primary" @click="resetCampaignForm">
                  <el-icon><Close /></el-icon>
                  取消编辑
                </el-button>
              </div>
            </template>
            <el-form label-width="120px">
              <el-form-item label="名称"><el-input v-model="campaignForm.name" /></el-form-item>
              <el-form-item label="发送模式">
                <el-select v-model="campaignForm.send_mode">
                  <el-option label="入群后" value="after_join" />
                  <el-option label="循环间隔（按群）" value="interval" />
                  <el-option label="定时" value="scheduled" />
                </el-select>
              </el-form-item>
              <el-form-item label="指定群">
                <div class="target-group-control">
                  <el-select
                    v-model="campaignForm.target_group_ids"
                    multiple
                    filterable
                    collapse-tags
                    collapse-tags-tooltip
                    clearable
                    placeholder="选择已加入群"
                  >
                    <el-option
                      v-for="item in targetGroups"
                      :key="item.id"
                      :label="targetGroupLabel(item.id)"
                      :value="item.id"
                      :disabled="item.accountCount <= 0"
                    />
                  </el-select>
                  <el-button @click="openTargetGroupDialog">
                    <el-icon><Plus /></el-icon>
                    添加群
                  </el-button>
                </div>
              </el-form-item>
              <el-form-item v-if="!campaignForm.target_group_ids.length" label="目标等级">
                <el-select v-model="campaignForm.target_group_levels" multiple>
                  <el-option label="A" value="A" />
                  <el-option label="B" value="B" />
                  <el-option label="C" value="C" />
                </el-select>
              </el-form-item>
              <el-form-item label="开始时间">
                <el-date-picker
                  v-model="campaignForm.start_at"
                  type="datetime"
                  placeholder="可选"
                  value-format="YYYY-MM-DDTHH:mm:ss"
                />
              </el-form-item>
              <el-form-item label="结束时间">
                <el-date-picker
                  v-model="campaignForm.end_at"
                  type="datetime"
                  placeholder="可选"
                  value-format="YYYY-MM-DDTHH:mm:ss"
                />
              </el-form-item>
              <el-form-item v-if="campaignForm.send_mode === 'after_join'" label="入群等待(分)">
                <el-input-number v-model="campaignForm.min_wait_after_join_minutes" :min="0" />
              </el-form-item>
              <el-form-item v-if="campaignForm.send_mode === 'interval'" label="每群发送间隔(分)">
                <el-input-number v-model="campaignForm.interval_minutes" :min="1" />
              </el-form-item>
              <el-form-item v-if="campaignForm.send_mode === 'scheduled'" label="每日时点">
                <el-input v-model="scheduledTimesText" placeholder="例如 09:00,14:30,21:00" />
              </el-form-item>
              <el-form-item label="单群每日上限">
                <el-input-number v-model="campaignForm.max_sends_per_group_per_day" :min="0" />
              </el-form-item>
              <el-form-item label="单号每日上限">
                <el-input-number v-model="campaignForm.max_sends_per_account_per_day" :min="0" />
              </el-form-item>
              <el-form-item label="状态"><el-input v-model="campaignForm.status" /></el-form-item>
              <el-form-item label="启用"><el-switch v-model="campaignForm.enabled" /></el-form-item>
              <el-button type="primary" :loading="savingCampaign" @click="saveCampaign">
                <el-icon><Plus v-if="!editingCampaignId" /><Edit v-else /></el-icon>
                {{ editingCampaignId ? '保存计划' : '创建计划' }}
              </el-button>
            </el-form>

            <el-table :data="campaigns" class="campaign-table" size="small" max-height="320">
              <el-table-column prop="name" label="计划" min-width="150" show-overflow-tooltip />
              <el-table-column label="运行" width="90">
                <template #default="{ row }">
                  <el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '已启动' : '已停止' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="模式" width="90">
                <template #default="{ row }">{{ sendModeText(row.send_mode) }}</template>
              </el-table-column>
              <el-table-column label="目标群" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ campaignTargetLabel(row) }}
                </template>
              </el-table-column>
              <el-table-column label="频率" min-width="150">
                <template #default="{ row }">
                  <span v-if="row.send_mode === 'after_join'">入群 {{ row.min_wait_after_join_minutes }} 分后</span>
                  <span v-else-if="row.send_mode === 'interval'">每 {{ row.interval_minutes }} 分钟</span>
                  <span v-else>{{ row.scheduled_times?.join(', ') || '-' }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="max_sends_per_account_per_day" label="单号/日" width="90" />
              <el-table-column label="操作" width="220" fixed="right">
                <template #default="{ row }">
                  <el-button link :type="row.enabled ? 'warning' : 'success'" size="small" @click="toggleCampaign(row)">
                    <el-icon><VideoPause v-if="row.enabled" /><VideoPlay v-else /></el-icon>
                    {{ row.enabled ? '停止' : '启动' }}
                  </el-button>
                  <el-button link type="primary" size="small" @click="editCampaign(row)">
                    <el-icon><Edit /></el-icon>
                    编辑
                  </el-button>
                  <el-button link type="danger" size="small" @click="deleteCampaign(row)">
                    <el-icon><Delete /></el-icon>
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-card>

          <el-card shadow="never">
            <template #header>账号绑定</template>
            <el-form label-width="88px">
              <el-form-item label="投放账号">
                <el-select
                  v-model="bindingForm.account_ids"
                  multiple
                  filterable
                  collapse-tags
                  collapse-tags-tooltip
                  clearable
                  placeholder="选择投放账号"
                >
                  <el-option
                    v-for="item in adBindingAccounts"
                    :key="item.id"
                    :label="accountLabel(item.id)"
                    :value="item.id"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="计划">
                <el-select v-model="bindingForm.ad_campaign_id" @change="bindingForm.creative_ids = []">
                  <el-option v-for="item in campaigns" :key="item.id" :label="item.name" :value="item.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="素材池">
                <el-select v-model="bindingForm.creative_ids" multiple filterable collapse-tags collapse-tags-tooltip clearable>
                  <el-option v-for="item in creatives" :key="item.id" :label="item.name" :value="item.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="池状态">
                <div class="inline-tags">
                  <el-tag type="info">素材总数 {{ creativePoolCount }}</el-tag>
                  <el-tag v-if="creativePoolStatus" type="success">
                    {{ creativePoolStatus.account_count }} 个账号 / 最小池 {{ creativePoolStatus.pool_size }} / 新增
                    {{ creativePoolStatus.created_count }}
                  </el-tag>
                  <el-tag v-else type="warning">未补齐</el-tag>
                </div>
              </el-form-item>
              <el-form-item label="已选素材">
                <div class="inline-tags">
                  <el-popover
                    v-for="item in selectedBindingCreatives"
                    :key="item.id"
                    placement="top-start"
                    width="460"
                    trigger="click"
                  >
                    <template #reference>
                      <el-tag type="success" class="clickable-tag">{{ item.name }}</el-tag>
                    </template>
                    <pre class="creative-full-text">{{ item.content }}</pre>
                  </el-popover>
                  <span v-if="!selectedBindingCreatives.length">-</span>
                </div>
              </el-form-item>
              <el-form-item label="启用"><el-switch v-model="bindingForm.enabled" /></el-form-item>
              <el-form-item label="优先级"><el-input-number v-model="bindingForm.priority" /></el-form-item>
              <el-space>
                <el-button
                  @click="ensureCreativePool"
                  :disabled="!bindingForm.account_ids.length || !bindingForm.ad_campaign_id"
                >
                  <el-icon><Select /></el-icon>
                  自动补齐素材池
                </el-button>
                <el-button type="primary" @click="createBinding">
                  <el-icon><Plus /></el-icon>
                  批量创建绑定
                </el-button>
              </el-space>
            </el-form>

            <el-table :data="bindings" class="binding-table" size="small" max-height="280">
              <el-table-column label="账号" min-width="140">
                <template #default="{ row }">{{ accountLabel(row.account_id) }}</template>
              </el-table-column>
              <el-table-column label="计划" min-width="140">
                <template #default="{ row }">{{ campaigns.find((item) => item.id === row.ad_campaign_id)?.name || row.ad_campaign_id }}</template>
              </el-table-column>
              <el-table-column label="素材" min-width="200">
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
              <el-table-column prop="priority" label="优先级" width="90" />
              <el-table-column label="启用" width="80">
                <template #default="{ row }">
                  <el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '是' : '否' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="150" fixed="right">
                <template #default="{ row }">
                  <el-button link :type="row.enabled ? 'warning' : 'success'" size="small" @click="toggleBinding(row)">
                    {{ row.enabled ? '停用' : '启用' }}
                  </el-button>
                  <el-button link type="danger" size="small" @click="deleteBinding(row)">
                    <el-icon><Delete /></el-icon>
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
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
        <el-button type="primary" :loading="savingTargetGroup" @click="saveTargetGroup">加入并添加</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
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
</style>
