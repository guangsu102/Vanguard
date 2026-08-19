<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Refresh, VideoPause } from '@element-plus/icons-vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import {
  automationApi,
  type AccountAssetPolicySettings,
  type AccountRiskGuardSettings,
  type AccountWarmupPolicySettings,
  type AdCapacitySettings,
  type AdDeliveryExecutionSettings,
  type AdDeliveryThrottleSettings,
  type AdDynamicStatus,
  type AdFailurePolicy,
  type EffectiveLimitItem,
  type EffectiveLimitSummary,
  type GroupAdProfile,
  type AutoJoinSchedulerConfig,
  type AutoJoinVerificationLog,
} from '@/api/automation'
import { settingsApi, type GroupAiInteractionSettings } from '@/api/settings'
import {
  createDefaultAdCapacity,
  createDefaultAdDeliveryExecution,
  createDefaultAdDeliveryThrottle,
  createDefaultAdFailurePolicy,
  createDefaultAutoJoinScheduler,
  createDefaultAssetPolicy,
  createDefaultRiskGuard,
  createDefaultWarmupPolicy,
} from '@/config/automationDefaults'

const loading = ref(false)
const saving = ref('')
const effectiveLimits = ref<EffectiveLimitSummary | null>(null)
const activeConfigTab = ref('join')
const activeEventTab = ref('attempts')
const route = useRoute()

const configTabNames = ['join', 'warmup', 'asset', 'risk', 'ads', 'group-ai'] as const
type ConfigTabName = (typeof configTabNames)[number]

const applyConfigQuery = (config: unknown) => {
  const value = Array.isArray(config) ? config[0] : config
  if (typeof value === 'string' && configTabNames.includes(value as ConfigTabName)) {
    activeConfigTab.value = value
  }
}

const dynamicStatuses = ref<AdDynamicStatus[]>([])
const autoJoinAttempts = ref<any[]>([])
const verificationLogs = ref<AutoJoinVerificationLog[]>([])
const deliveryLogs = ref<any[]>([])
const groupAdProfiles = ref<GroupAdProfile[]>([])
let groupProfilesRefreshTimer: number | null = null

const riskActionOptions = [
  { label: '搜群', value: 'search' },
  { label: '加群', value: 'join' },
  { label: '私聊', value: 'private_message' },
  { label: '群消息', value: 'group_message' },
  { label: '广告探针', value: 'ad_probe' },
  { label: 'AI暖群', value: 'ai_warmup' },
  { label: '审核', value: 'moderation' },
  { label: '广告', value: 'ad_delivery' },
  { label: '资料', value: 'profile_update' },
  { label: '回应', value: 'reaction' },
  { label: '转发', value: 'forward' },
  { label: '置顶', value: 'pin' },
  { label: 'Bot消息', value: 'bot_message' },
  { label: 'Bot置顶', value: 'bot_pin' },
  { label: '创建频道', value: 'channel_create' },
]

const riskLevelPolicyOptions = [
  { label: '正常', value: 'normal' },
  { label: '观察', value: 'watch' },
  { label: '限流', value: 'limited' },
  { label: '冻结', value: 'frozen' },
  { label: '隔离', value: 'quarantined' },
]

const riskScoreDeltaOptions = [
  { label: '群禁言', value: 'group_write_forbidden' },
  { label: '平台群禁言', value: 'platform_group_write_forbidden' },
  { label: 'Flood Wait', value: 'flood_wait' },
  { label: 'Peer Flood', value: 'peer_flood' },
  { label: '账号封禁', value: 'account_banned' },
  { label: '账号受限', value: 'account_restricted' },
  { label: '普通失败', value: 'generic_failure' },
  { label: '拦截加分', value: 'block' },
]

