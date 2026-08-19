<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElAlert, ElButton, ElIcon, ElMessage, ElMessageBox, ElTag } from 'element-plus'
import { ChatDotRound, CircleCheck, CircleClose, Delete, Edit, Plus, RefreshLeft, UserFilled, View } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import TableCard from '@/components/TableCard.vue'
import SearchBar from '@/components/SearchBar.vue'
import FormDrawer from '@/components/FormDrawer.vue'
import StatusTag from '@/components/StatusTag.vue'
import AccountLoginDialog from '@/components/AccountLoginDialog.vue'
import { useAccountStore } from '@/stores/account'
import { proxiesApi, type Proxy } from '@/api/proxies'
import {
  accountsApi,
  type Account,
  type AccountAssetTier,
  type AccountEnvironmentEvent,
  type AccountRiskEvent,
  type AccountRiskSummary,
  type AccountWarmupStage,
} from '@/api/accounts'
import { useRouter } from 'vue-router'
import { accountAssetTierOptions } from '@/config/accountAssetTiers'

const router = useRouter()
const accountStore = useAccountStore()

const loading = ref(false)
const drawerVisible = ref(false)
const loginDialogVisible = ref(false)
const editingId = ref<number | null>(null)
const proxyOptions = ref<Proxy[]>([])
const securityDrawerVisible = ref(false)
const securityLoading = ref(false)
const selectedSecurityAccount = ref<Account | null>(null)
const riskSummary = ref<AccountRiskSummary | null>(null)
const riskEvents = ref<AccountRiskEvent[]>([])
const environmentEvents = ref<AccountEnvironmentEvent[]>([])

const formData = reactive({
  display_name: '',
  profile_bio: '',
  asset_tier: 'unknown' as AccountAssetTier,
  registered_at: '',
  asset_note: '',
  managed_started_at: '',
  warmup_hold_until: '',
  warmup_note: '',
  country_code: 'US',
  fingerprint_id: '',
  is_active: true,
  proxy_mode: 'dynamic' as 'dynamic' | 'static' | 'none',
  static_proxy_id: undefined as number | undefined,
})

const formRules = {
  display_name: [{ required: true, message: '请输入显示名称', trigger: 'blur' }],
  country_code: [{ required: true, message: '请输入国家代码', trigger: 'blur' }],
  static_proxy_id: [
    {
      validator: (_rule: unknown, value: number | undefined, callback: (error?: Error) => void) => {
        if (formData.proxy_mode === 'static' && !value) {
          callback(new Error('请选择静态代理'))
          return
        }
        callback()
      },
      trigger: 'change',
    },
  ],
}

const searchFilters = [
  {
    type: 'input' as const,
    key: 'search',
    label: '关键词',
    placeholder: '手机号 / 标识 / 显示名称',
    width: '220px',
  },
  {
    type: 'select' as const,
    key: 'status_filter',
    label: '状态',
    placeholder: '全部状态',
    width: '140px',
    options: [
      { label: '全部', value: '' },
      { label: '在线', value: 'online' },
      { label: '离线', value: 'offline' },
      { label: '工作中', value: 'working' },
      { label: '空闲', value: 'idle' },
      { label: '异常', value: 'error' },
      { label: '封禁', value: 'banned' },
    ],
  },
  {
    type: 'select' as const,
    key: 'asset_tier',
    label: '资产',
    placeholder: '全部等级',
    width: '130px',
    options: [
      { label: '全部', value: '' },
      ...accountAssetTierOptions,
    ],
  },
  {
    type: 'input' as const,
    key: 'country_code',
    label: '国家',
    placeholder: '如 US / HK',
    width: '120px',
  },
]

const assetTierOptions = accountAssetTierOptions

const columns = [
  { prop: 'identifier', label: '账号标识', minWidth: '220', slot: 'identifier' },
  { prop: 'asset_tier', label: '资产等级', width: '110', slot: 'assetTier' },
  { prop: 'warmup_stage', label: '托管暖号', width: '130', slot: 'warmupStage' },
  { prop: 'status', label: '状态', width: '110', slot: 'status' },
  { prop: 'country_code', label: '国家/地区', width: '120', slot: 'country' },
  { prop: 'proxy_mode', label: '代理', width: '160', slot: 'proxy' },
  { prop: 'api_config_name', label: 'API配置', minWidth: '120' },
  { prop: 'connection_count', label: '连接数', width: '90' },
  { prop: 'error_count', label: '错误数', width: '90' },
  { prop: 'last_active_at', label: '最近活跃', width: '170', slot: 'lastActive' },
  { prop: 'created_at', label: '创建时间', width: '170', slot: 'createdAt' },
  { prop: 'actions', label: '操作', width: '380', fixed: 'right', slot: 'actions' },
]

