<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElAlert, ElButton, ElIcon, ElMessage, ElMessageBox, ElTag } from 'element-plus'
import { ChatDotRound, CircleCheck, CircleClose, Delete, Edit, MagicStick, Plus, RefreshLeft, UserFilled, View } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import TableCard from '@/components/TableCard.vue'
import SearchBar from '@/components/SearchBar.vue'
import FormDrawer from '@/components/FormDrawer.vue'
import StatusTag from '@/components/StatusTag.vue'
import AccountLoginDialog from '@/components/AccountLoginDialog.vue'
import AccountOperationalStatusPanel from '@/components/AccountOperationalStatusPanel.vue'
import AccountDeliveryBlockDrawer from '@/components/AccountDeliveryBlockDrawer.vue'
import AccountPersonaDrawer from '@/components/accounts/AccountPersonaDrawer.vue'
import AccountProfileUpdateDialog from '@/components/accounts/AccountProfileUpdateDialog.vue'
import ClientListPagination from '@/components/ClientListPagination.vue'
import { useAccountStore } from '@/stores/account'
import { useAuthStore } from '@/stores/auth'
import { MAX_STATIC_PROXY_BINDINGS, proxiesApi, type Proxy } from '@/api/proxies'
import {
  accountsApi,
  type Account,
  type AccountAssetTier,
  type AccountEnvironmentEvent,
  type AccountListParams,
  type AccountRiskEvent,
  type AccountRiskSummary,
  type AccountWarmupStage,
} from '@/api/accounts'
import {
  accountSpamChecksApi,
  createSpamCheckIdempotencyKey,
  type SpamCheckOperation,
  type SpamCheckOperationItem,
  type SpamCheckOperationStatus,
} from '@/api/accountSpamChecks'
import type { AccountPersonaDetail } from '@/api/accountPersonas'
import { automationApi, type AdDynamicStatus } from '@/api/automation'
import { useRoute, useRouter } from 'vue-router'
import { accountAssetTierOptions } from '@/config/accountAssetTiers'
import { useClientPagination } from '@/utils/clientPagination'
import { parseSafePositiveId } from '@/utils/groupOpsAccess'

const route = useRoute()
const router = useRouter()
const accountStore = useAccountStore()
const authStore = useAuthStore()
const returnAssetId = computed(() => parseSafePositiveId(route.query.assetId))

const loading = ref(false)
const activeAccountTab = ref(route.query.tab === 'operations' ? 'operations' : 'list')
const operationalStatusLoading = ref(false)
const dynamicStatuses = ref<AdDynamicStatus[]>([])
const deliveryBlockDrawerVisible = ref(false)
const selectedDeliveryAccount = ref<Account | null>(null)
const selectedDeliveryStatus = ref<AdDynamicStatus | null>(null)
const personaDrawerVisible = ref(false)
const selectedPersonaAccount = ref<Account | null>(null)
const accountListLoaded = ref(false)
const focusedAccountId = ref<number | null>(null)
const focusedAccount = ref<Account | null>(null)
const focusedAccountError = ref('')
let accountLocationSequence = 0
let accountLocationController: AbortController | null = null
let personaRouteSequence = 0
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
const todayUsageRows = computed(() => riskSummary.value?.today_usage || [])
const isAdmin = computed(() => authStore.userInfo?.role === 'admin')
const todayUsagePagination = useClientPagination(todayUsageRows)
const riskEventPagination = useClientPagination(riskEvents)
const environmentEventPagination = useClientPagination(environmentEvents)
const SPAM_CHECK_MAX_ACCOUNTS = 2000
const SPAM_CHECK_PAGE_SIZE = 2000
const SPAM_CHECK_POLL_INTERVAL_MS = 3000
const spamCheckEligibleStatuses = new Set<Account['status']>(['online', 'idle', 'offline', 'restricted'])
const spamCheckActiveStatuses = new Set<SpamCheckOperationStatus>(['queued', 'running', 'cancelling'])
const spamCheckDialogVisible = ref(false)
const profileUpdateDialogVisible = ref(false)
const spamCheckLoadingAccounts = ref(false)
const spamCheckOperationLoading = ref(false)
const spamCheckSubmitting = ref(false)
const spamCheckCancelling = ref(false)
const spamCheckAccounts = ref<Account[]>([])
const spamCheckSelectedIds = ref<number[]>([])
const spamCheckSearch = ref('')
const spamCheckLoadError = ref('')
const spamCheckOperationError = ref('')
const spamCheckOperation = ref<SpamCheckOperation | null>(null)
let spamCheckPollTimer: number | null = null
let spamCheckPollGeneration = 0
let pendingSpamCheckFingerprint = ''
let pendingSpamCheckIdempotencyKey = ''

const isSpamCheckOperationActive = (status?: SpamCheckOperationStatus) =>
  Boolean(status && spamCheckActiveStatuses.has(status))

const spamCheckOperationActive = computed(() =>
  isSpamCheckOperationActive(spamCheckOperation.value?.status),
)

const spamCheckEligibilityReason = (account: Account) => {
  if (account.account_type !== 'promoter') return '不是推广账号'
  if (!account.is_active) return '账号未启用'
  if (!account.session_name?.trim()) return '缺少登录会话'
  if (!spamCheckEligibleStatuses.has(account.status)) return '当前连接状态不可检测'
  if (account.spam_check_status === 'queued' || account.spam_check_status === 'checking') {
    return '检测任务处理中'
  }
  return ''
}

const spamCheckEligibleAccounts = computed(() =>
  spamCheckAccounts.value.filter((account) => !spamCheckEligibilityReason(account)),
)

