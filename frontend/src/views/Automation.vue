<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Refresh, Select, Setting, VideoPlay } from '@element-plus/icons-vue'
import { accountsApi } from '@/api/accounts'
import {
  automationApi,
  type AccountAdBinding,
  type AccountOperationConfig,
  type AdCampaign,
  type AdCreative,
  type AutomationRunResult,
} from '@/api/automation'
import { DEFAULT_GROUP_SEARCH_KEYWORD_TYPES, GROUP_SEARCH_KEYWORD_TYPE_OPTIONS } from '@/api/keywords'

type AccountOption = {
  id: number
  phone?: string
  session_name?: string
  status?: string
  is_active?: boolean
}

const loading = ref(false)
const running = ref('')
const activeTab = ref('runs')
const lastResult = ref<AutomationRunResult | null>(null)
const autoJoinAttempts = ref<any[]>([])
const deliveryLogs = ref<any[]>([])
const creatives = ref<AdCreative[]>([])
const campaigns = ref<AdCampaign[]>([])
const bindings = ref<AccountAdBinding[]>([])
const accounts = ref<AccountOption[]>([])
const accountConfigLoading = ref(false)
const savingAccountConfig = ref(false)
const selectedAccountId = ref<number>()

const autoJoinForm = reactive({
  max_accounts: 10,
  keywords_per_account: 5,
  max_groups_per_keyword: 10,
  dry_run: true,
})

const adRunForm = reactive({
  max_deliveries: 20,
  dry_run: true,
})

