<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElButton, ElDialog, ElForm, ElFormItem, ElInput, ElMessage, ElSwitch, ElTable, ElTableColumn, ElTag } from 'element-plus'
import {
  guardianApi,
  managedBotProvisionErrorMessage,
  type GuardianBot,
  type ManagedBotProvisionCapability,
  type ManagedBotProvisionOperation,
} from '@/api/guardian'
import ClientListPagination from '@/components/ClientListPagination.vue'
import { useAuthStore } from '@/stores/auth'
import { useClientPagination } from '@/utils/clientPagination'
import { useRoute, useRouter } from 'vue-router'
import { parseSafePositiveId } from '@/utils/groupOpsAccess'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const loading = ref(false)
const dialogVisible = ref(false)
const importingBot = ref(false)
const bots = ref<GuardianBot[]>([])
const botPagination = useClientPagination(bots)
const focusedGuardianBotId = ref<number | null>(null)
const focusedGuardianBot = ref<GuardianBot | null>(null)
const focusedGuardianBotError = ref('')
const returnAssetId = computed(() => parseSafePositiveId(route.query.assetId))
const isAdmin = computed(() => authStore.userInfo?.role === 'admin')
let locationSequence = 0
let locationController: AbortController | null = null
const guardianRowClassName = ({ row }: { row: GuardianBot }) => row.id === focusedGuardianBotId.value ? 'focused-guardian-bot' : ''

const form = reactive({
  identifier: '',
  display_name: '',
  bot_token: '',
  bot_username: '',
  enabled: true,
})

const managedDialogVisible = ref(false)
const managedCapabilityLoading = ref(false)
const managedSubmitting = ref(false)
const managedCapability = ref<ManagedBotProvisionCapability | null>(null)
const managedOperation = ref<ManagedBotProvisionOperation | null>(null)
const managedError = ref('')
const managedForm = reactive<{
  owner_account_id?: number
  manager_bot_profile_id?: number
  display_name: string
  username: string
}>({
  owner_account_id: undefined,
  manager_bot_profile_id: undefined,
  display_name: '',
  username: '',
})
let managedPollTimer: ReturnType<typeof setTimeout> | null = null

const terminalManagedStatuses = new Set([
  'succeeded',
  'failed',
  'needs_attention',
  'partial_failed',
  'cancelled',
])

const activeManagedOperation = computed(
  () =>
    Boolean(managedOperation.value) &&
    !terminalManagedStatuses.has(managedOperation.value?.status || ''),
)

const managedStatusLabel = computed(() => {
  const status = managedOperation.value?.status || ''
  return (
    {
      queued: '已排队',
      running: '创建中',
      retry_wait: '等待重试',
      succeeded: '创建成功',
      failed: '创建失败',
      needs_attention: '需要人工确认',
      partial_failed: '部分失败',
      cancelled: '已取消',
    }[status] || '处理中'
  )
})

const managedStepLabel = computed(() => {
  const step = managedOperation.value?.step || ''
  return (
    {
      queued: '等待执行',
      preflight: '检查账号与 Manager Bot 能力',
      checking_capability: '检查账号与 Manager Bot 能力',
      check_username: '检查用户名',
      checking_username: '检查用户名',
      create_bot: '向 Telegram 创建 Bot',
      creating_bot: '向 Telegram 创建 Bot',
      fetch_token: '安全获取凭证',
      obtaining_token: '安全获取凭证',
      register_profiles: '保存 Bot 配置',
      persisting_profile: '保存 Bot 配置',
      verify: '验证创建结果',
      complete: '已完成',
      completed: '已完成',
    }[step] || (step ? '正在处理' : '')
  )
})

const managedCapabilityMessage = computed(() => {
  const capability = managedCapability.value
  if (!capability || capability.available) return ''
  const blockers = new Set(capability.blockers)
  if (!capability.supported || blockers.has('unsupported')) {
    return '当前后端或 Telegram 客户端尚不支持一键创建 Bot'
  }
  if (
    blockers.has('manager_permission_missing') ||
    blockers.has('manager_bot_unavailable') ||
    capability.manager_bot_profiles.length === 0
  ) {
    return '没有已开启 Bot Management Mode 的 Manager Bot'
  }
  if (
    blockers.has('owner_account_unavailable') ||
    capability.owner_accounts.length === 0
  ) {
    return '没有可执行创建操作的用户账号'
  }
  return '当前条件不满足，暂时无法一键创建 Bot'
})