const assetTierOptions = [
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

const groupLevelOptions = [
  { label: 'A', value: 'A' },
  { label: 'B', value: 'B' },
  { label: 'C', value: 'C' },
  { label: '未评级', value: 'UNRATED' },
]

const adCapacityTierOptions = [
  { label: '封禁', value: 'blocked' },
  { label: '观察', value: 'observing' },
  { label: '试投', value: 'trial' },
  { label: '已验证', value: 'validated' },
  { label: '稳定', value: 'stable' },
  { label: '低', value: 'low' },
  { label: '中', value: 'medium' },
  { label: '高', value: 'high' },
  { label: '优质', value: 'premium' },
]

const adPolicyModeOptions = [
  { label: '禁止广告', value: 'forbidden' },
  { label: '许可未知', value: 'unknown' },
  { label: '需管理员审批', value: 'approval_required' },
  { label: '允许软广告', value: 'soft_ad_allowed' },
  { label: '允许高容量广告', value: 'high_volume_ad_allowed' },
]

const adHourlyWeightOptions = Array.from({ length: 24 }, (_, hour) => ({
  label: `${hour}:00`,
  value: String(hour),
}))

const unknownChallengeActionOptions = [
  { label: '退出', value: 'leave' },
  { label: '人工', value: 'manual' },
  { label: '等待', value: 'wait' },
  { label: '跳过', value: 'skip' },
]

const groupAiModeOptions = [
  { label: '辅助回复', value: 'assistive' },
  { label: '暖号互动', value: 'warmup' },
  { label: '转化引导', value: 'conversion' },
  { label: '关闭', value: 'off' },
]

const groupAiToneOptions = [
  { label: '自然', value: 'natural' },
  { label: '友好', value: 'friendly' },
  { label: '专业', value: 'professional' },
  { label: '柔和', value: 'soft' },
]

const schedulerForm = reactive<AutoJoinSchedulerConfig>(createDefaultAutoJoinScheduler())

const riskGuardForm = reactive<AccountRiskGuardSettings>(createDefaultRiskGuard())

const assetPolicyForm = reactive<AccountAssetPolicySettings>({
  ...createDefaultAssetPolicy(),
})

const warmupPolicyForm = reactive<AccountWarmupPolicySettings>(createDefaultWarmupPolicy())

const adExecutionForm = reactive<AdDeliveryExecutionSettings>(createDefaultAdDeliveryExecution())

const adThrottleForm = reactive<AdDeliveryThrottleSettings>(createDefaultAdDeliveryThrottle())

const adCapacityForm = reactive<AdCapacitySettings>(createDefaultAdCapacity())

const adFailurePolicyForm = reactive<AdFailurePolicy>(createDefaultAdFailurePolicy())

const groupAiForm = reactive<GroupAiInteractionSettings>({
  enabled: false,
  aiEnabled: false,
  dailyTokenBudget: 0,
  maxRepliesPerGroupPerDay: 3,
  maxRepliesPerAccountPerDay: 20,
  cooldownSeconds: 900,
  replyMaxChars: 120,
  blockAiSelfDisclosure: true,
  mode: 'assistive',
  tone: 'natural',
  temperature: 0.6,
  maxTokens: 180,
  allowKeywordTriggeredReply: false,
  allowSemanticTriggeredReply: true,
  semanticScanWindowMessages: 100,
  semanticEvaluateEveryMessages: 100,
  semanticMinConfidence: 0.78,
  semanticMinTextChars: 4,
  semanticAllowedIntents: ['question', 'buying_interest', 'problem', 'recommendation_request', 'experience_request'],
  semanticBlockedIntents: ['smalltalk', 'thanks', 'emoji', 'command', 'spam', 'ad', 'sensitive'],
  semanticDecisionPrompt: '你需要从最近的Telegram群聊消息中，选择最值得自然回复的一条真实用户消息。只有当消息明确表达问题、需求、使用障碍、推荐请求或可自然接话的经验讨论时才回复；闲聊、表情、感谢、广告、命令、敏感内容、低质量短句都不要回复。',
  allowProactiveWarmup: false,
  proactiveWarmupIntervalMinutes: 30,
  proactiveWarmupMaxGroupsPerRun: 5,
  proactiveWarmupMaxPerGroupPerDay: 2,
  proactiveWarmupMaxPerAccountPerDay: 20,
  proactiveWarmupCooldownSeconds: 3600,
  proactiveWarmupWindowStartHour: 9,
  proactiveWarmupWindowEndHour: 2,
  proactiveWarmupTopics: ['节点稳定性', '工具使用体验', '账号风控经验', '自动化效率', '群内常见问题'],
  proactiveWarmupTemplates: [
    '最近大家用节点稳定吗？有没有哪种线路体验比较好？',
    '你们平时会怎么判断一个工具到底稳不稳定？',
    '群里有人最近遇到账号风控吗？一般怎么处理比较稳？',
    '感觉自动化最麻烦的还是细节限制，大家一般怎么控频率？',
    '这个问题我也挺关心的，想听听群里有没有实际经验。',
  ],
  proactiveWarmupGroupOverrides: {},
  systemPrompt: '你是一个中文Telegram社群客服助手，回复要简洁、自然、友好，不要提及你是AI。',
})

const semanticAllowedIntentsText = ref(groupAiForm.semanticAllowedIntents.join('\n'))
const semanticBlockedIntentsText = ref(groupAiForm.semanticBlockedIntents.join('\n'))
const proactiveWarmupTopicsText = ref(groupAiForm.proactiveWarmupTopics.join('\n'))
const proactiveWarmupTemplatesText = ref(groupAiForm.proactiveWarmupTemplates.join('\n'))
const proactiveWarmupGroupOverridesText = ref(JSON.stringify(groupAiForm.proactiveWarmupGroupOverrides, null, 2))

const configSections = ['scheduler', 'warmup', 'asset', 'risk', 'ads', 'groupAi'] as const
type ConfigSection = (typeof configSections)[number]

const savedSnapshots = reactive<Record<ConfigSection, string>>({
  scheduler: '',
  warmup: '',
  asset: '',
  risk: '',
  ads: '',
  groupAi: '',
})
const saveErrors = reactive<Record<ConfigSection, string>>({
  scheduler: '',
  warmup: '',
  asset: '',
  risk: '',
  ads: '',
  groupAi: '',
})
const snapshotsReady = ref(false)

const serializeConfig = (value: unknown) => JSON.stringify(value)

const currentSectionState = (section: ConfigSection) => {
  if (section === 'scheduler') return schedulerForm
  if (section === 'warmup') return warmupPolicyForm
  if (section === 'asset') return assetPolicyForm
  if (section === 'risk') return riskGuardForm
  if (section === 'ads') {
    return {
      execution: adExecutionForm,
      throttle: adThrottleForm,
      capacity: adCapacityForm,
      failure: adFailurePolicyForm,
    }
  }
  return {
    form: groupAiForm,
    semanticAllowedIntentsText: semanticAllowedIntentsText.value,
    semanticBlockedIntentsText: semanticBlockedIntentsText.value,
    proactiveWarmupTopicsText: proactiveWarmupTopicsText.value,
    proactiveWarmupTemplatesText: proactiveWarmupTemplatesText.value,
    proactiveWarmupGroupOverridesText: proactiveWarmupGroupOverridesText.value,
  }
}

const captureSnapshot = (section: ConfigSection) => {
  savedSnapshots[section] = serializeConfig(currentSectionState(section))
  saveErrors[section] = ''
}

const captureAllSnapshots = () => {
  for (const section of configSections) captureSnapshot(section)
  snapshotsReady.value = true
}

const isSectionDirty = (section: ConfigSection) => snapshotsReady.value
  && savedSnapshots[section] !== serializeConfig(currentSectionState(section))

const unsavedCount = computed(() => configSections.filter(isSectionDirty).length)
const saveErrorCount = computed(() => configSections.filter((section) => Boolean(saveErrors[section])).length)
const hasUnsavedChanges = computed(() => unsavedCount.value > 0)

const requestDiscardChanges = async () => {
  if (!hasUnsavedChanges.value) return true
  try {
    await ElMessageBox.confirm('当前有未保存的配置，继续将丢失这些修改。', '未保存的配置', {
      confirmButtonText: '放弃修改',
      cancelButtonText: '继续编辑',
      type: 'warning',
    })
    return true
  } catch {
    return false
  }
}

const beforeWindowUnload = (event: BeforeUnloadEvent) => {
  if (!hasUnsavedChanges.value) return
  event.preventDefault()
  event.returnValue = ''
}

const errorDetail = (error: unknown) => {
  const candidate = error as {
    message?: string
    response?: { data?: { detail?: string; message?: string } }
  }
  return candidate.response?.data?.detail || candidate.response?.data?.message || candidate.message || '未知错误'
}

const recordSaveError = (section: ConfigSection, label: string, error: unknown) => {
  const detail = errorDetail(error)
  saveErrors[section] = `${label}：${detail}`
  ElMessage.error(saveErrors[section])
}

const linesToList = (value: string) => value
  .split(/\r?\n/)
  .map((item) => item.trim())
  .filter(Boolean)

const syncGroupAiTextFields = () => {
  semanticAllowedIntentsText.value = (groupAiForm.semanticAllowedIntents || []).join('\n')
  semanticBlockedIntentsText.value = (groupAiForm.semanticBlockedIntents || []).join('\n')
  proactiveWarmupTopicsText.value = (groupAiForm.proactiveWarmupTopics || []).join('\n')
  proactiveWarmupTemplatesText.value = (groupAiForm.proactiveWarmupTemplates || []).join('\n')
  proactiveWarmupGroupOverridesText.value = JSON.stringify(groupAiForm.proactiveWarmupGroupOverrides || {}, null, 2)
}

const buildGroupAiPayload = (): GroupAiInteractionSettings | null => {
  let groupOverrides: GroupAiInteractionSettings['proactiveWarmupGroupOverrides'] = {}
  try {
    const parsed = JSON.parse(proactiveWarmupGroupOverridesText.value || '{}')
    groupOverrides = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    ElMessage.error('按群暖场覆盖不是合法 JSON')
    return null
  }

  return {
    ...groupAiForm,
    semanticAllowedIntents: linesToList(semanticAllowedIntentsText.value),
    semanticBlockedIntents: linesToList(semanticBlockedIntentsText.value),
    proactiveWarmupTopics: linesToList(proactiveWarmupTopicsText.value),
    proactiveWarmupTemplates: linesToList(proactiveWarmupTemplatesText.value),
    proactiveWarmupGroupOverrides: groupOverrides,
  }
}

const metrics = computed(() => {
  const rows = dynamicStatuses.value
  const total = rows.length
  const activeAds = rows.filter((row) => row.auto_ads_enabled).length
  const activeJoin = rows.filter((row) => row.auto_join_enabled).length
  const paused = rows.filter((row) => ['frozen', 'quarantined', 'limited'].includes(row.risk_level)).length
  const eligibleGroups = rows.reduce((sum, row) => sum + Number(row.ad_eligible_groups || 0), 0)
  const avgWritable = total
    ? rows.reduce((sum, row) => sum + Number(row.writable_rate || 0), 0) / total
    : 0
  const avgAdSuccess = total
    ? rows.reduce((sum, row) => sum + Number(row.ad_success_rate_24h || 0), 0) / total
    : 0

  return { total, activeAds, activeJoin, paused, eligibleGroups, avgWritable, avgAdSuccess }
})

const redisFailClosedValue = computed({
  get: () => {
    if (riskGuardForm.redis_fail_closed === null) return 'system'
    return riskGuardForm.redis_fail_closed ? 'closed' : 'open'
  },
  set: (value: string) => {
    riskGuardForm.redis_fail_closed = value === 'system' ? null : value === 'closed'
  },
})

const titleBlacklistText = computed({
  get: () => (schedulerForm.search_filter?.title_blacklist || []).join('\n'),
  set: (value: string) => {
    if (!schedulerForm.search_filter) {
      schedulerForm.search_filter = { title_blacklist_enabled: true, title_blacklist: [] }
    }
    schedulerForm.search_filter.title_blacklist = value
      .split(/[\n,，]+/)
      .map((item) => item.trim())
      .filter(Boolean)
  },
})

const flowSteps = computed(() => [
  {
    key: 'search',
    title: '搜群',
    enabled: schedulerForm.enabled,
    status: schedulerForm.enabled ? `${schedulerForm.scan_interval_minutes} 分钟/轮` : '关闭',
    detail: schedulerForm.search_filter?.title_blacklist_enabled
      ? `标题黑名单 ${schedulerForm.search_filter?.title_blacklist?.length || 0} 条`
      : '标题黑名单关闭',
  },
  {
    key: 'verify',
    title: '群检测',
    enabled: Boolean(schedulerForm.join_verification?.enabled),
    status: schedulerForm.join_verification?.ai_enabled ? 'AI 辅助' : '本地规则',
    detail: `未知验证：${challengeActionLabel(schedulerForm.join_verification?.unknown_challenge_action)}`,
  },
  {
    key: 'join',
    title: '入群',
    enabled: Boolean(riskGuardForm.enabled && riskGuardForm.actions.join?.daily_limit),
    status: `${riskGuardForm.actions.join?.daily_limit || 0}/天`,
    detail: `冷却 ${formatDuration(riskGuardForm.actions.join?.cooldown_seconds || 0)}`,
  },
  {
    key: 'warmup',
    title: '暖号',
    enabled: warmupPolicyForm.enabled,
    status: `${warmupPolicyForm.default_warmup_days} 天`,
    detail: `最低 ${warmupPolicyForm.minimum_warmup_days} 天，广告等待 ${adCapacityForm.warmup_days_before_ads} 天`,
  },
  {
    key: 'interaction',
    title: '群AI互动',
    enabled: Boolean(groupAiForm.enabled && groupAiForm.aiEnabled),
    status: groupAiForm.enabled ? groupAiModeLabel(groupAiForm.mode) : '关闭',
    detail: `${groupAiForm.maxRepliesPerGroupPerDay}/群/天，冷却 ${formatDuration(groupAiForm.cooldownSeconds)}`,
  },
  {
    key: 'ad',
    title: '广告',
    enabled: adExecutionForm.enabled,
    status: `${adExecutionForm.max_deliveries_per_run}/轮`,
    detail: `账号 ${adExecutionForm.max_deliveries_per_account_per_run}/轮，删帖检测 ${formatDuration(adCapacityForm.survival_check_delay_seconds)}`,
  },
  {
    key: 'risk',
    title: '账号风控',
    enabled: riskGuardForm.enabled,
    status: `${riskGuardForm.global_daily_limit}/天`,
    detail: adFailurePolicyForm.leave_on_group_control_failure ? '群控失败自动退群' : '群控失败仅记录',
  },
])

const effectiveLimitLabels: Record<string, string> = {
  account_join_daily: '单号加群日上限',
  account_group_message_daily: '单号群消息日上限',
  account_ad_daily: '单号广告日上限',
  account_ad_per_run: '单号单轮广告上限',
  account_group_ad_daily: '单号单群广告日上限',
  group_global_ad_daily: '单群全局广告日上限',
  account_ad_min_interval: '单号广告最小间隔',
  group_ad_min_interval: '单群广告最小间隔',
}

const effectiveSourceLabels: Record<string, string> = {
  'risk.global_daily_limit': '风控单号总日额度',
  'risk.group_write_daily_limit': '风控群写共享日额度',
  'risk.actions.join.daily_limit': '风控加群日额度',
  'risk.actions.group_message.daily_limit': '风控群消息日额度',
  'risk.actions.ad_delivery.daily_limit': '风控广告日额度',
  'ads.capacity.account_ad_daily_hard_cap': '广告单号日硬上限',
  'ads.execution.max_deliveries_per_run': '广告单轮总上限',
  'ads.execution.max_deliveries_per_account_per_run': '广告单号单轮上限',
  'ads.capacity.account_group_daily_cap_default': '单号单群默认日上限',
  'ads.capacity.group_global_daily_hard_cap': '单群全局日硬上限',
  'ads.throttle.delivery_interval_seconds': '投放间隔',
  'ads.throttle.cooldown_min_seconds': '投放最小冷却',
  'ads.capacity.group_min_interval_seconds': '单群投放间隔',
  'ads.execution.group_campaign_cooldown_minutes': '群活动冷却',
}

const dynamicFactorLabels: Record<string, string> = {
  'account.max_groups_per_day': '账号加群额度',
  'account.max_messages_per_day': '账号消息额度',
  'asset.join_multiplier': '账号等级加群倍率',
  'asset.action_multiplier': '账号等级动作倍率',
  'asset.ad_multiplier': '账号等级广告倍率',
  'warmup.join_multiplier': '暖号加群倍率',
  'warmup.group_message_multiplier': '暖号群消息倍率',
  'warmup.ad_multiplier': '暖号广告倍率',
  'warmup.run_multiplier': '暖号单轮倍率',
  'risk.level_multiplier': '风险等级倍率',
  'account.health_score': '账号健康度',
  'probe.quality_multiplier': '探针质量',
  'time_window.join_multiplier': '加群时段倍率',
  'time_window.ad_multiplier': '广告时段倍率',
  'campaign.max_sends_per_account_per_day': '活动单号日上限',
  'campaign.max_sends_per_group_per_day': '活动单群日上限',
  'campaign.interval_minutes': '活动投放间隔',
  'group.ad_tier': '群广告档位',
  'group.tier_daily_capacity': '群档位容量',
  'group.evidence_capacity': '群证据容量',
  'group.ad_policy': '群广告许可',
  'group.last_ad_at': '群最近投放时间',
}

const effectiveLimitRows = computed(() => effectiveLimits.value?.items || [])
const groupGlobalAdDailyLimit = computed(() => effectiveLimitRows.value
  .find((item) => item.key === 'group_global_ad_daily')?.value)

const effectiveLimitLabel = (key: string) => effectiveLimitLabels[key] || key
const effectiveSourceLabel = (key: string) => effectiveSourceLabels[key] || key
const dynamicFactorLabel = (key: string) => dynamicFactorLabels[key] || key
const formulaLabel = (formula: string) => formula === 'max' ? '取最大值' : '取最小值'

const formatEffectiveValue = (value: number | null, unit: EffectiveLimitItem['unit']) => {
  if (value === null) return '未设置'
  if (unit === 'seconds') return formatDuration(value)
  if (unit === 'count_per_run') return `${value} 次/轮`
  return `${value} 次/天`
}

const fillSchedulerForm = (config: AutoJoinSchedulerConfig) => {
  Object.assign(schedulerForm, {
    ...config,
    search_filter: {
      ...schedulerForm.search_filter,
      ...(config.search_filter || {}),
      title_blacklist: config.search_filter?.title_blacklist || [],
    },
    join_verification: {
      ...schedulerForm.join_verification,
      ...(config.join_verification || {}),
    },
    group_capacity_cleanup: {
      ...schedulerForm.group_capacity_cleanup,
      ...(config.group_capacity_cleanup || {}),
    },
  })
}

const fillRiskGuardForm = (config: AccountRiskGuardSettings) => {
  riskGuardForm.enabled = config.enabled
  riskGuardForm.global_daily_limit = config.global_daily_limit
  riskGuardForm.group_write_daily_limit = config.group_write_daily_limit
  riskGuardForm.redis_fail_closed = config.redis_fail_closed
  const actions = { ...createDefaultRiskGuard().actions }
  for (const item of riskActionOptions) {
    actions[item.value] = {
      ...actions[item.value],
      ...(config.actions?.[item.value] || {}),
    }
  }
  riskGuardForm.actions = actions
  riskGuardForm.level_thresholds = {
    ...createDefaultRiskGuard().level_thresholds,
    ...(config.level_thresholds || {}),
  }
  riskGuardForm.level_budget_multipliers = {
    ...createDefaultRiskGuard().level_budget_multipliers,
    ...(config.level_budget_multipliers || {}),
  }
  riskGuardForm.risk_score_deltas = {
    ...createDefaultRiskGuard().risk_score_deltas,
    ...(config.risk_score_deltas || {}),
  }
  riskGuardForm.lifecycle = {
    ...createDefaultRiskGuard().lifecycle,
    ...(config.lifecycle || {}),
  }
  riskGuardForm.group_write_forbidden = {
    ...createDefaultRiskGuard().group_write_forbidden,
    ...(config.group_write_forbidden || {}),
  }
  riskGuardForm.retention = {
    ...createDefaultRiskGuard().retention,
    ...(config.retention || {}),
  }
}

const fillAssetPolicyForm = (config: AccountAssetPolicySettings) => {
  assetPolicyForm.enabled = config.enabled
  const tiers = createDefaultAssetPolicy().tiers
  for (const item of assetTierOptions) {
    tiers[item.value] = {
      ...tiers[item.value],
      ...(config.tiers?.[item.value] || {}),
    }
  }
  assetPolicyForm.tiers = tiers
}

const fillWarmupPolicyForm = (config: AccountWarmupPolicySettings) => {
  warmupPolicyForm.enabled = config.enabled
  warmupPolicyForm.default_warmup_days = config.default_warmup_days
  warmupPolicyForm.minimum_warmup_days = config.minimum_warmup_days
  warmupPolicyForm.user_initiated_private_message_multiplier = config.user_initiated_private_message_multiplier
  const tiers = createDefaultWarmupPolicy().tiers
  for (const item of assetTierOptions) {
    tiers[item.value] = {
      ...tiers[item.value],
      ...(config.tiers?.[item.value] || {}),
    }
  }
  warmupPolicyForm.tiers = tiers
  const stages = createDefaultWarmupPolicy().stages
  for (const item of warmupStageOptions) {
    stages[item.value] = {
      ...stages[item.value],
      ...(config.stages?.[item.value] || {}),
    }
  }
  warmupPolicyForm.stages = stages
}

const loadAll = async () => {
  loading.value = true
  try {
    const [
      dynamicRes,
      schedulerRes,
      riskRes,
      assetRes,
      warmupRes,
      executionRes,
      throttleRes,
      capacityRes,
      failureRes,
      effectiveLimitsRes,
      settingsRes,
      attemptsRes,
      verificationRes,
      deliveryRes,
      groupProfilesRes,
    ] = await Promise.all([
      automationApi.getAdDynamicStatus(),
      automationApi.getAutoJoinSchedulerConfig(),
      automationApi.getAccountRiskGuard(),
      automationApi.getAccountAssetPolicy(),
      automationApi.getAccountWarmupPolicy(),
      automationApi.getAdDeliveryExecution(),
      automationApi.getAdDeliveryThrottle(),
      automationApi.getAdCapacity(),
      automationApi.getAdFailurePolicy(),
      automationApi.getEffectiveLimits(),
      settingsApi.get(),
      automationApi.getAutoJoinAttempts({ limit: 20 }),
      automationApi.getAutoJoinVerificationLogs({ limit: 20 }),
      automationApi.getDeliveryLogs({ limit: 20 }),
      automationApi.getGroupAdProfiles(),
    ])

    dynamicStatuses.value = dynamicRes.data.data
    fillSchedulerForm(schedulerRes.data.data)
    fillRiskGuardForm(riskRes.data.data)
    fillAssetPolicyForm(assetRes.data.data)
    fillWarmupPolicyForm(warmupRes.data.data)
    Object.assign(adExecutionForm, executionRes.data.data)
    Object.assign(adThrottleForm, throttleRes.data.data)
    Object.assign(adCapacityForm, capacityRes.data.data)
    Object.assign(adFailurePolicyForm, failureRes.data.data)
    Object.assign(groupAiForm, settingsRes.data.data.groupAiInteraction || {})
    syncGroupAiTextFields()
    autoJoinAttempts.value = attemptsRes.data.data
    verificationLogs.value = verificationRes.data.data
    deliveryLogs.value = deliveryRes.data.data
    groupAdProfiles.value = groupProfilesRes.data.data
    effectiveLimits.value = effectiveLimitsRes.data.data
    captureAllSnapshots()
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

const refreshAll = async () => {
  if (await requestDiscardChanges()) await loadAll()
}

const refreshEffectiveLimits = async () => {
  try {
    const res = await automationApi.getEffectiveLimits()
    effectiveLimits.value = res.data.data
  } catch {
    ElMessage.warning('配置已保存，但最终生效上限刷新失败')
  }
}

const saveScheduler = async () => {
  saving.value = 'scheduler'
  saveErrors.scheduler = ''
  try {
    const res = await automationApi.updateAutoJoinSchedulerConfig(schedulerForm)
    fillSchedulerForm(res.data.data)
    captureSnapshot('scheduler')
    ElMessage.success('已保存入群与群检测配置')
  } catch (error) {
    recordSaveError('scheduler', '入群与群检测保存失败', error)
  } finally {
    saving.value = ''
  }
}

const saveRiskGuard = async () => {
  saving.value = 'risk'
  saveErrors.risk = ''
  try {
    const res = await automationApi.updateAccountRiskGuard(riskGuardForm)
    fillRiskGuardForm(res.data.data)
    captureSnapshot('risk')
    await refreshEffectiveLimits()
    ElMessage.success('已保存账号风控配置')
  } catch (error) {
    recordSaveError('risk', '账号风控保存失败', error)
  } finally {
    saving.value = ''
  }
}

const saveAssetPolicy = async () => {
  saving.value = 'asset'
  saveErrors.asset = ''
  try {
    const res = await automationApi.updateAccountAssetPolicy(assetPolicyForm)
    fillAssetPolicyForm(res.data.data)
    captureSnapshot('asset')
    ElMessage.success('已保存账号等级策略')
  } catch (error) {
    recordSaveError('asset', '账号等级保存失败', error)
  } finally {
    saving.value = ''
  }
}

const saveWarmupPolicy = async () => {
  saving.value = 'warmup'
  saveErrors.warmup = ''
  try {
    const res = await automationApi.updateAccountWarmupPolicy(warmupPolicyForm)
    fillWarmupPolicyForm(res.data.data)
    captureSnapshot('warmup')
    ElMessage.success('已保存暖号配置')
  } catch (error) {
    recordSaveError('warmup', '暖号配置保存失败', error)
  } finally {
    saving.value = ''
  }
}

const saveAdsPolicy = async () => {
  saving.value = 'ads'
  saveErrors.ads = ''
  try {
    const [executionResult, throttleResult, capacityResult, failureResult] = await Promise.allSettled([
      automationApi.updateAdDeliveryExecution(adExecutionForm),
      automationApi.updateAdDeliveryThrottle(adThrottleForm),
      automationApi.updateAdCapacity(adCapacityForm),
      automationApi.updateAdFailurePolicy(adFailurePolicyForm),
    ])

    const failures: string[] = []
    if (executionResult.status === 'fulfilled') Object.assign(adExecutionForm, executionResult.value.data.data)
    else failures.push(`执行策略（${errorDetail(executionResult.reason)}）`)
    if (throttleResult.status === 'fulfilled') Object.assign(adThrottleForm, throttleResult.value.data.data)
    else failures.push(`节流策略（${errorDetail(throttleResult.reason)}）`)
    if (capacityResult.status === 'fulfilled') Object.assign(adCapacityForm, capacityResult.value.data.data)
    else failures.push(`容量策略（${errorDetail(capacityResult.reason)}）`)
    if (failureResult.status === 'fulfilled') Object.assign(adFailurePolicyForm, failureResult.value.data.data)
    else failures.push(`失败策略（${errorDetail(failureResult.reason)}）`)

    if (failures.length) {
      saveErrors.ads = `部分保存失败：${failures.join('；')}`
      ElMessage.error(saveErrors.ads)
      return
    }

    captureSnapshot('ads')
    await refreshEffectiveLimits()
    ElMessage.success('已保存广告投放配置')
  } catch (error) {
    recordSaveError('ads', '广告投放配置保存失败', error)
  } finally {
    saving.value = ''
  }
}

const saveGroupAi = async () => {
  saving.value = 'groupAi'
  saveErrors.groupAi = ''
  try {
    const payload = buildGroupAiPayload()
    if (!payload) {
      saveErrors.groupAi = '按群暖场覆盖 JSON 格式错误'
      return
    }
    const res = await settingsApi.update({ groupAiInteraction: payload })
    Object.assign(groupAiForm, (res as any).data?.data?.groupAiInteraction || groupAiForm)
    syncGroupAiTextFields()
    captureSnapshot('groupAi')
    ElMessage.success('已保存群AI互动配置')
  } catch (error) {
    recordSaveError('groupAi', '群AI互动保存失败', error)
  } finally {
    saving.value = ''
  }
}

function pct(value: number | undefined) {
  return `${Math.round(Number(value || 0) * 100)}%`
}

function formatDuration(seconds: number) {
  if (!seconds) return '0 秒'
  if (seconds < 60) return `${seconds} 秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`
  return `${Math.round(seconds / 3600)} 小时`
}

function riskTagType(level: string) {
  if (['frozen', 'quarantined'].includes(level)) return 'danger'
  if (['limited', 'watch'].includes(level)) return 'warning'
  return 'success'
}

function statusTagType(status: string) {
  if (['success', 'joined', 'active'].includes(status)) return 'success'
  if (['failed', 'error', 'frozen', 'quarantined'].includes(status)) return 'danger'
  if (['pending', 'scheduled', 'limited', 'watch'].includes(status)) return 'warning'
  return 'info'
}

function challengeActionLabel(value?: string) {
  return unknownChallengeActionOptions.find((item) => item.value === value)?.label || value || '-'
}

function groupAiModeLabel(value: string) {
  return groupAiModeOptions.find((item) => item.value === value)?.label || value
}

function accountLabel(row: any) {
  return row.account_label || `#${row.account_id}`
}

function compactError(row: any) {
  const error = row.recent_errors?.[0]
  return error ? `${error.error || '-'} (${error.count})` : '-'
}

const saveGroupAdPolicy = async (row: any) => {
  let note = ''
  try {
    const result = await ElMessageBox.prompt('请填写许可依据、管理员确认或禁止原因', '确认群广告策略', {
      confirmButtonText: '确认保存',
      cancelButtonText: '取消',
      inputPattern: /\S{2,}/,
      inputErrorMessage: '至少填写 2 个字符',
    })
    note = result.value
  } catch {
    return
  }
  saving.value = `group-policy-${row.group_id}`
  try {
    const res = await automationApi.updateGroupAdPolicy(row.group_id, {
      mode: row.ad_policy_mode,
      confidence: row.ad_policy_mode === 'unknown' ? 0 : 100,
      note,
    })
    Object.assign(row, res.data.data)
    ElMessage.success('群广告许可已更新')
  } finally {
    saving.value = ''
  }
}

function severityTagType(severity?: string) {
  if (severity === 'danger') return 'danger'
  if (severity === 'warning') return 'warning'
  if (severity === 'success') return 'success'
  return 'info'
}

function diagnosticTagType(row: any) {
  return severityTagType(row.delivery_diagnostic?.primary_block_severity)
}

function diagnosticLabel(row: any) {
  return row.delivery_diagnostic?.primary_block_label || '-'
}

function dynamicHealthTagType(row: any) {
  return severityTagType(row.dynamic_health_diagnostic?.primary_severity)
}

function dynamicHealthText(row: any) {
  const diagnostic = row.dynamic_health_diagnostic
  if (!diagnostic) return '-'
  const main = diagnostic.primary_label
  const top = diagnostic.negative_adjustments?.[0]
  return top ? `${main} · ${top.label} ${top.delta}` : main
}

function nextActionText(row: any) {
  const diagnostic = row.delivery_diagnostic
  if (!diagnostic) return '-'
  return diagnostic.next_action_at
    ? `${diagnostic.next_action_label} · ${diagnostic.next_action_at.slice(5, 16).replace('T', ' ')}`
    : diagnostic.next_action_label
}

function groupDiagnosticText(row: any) {
  const item = row.delivery_diagnostic?.group_diagnostics
  if (!item) return '-'
  const probeState = row.delivery_diagnostic?.probe_execution_allowed ? '探针可运行' : '探针阻断'
  const adState = row.delivery_diagnostic?.ad_delivery_allowed ? '广告可发送' : '广告暂停'
  return `${probeState} · ${adState} · 就绪 ${item.ready} · Premium ${item.premium || 0} · 待许可 ${(item.ad_permission_unknown || 0) + (item.ad_policy_expired || 0)} · 待探针 ${item.pending_probe} · 阻断 ${item.probe_failed + item.blocked + (item.ad_permission_forbidden || 0)}`
}

watch(
  () => route.query.config,
  (config) => applyConfigQuery(config),
)

onBeforeRouteLeave(() => requestDiscardChanges())

onMounted(() => {
  window.addEventListener('beforeunload', beforeWindowUnload)
  applyConfigQuery(route.query.config)
  loadAll()
  groupProfilesRefreshTimer = window.setInterval(refreshGroupAdProfiles, 30_000)
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', beforeWindowUnload)
  if (groupProfilesRefreshTimer !== null) {
    window.clearInterval(groupProfilesRefreshTimer)
    groupProfilesRefreshTimer = null
  }
})
</script>

<template>
  <div class="growth-dashboard" v-loading="loading">
    <div class="page-toolbar">
      <div>
        <h2>增长驾驶舱</h2>
        <div class="toolbar-meta">
          账号 {{ metrics.total }} 个 · 加群 {{ metrics.activeJoin }} 个 · 广告 {{ metrics.activeAds }} 个 · 风控 {{ metrics.paused }} 个
        </div>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="refreshAll">刷新</el-button>
    </div>

    <div class="metric-grid">
      <div class="metric-cell">
        <span>可投放群</span>
        <strong>{{ metrics.eligibleGroups }}</strong>
      </div>
      <div class="metric-cell">
        <span>群可发言率</span>
        <strong>{{ pct(metrics.avgWritable) }}</strong>
      </div>
      <div class="metric-cell">
        <span>广告成功率</span>
        <strong>{{ pct(metrics.avgAdSuccess) }}</strong>
      </div>
      <div class="metric-cell">
            <span>账号风控</span>
        <strong>{{ riskGuardForm.enabled ? '运行' : '关闭' }}</strong>
      </div>
    </div>

    <section class="panel">
      <div class="panel-header">
        <h3>流程总览</h3>
      </div>
      <div class="flow-grid">
        <div v-for="step in flowSteps" :key="step.key" class="flow-item" :class="{ disabled: !step.enabled }">
          <div class="flow-icon">
            <el-icon>
              <component :is="step.enabled ? Check : VideoPause" />
            </el-icon>
          </div>
          <div class="flow-body">
            <div class="flow-title">{{ step.title }}</div>
            <div class="flow-status">{{ step.status }}</div>
            <div class="flow-detail">{{ step.detail }}</div>
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div>
          <h3>最终生效上限</h3>
          <div class="panel-subtitle">重复作用参数已归一化，日额度取最小值，安全间隔取最大值。</div>
        </div>
        <el-tag :type="effectiveLimits?.riskGuardEnabled ? 'success' : 'info'" effect="plain">
          {{ effectiveLimits?.riskGuardEnabled ? '账号风控参与计算' : '账号风控未参与' }}
        </el-tag>
      </div>
      <el-table :data="effectiveLimitRows" size="small" border empty-text="暂无生效上限数据">
        <el-table-column label="限制项" min-width="190" fixed>
          <template #default="{ row }">
            <strong class="limit-name">{{ effectiveLimitLabel(row.key) }}</strong>
          </template>
        </el-table-column>
        <el-table-column label="最终值" min-width="130">
          <template #default="{ row }">
            <span class="limit-value">{{ formatEffectiveValue(row.value, row.unit) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="规则" width="100">
          <template #default="{ row }">{{ formulaLabel(row.formula) }}</template>
        </el-table-column>
        <el-table-column label="静态来源" min-width="340">
          <template #default="{ row }">
            <div class="source-list">
              <span v-for="source in row.sources" :key="source.key" class="source-item">
                {{ effectiveSourceLabel(source.key) }}：{{ formatEffectiveValue(source.value, row.unit) }}
                <el-tag v-if="!source.active" type="info" size="small" effect="plain">未参与</el-tag>
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="运行时还会降低" min-width="300">
          <template #default="{ row }">
            <div v-if="row.dynamicFactors.length" class="factor-list">
              <el-tag v-for="factor in row.dynamicFactors" :key="factor" size="small" effect="plain">
                {{ dynamicFactorLabel(factor) }}
              </el-tag>
            </div>
            <span v-else class="muted-text">无</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="limit-note">
        此处是全局硬上限。账号运营态中的实时额度还会受账号等级、暖号阶段、风险、健康度、探针质量和活动策略动态降低。
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <h3>账号运营态</h3>
        <el-tag type="info" effect="plain">{{ dynamicStatuses.length }} 个账号</el-tag>
      </div>
      <el-table :data="dynamicStatuses" height="360" size="small" border>
        <el-table-column label="账号" min-width="150">
          <template #default="{ row }">
            <div class="account-cell">
              <span>{{ accountLabel(row) }}</span>
              <small>#{{ row.account_id }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="开关" width="130">
          <template #default="{ row }">
            <el-tag :type="row.auto_join_enabled ? 'success' : 'info'" size="small">加群</el-tag>
            <el-tag :type="row.auto_ads_enabled ? 'success' : 'info'" size="small">广告</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="风控" min-width="130">
          <template #default="{ row }">
            <el-tag :type="riskTagType(row.risk_level)" size="small">{{ row.risk_level }}</el-tag>
            <span class="inline-score">{{ row.risk_score }}</span>
          </template>
        </el-table-column>
        <el-table-column label="健康诊断" min-width="210" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag :type="dynamicHealthTagType(row)" size="small">{{ row.dynamic_health_diagnostic?.primary_label || '-' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="暖号" min-width="150">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.warmup_stage }}</el-tag>
            <span class="muted-text">{{ row.warmup_remaining_days }} 天</span>
          </template>
        </el-table-column>
        <el-table-column label="探针/发言" min-width="150">
          <template #default="{ row }">
            <div>{{ pct(row.probe_success_rate_24h) }} / {{ pct(row.writable_rate) }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="ad_eligible_groups" label="可投放群" width="100" />
        <el-table-column label="实时广告额度（日 / 轮）" min-width="190">
          <template #default="{ row }">
            {{ row.dynamic_daily_limit }} / {{ row.dynamic_run_limit }}
          </template>
        </el-table-column>
        <el-table-column label="投放阻塞" min-width="170">
          <template #default="{ row }">
            <el-tag :type="diagnosticTagType(row)" size="small">{{ diagnosticLabel(row) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="下一步" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ nextActionText(row) }}</template>
        </el-table-column>
        <el-table-column label="群状态" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ groupDiagnosticText(row) }}</template>
        </el-table-column>
        <el-table-column label="近期错误" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ compactError(row) }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-header">
        <h3>群广告许可与档位</h3>
        <el-tag type="info" effect="plain">
          单群全局硬上限 {{ groupGlobalAdDailyLimit ?? '-' }}/天
        </el-tag>
      </div>
      <el-table :data="groupAdProfiles" size="small" border height="360">
        <el-table-column label="群" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.group_title || row.telegram_group_id }}</template>
        </el-table-column>
        <el-table-column label="广告许可" min-width="180">
          <template #default="{ row }">
            <el-select v-model="row.ad_policy_mode" size="small">
              <el-option v-for="item in adPolicyModeOptions" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="档位" width="100">
          <template #default="{ row }"><el-tag size="small">{{ row.ad_tier }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="daily_capacity" label="日容量" width="90" />
        <el-table-column label="24h样本" width="100">
          <template #default="{ row }">{{ row.metrics?.completed_samples || 0 }}</template>
        </el-table-column>
        <el-table-column label="24h存活" width="100">
          <template #default="{ row }">{{ pct(row.metrics?.survival_rate_24h || 0) }}</template>
        </el-table-column>
        <el-table-column label="转化" width="80">
          <template #default="{ row }">{{ row.metrics?.conversions || 0 }}</template>
        </el-table-column>
        <el-table-column label="无删除" width="90">
          <template #default="{ row }">{{ row.metrics?.clean_days || 0 }}天</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button
              :icon="Check"
              type="primary"
              size="small"
              :loading="saving === `group-policy-${row.group_id}`"
              @click="saveGroupAdPolicy(row)"
            >保存</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-header">
        <h3>投放阻塞明细</h3>
        <el-tag type="info" effect="plain">只读诊断</el-tag>
      </div>
      <div class="diagnostic-grid">
        <div v-for="row in dynamicStatuses" :key="row.account_id" class="diagnostic-card">
          <div class="diagnostic-card__head">
            <div>
              <strong>{{ accountLabel(row) }}</strong>
              <small>#{{ row.account_id }}</small>
            </div>
            <el-tag :type="diagnosticTagType(row)" size="small">{{ diagnosticLabel(row) }}</el-tag>
          </div>
          <div class="diagnostic-next">{{ nextActionText(row) }}</div>
          <div class="diagnostic-next">健康：{{ dynamicHealthText(row) }}</div>
          <div class="diagnostic-counts">{{ groupDiagnosticText(row) }}</div>
          <div class="diagnostic-tags">
            <el-tag
              v-for="item in row.dynamic_health_diagnostic?.negative_adjustments || []"
              :key="item.reason"
              :type="severityTagType(item.severity)"
              size="small"
              effect="plain"
            >
              {{ item.label }} {{ item.delta }}
            </el-tag>
          </div>
          <div class="diagnostic-tags">
            <el-tag
              v-for="reason in row.delivery_diagnostic?.block_reasons || []"
              :key="reason.reason"
              :type="severityTagType(reason.severity)"
              size="small"
              effect="plain"
            >
              {{ reason.label }}{{ reason.detail ? `：${reason.detail}` : '' }}
            </el-tag>
          </div>
          <el-table
            v-if="row.delivery_diagnostic?.blocked_group_samples?.length"
            :data="row.delivery_diagnostic.blocked_group_samples"
            size="small"
            border
            height="180"
          >
            <el-table-column label="群" min-width="150" show-overflow-tooltip>
              <template #default="{ row: group }">{{ group.title || group.telegram_group_id }}</template>
            </el-table-column>
            <el-table-column label="原因" min-width="140">
              <template #default="{ row: group }">
                <el-tag :type="severityTagType(group.severity)" size="small">
                  {{ group.label }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" min-width="180" show-overflow-tooltip>
              <template #default="{ row: group }">
                {{ group.warmup_status }} / {{ group.probe_status }} / {{ group.ad_status }}
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <h3>配置中心</h3>
        <div class="config-summary">
          <el-tag v-if="unsavedCount" type="warning" effect="plain">
            {{ unsavedCount }} 项未保存
          </el-tag>
          <el-tag v-if="saveErrorCount" type="danger" effect="plain">
            {{ saveErrorCount }} 项保存失败
          </el-tag>
        </div>
      </div>
      <el-tabs v-model="activeConfigTab" class="config-tabs">
        <el-tab-pane label="入群与群检测" name="join">
          <div class="form-grid">
            <el-form label-width="160px" size="small">
              <el-form-item label="自动入群">
                <el-switch v-model="schedulerForm.enabled" />
              </el-form-item>
              <el-form-item label="扫描间隔">
                <el-input-number v-model="schedulerForm.scan_interval_minutes" :min="1" :max="1440" />
              </el-form-item>
              <el-form-item label="标题黑名单">
                <el-switch v-model="schedulerForm.search_filter!.title_blacklist_enabled" />
              </el-form-item>
              <el-form-item label="黑名单词表">
                <el-input
                  v-model="titleBlacklistText"
                  type="textarea"
                  :rows="8"
                  maxlength="4000"
                  show-word-limit
                />
              </el-form-item>
            </el-form>
            <el-form label-width="160px" size="small">
              <el-form-item label="群检测">
                <el-switch v-model="schedulerForm.join_verification!.enabled" />
              </el-form-item>
              <el-form-item label="检测AI">
                <el-switch v-model="schedulerForm.join_verification!.ai_enabled" />
              </el-form-item>
              <el-form-item label="可信阈值">
                <el-input-number v-model="schedulerForm.join_verification!.confidence_threshold" :min="0" :max="1" :step="0.01" />
              </el-form-item>
              <el-form-item label="消息采样">
                <el-input-number v-model="schedulerForm.join_verification!.message_limit" :min="5" :max="50" />
              </el-form-item>
              <el-form-item label="未知验证">
                <el-select v-model="schedulerForm.join_verification!.unknown_challenge_action">
                  <el-option v-for="item in unknownChallengeActionOptions" :key="item.value" :label="item.label" :value="item.value" />
                </el-select>
              </el-form-item>
            </el-form>
            <el-form label-width="160px" size="small">
              <el-form-item label="验证后等待">
                <el-input-number v-model="schedulerForm.join_verification!.post_action_wait_seconds" :min="0" :max="120" />
              </el-form-item>
              <el-form-item label="额外等待">
                <el-input-number v-model="schedulerForm.join_verification!.post_action_extra_wait_seconds" :min="0" :max="30" :step="1" />
              </el-form-item>
              <el-form-item label="复查次数">
                <el-input-number v-model="schedulerForm.join_verification!.post_action_recheck_attempts" :min="1" :max="10" />
              </el-form-item>
              <el-form-item label="AI超时">
                <el-input-number v-model="schedulerForm.join_verification!.ai_timeout_seconds" :min="1" :max="45" :step="1" />
              </el-form-item>
              <el-form-item label="操作超时">
                <el-input-number v-model="schedulerForm.join_verification!.action_timeout_seconds" :min="1" :max="20" :step="1" />
              </el-form-item>
              <el-form-item label="待同步最小时间">
                <el-input-number v-model="schedulerForm.join_verification!.pending_sync_min_age_seconds" :min="30" :max="3600" :step="10" />
              </el-form-item>
              <el-form-item label="待同步批量">
                <el-input-number v-model="schedulerForm.join_verification!.pending_sync_limit" :min="1" :max="20" />
              </el-form-item>
            </el-form>
            <el-form label-width="160px" size="small">
              <el-form-item label="允许点按钮">
                <el-switch v-model="schedulerForm.join_verification!.allow_button_clicks" />
              </el-form-item>
              <el-form-item label="允许文本回答">
                <el-switch v-model="schedulerForm.join_verification!.allow_text_answers" />
              </el-form-item>
              <el-form-item label="回答身份描述">
                <el-input
                  v-model="schedulerForm.join_verification!.answer_profile"
                  type="textarea"
                  :rows="3"
                  maxlength="500"
                  show-word-limit
                />
              </el-form-item>
              <el-form-item label="清理无转化群">
                <el-switch v-model="schedulerForm.group_capacity_cleanup!.enabled" />
              </el-form-item>
              <el-form-item label="无转化天数">
                <el-input-number v-model="schedulerForm.group_capacity_cleanup!.no_conversion_days" :min="1" :max="365" />
              </el-form-item>
              <el-form-item label="最小入群天数">
                <el-input-number v-model="schedulerForm.group_capacity_cleanup!.min_join_age_days" :min="1" :max="365" />
              </el-form-item>
              <el-form-item label="单轮清理上限">
                <el-input-number v-model="schedulerForm.group_capacity_cleanup!.max_cleanup_per_run" :min="1" :max="15" />
              </el-form-item>
              <el-form-item>
                <div class="config-save-row">
                  <span v-if="saveErrors.scheduler" class="config-save-error">{{ saveErrors.scheduler }}</span>
                  <el-tag v-else-if="isSectionDirty('scheduler')" type="warning" effect="plain">未保存</el-tag>
                  <el-button type="primary" :icon="Check" :loading="saving === 'scheduler'" @click="saveScheduler">保存</el-button>
                </div>
              </el-form-item>
            </el-form>
          </div>
        </el-tab-pane>

        <el-tab-pane label="暖号期" name="warmup">
          <div class="form-grid">
            <el-form label-width="150px" size="small">
              <el-form-item label="暖号策略">
                <el-switch v-model="warmupPolicyForm.enabled" />
              </el-form-item>
              <el-form-item label="默认天数">
                <el-input-number v-model="warmupPolicyForm.default_warmup_days" :min="7" :max="120" />
              </el-form-item>
              <el-form-item label="账号生命周期最短暖号天数">
                <el-input-number v-model="warmupPolicyForm.minimum_warmup_days" :min="7" :max="120" />
              </el-form-item>
              <el-form-item label="用户私聊倍率">
                <el-input-number v-model="warmupPolicyForm.user_initiated_private_message_multiplier" :min="0" :max="2" :step="0.05" />
              </el-form-item>
            </el-form>
            <el-table :data="assetTierOptions" size="small" border>
              <el-table-column label="账号等级" width="110">
                <template #default="{ row }">{{ row.label }}</template>
              </el-table-column>
              <el-table-column label="暖号天数" min-width="140">
                <template #default="{ row }">
                  <el-input-number v-model="warmupPolicyForm.tiers[row.value].warmup_days" :min="7" :max="120" size="small" />
                </template>
              </el-table-column>
            </el-table>
            <el-table :data="warmupStageOptions" size="small" border class="wide-config-table">
              <el-table-column label="阶段" width="90" fixed>
                <template #default="{ row }">{{ row.label }}</template>
              </el-table-column>
              <el-table-column label="总额度" min-width="130">
                <template #default="{ row }">
                  <el-input-number v-model="warmupPolicyForm.stages[row.value].limit_multiplier" :min="0" :max="2" :step="0.05" size="small" />
                </template>
              </el-table-column>
              <el-table-column label="加群" min-width="130">
                <template #default="{ row }">
                  <el-input-number v-model="warmupPolicyForm.stages[row.value].join_multiplier" :min="0" :max="2" :step="0.05" size="small" />
                </template>
              </el-table-column>
              <el-table-column label="广告" min-width="130">
                <template #default="{ row }">
                  <el-input-number v-model="warmupPolicyForm.stages[row.value].ad_multiplier" :min="0" :max="2" :step="0.05" size="small" />
                </template>
              </el-table-column>
              <el-table-column label="运行" min-width="130">
                <template #default="{ row }">
                  <el-input-number v-model="warmupPolicyForm.stages[row.value].run_multiplier" :min="0" :max="2" :step="0.05" size="small" />
                </template>
              </el-table-column>
              <el-table-column label="探针" min-width="130">
                <template #default="{ row }">
                  <el-input-number v-model="warmupPolicyForm.stages[row.value].probe_multiplier" :min="0" :max="2" :step="0.05" size="small" />
                </template>
              </el-table-column>
              <el-table-column label="私聊" min-width="130">
                <template #default="{ row }">
                  <el-input-number v-model="warmupPolicyForm.stages[row.value].private_message_multiplier" :min="0" :max="2" :step="0.05" size="small" />
                </template>
              </el-table-column>
              <el-table-column label="群消息" min-width="130">
                <template #default="{ row }">
                  <el-input-number v-model="warmupPolicyForm.stages[row.value].group_message_multiplier" :min="0" :max="2" :step="0.05" size="small" />
                </template>
              </el-table-column>
              <el-table-column label="资料" min-width="130">
                <template #default="{ row }">
                  <el-input-number v-model="warmupPolicyForm.stages[row.value].profile_update_multiplier" :min="0" :max="2" :step="0.05" size="small" />
                </template>
              </el-table-column>
              <el-table-column label="主动私聊" min-width="110">
                <template #default="{ row }">
                  <el-switch v-model="warmupPolicyForm.stages[row.value].allow_proactive_private_message" size="small" />
                </template>
              </el-table-column>
            </el-table>
          </div>
          <div class="form-actions">
            <span v-if="saveErrors.warmup" class="config-save-error">{{ saveErrors.warmup }}</span>
            <el-tag v-else-if="isSectionDirty('warmup')" type="warning" effect="plain">未保存</el-tag>
            <el-button type="primary" :icon="Check" :loading="saving === 'warmup'" @click="saveWarmupPolicy">保存</el-button>
          </div>
        </el-tab-pane>

        <el-tab-pane label="账号等级" name="asset">
          <el-form label-width="150px" size="small">
            <el-form-item label="等级策略">
              <el-switch v-model="assetPolicyForm.enabled" />
            </el-form-item>
          </el-form>
          <el-table :data="assetTierOptions" size="small" border>
            <el-table-column label="等级" width="110">
              <template #default="{ row }">{{ row.label }}</template>
            </el-table-column>
            <el-table-column label="加群倍率">
              <template #default="{ row }">
                <el-input-number v-model="assetPolicyForm.tiers[row.value].join_multiplier" :min="0" :max="3" :step="0.05" size="small" />
              </template>
            </el-table-column>
            <el-table-column label="广告倍率">
              <template #default="{ row }">
                <el-input-number v-model="assetPolicyForm.tiers[row.value].ad_multiplier" :min="0" :max="3" :step="0.05" size="small" />
              </template>
            </el-table-column>
            <el-table-column label="运行倍率">
              <template #default="{ row }">
                <el-input-number v-model="assetPolicyForm.tiers[row.value].run_multiplier" :min="0" :max="3" :step="0.05" size="small" />
              </template>
            </el-table-column>
            <el-table-column label="探针倍率">
              <template #default="{ row }">
                <el-input-number v-model="assetPolicyForm.tiers[row.value].probe_multiplier" :min="0" :max="3" :step="0.05" size="small" />
              </template>
            </el-table-column>
            <el-table-column label="暖号天数">
              <template #default="{ row }">
                <el-input-number v-model="assetPolicyForm.tiers[row.value].warmup_days" :min="7" :max="120" size="small" />
              </template>
            </el-table-column>
            <el-table-column label="年龄门槛天">
              <template #default="{ row }">
                <el-input-number v-model="assetPolicyForm.tiers[row.value].age_floor_days" :min="0" :max="3650" size="small" />
              </template>
            </el-table-column>
          </el-table>
          <div class="form-actions">
            <span v-if="saveErrors.asset" class="config-save-error">{{ saveErrors.asset }}</span>
            <el-tag v-else-if="isSectionDirty('asset')" type="warning" effect="plain">未保存</el-tag>
            <el-button type="primary" :icon="Check" :loading="saving === 'asset'" @click="saveAssetPolicy">保存</el-button>
          </div>
        </el-tab-pane>

        <el-tab-pane label="账号风控" name="risk">
          <div class="single-entry-note">
            配置全局生效到所有账号，额度按单账号独立累计，不是全部账号共享总额度。
          </div>
          <div class="form-grid">
            <el-form label-width="150px" size="small">
              <el-form-item label="风控开关">
                <el-switch v-model="riskGuardForm.enabled" />
              </el-form-item>
              <el-form-item label="单号总日额度">
                <el-input-number v-model="riskGuardForm.global_daily_limit" :min="1" :max="30" />
              </el-form-item>
              <el-form-item label="共享群写日额度">
                <el-input-number v-model="riskGuardForm.group_write_daily_limit" :min="1" :max="8" />
              </el-form-item>
              <el-form-item label="Redis失败关闭">
                <el-select v-model="redisFailClosedValue">
                  <el-option label="跟随系统" value="system" />
                  <el-option label="失败即关闭" value="closed" />
                  <el-option label="失败放行" value="open" />
                </el-select>
              </el-form-item>
            </el-form>
            <el-table :data="riskActionOptions" size="small" border>
              <el-table-column label="动作" width="100">
                <template #default="{ row }">{{ row.label }}</template>
              </el-table-column>
              <el-table-column label="单号日额度">
                <template #default="{ row }">
                  <el-input-number v-model="riskGuardForm.actions[row.value].daily_limit" :min="1" :max="100000" size="small" />
                </template>
              </el-table-column>
              <el-table-column label="冷却秒">
                <template #default="{ row }">
                  <el-input-number v-model="riskGuardForm.actions[row.value].cooldown_seconds" :min="0" :max="86400" size="small" />
                </template>
              </el-table-column>
            </el-table>
          </div>
          <div class="form-grid secondary-grid">
            <el-table :data="riskLevelPolicyOptions" size="small" border>
              <el-table-column label="风险等级" width="100">
                <template #default="{ row }">{{ row.label }}</template>
              </el-table-column>
              <el-table-column label="分数阈值">
                <template #default="{ row }">
                  <span v-if="row.value === 'normal'" class="muted-text">-</span>
                  <el-input-number
                    v-else
                    v-model="riskGuardForm.level_thresholds[row.value]"
                    :min="0"
                    :max="100"
                    :step="1"
                    size="small"
                  />
                </template>
              </el-table-column>
              <el-table-column label="频率倍率">
                <template #default="{ row }">
                  <el-input-number v-model="riskGuardForm.level_budget_multipliers[row.value]" :min="0" :max="2" :step="0.05" size="small" />
                </template>
              </el-table-column>
            </el-table>
            <el-table :data="riskScoreDeltaOptions" size="small" border>
              <el-table-column label="风险事件" width="130">
                <template #default="{ row }">{{ row.label }}</template>
              </el-table-column>
              <el-table-column label="风险分增量">
                <template #default="{ row }">
                  <el-input-number v-model="riskGuardForm.risk_score_deltas[row.value]" :min="0" :max="100" :step="0.5" size="small" />
                </template>
              </el-table-column>
            </el-table>
          </div>
          <div class="form-grid secondary-grid">
            <el-form label-width="170px" size="small">
              <el-form-item label="默认冻结秒">
                <el-input-number v-model="riskGuardForm.lifecycle.default_freeze_seconds" :min="60" :max="604800" :step="60" />
              </el-form-item>
              <el-form-item label="Flood缓冲秒">
                <el-input-number v-model="riskGuardForm.lifecycle.flood_wait_buffer_seconds" :min="0" :max="3600" :step="10" />
              </el-form-item>
              <el-form-item label="PeerFlood冻结秒">
                <el-input-number v-model="riskGuardForm.lifecycle.peer_flood_freeze_seconds" :min="60" :max="604800" :step="60" />
              </el-form-item>
              <el-form-item label="账号受限冻结秒">
                <el-input-number v-model="riskGuardForm.lifecycle.account_restricted_freeze_seconds" :min="60" :max="604800" :step="60" />
              </el-form-item>
              <el-form-item label="群禁言冻结秒">
                <el-input-number v-model="riskGuardForm.lifecycle.group_write_forbidden_freeze_seconds" :min="60" :max="604800" :step="60" />
              </el-form-item>
              <el-form-item label="恢复期秒">
                <el-input-number v-model="riskGuardForm.lifecycle.recovery_seconds" :min="60" :max="604800" :step="60" />
              </el-form-item>
              <el-form-item label="冻结恢复分数上限">
                <el-input-number v-model="riskGuardForm.lifecycle.post_freeze_score_cap" :min="0" :max="100" :step="1" />
              </el-form-item>
              <el-form-item label="手动清除分数上限">
                <el-input-number v-model="riskGuardForm.lifecycle.manual_clear_score_cap" :min="0" :max="100" :step="1" />
              </el-form-item>
              <el-form-item label="衰减周期小时">
                <el-input-number v-model="riskGuardForm.lifecycle.decay_interval_hours" :min="1" :max="720" />
              </el-form-item>
              <el-form-item label="每周期降低分">
                <el-input-number v-model="riskGuardForm.lifecycle.decay_points_per_interval" :min="0" :max="100" :step="0.5" />
              </el-form-item>
            </el-form>
            <el-form label-width="170px" size="small">
              <el-form-item label="新号天数">
                <el-input-number v-model="riskGuardForm.lifecycle.new_account_days" :min="0" :max="120" />
              </el-form-item>
              <el-form-item label="新号倍率">
                <el-input-number v-model="riskGuardForm.lifecycle.new_account_multiplier" :min="0" :max="2" :step="0.05" />
              </el-form-item>
              <el-form-item label="恢复期倍率">
                <el-input-number v-model="riskGuardForm.lifecycle.recovery_multiplier" :min="0" :max="2" :step="0.05" />
              </el-form-item>
              <el-form-item label="稳定账号天数">
                <el-input-number v-model="riskGuardForm.lifecycle.healthy_account_days" :min="0" :max="365" />
              </el-form-item>
              <el-form-item label="稳定账号倍率">
                <el-input-number v-model="riskGuardForm.lifecycle.healthy_account_multiplier" :min="0" :max="2" :step="0.05" />
              </el-form-item>
              <el-form-item label="最大频率倍率">
                <el-input-number v-model="riskGuardForm.lifecycle.max_budget_multiplier" :min="0" :max="2" :step="0.05" />
              </el-form-item>
              <el-form-item label="冻结窗口小时">
                <el-input-number v-model="riskGuardForm.group_write_forbidden.freeze_window_hours" :min="1" :max="168" />
              </el-form-item>
              <el-form-item label="冻结命中群数">
                <el-input-number v-model="riskGuardForm.group_write_forbidden.freeze_distinct_groups" :min="1" :max="100" />
              </el-form-item>
              <el-form-item label="隔离窗口小时">
                <el-input-number v-model="riskGuardForm.group_write_forbidden.quarantine_window_hours" :min="1" :max="720" />
              </el-form-item>
              <el-form-item label="隔离命中群数">
                <el-input-number v-model="riskGuardForm.group_write_forbidden.quarantine_distinct_groups" :min="1" :max="200" />
              </el-form-item>
              <el-form-item label="低价值日志保留天">
                <el-input-number v-model="riskGuardForm.retention.low_value_detail_retention_days" :min="1" :max="3650" />
              </el-form-item>
              <el-form-item label="高价值日志保留天">
                <el-input-number v-model="riskGuardForm.retention.high_value_detail_retention_days" :min="1" :max="3650" />
              </el-form-item>
              <el-form-item label="日统计保留天">
                <el-input-number v-model="riskGuardForm.retention.daily_stat_retention_days" :min="1" :max="3650" />
              </el-form-item>
            </el-form>
          </div>
          <div class="form-actions">
            <span v-if="saveErrors.risk" class="config-save-error">{{ saveErrors.risk }}</span>
            <el-tag v-else-if="isSectionDirty('risk')" type="warning" effect="plain">未保存</el-tag>
            <el-button type="primary" :icon="Check" :loading="saving === 'risk'" @click="saveRiskGuard">保存</el-button>
          </div>
        </el-tab-pane>

        <el-tab-pane label="广告投放" name="ads">
          <div class="form-grid">
            <el-form label-width="150px" size="small">
              <el-form-item label="投放执行">
                <el-switch v-model="adExecutionForm.enabled" />
              </el-form-item>
              <el-form-item label="执行间隔">
                <el-input-number v-model="adExecutionForm.dispatcher_interval_seconds" :min="1" :max="86400" />
              </el-form-item>
              <el-form-item label="单轮上限">
                <el-input-number v-model="adExecutionForm.max_deliveries_per_run" :min="1" :max="1" />
              </el-form-item>
              <el-form-item label="单号单轮上限">
                <el-input-number v-model="adExecutionForm.max_deliveries_per_account_per_run" :min="1" :max="1" />
              </el-form-item>
              <el-form-item label="群广告冷却">
                <el-input-number v-model="adExecutionForm.group_campaign_cooldown_minutes" :min="4320" :max="10080" />
              </el-form-item>
              <el-form-item label="成功后停号">
                <el-switch v-model="adExecutionForm.stop_account_after_success" />
              </el-form-item>
              <el-form-item label="失败后停号">
                <el-switch v-model="adExecutionForm.stop_account_after_failure" />
              </el-form-item>
              <el-form-item label="节流策略">
                <el-switch v-model="adThrottleForm.enabled" />
              </el-form-item>
              <el-form-item label="投放间隔">
                <el-input-number v-model="adThrottleForm.delivery_interval_seconds" :min="9000" :max="86400" />
              </el-form-item>
              <el-form-item label="批次窗口">
                <el-input-number v-model="adThrottleForm.batch_window_seconds" :min="1" :max="3600" />
              </el-form-item>
              <el-form-item label="批次最小量">
                <el-input-number v-model="adThrottleForm.batch_size_min" :min="1" :max="1" />
              </el-form-item>
              <el-form-item label="批次最大量">
                <el-input-number v-model="adThrottleForm.batch_size_max" :min="1" :max="1" />
              </el-form-item>
              <el-form-item label="冷却最小秒">
                <el-input-number v-model="adThrottleForm.cooldown_min_seconds" :min="9000" :max="86400" />
              </el-form-item>
              <el-form-item label="冷却最大秒">
                <el-input-number v-model="adThrottleForm.cooldown_max_seconds" :min="9000" :max="86400" />
              </el-form-item>
            </el-form>
            <el-form label-width="150px" size="small">
              <el-form-item label="动态容量">
                <el-switch v-model="adCapacityForm.enabled" />
              </el-form-item>
              <el-form-item label="时区偏移">
                <el-input-number v-model="adCapacityForm.timezone_offset_hours" :min="-12" :max="14" />
              </el-form-item>
              <el-form-item label="窗口开始小时">
                <el-input-number v-model="adCapacityForm.window_start_hour" :min="0" :max="23" />
              </el-form-item>
              <el-form-item label="窗口结束小时">
                <el-input-number v-model="adCapacityForm.window_end_hour" :min="0" :max="23" />
              </el-form-item>
              <el-form-item label="账号广告每日硬上限（系统）">
                <el-input-number v-model="adCapacityForm.account_ad_daily_hard_cap" :min="1" :max="5" />
              </el-form-item>
              <el-form-item label="单账号单群每日硬上限（系统）">
                <el-input-number v-model="adCapacityForm.account_group_daily_cap_default" :min="1" :max="1" />
              </el-form-item>
              <el-form-item label="群全局日硬上限">
                <el-input-number v-model="adCapacityForm.group_global_daily_hard_cap" :min="1" :max="400" />
              </el-form-item>
              <el-form-item label="群广告最小间隔秒">
                <el-input-number v-model="adCapacityForm.group_min_interval_seconds" :min="259200" :max="604800" />
              </el-form-item>
              <el-form-item label="单号最大群数">
                <el-input-number v-model="adCapacityForm.max_groups_per_account" :min="1" :max="1000" />
              </el-form-item>
              <el-form-item label="新广告群/天">
                <el-input-number v-model="adCapacityForm.max_new_ad_groups_per_day" :min="0" :max="2" />
              </el-form-item>
              <el-form-item label="删帖检测延迟">
                <el-input-number v-model="adCapacityForm.survival_check_delay_seconds" :min="30" :max="3600" :step="10" />
              </el-form-item>
              <el-form-item label="1小时检测点">
                <el-input-number v-model="adCapacityForm.survival_one_hour_seconds" :min="300" :max="7200" :step="300" />
              </el-form-item>
              <el-form-item label="24小时检测点">
                <el-input-number v-model="adCapacityForm.survival_twenty_four_hour_seconds" :min="3600" :max="172800" :step="3600" />
              </el-form-item>
              <el-form-item label="检测批量">
                <el-input-number v-model="adCapacityForm.survival_check_batch_size" :min="1" :max="500" />
              </el-form-item>
              <el-form-item label="检测重试次数">
                <el-input-number v-model="adCapacityForm.survival_retry_max_attempts" :min="1" :max="10" />
              </el-form-item>
              <el-form-item label="重试基础秒数">
                <el-input-number v-model="adCapacityForm.survival_retry_base_seconds" :min="60" :max="3600" />
              </el-form-item>
              <el-form-item label="删帖退群">
                <el-switch v-model="adCapacityForm.leave_on_deleted_ad" />
              </el-form-item>
              <el-form-item label="探针失败封群">
                <el-switch v-model="adCapacityForm.block_group_on_probe_failure" />
              </el-form-item>
              <el-form-item label="AI许可复核">
                <el-switch v-model="adCapacityForm.ad_policy_ai_enabled" />
              </el-form-item>
              <el-form-item label="许可识别模型">
                <el-input v-model="adCapacityForm.ad_policy_ai_model" maxlength="100" />
              </el-form-item>
              <el-form-item label="许可识别超时">
                <el-input-number v-model="adCapacityForm.ad_policy_ai_timeout_seconds" :min="5" :max="120" />
              </el-form-item>
              <el-form-item label="许可最低置信度">
                <el-input-number v-model="adCapacityForm.ad_policy_ai_min_confidence" :min="90" :max="100" />
              </el-form-item>
              <el-form-item label="双阶段复核">
                <el-switch v-model="adCapacityForm.ad_policy_ai_require_second_pass" />
              </el-form-item>
              <el-form-item label="未知群自动检测">
                <el-switch v-model="adCapacityForm.ad_policy_auto_probe_enabled" />
              </el-form-item>
              <el-form-item label="每账号检测每日上限">
                <el-input-number v-model="adCapacityForm.ad_policy_auto_probe_daily_limit_per_account" :min="0" :max="20" />
              </el-form-item>
              <el-form-item label="重复检测间隔小时">
                <el-input-number v-model="adCapacityForm.ad_policy_auto_probe_interval_hours" :min="1" :max="168" />
              </el-form-item>
              <el-form-item label="Premium最小样本">
                <el-input-number v-model="adCapacityForm.premium_min_samples" :min="1" :max="1000" />
              </el-form-item>
              <el-form-item label="Premium最小转化">
                <el-input-number v-model="adCapacityForm.premium_min_conversions" :min="1" :max="1000" />
              </el-form-item>
              <el-form-item label="Premium存活率%">
                <el-input-number v-model="adCapacityForm.premium_survival_rate_percent" :min="50" :max="100" />
              </el-form-item>
              <el-form-item label="自动许可清洁天数">
                <el-input-number v-model="adCapacityForm.premium_clean_days_auto" :min="3" :max="30" />
              </el-form-item>
              <el-form-item label="人工许可清洁天数">
                <el-input-number v-model="adCapacityForm.premium_clean_days_verified" :min="3" :max="30" />
              </el-form-item>
              <el-form-item label="Premium增长样本">
                <el-input-number v-model="adCapacityForm.premium_growth_samples" :min="20" :max="1000" />
              </el-form-item>
              <el-form-item label="Premium满额样本">
                <el-input-number v-model="adCapacityForm.premium_full_capacity_samples" :min="20" :max="5000" />
              </el-form-item>
              <el-form-item label="Premium入场容量">
                <el-input-number v-model="adCapacityForm.premium_entry_capacity" :min="1" :max="20" />
              </el-form-item>
              <el-form-item label="Premium增长容量">
                <el-input-number v-model="adCapacityForm.premium_growth_capacity" :min="1" :max="50" />
              </el-form-item>
              <el-form-item label="广告首次投放等待天数">
                <el-input-number v-model="adCapacityForm.warmup_days_before_ads" :min="7" :max="90" />
              </el-form-item>
              <el-form-item label="暖群互动最小">
                <el-input-number v-model="adCapacityForm.warmup_daily_interactions_min" :min="0" :max="20" />
              </el-form-item>
              <el-form-item label="暖群互动最大">
                <el-input-number v-model="adCapacityForm.warmup_daily_interactions_max" :min="0" :max="20" />
              </el-form-item>
              <el-form-item label="成熟互动最小">
                <el-input-number v-model="adCapacityForm.mature_daily_interactions_min" :min="0" :max="20" />
              </el-form-item>
              <el-form-item label="成熟互动最大">
                <el-input-number v-model="adCapacityForm.mature_daily_interactions_max" :min="0" :max="20" />
              </el-form-item>
              <el-form-item label="失败策略">
                <el-switch v-model="adFailurePolicyForm.enabled" />
              </el-form-item>
              <el-form-item label="群控失败退群">
                <el-switch v-model="adFailurePolicyForm.leave_on_group_control_failure" />
              </el-form-item>
              <el-form-item label="失败次数阈值">
                <el-input-number v-model="adFailurePolicyForm.group_control_failure_limit" :min="1" :max="20" />
              </el-form-item>
              <el-form-item label="失败统计窗口">
                <el-input-number v-model="adFailurePolicyForm.group_control_failure_window_hours" :min="1" :max="720" />
              </el-form-item>
              <el-form-item label="失败适用等级">
                <el-select v-model="adFailurePolicyForm.levels" multiple>
                  <el-option v-for="item in groupLevelOptions" :key="item.value" :label="item.label" :value="item.value" />
                </el-select>
              </el-form-item>
            </el-form>
          </div>
          <div class="form-grid secondary-grid">
            <el-table :data="adCapacityTierOptions" size="small" border>
              <el-table-column label="群等级" width="100">
                <template #default="{ row }">{{ row.label }}</template>
              </el-table-column>
              <el-table-column label="日容量">
                <template #default="{ row }">
                  <el-input-number v-model="adCapacityForm.tier_daily_capacities[row.value]" :min="0" :max="10000" size="small" />
                </template>
              </el-table-column>
            </el-table>
            <el-table :data="adHourlyWeightOptions" size="small" border height="360">
              <el-table-column label="小时" width="100">
                <template #default="{ row }">{{ row.label }}</template>
              </el-table-column>
              <el-table-column label="权重">
                <template #default="{ row }">
                  <el-input-number v-model="adCapacityForm.hourly_weights[row.value]" :min="0" :max="10000" size="small" />
                </template>
              </el-table-column>
            </el-table>
          </div>
          <div class="form-actions">
            <span v-if="saveErrors.ads" class="config-save-error">{{ saveErrors.ads }}</span>
            <el-tag v-else-if="isSectionDirty('ads')" type="warning" effect="plain">未保存</el-tag>
            <el-button type="primary" :icon="Check" :loading="saving === 'ads'" @click="saveAdsPolicy">保存</el-button>
          </div>
        </el-tab-pane>

        <el-tab-pane label="群AI互动" name="group-ai">
          <div class="form-grid">
            <el-form label-width="160px" size="small">
              <el-form-item label="互动模块">
                <el-switch v-model="groupAiForm.enabled" />
              </el-form-item>
              <el-form-item label="AI生成">
                <el-switch v-model="groupAiForm.aiEnabled" />
              </el-form-item>
              <el-form-item label="模式">
                <el-select v-model="groupAiForm.mode">
                  <el-option v-for="item in groupAiModeOptions" :key="item.value" :label="item.label" :value="item.value" />
                </el-select>
              </el-form-item>
              <el-form-item label="语气">
                <el-select v-model="groupAiForm.tone">
                  <el-option v-for="item in groupAiToneOptions" :key="item.value" :label="item.label" :value="item.value" />
                </el-select>
              </el-form-item>
              <el-form-item label="关键词群回复">
                <el-switch v-model="groupAiForm.allowKeywordTriggeredReply" />
              </el-form-item>
              <el-form-item label="语义回复">
                <el-switch v-model="groupAiForm.allowSemanticTriggeredReply" />
              </el-form-item>
              <el-form-item label="主动暖号">
                <el-switch v-model="groupAiForm.allowProactiveWarmup" />
              </el-form-item>
            </el-form>
            <el-form label-width="160px" size="small">
              <el-form-item label="群聊每日Token预算">
                <el-input-number v-model="groupAiForm.dailyTokenBudget" :min="0" :max="10000000" :step="1000" />
              </el-form-item>
              <el-form-item label="单群回复/天">
                <el-input-number v-model="groupAiForm.maxRepliesPerGroupPerDay" :min="0" :max="10000" />
              </el-form-item>
              <el-form-item label="单号回复/天">
                <el-input-number v-model="groupAiForm.maxRepliesPerAccountPerDay" :min="0" :max="100000" />
              </el-form-item>
              <el-form-item label="回复冷却">
                <el-input-number v-model="groupAiForm.cooldownSeconds" :min="0" :max="86400" :step="60" />
              </el-form-item>
              <el-form-item label="安全过滤">
                <el-switch v-model="groupAiForm.blockAiSelfDisclosure" />
              </el-form-item>
              <el-form-item label="单条字数">
                <el-input-number v-model="groupAiForm.replyMaxChars" :min="20" :max="500" :step="10" />
              </el-form-item>
              <el-form-item label="语义窗口消息">
                <el-input-number v-model="groupAiForm.semanticScanWindowMessages" :min="5" :max="100" />
              </el-form-item>
              <el-form-item label="语义评估间隔">
                <el-input-number v-model="groupAiForm.semanticEvaluateEveryMessages" :min="1" :max="100" />
              </el-form-item>
              <el-form-item label="语义最低置信度">
                <el-input-number v-model="groupAiForm.semanticMinConfidence" :min="0" :max="1" :step="0.01" />
              </el-form-item>
              <el-form-item label="最短文本长度">
                <el-input-number v-model="groupAiForm.semanticMinTextChars" :min="1" :max="80" />
              </el-form-item>
              <el-form-item label="暖场间隔">
                <el-input-number v-model="groupAiForm.proactiveWarmupIntervalMinutes" :min="1" :max="1440" />
              </el-form-item>
              <el-form-item label="单轮群数">
                <el-input-number v-model="groupAiForm.proactiveWarmupMaxGroupsPerRun" :min="1" :max="100" />
              </el-form-item>
              <el-form-item label="暖场单群/天">
                <el-input-number v-model="groupAiForm.proactiveWarmupMaxPerGroupPerDay" :min="0" :max="1000" />
              </el-form-item>
              <el-form-item label="暖场单号/天">
                <el-input-number v-model="groupAiForm.proactiveWarmupMaxPerAccountPerDay" :min="0" :max="10000" />
              </el-form-item>
              <el-form-item label="暖场冷却">
                <el-input-number v-model="groupAiForm.proactiveWarmupCooldownSeconds" :min="60" :max="86400" :step="60" />
              </el-form-item>
              <el-form-item label="暖场开始小时">
                <el-input-number v-model="groupAiForm.proactiveWarmupWindowStartHour" :min="0" :max="23" />
              </el-form-item>
              <el-form-item label="暖场结束小时">
                <el-input-number v-model="groupAiForm.proactiveWarmupWindowEndHour" :min="0" :max="23" />
              </el-form-item>
              <el-form-item label="温度">
                <el-input-number v-model="groupAiForm.temperature" :min="0" :max="2" :step="0.1" />
              </el-form-item>
              <el-form-item label="最大Token">
                <el-input-number v-model="groupAiForm.maxTokens" :min="20" :max="1000" />
              </el-form-item>
            </el-form>
          </div>
          <el-form label-width="160px" size="small">
            <el-form-item label="系统提示词">
              <el-input v-model="groupAiForm.systemPrompt" type="textarea" :rows="4" maxlength="2000" show-word-limit />
            </el-form-item>
            <el-form-item label="语义决策提示词">
              <el-input v-model="groupAiForm.semanticDecisionPrompt" type="textarea" :rows="4" maxlength="2000" show-word-limit />
            </el-form-item>
            <el-form-item label="允许意图">
              <el-input v-model="semanticAllowedIntentsText" type="textarea" :rows="5" maxlength="2000" show-word-limit />
            </el-form-item>
            <el-form-item label="排除意图">
              <el-input v-model="semanticBlockedIntentsText" type="textarea" :rows="5" maxlength="2000" show-word-limit />
            </el-form-item>
            <el-form-item label="暖场主题">
              <el-input v-model="proactiveWarmupTopicsText" type="textarea" :rows="5" maxlength="4000" show-word-limit />
            </el-form-item>
            <el-form-item label="暖场模板">
              <el-input v-model="proactiveWarmupTemplatesText" type="textarea" :rows="8" maxlength="8000" show-word-limit />
            </el-form-item>
            <el-form-item label="按群暖场覆盖">
              <el-input v-model="proactiveWarmupGroupOverridesText" type="textarea" :rows="8" maxlength="12000" show-word-limit />
            </el-form-item>
          </el-form>
          <div class="form-actions">
            <span v-if="saveErrors.groupAi" class="config-save-error">{{ saveErrors.groupAi }}</span>
            <el-tag v-else-if="isSectionDirty('groupAi')" type="warning" effect="plain">未保存</el-tag>
            <el-button type="primary" :icon="Check" :loading="saving === 'groupAi'" @click="saveGroupAi">保存</el-button>
          </div>
        </el-tab-pane>
      </el-tabs>
    </section>

    <section class="panel">
      <div class="panel-header">
        <h3>事件流水</h3>
      </div>
      <el-tabs v-model="activeEventTab">
        <el-tab-pane label="入群" name="attempts">
          <el-table :data="autoJoinAttempts" size="small" border height="260">
            <el-table-column prop="id" label="ID" width="80" />
            <el-table-column prop="account_id" label="账号" width="90" />
            <el-table-column label="群" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.group_title || row.group_username || row.keyword || '-' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag :type="statusTagType(row.status)" size="small">{{ row.status || '-' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="reason" label="原因" min-width="220" show-overflow-tooltip />
            <el-table-column prop="attempted_at" label="时间" min-width="170" />
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="群检测" name="verification">
          <el-table :data="verificationLogs" size="small" border height="260">
            <el-table-column prop="account_id" label="账号" width="90" />
            <el-table-column prop="group_title" label="群" min-width="180" show-overflow-tooltip />
            <el-table-column label="动作" width="110">
              <template #default="{ row }">{{ row.action }}</template>
            </el-table-column>
            <el-table-column label="结果" width="110">
              <template #default="{ row }">
                <el-tag :type="row.success ? 'success' : 'warning'" size="small">{{ row.success === false ? '失败' : '成功' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="reason" label="原因" min-width="220" show-overflow-tooltip />
            <el-table-column prop="updated_at" label="时间" min-width="170" />
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="广告" name="delivery">
          <el-table :data="deliveryLogs" size="small" border height="260">
            <el-table-column prop="account_id" label="账号" width="90" />
            <el-table-column prop="group_title" label="群" min-width="180" show-overflow-tooltip />
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag :type="statusTagType(row.status)" size="small">{{ row.status || '-' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="survival_status" label="存活" width="110" />
            <el-table-column prop="error" label="错误" min-width="220" show-overflow-tooltip />
            <el-table-column prop="created_at" label="时间" min-width="170" />
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </section>
  </div>
</template>

<style scoped lang="scss">
.growth-dashboard {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;

  h2 {
    margin: 0 0 6px;
    color: #1f2d3d;
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 0;
  }
}

.toolbar-meta,
.muted-text {
  color: #6b7280;
  font-size: 13px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.metric-cell {
  min-height: 76px;
  padding: 14px 16px;
  border: 1px solid #d8dee9;
  border-radius: 8px;
  background: #fff;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 8px;

  span {
    color: #6b7280;
    font-size: 13px;
  }

  strong {
    color: #111827;
    font-size: 24px;
    line-height: 1;
    letter-spacing: 0;
  }
}

.panel {
  padding: 16px;
  border: 1px solid #d8dee9;
  border-radius: 8px;
  background: #fff;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;

  h3 {
    margin: 0;
    color: #1f2d3d;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 0;
  }
}

.panel-subtitle {
  margin-top: 5px;
  color: #6b7280;
  font-size: 12px;
}

.flow-grid {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 10px;
}

.flow-item {
  min-height: 114px;
  padding: 12px;
  border: 1px solid #cfd8e3;
  border-radius: 8px;
  background: #fbfcfe;
  display: flex;
  gap: 10px;
  overflow: hidden;

  &.disabled {
    background: #f7f7f8;
    color: #7b8494;
  }
}

.flow-icon {
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 7px;
  color: #1f7a5b;
  background: #e8f5ef;

  .disabled & {
    color: #909399;
    background: #ebeef5;
  }
}

.flow-body {
  min-width: 0;
}

.flow-title {
  color: #111827;
  font-size: 14px;
  font-weight: 700;
}

.flow-status {
  margin-top: 8px;
  color: #1f5f99;
  font-size: 13px;
  font-weight: 600;
  word-break: break-word;
}

.flow-detail {
  margin-top: 6px;
  color: #6b7280;
  font-size: 12px;
  line-height: 1.35;
  word-break: break-word;
}

.account-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;

  small {
    color: #8b95a5;
  }
}

.inline-score {
  margin-left: 8px;
  color: #606266;
  font-size: 12px;
}

.config-tabs {
  :deep(.el-tabs__content) {
    padding-top: 10px;
  }
}

.config-summary {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
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

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
  align-items: start;
}

.secondary-grid,
.wide-config-table {
  margin-top: 16px;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
}

.config-save-row {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
}

.config-save-error {
  max-width: min(620px, 100%);
  color: #b42318;
  font-size: 12px;
  line-height: 1.4;
  text-align: right;
  overflow-wrap: anywhere;
}

.limit-name,
.limit-value {
  color: #111827;
}

.limit-value {
  font-weight: 700;
}

.source-list,
.factor-list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  line-height: 1.5;
}

.source-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: #4b5563;
  white-space: nowrap;
}

.limit-note {
  margin-top: 10px;
  color: #6b7280;
  font-size: 12px;
  line-height: 1.5;
}

:deep(.el-input-number) {
  width: 150px;
}

:deep(.el-select) {
  width: 220px;
}

:deep(.el-tag + .el-tag) {
  margin-left: 6px;
}

.diagnostic-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.diagnostic-card {
  min-width: 0;
  padding: 12px;
  border: 1px solid #d8dee9;
  border-radius: 8px;
  background: #fbfcfe;
}

.diagnostic-card__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;

  > div {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  strong {
    color: #111827;
    font-size: 14px;
    line-height: 1.3;
  }

  small {
    color: #8b95a5;
  }
}

.diagnostic-next,
.diagnostic-counts {
  margin-top: 8px;
  color: #4b5563;
  font-size: 13px;
  line-height: 1.4;
  word-break: break-word;
}

.diagnostic-counts {
  color: #6b7280;
}

.diagnostic-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 10px 0 12px;

  :deep(.el-tag) {
    margin-left: 0;
  }
}

@media (max-width: 1280px) {
  .flow-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .metric-grid,
  .diagnostic-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 860px) {
  .page-toolbar,
  .form-grid {
    grid-template-columns: 1fr;
    flex-direction: column;
    align-items: stretch;
  }

  .flow-grid,
  .metric-grid,
  .diagnostic-grid {
    grid-template-columns: 1fr;
  }
}
</style>
