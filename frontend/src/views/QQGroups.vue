<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import dayjs from 'dayjs'
import { Bell, ChatLineSquare, Edit, Plus, Refresh, RefreshLeft } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  qqApi,
  type QQConnectionStatus,
  type QQGroupMessage,
  type QQManagedGroup,
} from '@/api/qq'
import wsClient from '@/utils/websocket'

const loading = ref(false)
const connection = ref<QQConnectionStatus>({
  configured: false,
  provider: 'napcat_onebot11',
  enabled: false,
  status: 'offline',
})
const groups = ref<QQManagedGroup[]>([])
const total = ref(0)

const registerVisible = ref(false)
const registerLoading = ref(false)
const registerForm = reactive({ group_number: '', local_name: '' })

const notificationVisible = ref(false)
const notificationLoading = ref(false)
const notificationGroup = ref<QQManagedGroup | null>(null)
const notificationContent = ref('')

const messagesVisible = ref(false)
const messagesLoading = ref(false)
const currentGroup = ref<QQManagedGroup | null>(null)
const messages = ref<QQGroupMessage[]>([])
const messageTotal = ref(0)
const messageFilters = reactive({ keyword: '', member_qq: '' })

let refreshTimer: ReturnType<typeof setInterval> | null = null

const connectionLabel = computed(() => {
  if (!connection.value.configured) return '未配置'
  if (connection.value.status === 'online') return '在线'
  if (connection.value.status === 'degraded') return '连接降级'
  if (connection.value.status === 'error') return '连接异常'
  return '离线'
})

const connectionTagType = computed(() => {
  if (connection.value.status === 'online') return 'success'
  if (connection.value.status === 'degraded') return 'warning'
  if (connection.value.status === 'error') return 'danger'
  return 'info'
})

const formatTime = (value?: string | null) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-')

const maskOpenid = (value?: string | null) => {
  if (!value) return '-'
  if (value.length <= 16) return value
  return `${value.slice(0, 8)}...${value.slice(-6)}`
}

const attachmentNames = (row: QQGroupMessage) =>
  row.attachments.map((item) => item.filename || item.content_type || '附件').join('、')

const loadData = async () => {
  loading.value = true
  try {
    const [connectionRes, groupsRes] = await Promise.all([
      qqApi.getConnection(),
      qqApi.listGroups({ limit: 500 }),
    ])
    connection.value = connectionRes.data.data
    groups.value = groupsRes.data.data
    total.value = groupsRes.data.total
  } finally {
    loading.value = false
  }
}

const syncGroups = async () => {
  loading.value = true
  try {
    const response = await qqApi.syncGroups()
    ElMessage.success(`已从 NapCat 同步 ${response.data.data.total} 个 QQ 群`)
    await loadData()
  } finally {
    loading.value = false
  }
}

const registerGroup = async () => {
  if (!/^\d{5,20}$/.test(registerForm.group_number.trim())) {
    ElMessage.warning('请输入正确的 QQ 群号')
    return
  }
  registerLoading.value = true
  try {
    await qqApi.createGroup({
      group_number: registerForm.group_number.trim(),
      local_name: registerForm.local_name.trim() || undefined,
    })
    ElMessage.success('QQ 群已登记')
    registerVisible.value = false
    registerForm.group_number = ''
    registerForm.local_name = ''
    await loadData()
  } finally {
    registerLoading.value = false
  }
}

const editGroupName = async (row: QQManagedGroup) => {
  const { value } = await ElMessageBox.prompt('设置管理后台显示名称', '群名称', {
    inputValue: row.local_name || '',
    inputPlaceholder: '输入群名称',
    inputValidator: (text) => !text || text.length <= 255 || '名称不能超过 255 个字符',
  })
  const response = await qqApi.updateGroup(row.id, { local_name: value.trim() || undefined })
  Object.assign(row, response.data.data)
  ElMessage.success('群名称已更新')
}

const updateSwitch = async (
  row: QQManagedGroup,
  field: 'monitoring_enabled' | 'notifications_enabled',
  previous: boolean,
) => {
  try {
    const response = await qqApi.updateGroup(row.id, { [field]: row[field] })
    Object.assign(row, response.data.data)
  } catch (error) {
    row[field] = previous
    throw error
  }
}

const toggleGroupStatus = async (row: QQManagedGroup) => {
  const next = row.status === 'active' ? 'inactive' : 'active'
  await ElMessageBox.confirm(
    next === 'inactive' ? '将停止该群的监控与通知命令。' : '将恢复该群的治理功能。',
    next === 'inactive' ? '停用群治理' : '恢复群治理',
    { type: next === 'inactive' ? 'warning' : 'info' },
  )
  const response = await qqApi.updateGroup(row.id, { status: next })
  Object.assign(row, response.data.data)
  ElMessage.success(next === 'inactive' ? '群治理已停用' : '群治理已恢复')
}

const openNotification = (row: QQManagedGroup) => {
  notificationGroup.value = row
  notificationContent.value = ''
  notificationVisible.value = true
}