const selectableManagers = computed(
  () =>
    managedCapability.value?.manager_bot_profiles.filter(
      (item) => item.enabled && item.can_manage_bots,
    ) || [],
)

const loadBots = async () => {
  loading.value = true
  try {
    const res = await guardianApi.listBots({ limit: 200 })
    bots.value = res.data.data
    await locateFromRoute()
  } finally {
    loading.value = false
  }
}

const clearLocation = () => {
  locationSequence += 1
  locationController?.abort()
  locationController = null
  focusedGuardianBotId.value = null
  focusedGuardianBot.value = null
  focusedGuardianBotError.value = ''
}

const locateFromRoute = async () => {
  clearLocation()
  const profileId = parseSafePositiveId(route.query.profileId)
  if (!profileId) return
  const sequence = locationSequence
  const index = bots.value.findIndex((item) => item.id === profileId)
  if (index >= 0) {
    focusedGuardianBotId.value = profileId
    botPagination.page.value = Math.floor(index / botPagination.pageSize.value) + 1
    await nextTick()
    document.querySelector('.focused-guardian-bot')?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    return
  }
  const controller = new AbortController()
  locationController = controller
  try {
    const result = await guardianApi.getBot(profileId, controller.signal)
    if (sequence !== locationSequence || parseSafePositiveId(route.query.profileId) !== profileId) return
    focusedGuardianBot.value = result
  } catch (error) {
    const code = (error as { code?: string })?.code
    if (code !== 'ERR_CANCELED' && sequence === locationSequence) focusedGuardianBotError.value = 'Bot 配置不存在或无权限'
  } finally {
    if (sequence === locationSequence) locationController = null
  }
}

const resetManualForm = () => {
  Object.assign(form, {
    identifier: '',
    display_name: '',
    bot_token: '',
    bot_username: '',
    enabled: true,
  })
}

const createBot = async () => {
  if (!isAdmin.value) return
  if (!form.identifier || !form.bot_token) {
    ElMessage.warning('请填写 Bot 标识和 Token')
    return
  }
  importingBot.value = true
  try {
    await guardianApi.createBot({ ...form })
    ElMessage.success('现有 Bot 已导入')
    dialogVisible.value = false
    resetManualForm()
    await loadBots()
  } catch {
    form.bot_token = ''
    ElMessage.error('导入现有 Bot 失败，请检查配置后重试')
  } finally {
    importingBot.value = false
  }
}

const toggleBot = async (row: GuardianBot) => {
  if (!isAdmin.value) return
  await guardianApi.updateBot(row.id, { enabled: !row.enabled, is_active: !row.is_active })
  ElMessage.success('状态已更新')
  await loadBots()
}

const safeCapability = (
  capability: ManagedBotProvisionCapability,
): ManagedBotProvisionCapability => ({
  supported: Boolean(capability.supported),
  available: Boolean(capability.available),
  owner_accounts: capability.owner_accounts.map((item) => ({
    account_id: item.account_id,
    identifier: item.identifier,
    display_name: item.display_name,
    status: item.status,
  })),
  manager_bot_profiles: capability.manager_bot_profiles.map((item) => ({
    profile_id: item.profile_id,
    account_id: item.account_id,
    bot_user_id: item.bot_user_id,
    bot_username: item.bot_username,
    display_name: item.display_name,
    can_manage_bots: Boolean(item.can_manage_bots),
    enabled: Boolean(item.enabled),
  })),
  blockers: capability.blockers.map((item) =>
    [
      'unsupported',
      'manager_permission_missing',
      'manager_bot_unavailable',
      'owner_account_unavailable',
      'capability_unavailable',
    ].includes(item)
      ? item
      : 'capability_unavailable',
  ),
})