const promoterAccounts = computed(() => accountStore.list.filter((item) => item.account_type === 'promoter'))

const formatProxyOption = (proxy: Proxy) => {
  const bound = proxy.bindAccountCount || 0
  return `${proxy.protocol}://${proxy.address}:${proxy.port} (${bound}/3)`
}

const isProxyFullForAccount = (proxy: Proxy) => {
  if (formData.static_proxy_id === proxy.id) {
    return false
  }
  return (proxy.remainingBindSlots ?? Math.max(3 - (proxy.bindAccountCount || 0), 0)) <= 0
}

const assetTierText = (tier?: string) => assetTierOptions.find((item) => item.value === tier)?.label || '未标注'

const assetTierTagType = (tier?: string) => {
  if (tier === 'year_2' || tier === 'year_3_plus') return 'success'
  if (tier === 'year_1' || tier === 'month_3_6') return 'warning'
  if (tier === 'month_1') return 'danger'
  return 'info'
}

const warmupStageText = (stage?: string) => {
  const map: Record<AccountWarmupStage | string, string> = {
    observe: '观察',
    seed: '起步',
    soft: '低频',
    ramp: '提量',
    normal: '正常',
    cooldown: '冷却',
  }
  return map[stage || 'observe'] || '观察'
}

const warmupStageTagType = (stage?: string) => {
  if (stage === 'normal') return 'success'
  if (stage === 'ramp' || stage === 'soft') return 'warning'
  if (stage === 'cooldown') return 'danger'
  return 'info'
}

const fetchData = async (params?: Record<string, any>) => {
  loading.value = true
  try {
    accountStore.setAccountTypeFilter('promoter')
    await accountStore.fetchList({
      account_type: 'promoter',
      ...params,
    })
  } finally {
    loading.value = false
  }
}

const handleSearch = (values: Record<string, any>) => {
  accountStore.setPage(1)
  fetchData(values)
}

const handleReset = () => {
  accountStore.setPage(1)
  fetchData()
}

const handlePageChange = (page: number) => {
  accountStore.setPage(page)
  fetchData()
}

const handlePageSizeChange = (pageSize: number) => {
  accountStore.setPageSize(pageSize)
  fetchData()
}

const openAddDrawer = () => {
  loginDialogVisible.value = true
}

const loadProxyOptions = async () => {
  try {
    const response = await proxiesApi.list({ page: 1, pageSize: 200, status: 'active' })
    proxyOptions.value = response.data.data.list || []
  } catch (error) {
    console.error('Failed to load proxies:', error)
  }
}

const handleLoginSuccess = () => {
  fetchData()
}

const openEditDrawer = (row: Account) => {
  editingId.value = row.id
  Object.assign(formData, {
    display_name: row.display_name || row.identifier,
    profile_bio: row.profile_bio || '',
    asset_tier: row.asset_tier || 'unknown',
    registered_at: row.registered_at ? dayjs(row.registered_at).format('YYYY-MM-DD') : '',
    asset_note: row.asset_note || '',
    managed_started_at: row.managed_started_at ? dayjs(row.managed_started_at).format('YYYY-MM-DD') : '',
    warmup_hold_until: row.warmup_hold_until ? dayjs(row.warmup_hold_until).format('YYYY-MM-DD') : '',
    warmup_note: row.warmup_note || '',
    country_code: row.country_code || 'US',
    fingerprint_id: row.fingerprint_id || '',
    is_active: row.is_active,
    proxy_mode: row.proxy_mode || 'dynamic',
    static_proxy_id: row.static_proxy_id,
  })
  loadProxyOptions()
  drawerVisible.value = true
}