const spamCheckFilteredAccounts = computed(() => {
  const query = spamCheckSearch.value.trim().toLowerCase()
  if (!query) return spamCheckAccounts.value
  return spamCheckAccounts.value.filter((account) =>
    [
      String(account.id),
      account.display_name,
      account.identifier,
      account.phone,
      account.session_name,
    ].some((value) => value?.toLowerCase().includes(query)),
  )
})

const spamCheckSelectedCount = computed(() => spamCheckSelectedIds.value.length)
const spamCheckCanSubmit = computed(() =>
  spamCheckSelectedCount.value >= 1
  && spamCheckSelectedCount.value <= SPAM_CHECK_MAX_ACCOUNTS
  && !spamCheckLoadingAccounts.value
  && !spamCheckSubmitting.value
  && !spamCheckOperationActive.value,
)
const spamCheckAccountMap = computed(
  () => new Map(spamCheckAccounts.value.map((account) => [account.id, account])),
)
const spamCheckRecentItems = computed(() =>
  [...(spamCheckOperation.value?.items || [])].slice(-100).reverse(),
)
const spamCheckProgressPercent = computed(() => {
  const operation = spamCheckOperation.value
  if (!operation) return 0
  if (operation.status === 'succeeded') return 100
  if (!operation.total_accounts) return 0
  return Math.min(100, Math.round((operation.processed_accounts / operation.total_accounts) * 100))
})
const spamCheckRemainingAccounts = computed(() => {
  const operation = spamCheckOperation.value
  if (!operation) return 0
  return Math.max(operation.total_accounts - operation.processed_accounts, 0)
})
const spamCheckProgressStatus = computed<'success' | 'warning' | 'exception' | undefined>(() => {
  const status = spamCheckOperation.value?.status
  if (status === 'succeeded') return 'success'
  if (status === 'partial_failed') return 'warning'
  if (status === 'failed') return 'exception'
  return undefined
})

watch(spamCheckSelectedIds, (ids) => {
  if (ids.length > SPAM_CHECK_MAX_ACCOUNTS) {
    spamCheckSelectedIds.value = ids.slice(0, SPAM_CHECK_MAX_ACCOUNTS)
    ElMessage.warning('一次最多选择 2000 个账号')
    return
  }
  const fingerprint = ids.join(',')
  if (pendingSpamCheckFingerprint && fingerprint !== pendingSpamCheckFingerprint) {
    pendingSpamCheckFingerprint = ''
    pendingSpamCheckIdempotencyKey = ''
  }
})

const spamCheckAccountStatusText = (status: Account['status']) => {
  const labels: Record<Account['status'], string> = {
    offline: '离线',
    online: '在线',
    working: '工作中',
    idle: '空闲',
    restricted: '受限',
    error: '异常',
    banned: '封禁',
  }
  return labels[status] || status
}

const accountRestrictionSourceText = (account: Account) => {
  const source = (account.restriction_source || '').toLowerCase()
  if (source.includes('spam')) return 'SpamBot 限制'
  if (source.includes('rpc') || source.includes('telegram')) return 'Telegram 操作限制'
  return account.restriction_source || 'Telegram 操作限制'
}

const spamCheckResultText = (status: Account['spam_check_status']) => {
  const labels: Record<Account['spam_check_status'], string> = {
    unknown: '未检测',
    queued: '排队中',
    checking: '检测中',
    clear: 'SpamBot 未检出限制',
    restricted: '临时受限（非封号）',
    flagged: '号码被风控关注（未封号）',
    error: '检测失败',
  }
  return labels[status] || status
}

const spamCheckResultType = (status: Account['spam_check_status']) => {
  if (status === 'clear') return 'success'
  if (status === 'restricted' || status === 'error') return 'danger'
  if (status === 'flagged' || status === 'queued' || status === 'checking') return 'warning'
  return 'info'
}

const spamCheckOperationStatusText = (status: SpamCheckOperationStatus) => {
  const labels: Record<SpamCheckOperationStatus, string> = {
    queued: '排队中',
    running: '执行中',
    cancelling: '取消中',
    succeeded: '已完成',
    partial_failed: '部分失败',
    failed: '失败',
    cancelled: '已取消',
  }
  return labels[status] || status
}

const spamCheckOperationStatusType = (status: SpamCheckOperationStatus) => {
  if (status === 'succeeded') return 'success'
  if (status === 'partial_failed' || status === 'cancelling') return 'warning'
  if (status === 'failed' || status === 'cancelled') return 'danger'
  return 'info'
}

const spamCheckItemStatusText = (
  item: { result?: SpamCheckOperationItem['result']; status?: SpamCheckOperationItem['status'] },
) => {
  if (item.result === 'clear') return 'SpamBot 未检出限制'
  if (item.result === 'restricted') return '受限'
  const labels: Partial<Record<SpamCheckOperationItem['status'], string>> = {
    pending: '等待中',
    in_progress: '检测中',
    retry_wait: '等待重试',
    succeeded: '已完成',
    failed: '失败',
    cancelled: '已取消',
  }
  return (item.status && labels[item.status]) || item.status || '未知'
}

const spamCheckItemStatusType = (
  item: { result?: SpamCheckOperationItem['result']; status?: SpamCheckOperationItem['status'] },
) => {
  if (item.result === 'clear') return 'success'
  if (item.result === 'restricted' || item.status === 'failed' || item.status === 'cancelled') return 'danger'
  if (item.status === 'retry_wait') return 'warning'
  return 'info'
}

const spamCheckAccountLabel = (accountId: number | null) => {
  if (accountId === null) return '账号已删除'
  const account = spamCheckAccountMap.value.get(accountId)
  return account?.display_name || account?.identifier || ('账号 #' + accountId)
}