const sendNotification = async () => {
  if (!notificationGroup.value || !notificationContent.value.trim()) {
    ElMessage.warning('请输入通知内容')
    return
  }
  notificationLoading.value = true
  try {
    await qqApi.sendNotification(notificationGroup.value.id, notificationContent.value.trim())
    ElMessage.success('群通知已进入发送队列')
    notificationVisible.value = false
  } finally {
    notificationLoading.value = false
  }
}

const loadMessages = async () => {
  if (!currentGroup.value) return
  messagesLoading.value = true
  try {
    const response = await qqApi.listMessages(currentGroup.value.id, {
      keyword: messageFilters.keyword.trim() || undefined,
      member_qq: messageFilters.member_qq.trim() || undefined,
      limit: 200,
    })
    messages.value = response.data.data
    messageTotal.value = response.data.total
  } finally {
    messagesLoading.value = false
  }
}

const openMessages = async (row: QQManagedGroup) => {
  currentGroup.value = row
  messagesVisible.value = true
  messageFilters.keyword = ''
  messageFilters.member_qq = ''
  await loadMessages()
}

const recallMessage = async (row: QQGroupMessage) => {
  await ElMessageBox.confirm('确认通过 NapCat OneBot 撤回该消息？', '撤回消息', {
    type: 'warning',
  })
  await qqApi.recallMessage(row.id)
  row.moderation_status = 'recall_queued'
  ElMessage.success('撤回命令已进入队列')
}

const handleRealtimeMessage = (payload: unknown) => {
  const message = payload as QQGroupMessage
  if (!message || message.group_id !== currentGroup.value?.id) return
  if (messages.value.some((item) => item.id === message.id)) return
  messages.value.unshift(message)
  messageTotal.value += 1
}

const handleRealtimeCommand = (payload: unknown) => {
  const command = payload as { group_id?: number; command_type?: string; status?: string }
  if (
    messagesVisible.value
    && command.group_id === currentGroup.value?.id
    && command.command_type === 'recall'
    && ['succeeded', 'failed', 'unknown'].includes(command.status || '')
  ) {
    loadMessages()
  }
}

const setupRealtime = async () => {
  wsClient.on('qq:message', handleRealtimeMessage)
  wsClient.on('qq:command', handleRealtimeCommand)
  if (!wsClient.isConnected()) {
    const token = localStorage.getItem('token')
    if (!token) return
    await wsClient.connect(import.meta.env.VITE_API_BASE_URL || '/api', Date.now(), token)
  }
  wsClient.subscribe('qq:messages')
  wsClient.subscribe('qq:groups')
}

onMounted(async () => {
  await loadData()
  setupRealtime().catch(() => undefined)
  refreshTimer = setInterval(loadData, 30000)
})

onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  wsClient.unsubscribe('qq:messages')
  wsClient.unsubscribe('qq:groups')
  wsClient.off('qq:message', handleRealtimeMessage)
  wsClient.off('qq:command', handleRealtimeCommand)
})
</script>