const handleSubmit = async () => {
  if (!editingId.value) return
  try {
    await accountStore.update(editingId.value, {
      display_name: formData.display_name.trim(),
      profile_bio: formData.profile_bio.trim(),
      asset_tier: formData.asset_tier,
      registered_at: formData.registered_at || undefined,
      asset_note: formData.asset_note.trim(),
      managed_started_at: formData.managed_started_at || undefined,
      warmup_hold_until: formData.warmup_hold_until || undefined,
      warmup_note: formData.warmup_note.trim(),
      country_code: formData.country_code.trim().toUpperCase(),
      fingerprint_id: formData.fingerprint_id.trim() || undefined,
      is_active: formData.is_active,
    })
    await accountStore.updateProxyPolicy(editingId.value, {
      proxy_mode: formData.proxy_mode,
      static_proxy_id: formData.proxy_mode === 'static' ? formData.static_proxy_id : undefined,
    })
    ElMessage.success('推广账号已更新')
    drawerVisible.value = false
  } catch (error) {
    console.error('Failed to save account:', error)
  }
}

const riskStatusType = (status?: string) => {
  if (status === 'block' || status === 'failure' || status === 'freeze' || status === 'blocked') return 'danger'
  if (status === 'warning') return 'warning'
  if (status === 'success' || status === 'allow' || status === 'ok') return 'success'
  return 'info'
}

const riskLevelType = (summary?: AccountRiskSummary | null) => {
  if (!summary) return 'info'
  if (summary.risk_level === 'quarantined' || summary.risk_level === 'frozen' || summary.risk_pause_until || summary.risk_score >= 70) return 'danger'
  if (summary.risk_level === 'limited' || summary.risk_level === 'watch' || summary.risk_score >= 30 || summary.blocked_count > 0 || summary.failure_count > 0) return 'warning'
  return 'success'
}

const riskLevelText = (level?: string) => {
  const map: Record<string, string> = {
    normal: '正常',
    watch: '观察',
    limited: '限流',
    frozen: '冻结',
    quarantined: '隔离',
  }
  return level ? map[level] || level : '正常'
}

const formatDetails = (details?: string) => {
  if (!details) return '-'
  try {
    const parsed = JSON.parse(details)
    return Object.entries(parsed)
      .map(([key, value]) => `${key}: ${String(value)}`)
      .join('，')
  } catch {
    return details
  }
}

const openSecurityDrawer = async (row: Account) => {
  selectedSecurityAccount.value = row
  securityDrawerVisible.value = true
  securityLoading.value = true
  try {
    const [summary, events] = await Promise.all([
      accountsApi.getRiskSummary(row.id),
      accountsApi.getRiskEvents(row.id, { limit: 30 }),
    ])
    riskSummary.value = summary
    riskEvents.value = events.risk_events || []
    environmentEvents.value = events.environment_events || []
  } finally {
    securityLoading.value = false
  }
}

const refreshSecurity = async () => {
  if (!selectedSecurityAccount.value) return
  await openSecurityDrawer(selectedSecurityAccount.value)
}

const handleClearRiskPause = async () => {
  if (!selectedSecurityAccount.value) return
  securityLoading.value = true
  try {
    riskSummary.value = await accountsApi.manualAdjustRisk(selectedSecurityAccount.value.id, {
      clear_pause: true,
      target_level: 'watch',
      reason: 'manual_unfreeze',
    })
    await refreshSecurity()
    ElMessage.success('已解除冻结，账号进入观察恢复期')
  } finally {
    securityLoading.value = false
  }
}

