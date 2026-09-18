<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { accountsApi, type Account } from '@/api/accounts'
import {
  accountProfileUpdatesApi,
  createAccountProfileUpdateIdempotencyKey,
  type AccountProfileUpdateItem,
  type AccountProfileUpdateOperation,
  type AccountProfileUpdateOperationStatus,
} from '@/api/accountProfileUpdates'

const props = defineProps<{
  visible: boolean
}>()

const emit = defineEmits<{
  'update:visible': [value: boolean]
  completed: []
}>()

const PROFILE_UPDATE_MAX_ACCOUNTS = 2000
const PROFILE_UPDATE_PAGE_SIZE = 2000
const PROFILE_UPDATE_POLL_INTERVAL_MS = 3000
const profileUpdateEligibleStatuses = new Set<Account['status']>(['online', 'idle', 'offline'])
const profileUpdateActiveStatuses = new Set<AccountProfileUpdateOperationStatus>([
  'queued',
  'running',
  'cancelling',
])

type TagType = 'success' | 'warning' | 'danger' | 'info'

const dialogVisible = computed({
  get: () => props.visible,
  set: (value: boolean) => emit('update:visible', value),
})
const profileUpdateLoadingAccounts = ref(false)
const profileUpdateOperationLoading = ref(false)
const profileUpdateSubmitting = ref(false)
const profileUpdateCancelling = ref(false)
const profileUpdateAccounts = ref<Account[]>([])
const profileUpdateSelectedIds = ref<number[]>([])
const profileUpdateBio = ref('')
const profileUpdateSearch = ref('')
const profileUpdateLoadError = ref('')
const profileUpdateOperationError = ref('')
const profileUpdateOperation = ref<AccountProfileUpdateOperation | null>(null)
let profileUpdatePollTimer: number | null = null
let profileUpdatePollGeneration = 0
let pendingProfileUpdateFingerprint = ''
let pendingProfileUpdateIdempotencyKey = ''

const isProfileUpdateOperationActive = (status?: AccountProfileUpdateOperationStatus) =>
  Boolean(status && profileUpdateActiveStatuses.has(status))

const profileUpdateOperationActive = computed(() =>
  isProfileUpdateOperationActive(profileUpdateOperation.value?.status),
)

const profileUpdateActiveAccountIds = computed(() => {
  if (!profileUpdateOperationActive.value) return new Set<number>()
  return new Set(
    (profileUpdateOperation.value?.items || [])
      .map((item) => item.account_id)
      .filter((accountId): accountId is number => typeof accountId === 'number'),
  )
})

const profileUpdateEligibilityReason = (account: Account) => {
  if (account.account_type !== 'promoter') return '不是推广账号'
  if (account.operation_mode !== 'ad_only') return '不是广告专用账号'
  if (!account.is_active) return '账号未启用'
  if (!account.session_name?.trim()) return '缺少登录会话'
  if (!profileUpdateEligibleStatuses.has(account.status)) return '当前连接状态不可更新'
  if (profileUpdateActiveAccountIds.value.has(account.id)) return '已有简介任务处理中'
  return ''
}

const profileUpdateEligibleAccounts = computed(() =>
  profileUpdateAccounts.value.filter((account) => !profileUpdateEligibilityReason(account)),
)

const profileUpdateFilteredAccounts = computed(() => {
  const query = profileUpdateSearch.value.trim().toLowerCase()
  if (!query) return profileUpdateAccounts.value
  return profileUpdateAccounts.value.filter((account) =>
    [
      String(account.id),
      account.display_name,
      account.identifier,
      account.phone,
      account.session_name,
    ].some((value) => value?.toLowerCase().includes(query)),
  )
})