<template>
  <div class="qq-page">
    <header class="page-header">
      <div>
        <h1>NapCat QQ 群</h1>
        <div class="connection-line">
          <el-tag :type="connectionTagType" effect="plain">{{ connectionLabel }}</el-tag>
          <span>QQ {{ connection.account_id || '-' }}</span>
          <span>最近心跳 {{ formatTime(connection.last_heartbeat_at) }}</span>
          <span v-if="connection.last_error" class="connection-error">{{ connection.last_error }}</span>
        </div>
      </div>
      <div class="header-actions">
        <el-button :icon="Refresh" :loading="loading" @click="syncGroups">同步群列表</el-button>
        <el-button :icon="Plus" @click="registerVisible = true">手动登记</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="loadData">刷新</el-button>
      </div>
    </header>

    <section class="group-section">
      <div class="section-bar">
        <strong>管理群</strong>
        <span>{{ total }}</span>
      </div>
      <el-table v-loading="loading" :data="groups" row-key="id">
        <el-table-column label="群名称" min-width="180">
          <template #default="{ row }">
            <div class="group-name">
              <span>{{ row.local_name || '未命名 QQ 群' }}</span>
              <el-button :icon="Edit" link circle title="编辑群名称" @click="editGroupName(row as QQManagedGroup)" />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="QQ群号" min-width="150">
          <template #default="{ row }">
            <span class="mono">{{ row.group_number }}</span>
          </template>
        </el-table-column>
        <el-table-column label="消息监控" width="100">
          <template #default="{ row }">
            <el-switch
              v-model="row.monitoring_enabled"
              :disabled="row.status !== 'active'"
              @change="updateSwitch(row as QQManagedGroup, 'monitoring_enabled', !row.monitoring_enabled)"
            />
          </template>
        </el-table-column>
        <el-table-column label="群通知" width="100">
          <template #default="{ row }">
            <el-switch
              v-model="row.notifications_enabled"
              :disabled="row.status !== 'active'"
              @change="updateSwitch(row as QQManagedGroup, 'notifications_enabled', !row.notifications_enabled)"
            />
          </template>
        </el-table-column>
        <el-table-column label="最近消息" min-width="170">
          <template #default="{ row }">{{ formatTime(row.last_message_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'info'" effect="plain">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" fixed="right" min-width="245">
          <template #default="{ row }">
            <el-button :icon="ChatLineSquare" link @click="openMessages(row as QQManagedGroup)">消息</el-button>
            <el-button
              :icon="Bell"
              type="primary"
              link
              :disabled="row.status !== 'active' || !row.notifications_enabled"
              @click="openNotification(row as QQManagedGroup)"
            >
              群通知
            </el-button>
            <el-button type="warning" link @click="toggleGroupStatus(row as QQManagedGroup)">
              {{ row.status === 'active' ? '停用治理' : '恢复治理' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-dialog v-model="registerVisible" title="登记 QQ 群" width="520px">
      <el-form label-width="110px">
        <el-form-item label="QQ群号" required>
          <el-input v-model="registerForm.group_number" maxlength="20" />
        </el-form-item>
        <el-form-item label="群名称">
          <el-input v-model="registerForm.local_name" maxlength="255" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="registerVisible = false">取消</el-button>
        <el-button type="primary" :loading="registerLoading" @click="registerGroup">登记</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="notificationVisible" title="发送群通知" width="620px">
      <el-form label-width="90px">
        <el-form-item label="目标群">
          <el-input :model-value="notificationGroup?.local_name || notificationGroup?.group_number" disabled />
        </el-form-item>
        <el-form-item label="通知内容" required>
          <el-input
            v-model="notificationContent"
            type="textarea"
            :rows="8"
            maxlength="4000"
            show-word-limit
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="notificationVisible = false">取消</el-button>
        <el-button type="primary" :loading="notificationLoading" @click="sendNotification">发送</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="messagesVisible" :title="currentGroup?.local_name || '群消息'" size="78%">
      <div class="message-toolbar">
        <el-input v-model="messageFilters.keyword" clearable placeholder="消息关键词" @keyup.enter="loadMessages" />
        <el-input v-model="messageFilters.member_qq" clearable placeholder="成员 QQ" @keyup.enter="loadMessages" />
        <el-button :icon="Refresh" :loading="messagesLoading" @click="loadMessages">查询</el-button>
        <span class="message-total">{{ messageTotal }} 条</span>
      </div>
      <el-table v-loading="messagesLoading" :data="messages" row-key="id" height="calc(100vh - 190px)">
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatTime(row.occurred_at) }}</template>
        </el-table-column>
        <el-table-column label="成员" min-width="165">
          <template #default="{ row }">
            <el-tooltip :content="row.member_qq || '-'">
              <span class="mono">{{ maskOpenid(row.member_qq) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column prop="member_role" label="角色" width="85" />
        <el-table-column label="消息" min-width="320">
          <template #default="{ row }">
            <div class="message-content">{{ row.content || '-' }}</div>
            <div v-if="row.attachments.length" class="attachments">
              {{ attachmentNames(row as QQGroupMessage) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.is_at_account" effect="plain">@账号</el-tag>
            <span v-else>普通</span>
          </template>
        </el-table-column>
        <el-table-column label="处置" width="105">
          <template #default="{ row }">
            <el-tag v-if="row.recalled_at" type="info" effect="plain">已撤回</el-tag>
            <el-tag v-else-if="row.moderation_status === 'recall_queued'" type="warning" effect="plain">撤回中</el-tag>
            <el-button
              v-else
              :icon="RefreshLeft"
              type="danger"
              link
              title="撤回消息"
              @click="recallMessage(row as QQGroupMessage)"
            >
              撤回
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
</template>

<style scoped lang="scss">
.qq-page {
  display: grid;
  gap: 18px;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid #dcdfe6;
}

h1 {
  margin: 0;
  font-size: 22px;
  line-height: 1.3;
}

.connection-line,
.header-actions,
.section-bar,
.group-name,
.message-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
}

.connection-line {
  flex-wrap: wrap;
  margin-top: 10px;
  color: #606266;
  font-size: 13px;
}

.connection-error {
  color: #c45656;
  max-width: 520px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.group-section {
  background: #fff;
  border-top: 1px solid #dcdfe6;
  border-bottom: 1px solid #dcdfe6;
}

.section-bar {
  min-height: 48px;
  padding: 0 14px;
  border-bottom: 1px solid #ebeef5;
}

.section-bar span,
.message-total {
  color: #909399;
  font-size: 13px;
}

.group-name {
  justify-content: space-between;
}

.mono {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
}

.message-toolbar {
  margin-bottom: 14px;
}

.message-toolbar .el-input {
  width: 230px;
}

.message-total {
  margin-left: auto;
}

.message-content {
  white-space: pre-wrap;
  word-break: break-word;
}

.attachments {
  margin-top: 5px;
  color: #909399;
  font-size: 12px;
}

@media (max-width: 900px) {
  .page-header,
  .message-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .header-actions {
    justify-content: flex-end;
  }

  .message-toolbar .el-input {
    width: 100%;
  }

  .message-total {
    margin-left: 0;
  }
}
</style>