const handleLowerRiskScore = async () => {
  if (!selectedSecurityAccount.value) return
  securityLoading.value = true
  try {
    riskSummary.value = await accountsApi.manualAdjustRisk(selectedSecurityAccount.value.id, {
      score_delta: -20,
      reason: 'manual_lower_score',
    })
    await refreshSecurity()
    ElMessage.success('已降低风险分')
  } finally {
    securityLoading.value = false
  }
}
const handleManualBan = async (row: Account) => {
  if (row.status === 'banned') return
  try {
    const { value } = await ElMessageBox.prompt(
      `确定要手动封禁推广账号 ${row.display_name || row.identifier} 吗？封禁后账号会停用，并进入群资源接管流程。`,
      '手动封禁账号',
      {
        confirmButtonText: '确认封禁',
        cancelButtonText: '取消',
        inputValue: 'manual_ban',
        inputPlaceholder: '请输入封禁原因',
        inputValidator: (value: string) => (value.trim() ? true : '请输入封禁原因'),
        type: 'warning',
      },
    )
    await accountsApi.manualBan(row.id, value.trim())
    await fetchData()
    if (selectedSecurityAccount.value?.id === row.id) {
      await refreshSecurity()
    }
    ElMessage.success('账号已手动封禁')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      console.error('Failed to manually ban account:', error)
    }
  }
}
const handleDelete = async (row: Account) => {
  try {
    await ElMessageBox.confirm(`确定要删除推广账号 ${row.display_name || row.identifier} 吗？`, '提示', {
      type: 'warning',
    })
    await accountStore.remove(row.id)
    ElMessage.success('删除成功')
  } catch {
    // cancelled
  }
}

const handleEnable = async (row: Account) => {
  try {
    await accountStore.enable(row.id)
    ElMessage.success('账号已连接')
  } catch (error) {
    console.error('Failed to connect account:', error)
  }
}

const handleDisable = async (row: Account) => {
  try {
    await accountStore.disable(row.id)
    ElMessage.success('账号已断开')
  } catch (error) {
    console.error('Failed to disconnect account:', error)
  }
}

const handleSyncProfileBio = async (row: Account) => {
  try {
    const updated = await accountStore.syncProfileBio(row.id)
    ElMessage.success(updated.profile_bio ? '简介已同步到Telegram' : 'Telegram简介已清空')
  } catch (error) {
    console.error('Failed to sync profile bio:', error)
  }
}

const formatDate = (date?: string) => (date ? dayjs(date).format('YYYY-MM-DD HH:mm') : '-')

const goToGuardianBots = () => {
  router.push('/guardian/bots')
}

onMounted(() => {
  fetchData()
  loadProxyOptions()
})
</script>