const profileUpdateSelectedCount = computed(() => profileUpdateSelectedIds.value.length)
const profileUpdateCanSubmit = computed(() =>
  profileUpdateSelectedCount.value >= 1
  && profileUpdateSelectedCount.value <= PROFILE_UPDATE_MAX_ACCOUNTS
  && profileUpdateBio.value.trim().length >= 1
  && profileUpdateBio.value.trim().length <= 70
  && !profileUpdateLoadingAccounts.value
  && !profileUpdateSubmitting.value,
)
const profileUpdateAccountMap = computed(
  () => new Map(profileUpdateAccounts.value.map((account) => [account.id, account])),
)
const profileUpdateRecentItems = computed(() =>
  [...(profileUpdateOperation.value?.items || [])].slice(-100).reverse(),
)
const profileUpdateProgressPercent = computed(() => {
  const operation = profileUpdateOperation.value
  if (!operation) return 0
  if (operation.status === 'succeeded') return 100
  if (!operation.total_accounts) return 0
  return Math.min(100, Math.round((operation.processed_accounts / operation.total_accounts) * 100))
})
const profileUpdateRemainingAccounts = computed(() => {
  const operation = profileUpdateOperation.value
  if (!operation) return 0
  return Math.max(operation.total_accounts - operation.processed_accounts, 0)
})
const profileUpdateProgressStatus = computed<'success' | 'warning' | 'exception' | undefined>(() => {
  const status = profileUpdateOperation.value?.status
  if (status === 'succeeded') return 'success'
  if (status === 'partial_failed') return 'warning'
  if (status === 'failed') return 'exception'
  return undefined
})

const operationSnapshotFingerprint = () => {
  const ids = [...profileUpdateSelectedIds.value].sort((left, right) => left - right)
  return ids.join(',') + '|' + profileUpdateBio.value.trim()
}

const clearPendingProfileUpdateKeyIfSnapshotChanged = () => {
  if (
    pendingProfileUpdateFingerprint
    && operationSnapshotFingerprint() !== pendingProfileUpdateFingerprint
  ) {
    pendingProfileUpdateFingerprint = ''
    pendingProfileUpdateIdempotencyKey = ''
  }
}

watch([profileUpdateSelectedIds, profileUpdateBio], () => {
  if (profileUpdateSelectedIds.value.length > PROFILE_UPDATE_MAX_ACCOUNTS) {
    profileUpdateSelectedIds.value = profileUpdateSelectedIds.value.slice(0, PROFILE_UPDATE_MAX_ACCOUNTS)
    ElMessage.warning('一次最多选择 2000 个广告账号')
    return
  }
  clearPendingProfileUpdateKeyIfSnapshotChanged()
})