const spamCheckErrorText = (error: unknown, fallback: string) => {
  const responseData = (error as {
    response?: { data?: { detail?: string | { message?: string }; message?: string } }
  })?.response?.data
  if (typeof responseData?.detail === 'string') return responseData.detail
  if (responseData?.detail && typeof responseData.detail === 'object' && responseData.detail.message) {
    return responseData.detail.message
  }
  if (responseData?.message) return responseData.message
  return error instanceof Error && error.message ? error.message : fallback
}

const stopSpamCheckPolling = () => {
  spamCheckPollGeneration += 1
  if (spamCheckPollTimer !== null) window.clearTimeout(spamCheckPollTimer)
  spamCheckPollTimer = null
}

const loadSpamCheckAccounts = async () => {
  spamCheckLoadingAccounts.value = true
  spamCheckLoadError.value = ''
  try {
    const collected = new Map<number, Account>()
    const seenCursors = new Set<string>()
    let cursor: string | undefined
    while (true) {
      const response = await accountsApi.list({
        account_type: 'promoter',
        limit: SPAM_CHECK_PAGE_SIZE,
        ...(cursor ? { cursor } : {}),
      })
      response.list.forEach((account) => {
        if (account.account_type === 'promoter') collected.set(account.id, account)
      })
      const nextCursor = response.nextCursor || undefined
      if (!response.hasMore || !nextCursor) break
      if (seenCursors.has(nextCursor)) throw new Error('账号分页游标重复，已停止加载')
      seenCursors.add(nextCursor)
      cursor = nextCursor
    }
    spamCheckAccounts.value = Array.from(collected.values())
    const eligibleIds = new Set(spamCheckEligibleAccounts.value.map((account) => account.id))
    spamCheckSelectedIds.value = spamCheckSelectedIds.value.filter((id) => eligibleIds.has(id))
  } catch (error) {
    spamCheckLoadError.value = spamCheckErrorText(error, '推广账号加载失败')
  } finally {
    spamCheckLoadingAccounts.value = false
  }
}

const refreshSpamCheckAccountSnapshots = async () => {
  await Promise.allSettled([
    fetchData(),
    loadSpamCheckAccounts(),
  ])
}

const scheduleSpamCheckPoll = (operationId: number, generation: number) => {
  spamCheckPollTimer = window.setTimeout(async () => {
    try {
      const refreshed = await accountSpamChecksApi.getOperation(operationId)
      if (generation !== spamCheckPollGeneration) return
      spamCheckOperation.value = refreshed
      spamCheckOperationError.value = ''
    } catch (error) {
      if (generation !== spamCheckPollGeneration) return
      spamCheckOperationError.value = spamCheckErrorText(error, 'SpamBot 检测进度刷新失败')
    }
    if (
      generation === spamCheckPollGeneration
      && spamCheckOperation.value?.id === operationId
      && isSpamCheckOperationActive(spamCheckOperation.value.status)
    ) {
      scheduleSpamCheckPoll(operationId, generation)
    } else if (generation === spamCheckPollGeneration) {
      spamCheckPollTimer = null
      await refreshSpamCheckAccountSnapshots()
    }
  }, SPAM_CHECK_POLL_INTERVAL_MS)
}

const startSpamCheckPolling = (operationId: number) => {
  stopSpamCheckPolling()
  const generation = spamCheckPollGeneration
  scheduleSpamCheckPoll(operationId, generation)
}

const loadLatestSpamCheckOperation = async () => {
  spamCheckOperationLoading.value = true
  spamCheckOperationError.value = ''
  try {
    const latest = await accountSpamChecksApi.getLatestOperation()
    spamCheckOperation.value = latest
    if (latest && isSpamCheckOperationActive(latest.status)) startSpamCheckPolling(latest.id)
    else stopSpamCheckPolling()
  } catch (error) {
    const status = (error as { response?: { status?: number } })?.response?.status
    if (status === 404) {
      spamCheckOperation.value = null
      stopSpamCheckPolling()
    } else {
      spamCheckOperationError.value = spamCheckErrorText(error, '最近 SpamBot 检测任务加载失败')
    }
  } finally {
    spamCheckOperationLoading.value = false
  }
}

const openSpamCheckDialog = async () => {
  spamCheckDialogVisible.value = true
  await Promise.allSettled([
    loadSpamCheckAccounts(),
    loadLatestSpamCheckOperation(),
  ])
}

const refreshSpamCheckOperation = async () => {
  const operationId = spamCheckOperation.value?.id
  if (!operationId) {
    await loadLatestSpamCheckOperation()
    return
  }
  stopSpamCheckPolling()
  spamCheckOperationLoading.value = true
  spamCheckOperationError.value = ''
  try {
    const refreshed = await accountSpamChecksApi.getOperation(operationId)
    spamCheckOperation.value = refreshed
    if (isSpamCheckOperationActive(refreshed.status)) startSpamCheckPolling(refreshed.id)
    else await refreshSpamCheckAccountSnapshots()
  } catch (error) {
    spamCheckOperationError.value = spamCheckErrorText(error, 'SpamBot 检测进度刷新失败')
  } finally {
    spamCheckOperationLoading.value = false
  }
}

const selectAllSpamCheckAccounts = () => {
  const eligible = spamCheckEligibleAccounts.value
  spamCheckSelectedIds.value = eligible
    .slice(0, SPAM_CHECK_MAX_ACCOUNTS)
    .map((account) => account.id)
  if (eligible.length > SPAM_CHECK_MAX_ACCOUNTS) {
    ElMessage.warning('可检测账号超过 2000 个，本次已选择前 2000 个')
  }
}