<template>
  <div class="accounts-page">
    <div class="page-header">
      <div>
        <h2 class="page-title">推广账号</h2>
        <p class="page-desc">这里只管理用于搜群、加群、广告投放和私聊引导的推广账号。</p>
      </div>
      <div class="header-actions">
        <el-button @click="goToGuardianBots">
          <el-icon><ChatDotRound /></el-icon>
          查看Bot账号
        </el-button>
        <el-button type="primary" @click="openAddDrawer">
          <el-icon><Plus /></el-icon>
          添加推广账号
        </el-button>
      </div>
    </div>

    <el-alert
      title="群治理 Bot 已独立到“群治理中心”，不会再和推广账号混用。"
      type="info"
      :closable="false"
      show-icon
      class="page-alert"
    />

    <SearchBar
      :filters="searchFilters"
      :loading="loading"
      @search="handleSearch"
      @reset="handleReset"
    />

    <TableCard
      :columns="columns"
      :data="promoterAccounts"
      :total="accountStore.total"
      :loading="loading"
      :page="accountStore.page"
      :page-size="accountStore.pageSize"
      row-key="id"
      @page-change="handlePageChange"
      @page-size-change="handlePageSizeChange"
    >
      <template #identifier="{ row }">
        <div class="identifier-cell">
          <div class="primary-line">
            <el-icon><UserFilled /></el-icon>
            <span>{{ row.display_name || row.identifier }}</span>
          </div>
          <div class="secondary-line">
            <span>{{ row.phone || row.identifier }}</span>
            <span>{{ row.session_name }}</span>
          </div>
          <div v-if="row.profile_bio" class="bio-line">
            {{ row.profile_bio }}
          </div>
        </div>
      </template>

      <template #assetTier="{ row }">
        <div class="asset-tier-cell">
          <el-tag :type="assetTierTagType(row.asset_tier)" effect="plain">
            {{ assetTierText(row.asset_tier) }}
          </el-tag>
          <span v-if="row.registered_at" class="asset-date">{{ dayjs(row.registered_at).format('YYYY-MM-DD') }}</span>
        </div>
      </template>

      <template #warmupStage="{ row }">
        <div class="asset-tier-cell">
          <el-tag :type="warmupStageTagType(row.warmup_stage)" effect="plain">
            {{ warmupStageText(row.warmup_stage) }}
          </el-tag>
          <span v-if="row.managed_started_at" class="asset-date">{{ dayjs(row.managed_started_at).format('YYYY-MM-DD') }}</span>
        </div>
      </template>

      <template #status="{ row }">
        <div class="status-cell">
          <StatusTag :status="row.status" type="account" />
          <el-tag :type="row.is_active ? 'success' : 'info'" effect="plain">
            {{ row.is_active ? '已启用' : '已停用' }}
          </el-tag>
        </div>
      </template>

      <template #country="{ row }">
        <span>{{ row.country_code }}{{ row.country_name ? ` / ${row.country_name}` : '' }}</span>
      </template>

      <template #proxy="{ row }">
        <el-tag v-if="row.proxy_mode === 'static'" type="warning" effect="plain">
          {{ row.static_proxy_address || `静态 #${row.static_proxy_id}` }}
        </el-tag>
        <el-tag v-else-if="row.proxy_mode === 'none'" type="info" effect="plain">
          无代理
        </el-tag>
        <el-tag v-else type="success" effect="plain">
          动态住宅
        </el-tag>
      </template>

      <template #lastActive="{ row }">
        {{ formatDate(row.last_active_at || row.last_connected_at) }}
      </template>

      <template #createdAt="{ row }">
        {{ formatDate(row.created_at) }}
      </template>

      <template #actions="{ row }">
        <el-button type="primary" link size="small" @click="openEditDrawer(row)">
          <el-icon><Edit /></el-icon>
          编辑
        </el-button>
        <el-button
          v-if="row.is_active"
          type="warning"
          link
          size="small"
          @click="handleDisable(row)"
        >
          <el-icon><CircleClose /></el-icon>
          断开
        </el-button>
        <el-button
          v-else
          type="success"
          link
          size="small"
          @click="handleEnable(row)"
        >
          <el-icon><CircleCheck /></el-icon>
          连接
        </el-button>
        <el-button type="info" link size="small" @click="openSecurityDrawer(row)">
          <el-icon><View /></el-icon>
          安全
        </el-button>
        <el-button type="primary" link size="small" @click="handleSyncProfileBio(row)">
          <el-icon><RefreshLeft /></el-icon>
          同步简介
        </el-button>
        <el-button
          v-if="row.status !== 'banned'"
          type="danger"
          link
          size="small"
          @click="handleManualBan(row)"
        >
          <el-icon><CircleClose /></el-icon>
          手动封禁
        </el-button>
        <el-button type="danger" link size="small" @click="handleDelete(row)">
          <el-icon><Delete /></el-icon>
          删除
        </el-button>
      </template>
    </TableCard>

    <AccountLoginDialog
      v-model:visible="loginDialogVisible"
      @success="handleLoginSuccess"
    />


    <el-drawer
      v-model="securityDrawerVisible"
      :title="`账号安全 - ${selectedSecurityAccount?.display_name || selectedSecurityAccount?.identifier || ''}`"
      size="720px"
      class="security-drawer"
    >
      <div v-loading="securityLoading" class="security-panel">
        <div class="security-summary">
          <div class="summary-item">
            <span class="summary-label">风险等级</span>
            <el-tag :type="riskLevelType(riskSummary)" effect="plain">
              {{ riskLevelText(riskSummary?.risk_level) }}
            </el-tag>
          </div>
          <div class="summary-item">
            <span class="summary-label">资产等级</span>
            <el-tag :type="assetTierTagType(riskSummary?.asset_tier || selectedSecurityAccount?.asset_tier)" effect="plain">
              {{ assetTierText(riskSummary?.asset_tier || selectedSecurityAccount?.asset_tier) }}
            </el-tag>
          </div>
          <div class="summary-item">
            <span class="summary-label">风险分</span>
            <el-tag :type="riskLevelType(riskSummary)" effect="plain">
              {{ riskSummary?.risk_score ?? 0 }}
            </el-tag>
          </div>
          <div class="summary-item">
            <span class="summary-label">阻断</span>
            <strong>{{ riskSummary?.blocked_count ?? 0 }}</strong>
          </div>
          <div class="summary-item">
            <span class="summary-label">失败</span>
            <strong>{{ riskSummary?.failure_count ?? 0 }}</strong>
          </div>
          <div class="summary-item wide">
            <span class="summary-label">暂停至</span>
            <span>{{ formatDate(riskSummary?.risk_pause_until) }}</span>
          </div>
          <div class="summary-item wide">
            <span class="summary-label">恢复期至</span>
            <span>{{ formatDate(riskSummary?.risk_recovery_until) }}</span>
          </div>
          <div class="summary-item wide">
            <span class="summary-label">注册时间</span>
            <span>{{ formatDate(riskSummary?.registered_at || selectedSecurityAccount?.registered_at) }}</span>
          </div>
          <div class="summary-item">
            <span class="summary-label">暖号阶段</span>
            <el-tag :type="warmupStageTagType(riskSummary?.warmup_stage || selectedSecurityAccount?.warmup_stage)" effect="plain">
              {{ warmupStageText(riskSummary?.warmup_stage || selectedSecurityAccount?.warmup_stage) }}
            </el-tag>
          </div>
          <div class="summary-item wide">
            <span class="summary-label">托管起点</span>
            <span>{{ formatDate(riskSummary?.managed_started_at || selectedSecurityAccount?.managed_started_at) }}</span>
          </div>
          <div class="summary-item wide">
            <span class="summary-label">暖号延长至</span>
            <span>{{ formatDate(riskSummary?.warmup_hold_until || selectedSecurityAccount?.warmup_hold_until) }}</span>
          </div>
        </div>

        <div class="security-actions">
          <el-button type="warning" plain :disabled="!riskSummary?.risk_pause_until" @click="handleClearRiskPause">
            <el-icon><RefreshLeft /></el-icon>
            解除冻结
          </el-button>
          <el-button type="primary" plain :disabled="(riskSummary?.risk_score ?? 0) <= 0" @click="handleLowerRiskScore">
            <el-icon><RefreshLeft /></el-icon>
            降低风险分
          </el-button>
        </div>

        <el-descriptions :column="1" size="small" border class="security-descriptions">
          <el-descriptions-item label="风险原因">{{ riskSummary?.risk_reason || '-' }}</el-descriptions-item>
          <el-descriptions-item label="最近衰减">{{ formatDate(riskSummary?.last_risk_decay_at) }}</el-descriptions-item>
          <el-descriptions-item label="指纹ID">{{ riskSummary?.fingerprint_id || '-' }}</el-descriptions-item>
          <el-descriptions-item label="设备模型">{{ riskSummary?.device_model || '-' }}</el-descriptions-item>
          <el-descriptions-item label="系统版本">{{ riskSummary?.system_version || '-' }}</el-descriptions-item>
          <el-descriptions-item label="应用版本">{{ riskSummary?.app_version || '-' }}</el-descriptions-item>
        </el-descriptions>

        <h3 class="security-title">今日动作使用量</h3>
        <el-table :data="riskSummary?.today_usage || []" size="small" max-height="220" empty-text="暂无今日动作">
          <el-table-column prop="action" label="动作" width="130" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="riskStatusType(row.status)" effect="plain">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="target_type" label="目标" width="90" />
          <el-table-column prop="count" label="次数" width="80" />
          <el-table-column prop="last_reason" label="最近原因" min-width="150" show-overflow-tooltip />
        </el-table>

        <h3 class="security-title">风险事件</h3>
        <el-table :data="riskEvents" size="small" max-height="260" empty-text="暂无风险事件">
          <el-table-column prop="created_at" label="时间" width="150">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="action" label="动作" width="120" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="riskStatusType(row.status)" effect="plain">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="原因" min-width="150" show-overflow-tooltip />
          <el-table-column label="目标" width="130" show-overflow-tooltip>
            <template #default="{ row }">{{ row.target_id || '-' }}</template>
          </el-table-column>
        </el-table>

        <h3 class="security-title">环境事件</h3>
        <el-table :data="environmentEvents" size="small" max-height="260" empty-text="暂无环境事件">
          <el-table-column prop="created_at" label="时间" width="150">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="event_type" label="类型" width="110" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="riskStatusType(row.status)" effect="plain">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="proxy_country" label="代理国家" width="90" />
          <el-table-column prop="fingerprint_id" label="指纹" min-width="150" show-overflow-tooltip />
          <el-table-column label="详情" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ formatDetails(row.details) }}</template>
          </el-table-column>
        </el-table>
      </div>
    </el-drawer>
    <FormDrawer
      v-model:visible="drawerVisible"
      title="编辑推广账号"
      :fields="[
        { prop: 'display_name', label: '显示名称', type: 'input', placeholder: '便于运营识别的名称' },
        {
          prop: 'profile_bio',
          label: '账号简介',
          type: 'textarea',
          placeholder: '用户点开账号资料时看到的简介，最多70字',
          props: { maxlength: 70, showWordLimit: true, rows: 3 },
        },
        {
          prop: 'registered_at',
          label: '注册时间',
          type: 'date',
          placeholder: '可选，用于替代导入时间判断号龄',
          props: { clearable: true },
        },
        {
          prop: 'asset_tier',
          label: '资产等级',
          type: 'select',
          options: assetTierOptions,
          props: { clearable: false },
        },
        {
          prop: 'asset_note',
          label: '资产备注',
          type: 'input',
          placeholder: '采购批次 / 来源 / 备注',
          props: { maxlength: 255, showWordLimit: true },
        },
        {
          prop: 'managed_started_at',
          label: '托管起点',
          type: 'date',
          placeholder: '为空则按创建时间/首次托管时间',
          props: { clearable: true },
        },
        {
          prop: 'warmup_hold_until',
          label: '暖号延长至',
          type: 'date',
          placeholder: '可选，用于人工延长提量期',
          props: { clearable: true },
        },
        {
          prop: 'warmup_note',
          label: '暖号备注',
          type: 'input',
          placeholder: '风控观察 / 采购批次 / 人工说明',
          props: { maxlength: 255, showWordLimit: true },
        },
        { prop: 'country_code', label: '国家代码', type: 'input', placeholder: '如 US / SG / HK' },
        { prop: 'fingerprint_id', label: '指纹ID', type: 'input', placeholder: '可选' },
        {
          prop: 'proxy_mode',
          label: '代理模式',
          type: 'select',
          options: [
            { label: '动态住宅代理', value: 'dynamic' },
            { label: '静态绑定代理', value: 'static' },
            { label: '不使用代理', value: 'none' },
          ],
        },
        {
          prop: 'static_proxy_id',
          label: '静态代理',
          type: 'select',
          placeholder: '静态模式必选；其他模式会忽略',
          options: proxyOptions.map((proxy) => ({
            label: formatProxyOption(proxy),
            value: proxy.id,
            disabled: isProxyFullForAccount(proxy),
          })),
          props: { filterable: true, clearable: true },
        },
        { prop: 'is_active', label: '启用', type: 'switch' },
      ]"
      :model-value="formData"
      :rules="formRules"
      width="520px"
      @confirm="handleSubmit"
    />
  </div>