const keywordReplenishForm = reactive({
  auto_approve: false,
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

const campaignForm = reactive({
  name: '',
  enabled: false,
  status: 'draft',
  send_mode: 'after_join' as 'after_join' | 'interval' | 'scheduled',
  target_group_levels: ['A'],
  start_at: '',
  end_at: '',
  min_wait_after_join_minutes: 60,
  interval_minutes: 1440,
  max_sends_per_group_per_day: 1,
  max_sends_per_account_per_day: 20,
})

const scheduledTimesText = ref('')

const bindingForm = reactive({
  account_id: undefined as number | undefined,
  ad_campaign_id: undefined as number | undefined,
  creative_id: undefined as number | undefined,
  enabled: true,
  priority: 0,
})

const accountConfigForm = reactive({
  enabled: true,
  auto_join_enabled: false,
  auto_ads_enabled: true,
  max_groups_per_day: 5,
  max_groups_total: 100,
  join_interval_min_seconds: 1800,
  join_interval_max_seconds: 7200,
  max_messages_per_day: 30,
  message_interval_seconds: 300,
  quiet_hours_start: '',
  quiet_hours_end: '',
  keyword_types: [...DEFAULT_GROUP_SEARCH_KEYWORD_TYPES] as string[],
  keyword_auto_replenish_enabled: false,
  keyword_replenish_requires_review: true,
  risk_level: 'normal',
  next_join_after: '',
})

const keywordTypeOptions = GROUP_SEARCH_KEYWORD_TYPE_OPTIONS

const riskLevelOptions = [
  { label: '低', value: 'low' },
  { label: '普通', value: 'normal' },
  { label: '高', value: 'high' },
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

const accountMap = computed(() => {
  return new Map(accounts.value.map((item) => [item.id, item]))
})

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

const fillAccountConfigForm = (config: AccountOperationConfig) => {
  Object.assign(accountConfigForm, {
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
    keyword_auto_replenish_enabled: config.keyword_auto_replenish_enabled ?? false,
    keyword_replenish_requires_review: config.keyword_replenish_requires_review ?? true,
    risk_level: config.risk_level || 'normal',
    next_join_after: config.next_join_after || '',
  })
}

const loadAccounts = async () => {
  const payload = await accountsApi.list({ limit: 100, account_type: 'promoter' })
  accounts.value = payload.list
  if (!selectedAccountId.value && accounts.value.length > 0) {
    selectedAccountId.value = accounts.value[0].id
    bindingForm.account_id = accounts.value[0].id
  }
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

const saveAccountConfig = async () => {
  if (!selectedAccountId.value) {
    ElMessage.warning('请先选择账号')
    return
  }
  if (accountConfigForm.join_interval_max_seconds < accountConfigForm.join_interval_min_seconds) {
    ElMessage.warning('最大加群间隔不能小于最小间隔')
    return
  }

  savingAccountConfig.value = true
  try {
    const response = await automationApi.updateAccountOperationConfig(selectedAccountId.value, {
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
      risk_level: accountConfigForm.risk_level,
    })
    fillAccountConfigForm(response.data.data)
    ElMessage.success('账号自动化配置已保存')
  } finally {
    savingAccountConfig.value = false
  }
}

const refreshData = async () => {
  loading.value = true
  try {
    const [attemptsRes, logsRes, creativesRes, campaignsRes, bindingsRes] = await Promise.all([
      automationApi.getAutoJoinAttempts({ limit: 30 }),
      automationApi.getDeliveryLogs({ limit: 30 }),
      automationApi.getCreatives({ page_size: 50 }),
      automationApi.getCampaigns({ page_size: 50 }),
      automationApi.getBindings(),
    ])
    autoJoinAttempts.value = attemptsRes.data.data
    deliveryLogs.value = logsRes.data.data
    creatives.value = creativesRes.data.data
    campaigns.value = campaignsRes.data.data
    bindings.value = bindingsRes.data.data
  } finally {
    loading.value = false
  }
}

const refreshPage = async () => {
  loading.value = true
  try {
    await Promise.all([loadAccounts(), refreshData()])
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

const runAds = () => {
  runTask('ads', () => automationApi.runAds({ ...adRunForm }))
}

const createCreative = async () => {
  if (!creativeForm.name || !creativeForm.content) {
    ElMessage.warning('请填写广告名称和内容')
    return
  }
  await automationApi.createCreative({ ...creativeForm })
  ElMessage.success('广告素材已创建')
  Object.assign(creativeForm, {
    name: '',
    content: '',
    media_url: '',
    link_url: '',
    weight: 100,
    enabled: true,
    creative_type: 'text',
  })
  await refreshData()
}

const createCampaign = async () => {
  if (!campaignForm.name) {
    ElMessage.warning('请填写广告计划名称')
    return
  }
  await automationApi.createCampaign({
    ...campaignForm,
    start_at: campaignForm.start_at || undefined,
    end_at: campaignForm.end_at || undefined,
    scheduled_times: parseScheduledTimes(),
  })
  ElMessage.success('广告计划已创建')
  Object.assign(campaignForm, {
    name: '',
    enabled: false,
    status: 'draft',
    send_mode: 'after_join',
    target_group_levels: ['A'],
    start_at: '',
    end_at: '',
    min_wait_after_join_minutes: 60,
    interval_minutes: 1440,
    max_sends_per_group_per_day: 1,
    max_sends_per_account_per_day: 20,
  })
  scheduledTimesText.value = ''
  await refreshData()
}

const createBinding = async () => {
  if (!bindingForm.account_id || !bindingForm.ad_campaign_id) {
    ElMessage.warning('请选择账号和广告计划')
    return
  }
  await automationApi.createBinding({
    account_id: bindingForm.account_id,
    ad_campaign_id: bindingForm.ad_campaign_id,
    creative_id: bindingForm.creative_id,
    enabled: bindingForm.enabled,
    priority: bindingForm.priority,
  })
  ElMessage.success('账号广告绑定已创建')
  Object.assign(bindingForm, {
    account_id: selectedAccountId.value,
    ad_campaign_id: undefined,
    creative_id: undefined,
    enabled: true,
    priority: 0,
  })
  await refreshData()
}

const statusType = (status: string) => {
  if (status === 'success') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'skipped') return 'warning'
  return 'info'
}

watch(selectedAccountId, async (accountId) => {
  if (!accountId) return
  bindingForm.account_id = accountId
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
      <el-tab-pane label="任务执行" name="runs">
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
            <template #header>自动加群</template>
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
          </el-card>

          <el-card shadow="never">
            <template #header>广告投放</template>
            <el-form label-width="120px">
              <el-form-item label="发送上限">
                <el-input-number v-model="adRunForm.max_deliveries" :min="1" :max="200" />
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
        </div>
      </el-tab-pane>

      <el-tab-pane label="账号配置" name="accounts">
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

          <el-form v-loading="accountConfigLoading" label-width="150px" class="account-config-form">
            <div class="account-config-grid">
              <el-form-item label="启用配置">
                <el-switch v-model="accountConfigForm.enabled" />
              </el-form-item>
              <el-form-item label="自动加群">
                <el-switch v-model="accountConfigForm.auto_join_enabled" />
              </el-form-item>
              <el-form-item label="自动广告">
                <el-switch v-model="accountConfigForm.auto_ads_enabled" />
              </el-form-item>
              <el-form-item label="每日最大加群数">
                <el-input-number v-model="accountConfigForm.max_groups_per_day" :min="0" :max="200" />
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
                <el-input-number v-model="accountConfigForm.max_messages_per_day" :min="0" :max="1000" />
              </el-form-item>
              <el-form-item label="消息发送间隔(秒)">
                <el-input-number v-model="accountConfigForm.message_interval_seconds" :min="1" :max="86400" />
              </el-form-item>
              <el-form-item label="关键词不足自动补充">
                <el-switch v-model="accountConfigForm.keyword_auto_replenish_enabled" />
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
                <el-select v-model="accountConfigForm.risk_level">
                  <el-option v-for="item in riskLevelOptions" :key="item.value" :label="item.label" :value="item.value" />
                </el-select>
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

      <el-tab-pane label="广告配置" name="ads">
        <div class="config-grid">
          <el-card shadow="never">
            <template #header>广告素材</template>
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
              <el-button type="primary" @click="createCreative">
                <el-icon><Plus /></el-icon>
                创建素材
              </el-button>
            </el-form>
          </el-card>

          <el-card shadow="never">
            <template #header>广告计划</template>
            <el-form label-width="120px">
              <el-form-item label="名称"><el-input v-model="campaignForm.name" /></el-form-item>
              <el-form-item label="发送模式">
                <el-select v-model="campaignForm.send_mode">
                  <el-option label="入群后" value="after_join" />
                  <el-option label="间隔" value="interval" />
                  <el-option label="定时" value="scheduled" />
                </el-select>
              </el-form-item>
              <el-form-item label="目标等级">
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
              <el-form-item label="入群等待(分)">
                <el-input-number v-model="campaignForm.min_wait_after_join_minutes" :min="0" />
              </el-form-item>
              <el-form-item label="间隔发送(分)">
                <el-input-number v-model="campaignForm.interval_minutes" :min="1" />
              </el-form-item>
              <el-form-item label="定时时点">
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
              <el-button type="primary" @click="createCampaign">
                <el-icon><Plus /></el-icon>
                创建计划
              </el-button>
            </el-form>
          </el-card>

          <el-card shadow="never">
            <template #header>账号绑定</template>
            <el-form label-width="88px">
              <el-form-item label="账号">
                <el-select v-model="bindingForm.account_id" filterable placeholder="选择账号">
                  <el-option
                    v-for="item in accounts"
                    :key="item.id"
                    :label="accountLabel(item.id)"
                    :value="item.id"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="计划">
                <el-select v-model="bindingForm.ad_campaign_id">
                  <el-option v-for="item in campaigns" :key="item.id" :label="item.name" :value="item.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="素材">
                <el-select v-model="bindingForm.creative_id" clearable>
                  <el-option v-for="item in creatives" :key="item.id" :label="item.name" :value="item.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="启用"><el-switch v-model="bindingForm.enabled" /></el-form-item>
              <el-form-item label="优先级"><el-input-number v-model="bindingForm.priority" /></el-form-item>
              <el-button type="primary" @click="createBinding">
                <el-icon><Plus /></el-icon>
                创建绑定
              </el-button>
            </el-form>

            <el-table :data="bindings" class="binding-table" size="small" max-height="280">
              <el-table-column label="账号" min-width="140">
                <template #default="{ row }">{{ accountLabel(row.account_id) }}</template>
              </el-table-column>
              <el-table-column prop="ad_campaign_id" label="计划" width="80" />
              <el-table-column prop="creative_id" label="素材" width="80" />
              <el-table-column prop="priority" label="优先级" width="90" />
              <el-table-column label="启用" width="80">
                <template #default="{ row }">
                  <el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '是' : '否' }}</el-tag>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="日志" name="logs">
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

        <el-card shadow="never" class="log-card">
          <template #header>最近广告投放</template>
          <el-table :data="deliveryLogs" height="320">
            <el-table-column label="账号" min-width="140">
              <template #default="{ row }">{{ accountLabel(row.account_id) }}</template>
            </el-table-column>
            <el-table-column prop="telegram_group_id" label="群ID" min-width="140" />
            <el-table-column prop="ad_campaign_id" label="计划" width="90" />
            <el-table-column prop="creative_id" label="素材" width="90" />
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="statusType(row.status)">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="error" label="错误" min-width="180" />
            <el-table-column prop="created_at" label="时间" width="180" />
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>
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

.form-helper {
  color: #606266;
  line-height: 1.6;
}

.log-card + .log-card {
  margin-top: 16px;
}

@media (max-width: 1200px) {
  .control-grid,
  .config-grid,
  .account-config-grid {
    grid-template-columns: 1fr;
  }
}
</style>