const safeManagedOperation = (
  operation: ManagedBotProvisionOperation,
): ManagedBotProvisionOperation => ({
  id: Number(operation.id),
  status: String(operation.status || 'queued').toLowerCase(),
  step: operation.step,
  owner_account_id: operation.owner_account_id,
  manager_bot_profile_id: operation.manager_bot_profile_id,
  display_name: operation.display_name,
  username: operation.username,
  bot_user_id: operation.bot_user_id,
  guardian_bot_profile_id: operation.guardian_bot_profile_id,
  owned_bot_profile_id: operation.owned_bot_profile_id,
  error_code: operation.error_code,
  retry_at: operation.retry_at,
  created_at: operation.created_at,
  started_at: operation.started_at,
  finished_at: operation.finished_at,
})

const loadManagedCapability = async () => {
  managedCapabilityLoading.value = true
  managedError.value = ''
  try {
    const capability = safeCapability(
      await guardianApi.getManagedProvisionCapability(),
    )
    managedCapability.value = capability
    if (
      !capability.owner_accounts.some(
        (item) => item.account_id === managedForm.owner_account_id,
      )
    ) {
      managedForm.owner_account_id = capability.owner_accounts[0]?.account_id
    }
    const eligibleManagers = capability.manager_bot_profiles.filter(
      (item) => item.enabled && item.can_manage_bots,
    )
    if (
      !eligibleManagers.some(
        (item) => item.profile_id === managedForm.manager_bot_profile_id,
      )
    ) {
      managedForm.manager_bot_profile_id = eligibleManagers[0]?.profile_id
    }
  } catch {
    managedCapability.value = null
    managedError.value = '无法读取一键创建能力，请稍后重试'
  } finally {
    managedCapabilityLoading.value = false
  }
}

const stopManagedPolling = () => {
  if (managedPollTimer) clearTimeout(managedPollTimer)
  managedPollTimer = null
}

const applyManagedOperation = async (
  operation: ManagedBotProvisionOperation,
) => {
  const previousStatus = managedOperation.value?.status
  const safeOperation = safeManagedOperation(operation)
  managedOperation.value = safeOperation
  if (!terminalManagedStatuses.has(safeOperation.status)) return

  stopManagedPolling()
  if (safeOperation.status === 'succeeded') {
    managedError.value = ''
    if (previousStatus !== 'succeeded') {
      ElMessage.success('Bot 创建成功，列表已刷新')
      await loadBots()
    }
    return
  }
  managedError.value = managedBotProvisionErrorMessage(
    safeOperation.error_code || safeOperation.status,
  )
}

const refreshManagedOperation = async () => {
  const operationId = managedOperation.value?.id
  if (!operationId) {
    stopManagedPolling()
    managedError.value = '任务编号无效，请重新发起'
    return
  }
  try {
    await applyManagedOperation(
      await guardianApi.getManagedProvision(operationId),
    )
  } catch {
    stopManagedPolling()
    managedError.value = '无法读取创建进度，请稍后重新打开页面查看'
  }
}

const scheduleManagedPoll = () => {
  stopManagedPolling()
  managedPollTimer = setTimeout(async () => {
    await refreshManagedOperation()
    if (activeManagedOperation.value) scheduleManagedPoll()
  }, 2000)
}

const resetManagedForm = () => {
  Object.assign(managedForm, {
    owner_account_id: undefined,
    manager_bot_profile_id: undefined,
    display_name: '',
    username: '',
  })
  managedOperation.value = null
  managedError.value = ''
}

const openManagedCreation = async () => {
  if (!isAdmin.value) return
  if (activeManagedOperation.value) {
    managedDialogVisible.value = true
    if (!managedPollTimer) scheduleManagedPoll()
    return
  }
  stopManagedPolling()
  resetManagedForm()
  managedDialogVisible.value = true
  await loadManagedCapability()
}

const validateManagedForm = (): string | null => {
  const displayName = managedForm.display_name.trim()
  const username = managedForm.username.trim()
  if (!managedForm.owner_account_id) return '请选择用户账号'
  if (!managedForm.manager_bot_profile_id) return '请选择 Manager Bot'
  if (displayName.length < 1 || displayName.length > 64) {
    return '展示名称长度必须为 1–64 个字符'
  }
  if (!/^[A-Za-z0-9_]{5,32}$/.test(username) || !/bot$/i.test(username)) {
    return 'Bot 用户名须为 5–32 位 ASCII 字母、数字或下划线，并以 bot 结尾'
  }
  return null
}