const clearSpamCheckAccounts = () => {
  spamCheckSelectedIds.value = []
}

const submitSpamCheckOperation = async () => {
  const accountIds = [...spamCheckSelectedIds.value]
  if (!accountIds.length) {
    ElMessage.warning('请至少选择 1 个可检测账号')
    return
  }
  if (accountIds.length > SPAM_CHECK_MAX_ACCOUNTS) {
    ElMessage.warning('一次最多选择 2000 个账号')
    return
  }
  const eligibleIds = new Set(spamCheckEligibleAccounts.value.map((account) => account.id))
  if (accountIds.some((id) => !eligibleIds.has(id))) {
    ElMessage.warning('选择中包含当前不可检测账号，请刷新账号后重新选择')
    return
  }
  if (spamCheckOperationActive.value) {
    ElMessage.warning('已有 SpamBot 检测任务正在执行')
    return
  }

  try {
    await ElMessageBox.confirm(
      '将使用 ' + accountIds.length + ' 个真实 Telegram 账号向 @SpamBot 发起检测。'
        + '这会产生真实 Telegram 消息和网络请求，不是演练。是否继续？',
      '确认执行 SpamBot 检测',
      {
        type: 'warning',
        confirmButtonText: '确认真实执行',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  const fingerprint = accountIds.join(',')
  if (!pendingSpamCheckIdempotencyKey || pendingSpamCheckFingerprint !== fingerprint) {
    pendingSpamCheckFingerprint = fingerprint
    pendingSpamCheckIdempotencyKey = createSpamCheckIdempotencyKey()
  }
  spamCheckSubmitting.value = true
  spamCheckOperationError.value = ''
  try {
    const operation = await accountSpamChecksApi.createOperation(
      { account_ids: accountIds },
      pendingSpamCheckIdempotencyKey,
    )
    spamCheckOperation.value = operation
    pendingSpamCheckFingerprint = ''
    pendingSpamCheckIdempotencyKey = ''
    if (isSpamCheckOperationActive(operation.status)) startSpamCheckPolling(operation.id)
    ElMessage.success('SpamBot 检测任务已提交')
  } catch (error) {
    spamCheckOperationError.value = spamCheckErrorText(error, 'SpamBot 检测任务提交失败')
  } finally {
    spamCheckSubmitting.value = false
  }
}

const cancelSpamCheckOperation = async () => {
  const operation = spamCheckOperation.value
  if (!operation || !isSpamCheckOperationActive(operation.status)) return
  try {
    await ElMessageBox.confirm(
      '取消后，已经完成的检测结果会保留。是否继续？',
      '取消 SpamBot 检测任务',
      {
        type: 'warning',
        confirmButtonText: '确认取消',
        cancelButtonText: '继续执行',
      },
    )
  } catch {
    return
  }

  spamCheckCancelling.value = true
  spamCheckOperationError.value = ''
  try {
    const updated = await accountSpamChecksApi.cancelOperation(operation.id)
    spamCheckOperation.value = updated
    if (isSpamCheckOperationActive(updated.status)) startSpamCheckPolling(updated.id)
    else {
      stopSpamCheckPolling()
      await refreshSpamCheckAccountSnapshots()
    }
    ElMessage.success('已提交取消请求')
  } catch (error) {
    spamCheckOperationError.value = spamCheckErrorText(error, 'SpamBot 检测任务取消失败')
  } finally {
    spamCheckCancelling.value = false
  }
}

const resetSecurityPagination = () => {
  todayUsagePagination.reset()
  riskEventPagination.reset()
  environmentEventPagination.reset()
}

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
  { prop: 'operation_mode', label: '账号职责', width: '130', slot: 'operationMode' },
  { prop: 'persona', label: 'AI 性格', width: '150', slot: 'persona' },
  { prop: 'asset_tier', label: '资产等级', width: '110', slot: 'assetTier' },
  { prop: 'warmup_stage', label: '托管暖号', width: '130', slot: 'warmupStage' },
  { prop: 'status', label: '状态', width: '110', slot: 'status' },
  { prop: 'spam_check_status', label: 'SpamBot 检测', minWidth: '190', slot: 'spamCheck' },
  { prop: 'delivery_status', label: '投放状态', width: '190', slot: 'deliveryStatus' },
  { prop: 'country_code', label: '国家/地区', width: '120', slot: 'country' },
  { prop: 'proxy_mode', label: '代理', width: '160', slot: 'proxy' },
  { prop: 'api_config_name', label: 'API配置', minWidth: '120' },
  { prop: 'connection_count', label: '连接数', width: '90' },
  { prop: 'error_count', label: '错误数', width: '90' },
  { prop: 'last_active_at', label: '最近活跃', width: '170', slot: 'lastActive' },
  { prop: 'created_at', label: '创建时间', width: '170', slot: 'createdAt' },
  { prop: 'actions', label: '操作', width: '450', fixed: 'right', slot: 'actions' },
]

const promoterAccounts = computed(() => accountStore.list.filter((item) => item.account_type === 'promoter'))

type DeliveryTagType = 'success' | 'warning' | 'danger' | 'info'

const operationalStatusMap = computed(
  () => new Map(dynamicStatuses.value.map((item) => [item.account_id, item])),
)

const deliveryStatusFor = (account: Pick<Account, 'id'>) => operationalStatusMap.value.get(account.id) || null

const deliveryStatusType = (account: Pick<Account, 'id'>): DeliveryTagType => {
  const diagnostic = deliveryStatusFor(account)?.delivery_diagnostic
  if (!diagnostic) return 'info'
  if (diagnostic.ad_delivery_allowed) return 'success'
  if (diagnostic.primary_block_severity === 'warning') return 'warning'
  return 'danger'
}

const deliveryStatusLabel = (account: Pick<Account, 'id'>) => {
  const diagnostic = deliveryStatusFor(account)?.delivery_diagnostic
  if (!diagnostic) return '状态评估中'
  if (diagnostic.ad_delivery_allowed) return '可投放'
  return diagnostic.primary_block_label || '投放阻塞'
}

const openDeliveryBlockDrawer = (account: Account) => {
  selectedDeliveryAccount.value = account
  selectedDeliveryStatus.value = deliveryStatusFor(account)
  deliveryBlockDrawerVisible.value = true
}

const personaStatusLabel = (account: Account) => {
  if (account.operation_mode === 'ad_only') return '当前不适用'
  if (account.persona?.configured && !account.persona.name) return '配置异常'
  if (account.persona?.effective_enabled === false) return '已暂停应用'
  if (account.persona?.configured) return `已配置 · v${account.persona.revision}`
  return '中性默认'
}

const personaStatusType = (account: Account): DeliveryTagType => {
  if (account.operation_mode === 'ad_only') return 'info'
  if (account.persona?.configured && !account.persona.name) return 'danger'
  if (account.persona?.effective_enabled === false) return 'warning'
  return account.persona?.configured ? 'success' : 'info'
}

const openPersonaDrawer = (account: Account) => {
  if (!isAdmin.value) return
  selectedPersonaAccount.value = account
  personaDrawerVisible.value = true
}

const personaAccountIdFromRoute = () => {
  return parseSafePositiveId(route.query.persona_account_id)
}

const openPersonaFromRoute = async () => {
  const accountId = personaAccountIdFromRoute()
  if (!accountListLoaded.value || !isAdmin.value || !accountId) return
  const sequence = ++personaRouteSequence

  activeAccountTab.value = 'list'
  let account = accountStore.list.find((item) => item.id === accountId) || null
  if (!account) {
    try {
      account = await accountsApi.getById(accountId)
    } catch {
      if (sequence === personaRouteSequence) ElMessage.warning(`未找到推广账号 #${accountId}，无法打开 Persona 设置`)
      return
    }
  }
  if (sequence !== personaRouteSequence || personaAccountIdFromRoute() !== accountId) return
  openPersonaDrawer(account)
}

const clearAccountLocation = () => {
  accountLocationSequence += 1
  accountLocationController?.abort()
  accountLocationController = null
  focusedAccountId.value = null
  focusedAccount.value = null
  focusedAccountError.value = ''
}

const locateAccountFromRoute = async () => {
  clearAccountLocation()
  activeAccountTab.value = 'list'
  if (!accountListLoaded.value || (isAdmin.value && personaAccountIdFromRoute())) return
  const accountId = parseSafePositiveId(route.query.account_id)
  if (!accountId) return
  const sequence = accountLocationSequence
  const existing = accountStore.list.find((item) => item.id === accountId)
  if (existing) {
    focusedAccountId.value = accountId
    await nextTick()
    document.querySelector('.focused-account')?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    return
  }
  const controller = new AbortController()
  accountLocationController = controller
  try {
    const result = await accountsApi.getById(accountId, controller.signal)
    if (sequence !== accountLocationSequence || parseSafePositiveId(route.query.account_id) !== accountId) return
    focusedAccount.value = result
  } catch (error) {
    const code = (error as { code?: string })?.code
    if (code !== 'ERR_CANCELED' && sequence === accountLocationSequence) focusedAccountError.value = '账号不存在或无权限'
  } finally {
    if (sequence === accountLocationSequence) accountLocationController = null
  }
}

const applyAccountRoute = async () => {
  personaRouteSequence += 1
  if (isAdmin.value && personaAccountIdFromRoute()) {
    clearAccountLocation()
    await openPersonaFromRoute()
    return
  }
  await locateAccountFromRoute()
}

const handlePersonaUpdated = (detail: AccountPersonaDetail) => {
  accountStore.updatePersonaSummary(detail)
  const current = accountStore.list.find((item) => item.id === detail.account_id)
  if (current) selectedPersonaAccount.value = current
}

const loadOperationalStatuses = async () => {
  operationalStatusLoading.value = true
  try {
    const response = await automationApi.getAdDynamicStatus()
    dynamicStatuses.value = response.data.data
    if (selectedDeliveryAccount.value) {
      selectedDeliveryStatus.value = deliveryStatusFor(selectedDeliveryAccount.value)
    }
  } catch (error) {
    console.error('Failed to load account operational statuses:', error)
    ElMessage.error('账号投放状态加载失败')
  } finally {
    operationalStatusLoading.value = false
  }
}

const formatProxyOption = (proxy: Proxy) => {
  const bound = proxy.bindAccountCount || 0
  const capacity = proxy.maxBindAccounts ?? MAX_STATIC_PROXY_BINDINGS
  return `${proxy.protocol}://${proxy.address}:${proxy.port} (${bound}/${capacity})`
}

const isProxyFullForAccount = (proxy: Proxy) => {
  if (formData.static_proxy_id === proxy.id) {
    return false
  }
  const capacity = proxy.maxBindAccounts ?? MAX_STATIC_PROXY_BINDINGS
  return (proxy.remainingBindSlots ?? Math.max(capacity - (proxy.bindAccountCount || 0), 0)) <= 0
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

const fetchData = async (params?: AccountListParams) => {
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

const handleSearch = (values: AccountListParams) => {
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
  resetSecurityPagination()
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

watch(
  () => [route.query.persona_account_id, route.query.account_id, route.query.assetId],
  () => { void applyAccountRoute() },
)

onMounted(() => {
  void fetchData().then(async () => {
    accountListLoaded.value = true
    await applyAccountRoute()
  })
  loadProxyOptions()
  loadOperationalStatuses()
})
onBeforeUnmount(() => {
  clearAccountLocation()
  personaRouteSequence += 1
  stopSpamCheckPolling()
})
</script>

<template>
  <div class="accounts-page">
    <div class="page-header">
      <div>
        <h2 class="page-title">推广账号</h2>
        <p class="page-desc">这里只管理用于搜群、加群、广告投放和私聊引导的推广账号。</p>
      </div>
      <div v-if="activeAccountTab === 'list'" class="header-actions">
        <el-button v-if="returnAssetId" @click="router.push(`/owned-groups/${returnAssetId}/operations?tab=members`)">返回群运营中心</el-button>
        <el-button @click="goToGuardianBots">
          <el-icon><ChatDotRound /></el-icon>
          查看Bot账号
        </el-button>
        <el-button @click="openSpamCheckDialog">
          <el-icon><CircleCheck /></el-icon>
          SpamBot 封禁检测
        </el-button>
        <el-button v-if="isAdmin" @click="profileUpdateDialogVisible = true">
          <el-icon><Edit /></el-icon>
          批量设置账号简介
        </el-button>
        <el-button type="primary" @click="openAddDrawer">
          <el-icon><Plus /></el-icon>
          添加推广账号
        </el-button>
      </div>
    </div>

    <el-tabs v-model="activeAccountTab" class="account-tabs">
      <el-tab-pane label="账号列表" name="list" />
      <el-tab-pane label="账号运营态" name="operations" />
    </el-tabs>

    <div v-show="activeAccountTab === 'list'">
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

    <el-alert v-if="focusedAccountError" type="warning" :closable="false" show-icon :title="focusedAccountError" class="page-alert" />
    <el-card v-if="focusedAccount" shadow="never" class="focused-result-card">
      <template #header><strong>定位结果（不影响当前分页）</strong></template>
      <el-descriptions :column="4" border>
        <el-descriptions-item label="账号 ID">#{{ focusedAccount.id }}</el-descriptions-item>
        <el-descriptions-item label="名称">{{ focusedAccount.display_name || `账号 #${focusedAccount.id}` }}</el-descriptions-item>
        <el-descriptions-item label="职责">{{ focusedAccount.operation_mode === 'ad_only' ? '仅外部广告' : '增长综合' }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ focusedAccount.status }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

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
        <div class="identifier-cell" :class="{ 'focused-account': row.id === focusedAccountId }">
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

      <template #operationMode="{ row }">
        <el-tag :type="row.operation_mode === 'ad_only' ? 'warning' : 'success'" effect="plain">
          {{ row.operation_mode === 'ad_only' ? 'Ad-only 专用' : 'Growth 增长' }}
        </el-tag>
      </template>

      <template #persona="{ row }">
        <div class="persona-status-cell">
          <el-tag :type="personaStatusType(row)" effect="plain">
            {{ personaStatusLabel(row) }}
          </el-tag>
          <span v-if="row.persona?.configured && row.persona.name" class="persona-name">
            {{ row.persona.name }}
          </span>
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
          <div v-if="row.status === 'restricted'" class="account-restriction-detail">
            <strong>{{ accountRestrictionSourceText(row) }}</strong>
            <span v-if="row.restriction_reason">{{ row.restriction_reason }}</span>
            <small v-if="row.restriction_detected_at">{{ formatDate(row.restriction_detected_at) }}</small>
          </div>
        </div>
      </template>

      <template #spamCheck="{ row }">
        <div class="spam-check-status-cell">
          <el-tag :type="spamCheckResultType(row.spam_check_status)" effect="plain">
            {{ spamCheckResultText(row.spam_check_status) }}
          </el-tag>
          <small v-if="row.spam_checked_at">{{ formatDate(row.spam_checked_at) }}</small>
          <span
            v-if="row.spam_check_summary"
            class="spam-check-summary"
            :title="row.spam_check_summary"
          >
            {{ row.spam_check_summary }}
          </span>
        </div>
      </template>

      <template #deliveryStatus="{ row }">
        <div class="delivery-status-cell">
          <el-tag :type="deliveryStatusType(row)" effect="dark" size="small">
            {{ deliveryStatusLabel(row) }}
          </el-tag>
          <el-button type="primary" link size="small" @click="openDeliveryBlockDrawer(row)">
            阻塞明细
          </el-button>
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
        <div class="account-action-buttons">
          <el-button v-if="isAdmin" type="primary" link size="small" @click="openPersonaDrawer(row)">
            <el-icon><MagicStick /></el-icon>
            AI 性格
          </el-button>
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
        </div>
      </template>
    </TableCard>
    </div>

    <AccountOperationalStatusPanel
      v-if="activeAccountTab === 'operations'"
      :statuses="dynamicStatuses"
      :loading="operationalStatusLoading"
      @refresh="loadOperationalStatuses"
    />

    <AccountDeliveryBlockDrawer
      v-model:visible="deliveryBlockDrawerVisible"
      :account="selectedDeliveryAccount"
      :status="selectedDeliveryStatus"
      @recovered="loadOperationalStatuses"
    />

    <AccountPersonaDrawer
      v-model:visible="personaDrawerVisible"
      :account="selectedPersonaAccount"
      :can-edit="isAdmin"
      @updated="handlePersonaUpdated"
    />

    <AccountLoginDialog
      v-model:visible="loginDialogVisible"
      @success="handleLoginSuccess"
    />

    <AccountProfileUpdateDialog
      v-if="profileUpdateDialogVisible && isAdmin"
      v-model:visible="profileUpdateDialogVisible"
      @completed="fetchData"
    />


    <el-dialog
      v-model="spamCheckDialogVisible"
      title="SpamBot 封禁检测"
      width="min(880px, 94vw)"
      :close-on-click-modal="!spamCheckSubmitting"
      :close-on-press-escape="!spamCheckSubmitting"
    >
      <div
        v-loading="spamCheckLoadingAccounts || spamCheckOperationLoading"
        class="spam-check-dialog-body"
      >
        <el-alert
          title="这会执行真实 Telegram 操作"
          description="每个选中账号会向 @SpamBot 发起真实检测。SpamBot 显示未受限，只代表它未检测到垃圾消息限制，不代表账号所有 Telegram 操作正常；RPC 等操作限制仍会独立保留并阻止建群。"
          type="warning"
          :closable="false"
          show-icon
        />

        <el-alert
          v-if="spamCheckLoadError"
          :title="spamCheckLoadError"
          type="error"
          :closable="false"
          show-icon
        />

        <section v-if="spamCheckOperation" class="spam-check-operation">
          <div class="spam-check-section-heading">
            <div>
              <strong>最近任务 #{{ spamCheckOperation.id }}</strong>
              <el-tag
                :type="spamCheckOperationStatusType(spamCheckOperation.status)"
                effect="plain"
              >
                {{ spamCheckOperationStatusText(spamCheckOperation.status) }}
              </el-tag>
            </div>
            <el-button
              link
              type="primary"
              :loading="spamCheckOperationLoading"
              @click="refreshSpamCheckOperation"
            >
              刷新进度
            </el-button>
          </div>

          <el-progress
            :percentage="spamCheckProgressPercent"
            :status="spamCheckProgressStatus"
          />

          <div class="spam-check-operation-metrics">
            <div><span>总账号</span><strong>{{ spamCheckOperation.total_accounts }}</strong></div>
            <div><span>已处理</span><strong>{{ spamCheckOperation.processed_accounts }}</strong></div>
            <div><span>SpamBot 未限</span><strong>{{ spamCheckOperation.clear_accounts }}</strong></div>
            <div><span>受限</span><strong>{{ spamCheckOperation.restricted_accounts }}</strong></div>
            <div><span>失败</span><strong>{{ spamCheckOperation.failed_accounts }}</strong></div>
            <div><span>已取消</span><strong>{{ spamCheckOperation.cancelled_accounts }}</strong></div>
            <div><span>剩余</span><strong>{{ spamCheckRemainingAccounts }}</strong></div>
          </div>

          <el-alert
            v-if="spamCheckOperation.last_error || spamCheckOperationError"
            :title="spamCheckOperationError || spamCheckOperation.last_error || ''"
            type="error"
            :closable="false"
            show-icon
          />

          <div v-if="spamCheckRecentItems.length" class="spam-check-items">
            <div class="spam-check-items-title">
              账号明细
              <small>显示最近 {{ spamCheckRecentItems.length }} 条</small>
            </div>
            <el-table :data="spamCheckRecentItems" size="small" max-height="260">
              <el-table-column label="账号" min-width="150">
                <template #default="{ row }">
                  {{ spamCheckAccountLabel(row.account_id) }}
                </template>
              </el-table-column>
              <el-table-column label="结果" width="100">
                <template #default="{ row }">
                  <el-tag :type="spamCheckItemStatusType(row)" effect="plain">
                    {{ spamCheckItemStatusText(row) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="attempts" label="尝试" width="65" />
              <el-table-column label="检测时间" width="155">
                <template #default="{ row }">
                  {{ formatDate(row.checked_at || row.finished_at) }}
                </template>
              </el-table-column>
              <el-table-column label="回复摘要" min-width="210" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ row.response_summary || row.reason_code || '-' }}
                </template>
              </el-table-column>
            </el-table>
          </div>
        </section>

        <el-alert
          v-else-if="spamCheckOperationError"
          :title="spamCheckOperationError"
          type="error"
          :closable="false"
          show-icon
        />

        <section class="spam-check-plan">
          <div class="spam-check-section-heading">
            <div>
              <strong>选择推广账号</strong>
              <small>
                共 {{ spamCheckAccounts.length }} 个，当前可检测 {{ spamCheckEligibleAccounts.length }} 个
              </small>
            </div>
            <el-button
              link
              type="primary"
              :loading="spamCheckLoadingAccounts"
              @click="loadSpamCheckAccounts"
            >
              刷新账号
            </el-button>
          </div>

          <el-input
            v-model="spamCheckSearch"
            clearable
            placeholder="搜索账号名称、手机号、会话或 ID"
          />

          <div class="spam-check-picker-toolbar">
            <span>已选择 {{ spamCheckSelectedCount }} / {{ SPAM_CHECK_MAX_ACCOUNTS }}</span>
            <div>
              <el-button
                link
                type="primary"
                :disabled="spamCheckOperationActive || !spamCheckEligibleAccounts.length"
                @click="selectAllSpamCheckAccounts"
              >
                全选可检测账号
              </el-button>
              <el-button
                link
                :disabled="spamCheckOperationActive || !spamCheckSelectedCount"
                @click="clearSpamCheckAccounts"
              >
                清空
              </el-button>
            </div>
          </div>

          <el-scrollbar max-height="300px" class="spam-check-scrollbar">
            <el-checkbox-group
              v-if="spamCheckFilteredAccounts.length"
              v-model="spamCheckSelectedIds"
              :max="SPAM_CHECK_MAX_ACCOUNTS"
              class="spam-check-checkbox-group"
            >
              <div
                v-for="account in spamCheckFilteredAccounts"
                :key="account.id"
                class="spam-check-account-row"
                :class="{ 'is-disabled': Boolean(spamCheckEligibilityReason(account)) }"
              >
                <el-checkbox
                  :label="account.id"
                  :disabled="spamCheckOperationActive || Boolean(spamCheckEligibilityReason(account))"
                  class="spam-check-checkbox"
                >
                  <span class="spam-check-account-name">
                    {{ account.display_name || account.identifier || ('账号 #' + account.id) }}
                  </span>
                  <small>
                    #{{ account.id }} · {{ account.phone || account.session_name }} ·
                    {{ spamCheckAccountStatusText(account.status) }} ·
                    {{ spamCheckResultText(account.spam_check_status) }}
                    <template v-if="spamCheckEligibilityReason(account)">
                      · {{ spamCheckEligibilityReason(account) }}
                    </template>
                  </small>
                </el-checkbox>
              </div>
            </el-checkbox-group>
            <el-empty v-else description="没有匹配的推广账号" :image-size="56" />
          </el-scrollbar>
        </section>
      </div>

      <template #footer>
        <el-button @click="spamCheckDialogVisible = false">关闭</el-button>
        <el-button
          v-if="spamCheckOperationActive"
          type="danger"
          plain
          :loading="spamCheckCancelling"
          @click="cancelSpamCheckOperation"
        >
          取消任务
        </el-button>
        <el-button
          type="primary"
          :loading="spamCheckSubmitting"
          :disabled="!spamCheckCanSubmit"
          @click="submitSpamCheckOperation"
        >
          确认并检测
        </el-button>
      </template>
    </el-dialog>

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
        <el-table :data="todayUsagePagination.rows.value" size="small" max-height="220" empty-text="暂无今日动作">
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
        <ClientListPagination
          v-model:page="todayUsagePagination.page.value"
          v-model:page-size="todayUsagePagination.pageSize.value"
          :total="todayUsagePagination.total.value"
        />

        <h3 class="security-title">风险事件</h3>
        <el-table :data="riskEventPagination.rows.value" size="small" max-height="260" empty-text="暂无风险事件">
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
        <ClientListPagination
          v-model:page="riskEventPagination.page.value"
          v-model:page-size="riskEventPagination.pageSize.value"
          :total="riskEventPagination.total.value"
        />

        <h3 class="security-title">环境事件</h3>
        <el-table :data="environmentEventPagination.rows.value" size="small" max-height="260" empty-text="暂无环境事件">
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
        <ClientListPagination
          v-model:page="environmentEventPagination.page.value"
          v-model:page-size="environmentEventPagination.pageSize.value"
          :total="environmentEventPagination.total.value"
        />
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

.account-tabs {
  min-width: 0;
}

.header-actions {
  display: flex;
  gap: 12px;
}

.spam-check-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.spam-check-operation,
.spam-check-plan {
  padding: 14px;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #fff;
}

.spam-check-section-heading,
.spam-check-picker-toolbar,
.spam-check-items-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.spam-check-section-heading {
  margin-bottom: 12px;

  > div {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
  }

  small {
    color: #909399;
  }
}

.spam-check-operation-metrics {
  display: grid;
  gap: 8px;
  margin: 12px 0;
  grid-template-columns: repeat(4, minmax(0, 1fr));

  > div {
    display: flex;
    min-height: 52px;
    padding: 8px 10px;
    border-radius: 6px;
    background: #f5f7fa;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
  }

  span {
    color: #909399;
    font-size: 12px;
  }

  strong {
    color: #303133;
    font-size: 18px;
  }
}

.spam-check-picker-toolbar {
  margin: 10px 0;
  color: #606266;
  font-size: 13px;
}

.spam-check-scrollbar {
  border: 1px solid #ebeef5;
  border-radius: 6px;
}

.spam-check-checkbox-group {
  display: grid;
  padding: 8px;
  gap: 8px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.spam-check-account-row {
  min-width: 0;
  border: 1px solid #e4e7ed;
  border-radius: 6px;

  &.is-disabled {
    background: #f5f7fa;
  }
}

.spam-check-checkbox {
  width: 100%;
  height: auto;
  min-height: 56px;
  margin: 0;
  padding: 8px 10px;
  white-space: normal;

  :deep(.el-checkbox__label) {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 4px;
  }

  small {
    overflow: hidden;
    color: #909399;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.spam-check-account-name {
  overflow: hidden;
  color: #303133;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.spam-check-items {
  margin-top: 14px;
}

.spam-check-items-title {
  margin-bottom: 8px;
  font-weight: 600;

  small {
    color: #909399;
    font-weight: 400;
  }
}

.account-restriction-detail {
  display: flex;
  max-width: 150px;
  flex-direction: column;
  gap: 3px;
  color: #f56c6c;
  font-size: 12px;

  span,
  small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}
.spam-check-status-cell {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  flex-direction: column;
  gap: 4px;

  small,
  .spam-check-summary {
    max-width: 180px;
    overflow: hidden;
    color: #909399;
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.delivery-status-cell,
.persona-status-cell {
  display: flex;
  align-items: flex-start;
  flex-direction: column;
  gap: 4px;
}

.persona-name {
  max-width: 140px;
  overflow: hidden;
  color: #909399;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-action-buttons {
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 6px;
  white-space: nowrap;

  :deep(.el-button) {
    flex: 0 0 auto;
    margin-left: 0;
  }
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

@media (max-width: 760px) {
  .spam-check-operation-metrics,
  .spam-check-checkbox-group {
    grid-template-columns: 1fr;
  }

  .spam-check-section-heading,
  .spam-check-picker-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