const profileUpdateOperationStatusText = (status: AccountProfileUpdateOperationStatus) => {
  const labels: Record<AccountProfileUpdateOperationStatus, string> = {
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

const profileUpdateOperationStatusType = (status: AccountProfileUpdateOperationStatus): TagType => {
  if (status === 'succeeded') return 'success'
  if (status === 'partial_failed' || status === 'cancelling') return 'warning'
  if (status === 'failed' || status === 'cancelled') return 'danger'
  return 'info'
}

const profileUpdateItemStatusText = (item: { status?: AccountProfileUpdateItem['status'] }) => {
  const labels: Record<AccountProfileUpdateItem['status'], string> = {
    pending: '等待中',
    in_progress: '更新中',
    retry_wait: '等待重试',
    succeeded: '已完成',
    failed: '失败',
    cancelled: '已取消',
    skipped: '已跳过',
  }
  const status = item.status
  return status ? labels[status] || status : '未知'
}

const profileUpdateItemStatusType = (item: { status?: AccountProfileUpdateItem['status'] }): TagType => {
  if (item.status === 'succeeded') return 'success'
  if (item.status === 'retry_wait' || item.status === 'in_progress') return 'warning'
  if (item.status === 'failed' || item.status === 'cancelled') return 'danger'
  return 'info'
}

const profileUpdateAccountLabel = (accountId: number | null) => {
  if (accountId === null) return '账号已删除'
  const account = profileUpdateAccountMap.value.get(accountId)
  return account?.display_name || account?.identifier || ('账号 #' + accountId)
}

const profileUpdateAccountStatusText = (status: Account['status']) => {
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

const profileUpdateErrorText = (error: unknown, fallback: string) => {
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

const stopProfileUpdatePolling = () => {
  profileUpdatePollGeneration += 1
  if (profileUpdatePollTimer !== null) window.clearTimeout(profileUpdatePollTimer)
  profileUpdatePollTimer = null
}

const loadProfileUpdateAccounts = async () => {
  profileUpdateLoadingAccounts.value = true
  profileUpdateLoadError.value = ''
  try {
    const collected = new Map<number, Account>()
    const seenCursors = new Set<string>()
    let cursor: string | undefined
    while (true) {
      const response = await accountsApi.list({
        account_type: 'promoter',
        limit: PROFILE_UPDATE_PAGE_SIZE,
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
    profileUpdateAccounts.value = Array.from(collected.values())
    const eligibleIds = new Set(profileUpdateEligibleAccounts.value.map((account) => account.id))
    profileUpdateSelectedIds.value = profileUpdateSelectedIds.value.filter((id) => eligibleIds.has(id))
  } catch (error) {
    profileUpdateLoadError.value = profileUpdateErrorText(error, '广告账号加载失败')
  } finally {
    profileUpdateLoadingAccounts.value = false
  }
}

const refreshProfileUpdateAccountSnapshots = async () => {
  await loadProfileUpdateAccounts()
  emit('completed')
}

const scheduleProfileUpdatePoll = (operationId: number, generation: number) => {
  profileUpdatePollTimer = window.setTimeout(async () => {
    try {
      const refreshed = await accountProfileUpdatesApi.getOperation(operationId)
      if (generation !== profileUpdatePollGeneration) return
      profileUpdateOperation.value = refreshed
      profileUpdateOperationError.value = ''
    } catch (error) {
      if (generation !== profileUpdatePollGeneration) return
      profileUpdateOperationError.value = profileUpdateErrorText(error, '简介任务进度刷新失败')
    }
    if (
      generation === profileUpdatePollGeneration
      && profileUpdateOperation.value?.id === operationId
      && isProfileUpdateOperationActive(profileUpdateOperation.value.status)
    ) {
      scheduleProfileUpdatePoll(operationId, generation)
    } else if (generation === profileUpdatePollGeneration) {
      profileUpdatePollTimer = null
      await refreshProfileUpdateAccountSnapshots()
    }
  }, PROFILE_UPDATE_POLL_INTERVAL_MS)
}

const startProfileUpdatePolling = (operationId: number) => {
  stopProfileUpdatePolling()
  const generation = profileUpdatePollGeneration
  scheduleProfileUpdatePoll(operationId, generation)
}

const loadLatestProfileUpdateOperation = async () => {
  profileUpdateOperationLoading.value = true
  profileUpdateOperationError.value = ''
  try {
    const latest = await accountProfileUpdatesApi.getLatestOperation()
    profileUpdateOperation.value = latest
    if (latest && isProfileUpdateOperationActive(latest.status)) startProfileUpdatePolling(latest.id)
    else stopProfileUpdatePolling()
  } catch (error) {
    const status = (error as { response?: { status?: number } })?.response?.status
    if (status === 404) {
      profileUpdateOperation.value = null
      stopProfileUpdatePolling()
    } else {
      profileUpdateOperationError.value = profileUpdateErrorText(error, '最近简介任务加载失败')
    }
  } finally {
    profileUpdateOperationLoading.value = false
  }
}

const openProfileUpdateDialog = async () => {
  await Promise.allSettled([
    loadProfileUpdateAccounts(),
    loadLatestProfileUpdateOperation(),
  ])
}

const refreshProfileUpdateOperation = async () => {
  const operationId = profileUpdateOperation.value?.id
  if (!operationId) {
    await loadLatestProfileUpdateOperation()
    return
  }
  stopProfileUpdatePolling()
  profileUpdateOperationLoading.value = true
  profileUpdateOperationError.value = ''
  try {
    const refreshed = await accountProfileUpdatesApi.getOperation(operationId)
    profileUpdateOperation.value = refreshed
    if (isProfileUpdateOperationActive(refreshed.status)) startProfileUpdatePolling(refreshed.id)
    else await refreshProfileUpdateAccountSnapshots()
  } catch (error) {
    profileUpdateOperationError.value = profileUpdateErrorText(error, '简介任务进度刷新失败')
  } finally {
    profileUpdateOperationLoading.value = false
  }
}

const selectAllProfileUpdateAccounts = () => {
  const eligible = profileUpdateEligibleAccounts.value
  profileUpdateSelectedIds.value = eligible
    .slice(0, PROFILE_UPDATE_MAX_ACCOUNTS)
    .map((account) => account.id)
  if (eligible.length > PROFILE_UPDATE_MAX_ACCOUNTS) {
    ElMessage.warning('可更新账号超过 2000 个，本次已选择前 2000 个')
  }
}

const clearProfileUpdateAccounts = () => {
  profileUpdateSelectedIds.value = []
}

const submitProfileUpdateOperation = async () => {
  const accountIds = [...profileUpdateSelectedIds.value].sort((left, right) => left - right)
  const profileBio = profileUpdateBio.value.trim()
  if (!accountIds.length) {
    ElMessage.warning('请至少选择 1 个可更新的广告账号')
    return
  }
  if (accountIds.length > PROFILE_UPDATE_MAX_ACCOUNTS) {
    ElMessage.warning('一次最多选择 2000 个广告账号')
    return
  }
  if (!profileBio || profileBio.length > 70) {
    ElMessage.warning('请输入 1 到 70 个字符的账号简介')
    return
  }
  const eligibleIds = new Set(profileUpdateEligibleAccounts.value.map((account) => account.id))
  if (accountIds.some((id) => !eligibleIds.has(id))) {
    ElMessage.warning('选择中包含当前不可更新账号，请刷新账号后重新选择')
    return
  }

  try {
    await ElMessageBox.confirm(
      '将把 ' + accountIds.length + ' 个 ad_only 广告账号的简介写入持久化队列。'
        + '服务器会全局单线程、逐个执行真实 Telegram 更新，不会由浏览器并发发起。是否继续？',
      '确认排队批量设置简介',
      {
        type: 'warning',
        confirmButtonText: '确认排队',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  const fingerprint = operationSnapshotFingerprint()
  if (!pendingProfileUpdateIdempotencyKey || pendingProfileUpdateFingerprint !== fingerprint) {
    pendingProfileUpdateFingerprint = fingerprint
    pendingProfileUpdateIdempotencyKey = createAccountProfileUpdateIdempotencyKey()
  }
  profileUpdateSubmitting.value = true
  profileUpdateOperationError.value = ''
  try {
    const operation = await accountProfileUpdatesApi.createOperation(
      { account_ids: accountIds, profile_bio: profileBio },
      pendingProfileUpdateIdempotencyKey,
    )
    profileUpdateOperation.value = operation
    pendingProfileUpdateFingerprint = ''
    pendingProfileUpdateIdempotencyKey = ''
    if (isProfileUpdateOperationActive(operation.status)) startProfileUpdatePolling(operation.id)
    ElMessage.success('广告账号简介已排入单线程队列')
  } catch (error) {
    profileUpdateOperationError.value = profileUpdateErrorText(error, '广告账号简介任务提交失败')
  } finally {
    profileUpdateSubmitting.value = false
  }
}

const cancelProfileUpdateOperation = async () => {
  const operation = profileUpdateOperation.value
  if (!operation || !isProfileUpdateOperationActive(operation.status)) return
  try {
    await ElMessageBox.confirm(
      '取消后，已经同步到 Telegram 的简介会保留；尚未执行的账号将不再更新。是否继续？',
      '取消批量简介任务',
      {
        type: 'warning',
        confirmButtonText: '确认取消',
        cancelButtonText: '继续执行',
      },
    )
  } catch {
    return
  }

  profileUpdateCancelling.value = true
  profileUpdateOperationError.value = ''
  try {
    const updated = await accountProfileUpdatesApi.cancelOperation(operation.id)
    profileUpdateOperation.value = updated
    if (isProfileUpdateOperationActive(updated.status)) startProfileUpdatePolling(updated.id)
    else await refreshProfileUpdateAccountSnapshots()
    ElMessage.success('已提交取消请求')
  } catch (error) {
    profileUpdateOperationError.value = profileUpdateErrorText(error, '简介任务取消失败')
  } finally {
    profileUpdateCancelling.value = false
  }
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) void openProfileUpdateDialog()
    else stopProfileUpdatePolling()
  },
  { immediate: true },
)

onBeforeUnmount(stopProfileUpdatePolling)
</script>

<template>
  <el-dialog
    v-model="dialogVisible"
    title="批量设置广告账号简介"
    width="min(900px, 94vw)"
    :close-on-click-modal="!profileUpdateSubmitting"
    :close-on-press-escape="!profileUpdateSubmitting"
  >
    <div
      v-loading="profileUpdateLoadingAccounts || profileUpdateOperationLoading"
      class="profile-update-dialog-body"
    >
      <el-alert
        title="全局单线程队列：不会批量并发调用 Telegram"
        description="提交只会创建持久化任务。服务端全局每次最多处理一个 ad_only 广告账号，并在执行时再次核验会话、账号职责和简介是否被人工改动。"
        type="info"
        :closable="false"
        show-icon
      />

      <el-alert
        v-if="profileUpdateLoadError"
        :title="profileUpdateLoadError"
        type="error"
        :closable="false"
        show-icon
      />

      <section v-if="profileUpdateOperation" class="profile-update-operation">
        <div class="profile-update-section-heading">
          <div>
            <strong>最近任务 #{{ profileUpdateOperation.id }}</strong>
            <el-tag :type="profileUpdateOperationStatusType(profileUpdateOperation.status)" effect="plain">
              {{ profileUpdateOperationStatusText(profileUpdateOperation.status) }}
            </el-tag>
          </div>
          <el-button
            link
            type="primary"
            :loading="profileUpdateOperationLoading"
            @click="refreshProfileUpdateOperation"
          >
            刷新进度
          </el-button>
        </div>

        <div class="profile-update-bio-preview">
          <span>目标简介</span>
          <strong>{{ profileUpdateOperation.profile_bio }}</strong>
        </div>
        <el-progress :percentage="profileUpdateProgressPercent" :status="profileUpdateProgressStatus" />

        <div class="profile-update-operation-metrics">
          <div><span>总账号</span><strong>{{ profileUpdateOperation.total_accounts }}</strong></div>
          <div><span>已处理</span><strong>{{ profileUpdateOperation.processed_accounts }}</strong></div>
          <div><span>成功</span><strong>{{ profileUpdateOperation.succeeded_accounts }}</strong></div>
          <div><span>失败</span><strong>{{ profileUpdateOperation.failed_accounts }}</strong></div>
          <div><span>跳过</span><strong>{{ profileUpdateOperation.skipped_accounts }}</strong></div>
          <div><span>已取消</span><strong>{{ profileUpdateOperation.cancelled_accounts }}</strong></div>
          <div><span>剩余</span><strong>{{ profileUpdateRemainingAccounts }}</strong></div>
        </div>

        <el-alert
          v-if="profileUpdateOperation.last_error || profileUpdateOperationError"
          :title="profileUpdateOperationError || profileUpdateOperation.last_error || ''"
          type="error"
          :closable="false"
          show-icon
        />

        <div v-if="profileUpdateRecentItems.length" class="profile-update-items">
          <div class="profile-update-items-title">
            账号明细
            <small>显示最近 {{ profileUpdateRecentItems.length }} 条</small>
          </div>
          <el-table :data="profileUpdateRecentItems" size="small" max-height="260">
            <el-table-column label="账号" min-width="150">
              <template #default="{ row }">
                {{ profileUpdateAccountLabel(row.account_id) }}
              </template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag :type="profileUpdateItemStatusType(row)" effect="plain">
                  {{ profileUpdateItemStatusText(row) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="attempts" label="尝试" width="65" />
            <el-table-column label="原因" min-width="210" show-overflow-tooltip>
              <template #default="{ row }">
                {{ row.error_message || row.reason_code || '-' }}
              </template>
            </el-table-column>
          </el-table>
        </div>
      </section>

      <el-alert
        v-else-if="profileUpdateOperationError"
        :title="profileUpdateOperationError"
        type="error"
        :closable="false"
        show-icon
      />

      <section class="profile-update-plan">
        <div class="profile-update-section-heading">
          <div>
            <strong>选择 ad_only 广告账号</strong>
            <small>
              共 {{ profileUpdateAccounts.length }} 个推广账号，当前可排队 {{ profileUpdateEligibleAccounts.length }} 个
            </small>
          </div>
          <el-button
            link
            type="primary"
            :loading="profileUpdateLoadingAccounts"
            @click="loadProfileUpdateAccounts"
          >
            刷新账号
          </el-button>
        </div>

        <el-input
          v-model="profileUpdateBio"
          type="textarea"
          :rows="3"
          maxlength="70"
          show-word-limit
          placeholder="输入要批量设置的 Telegram 公开简介，最多 70 字"
        />

        <el-input
          v-model="profileUpdateSearch"
          clearable
          placeholder="搜索账号名称、手机号、会话或 ID"
        />

        <div class="profile-update-picker-toolbar">
          <span>已选择 {{ profileUpdateSelectedCount }} / {{ PROFILE_UPDATE_MAX_ACCOUNTS }}</span>
          <div>
            <el-button
              link
              type="primary"
              :disabled="profileUpdateSubmitting || !profileUpdateEligibleAccounts.length"
              @click="selectAllProfileUpdateAccounts"
            >
              全选可排队账号
            </el-button>
            <el-button
              link
              :disabled="profileUpdateSubmitting || !profileUpdateSelectedCount"
              @click="clearProfileUpdateAccounts"
            >
              清空
            </el-button>
          </div>
        </div>

        <el-scrollbar max-height="300px" class="profile-update-scrollbar">
          <el-checkbox-group
            v-if="profileUpdateFilteredAccounts.length"
            v-model="profileUpdateSelectedIds"
            :max="PROFILE_UPDATE_MAX_ACCOUNTS"
            class="profile-update-checkbox-group"
          >
            <div
              v-for="account in profileUpdateFilteredAccounts"
              :key="account.id"
              class="profile-update-account-row"
              :class="{ 'is-disabled': Boolean(profileUpdateEligibilityReason(account)) }"
            >
              <el-checkbox
                :label="account.id"
                :disabled="profileUpdateSubmitting || Boolean(profileUpdateEligibilityReason(account))"
                class="profile-update-checkbox"
              >
                <span class="profile-update-account-name">
                  {{ account.display_name || account.identifier || ('账号 #' + account.id) }}
                </span>
                <small>
                  #{{ account.id }} · {{ account.phone || account.session_name }} ·
                  {{ profileUpdateAccountStatusText(account.status) }} · ad_only
                  <template v-if="profileUpdateEligibilityReason(account)">
                    · {{ profileUpdateEligibilityReason(account) }}
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
      <el-button @click="dialogVisible = false">关闭</el-button>
      <el-button
        v-if="profileUpdateOperationActive"
        type="danger"
        plain
        :loading="profileUpdateCancelling"
        @click="cancelProfileUpdateOperation"
      >
        取消最近任务
      </el-button>
      <el-button
        type="primary"
        :loading="profileUpdateSubmitting"
        :disabled="!profileUpdateCanSubmit"
        @click="submitProfileUpdateOperation"
      >
        排队设置简介
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.profile-update-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.profile-update-operation,
.profile-update-plan {
  padding: 14px;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #fff;
}

.profile-update-section-heading,
.profile-update-picker-toolbar,
.profile-update-items-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.profile-update-section-heading {
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

.profile-update-bio-preview {
  display: flex;
  margin: 0 0 10px;
  padding: 8px 10px;
  border-radius: 6px;
  background: #f5f7fa;
  flex-direction: column;
  gap: 4px;

  span {
    color: #909399;
    font-size: 12px;
  }

  strong {
    overflow-wrap: anywhere;
  }
}

.profile-update-operation-metrics {
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

.profile-update-plan :deep(.el-textarea) {
  margin-bottom: 10px;
}

.profile-update-picker-toolbar {
  margin: 10px 0;
  color: #606266;
  font-size: 13px;
}

.profile-update-scrollbar {
  border: 1px solid #ebeef5;
  border-radius: 6px;
}

.profile-update-checkbox-group {
  display: grid;
  padding: 8px;
  gap: 8px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.profile-update-account-row {
  min-width: 0;
  border: 1px solid #e4e7ed;
  border-radius: 6px;

  &.is-disabled {
    background: #f5f7fa;
  }
}

.profile-update-checkbox {
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

.profile-update-account-name {
  overflow: hidden;
  color: #303133;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.profile-update-items {
  margin-top: 14px;
}

.profile-update-items-title {
  margin-bottom: 8px;
  font-weight: 600;

  small {
    color: #909399;
    font-weight: 400;
  }
}

@media (max-width: 760px) {
  .profile-update-operation-metrics,
  .profile-update-checkbox-group {
    grid-template-columns: 1fr;
  }

  .profile-update-section-heading,
  .profile-update-picker-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