const submitManagedProvision = async () => {
  const validationError = validateManagedForm()
  if (validationError) {
    managedError.value = validationError
    return
  }
  managedSubmitting.value = true
  managedError.value = ''
  try {
    const operation = await guardianApi.createManagedProvision({
      owner_account_id: managedForm.owner_account_id as number,
      manager_bot_profile_id: managedForm.manager_bot_profile_id as number,
      display_name: managedForm.display_name.trim(),
      username: managedForm.username.trim(),
    })
    await applyManagedOperation(operation)
    if (activeManagedOperation.value) scheduleManagedPoll()
  } catch (error) {
    managedError.value = managedBotProvisionErrorMessage(error)
  } finally {
    managedSubmitting.value = false
  }
}

const closeManagedDialog = (done: () => void) => {
  if (activeManagedOperation.value) {
    ElMessage.info('创建任务将在后台继续，完成后会自动刷新 Bot 列表')
  }
  done()
}

const hideManagedDialog = () => {
  if (activeManagedOperation.value) {
    ElMessage.info('创建任务将在后台继续，完成后会自动刷新 Bot 列表')
  }
  managedDialogVisible.value = false
}

onMounted(loadBots)
watch(() => [route.query.profileId, route.query.assetId], () => { void locateFromRoute() })
onBeforeUnmount(() => {
  clearLocation()
  stopManagedPolling()
  form.bot_token = ''
})
</script>