</template>

<style scoped lang="scss">
.accounts-page {
  padding: 0;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 16px;
}

.page-title {
  margin: 0;
  color: #303133;
  font-size: 20px;
  font-weight: 600;
}

.page-desc {
  margin: 6px 0 0;
  color: #606266;
}

.page-alert {
  margin-bottom: 16px;
}

.header-actions {
  display: flex;
  gap: 12px;
}

.identifier-cell,
.status-cell,
.asset-tier-cell {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.asset-tier-cell {
  align-items: flex-start;
}

.asset-date {
  color: #909399;
  font-size: 12px;
}

.primary-line,
.secondary-line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.primary-line {
  font-weight: 600;
  color: #303133;
}

.secondary-line {
  color: #909399;
  font-size: 12px;
  flex-wrap: wrap;
}

.bio-line {
  color: #606266;
  font-size: 12px;
  line-height: 1.4;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.security-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.security-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.summary-item {
  display: flex;
  min-height: 54px;
  flex-direction: column;
  justify-content: center;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  background: #fff;
}

.summary-item.wide {
  grid-column: span 1;
}

.summary-label {
  color: #909399;
  font-size: 12px;
}

.security-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.security-title {
  margin: 4px 0 0;
  color: #303133;
  font-size: 15px;
  font-weight: 600;
}

.security-descriptions {
  margin-top: 0;
}

@media (max-width: 760px) {
  .security-summary {
    grid-template-columns: 1fr;
  }
}
</style>