<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h2 class="page-title">Bot账号</h2>
        <p class="page-desc">纯 Telegram Bot，用于管理群验证、处罚、广播和群内活动。</p>
      </div>
      <div class="header-actions">
        <el-button
          v-if="returnAssetId"
          @click="
            router.push(`/owned-groups/${returnAssetId}/operations?tab=members`)
          "
        >
          返回群运营中心
        </el-button>
        <el-button @click="loadBots">刷新</el-button>
        <el-button v-if="isAdmin" data-testid="import-existing-bot" @click="dialogVisible = true">
          导入现有 Bot
        </el-button>
        <el-button v-if="isAdmin" type="primary" @click="openManagedCreation">
          一键创建 Bot
        </el-button>
      </div>
    </div>

    <el-alert v-if="focusedGuardianBotError" type="warning" :closable="false" show-icon :title="focusedGuardianBotError" />
    <el-card v-if="focusedGuardianBot" shadow="never">
      <template #header><strong>定位结果（不影响当前分页）</strong></template>
      <el-descriptions :column="4" border><el-descriptions-item label="Profile ID">#{{ focusedGuardianBot.id }}</el-descriptions-item><el-descriptions-item label="名称">{{ focusedGuardianBot.display_name || `Bot #${focusedGuardianBot.id}` }}</el-descriptions-item><el-descriptions-item label="健康">{{ focusedGuardianBot.health_status }}</el-descriptions-item><el-descriptions-item label="同步">{{ focusedGuardianBot.sync_status }}</el-descriptions-item></el-descriptions>
    </el-card>

    <el-table v-loading="loading" :data="botPagination.rows.value" :row-class-name="guardianRowClassName" border>
      <el-table-column prop="identifier" label="标识" min-width="180" />
      <el-table-column prop="display_name" label="名称" min-width="160" />
      <el-table-column prop="bot_username" label="Bot 用户名" min-width="160" />
      <el-table-column prop="status" label="账号状态" width="120">
        <template #default="{ row }">
          <el-tag>{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="health_status" label="健康状态" width="120">
        <template #default="{ row }">
          <el-tag :type="row.health_status === 'healthy' ? 'success' : 'info'">{{ row.health_status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="sync_status" label="同步状态" width="120" />
      <el-table-column label="启用" width="100">
        <template #default="{ row }">
          <el-switch
            data-testid="guardian-enabled-switch"
            :model-value="row.enabled"
            :disabled="!isAdmin"
            @change="toggleBot(row as GuardianBot)"
          />
        </template>
      </el-table-column>
    </el-table>
    <ClientListPagination
      v-model:page="botPagination.page.value"
      v-model:page-size="botPagination.pageSize.value"
      :total="botPagination.total.value"
    />

    <el-dialog
      v-model="dialogVisible"
      title="导入现有 Bot"
      width="520px"
      @closed="resetManualForm"
    >
      <el-form label-width="110px">
        <el-form-item label="标识">
          <el-input v-model="form.identifier" placeholder="@guardian_bot_a" />
        </el-form-item>
        <el-form-item label="展示名称">
          <el-input v-model="form.display_name" placeholder="群管 Bot A" />
        </el-form-item>
        <el-form-item label="Bot Token">
          <el-input v-model="form.bot_token" type="password" show-password />
        </el-form-item>
        <el-form-item label="Bot 用户名">
          <el-input v-model="form.bot_username" placeholder="@guardian_bot_a" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="importingBot" @click="createBot">
          导入
        </el-button>
      </template>
    </el-dialog>
    <el-dialog
      v-model="managedDialogVisible"
      title="一键创建 Bot"
      width="620px"
      :close-on-click-modal="false"
      :before-close="closeManagedDialog"
    >
      <el-alert
        title="系统通过 Telegram Managed Bots 创建并安全保存凭证；页面不会显示 Bot Token。"
        type="info"
        :closable="false"
        show-icon
      />

      <el-alert
        v-if="managedError"
        class="managed-alert"
        :title="managedError"
        type="error"
        :closable="false"
        show-icon
      />

      <el-alert
        v-if="managedCapabilityMessage"
        class="managed-alert"
        :title="managedCapabilityMessage"
        type="warning"
        :closable="false"
        show-icon
      />

      <el-form
        v-loading="managedCapabilityLoading"
        class="managed-form"
        label-width="130px"
      >
        <el-form-item label="用户账号" required>
          <el-select
            v-model="managedForm.owner_account_id"
            placeholder="选择执行创建的用户账号"
            filterable
            style="width: 100%"
          >
            <el-option
              v-for="item in managedCapability?.owner_accounts || []"
              :key="item.account_id"
              :label="
                item.display_name ||
                item.identifier ||
                `账号 #${item.account_id}`
              "
              :value="item.account_id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="Manager Bot" required>
          <el-select
            v-model="managedForm.manager_bot_profile_id"
            placeholder="选择具备管理能力的 Bot"
            filterable
            style="width: 100%"
          >
            <el-option
              v-for="item in selectableManagers"
              :key="item.profile_id"
              :label="
                item.display_name ||
                item.bot_username ||
                `Bot #${item.profile_id}`
              "
              :value="item.profile_id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="展示名称" required>
          <el-input
            v-model="managedForm.display_name"
            maxlength="64"
            show-word-limit
            placeholder="例如：群管 Bot A"
          />
        </el-form-item>

        <el-form-item label="Bot 用户名" required>
          <el-input
            v-model="managedForm.username"
            maxlength="32"
            placeholder="例如：guardian_matrix_bot"
          />
          <div class="field-tip">
            5–32 位 ASCII 字母、数字或下划线，必须以 bot 结尾，不含 @
          </div>
        </el-form-item>
      </el-form>

      <el-card v-if="managedOperation" class="operation-card" shadow="never">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="任务编号">
            #{{ managedOperation.id }}
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            {{ managedStatusLabel }}
          </el-descriptions-item>
          <el-descriptions-item v-if="managedStepLabel" label="当前步骤">
            {{ managedStepLabel }}
          </el-descriptions-item>
          <el-descriptions-item
            v-if="managedOperation.username"
            label="Bot 用户名"
          >
            @{{ managedOperation.username }}
          </el-descriptions-item>
        </el-descriptions>
      </el-card>

      <template #footer>
        <el-button @click="hideManagedDialog">关闭</el-button>
        <el-button
          type="primary"
          :loading="managedSubmitting || activeManagedOperation"
          :disabled="
            managedCapabilityLoading ||
            !managedCapability?.available ||
            activeManagedOperation
          "
          @click="submitManagedProvision"
        >
          创建 Bot
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.page-shell { display: grid; gap: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.page-title { margin: 0; font-size: 20px; }
.page-desc { margin: 6px 0 0; color: #606266; }
.header-actions { display: flex; flex-wrap: wrap; gap: 12px; }
.managed-form {
  margin-top: 18px;
}

.managed-alert,
.operation-card {
  margin-top: 12px;
}

.field-tip {
  margin-top: 4px;
  color: #909399;
  font-size: 12px;
  line-height: 1.5;
}
</style>
