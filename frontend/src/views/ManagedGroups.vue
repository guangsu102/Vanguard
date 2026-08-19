<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton, ElCheckbox, ElDialog, ElForm, ElFormItem, ElInput, ElInputNumber, ElMessage, ElMessageBox, ElOption, ElSelect, ElTable, ElTableColumn, ElTag } from 'element-plus'
import { guardianApi, type GuardianBot, type ManagedGroupBinding } from '@/api/guardian'
import { accountsApi, type Account } from '@/api/accounts'

const router = useRouter()
const loading = ref(false)
const dialogVisible = ref(false)
const pinnedDialogVisible = ref(false)
const sendingPinned = ref(false)
const savingPinnedConfig = ref(false)
const syncingConfirmed = ref(false)
const channelCreateDialogVisible = ref(false)
const channelMessageDialogVisible = ref(false)
const channelUsernameDialogVisible = ref(false)
const creatingChannel = ref(false)
const sendingChannelMessage = ref(false)
const savingChannelUsername = ref(false)
const refreshingChannelId = ref<number | null>(null)
const mutingBindingId = ref<number | null>(null)
const currentChannel = ref<ManagedGroupBinding | null>(null)
const currentPinnedGroup = ref<ManagedGroupBinding | null>(null)
const announcementChannelId = ref<number | undefined>()
const groups = ref<ManagedGroupBinding[]>([])
const bots = ref<GuardianBot[]>([])
const promoterAccounts = ref<Account[]>([])
const selectedSyncBotId = ref<number | undefined>()

const form = reactive({
  telegram_group_id: 0,
  title: '',
  username: '',
  member_count: 0,
  bot_account_id: undefined as number | undefined,
  binding_status: 'active',
  bot_role: 'admin',
  chat_type: 'group' as 'group' | 'supergroup' | 'channel',
})

const pinnedForm = reactive({
  enabled: true,
  content: '',
  parse_mode: '' as '' | 'Markdown' | 'HTML',
  disable_web_page_preview: false,
  disable_notification: true,
  button_text: null as string | null,
  button_url: null as string | null,
})

const publicChannels = computed(() =>
  groups.value.filter((item) => item.chat_type === 'channel' && Boolean(item.username)),
)

const channelCreateForm = reactive({
  creator_account_id: undefined as number | undefined,
  bot_account_id: undefined as number | undefined,
  title: '',
  about: '',
  visibility: 'public' as 'public' | 'private',
  username: '',
})

const channelMessageForm = reactive({
  content: '',
  parse_mode: '' as '' | 'Markdown' | 'HTML',
  disable_web_page_preview: false,
  disable_notification: false,
})

const channelUsernameForm = reactive({
  username: '',
})

const loadData = async () => {
  loading.value = true
  try {
    const [groupsRes, botsRes, accountsRes] = await Promise.all([
      guardianApi.listManagedGroups({ limit: 100 }),
      guardianApi.listBots({ enabled: true, limit: 100 }),
      accountsApi.list({ account_type: 'promoter', limit: 100 }),
    ])
    groups.value = groupsRes.data.data
    bots.value = botsRes.data.data
    promoterAccounts.value = accountsRes.list.filter((item) => item.is_active && !['error', 'banned'].includes(item.status))
    if (!selectedSyncBotId.value && bots.value.length > 0) {
      selectedSyncBotId.value = bots.value[0].account_id
    }
    if (!channelCreateForm.bot_account_id && bots.value.length > 0) {
      channelCreateForm.bot_account_id = bots.value[0].account_id
    }
    if (!channelCreateForm.creator_account_id && promoterAccounts.value.length > 0) {
      channelCreateForm.creator_account_id = promoterAccounts.value[0].id
    }
  } finally {
    loading.value = false
  }
}

const syncConfirmedGroups = async () => {
  if (!selectedSyncBotId.value) {
    ElMessage.warning('请选择用于同步的 Bot')
    return
  }
  syncingConfirmed.value = true
  try {
    const res = await guardianApi.syncConfirmedGroups({
      bot_account_id: selectedSyncBotId.value,
      statuses: ['active'],
      limit: 500,
    })
    const data = res.data.data
    ElMessage.success(`已检查 ${data.checked} 个群，同步 ${data.synced} 个，跳过 ${data.skipped} 个`)
    await loadData()
  } finally {
    syncingConfirmed.value = false
  }
}

const createBinding = async () => {
  if (!form.telegram_group_id || !form.bot_account_id) {
    ElMessage.warning('请填写群 ID 并选择主 Bot')
    return
  }
  await guardianApi.createManagedGroup({ ...form })
  ElMessage.success('Bot 管理群已绑定')
  dialogVisible.value = false
  Object.assign(form, {
    telegram_group_id: 0,
    title: '',
    username: '',
    member_count: 0,
    bot_account_id: undefined,
    binding_status: 'active',
    bot_role: 'admin',
    chat_type: 'group',
  })
  await loadData()
}

const toggleMuteAll = async (row: ManagedGroupBinding) => {
  const muted = !row.all_members_muted
  await ElMessageBox.confirm(
    muted
      ? '普通成员将无法发送文字、媒体、链接和邀请，管理员仍可发言。'
      : '将恢复执行禁言前保存的群默认权限。',
    muted ? '确认全员禁言' : '确认解除全员禁言',
    { type: muted ? 'warning' : 'info', confirmButtonText: '确认', cancelButtonText: '取消' },
  )
  mutingBindingId.value = row.id
  try {
    await guardianApi.setMuteAll(row.id, muted)
    ElMessage.success(muted ? '已开启全员禁言' : '已恢复成员权限')
    await loadData()
  } finally {
    mutingBindingId.value = null
  }
}

const createChannel = async () => {
  if (!channelCreateForm.creator_account_id || !channelCreateForm.bot_account_id || !channelCreateForm.title.trim()) {
    ElMessage.warning('请选择创建账号、治理 Bot 并填写频道名称')
    return
  }
  const username = channelCreateForm.username.trim().replace(/^@/, '')
  if (channelCreateForm.visibility === 'public' && !/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(username)) {
    ElMessage.warning('公开频道必须填写 5-32 位有效用户名')
    return
  }
  creatingChannel.value = true
  try {
    const res = await guardianApi.createChannel({
      creator_account_id: channelCreateForm.creator_account_id,
      bot_account_id: channelCreateForm.bot_account_id,
      title: channelCreateForm.title.trim(),
      about: channelCreateForm.about.trim(),
      is_public: channelCreateForm.visibility === 'public',
      username: channelCreateForm.visibility === 'public' ? username : undefined,
    })
    const warnings = res.data.data.warnings || []
    if (warnings.length > 0) {
      ElMessage.warning(`频道已创建，但配置未完成：${warnings.join('；')}`)
    } else {
      ElMessage.success(channelCreateForm.visibility === 'public' ? '公开频道已创建' : '私密频道已创建')
    }
    channelCreateDialogVisible.value = false
    Object.assign(channelCreateForm, { title: '', about: '', visibility: 'public', username: '' })
    await loadData()
  } finally {
    creatingChannel.value = false
  }
}

const openChannelMessage = (row: ManagedGroupBinding) => {
  currentChannel.value = row
  Object.assign(channelMessageForm, {
    content: '',
    parse_mode: '',
    disable_web_page_preview: false,
    disable_notification: false,
  })
  channelMessageDialogVisible.value = true
}

const sendChannelMessage = async () => {
  if (!currentChannel.value || !channelMessageForm.content.trim()) {
    ElMessage.warning('请填写频道消息')
    return
  }
  sendingChannelMessage.value = true
  try {
    const res = await guardianApi.sendChannelMessage(currentChannel.value.id, {
      ...channelMessageForm,
      content: channelMessageForm.content.trim(),
    })
    ElMessage.success(`频道消息已发送，消息 ID ${res.data.data.message_id}`)
    channelMessageDialogVisible.value = false
    await loadData()
  } finally {
    sendingChannelMessage.value = false
  }
}

const openChannelUsername = (row: ManagedGroupBinding) => {
  currentChannel.value = row
  channelUsernameForm.username = (row.username || '').replace(/^@/, '')
  channelUsernameDialogVisible.value = true
}

const saveChannelUsername = async () => {
  if (!currentChannel.value) return
  const username = channelUsernameForm.username.trim().replace(/^@/, '')
  if (username && !/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(username)) {
    ElMessage.warning('公开用户名需为 5-32 位字母、数字或下划线，并以字母开头')
    return
  }
  savingChannelUsername.value = true
  try {
    await guardianApi.updateChannelUsername(currentChannel.value.id, username)
    ElMessage.success(username ? `公开用户名已设置为 @${username}` : '频道已改为私有')
    channelUsernameDialogVisible.value = false
    await loadData()
  } finally {
    savingChannelUsername.value = false
  }
}

const makeChannelPrivate = async () => {
  channelUsernameForm.username = ''
  await saveChannelUsername()
}

const refreshChannelStatus = async (row: ManagedGroupBinding) => {
  refreshingChannelId.value = row.id
  try {
    const res = await guardianApi.refreshChannelStatus(row.id)
    ElMessage.success(res.data.data.bot_assignment_complete ? 'Bot 发布权限正常' : 'Bot 尚未取得发布权限')
    await loadData()
  } finally {
    refreshingChannelId.value = null
  }
}

const markDegraded = async (row: ManagedGroupBinding) => {
  await guardianApi.updateManagedGroup(row.id, { binding_status: row.binding_status === 'active' ? 'degraded' : 'active' })
  ElMessage.success('绑定状态已更新')
  await loadData()
}

const openPolicies = (row: ManagedGroupBinding) => {
  router.push({
    path: '/guardian/policies',
    query: {
      groupId: String(row.telegram_group_id),
      title: row.title || row.username || String(row.telegram_group_id),
      botId: String(row.bot_account_id),
    },
  })
}

const openSensitiveKeywords = (row: ManagedGroupBinding) => {
  router.push({
    path: '/guardian/keywords',
    query: {
      groupId: String(row.telegram_group_id),
      title: row.title || row.username || String(row.telegram_group_id),
    },
  })
}

const openGroupCampaigns = (row: ManagedGroupBinding) => {
  router.push({
    path: '/campaigns',
    query: {
      scope: 'managed_group',
      groupId: String(row.telegram_group_id),
      title: row.title || row.username || String(row.telegram_group_id),
      botId: String(row.bot_account_id),
    },
  })
}

const openPeriodicCoupons = (row: ManagedGroupBinding) => {
  router.push({
    path: '/campaigns',
    query: {
      scope: 'managed_group',
      triggerEvent: 'periodic',
      groupId: String(row.telegram_group_id),
      title: row.title || row.username || String(row.telegram_group_id),
      botId: String(row.bot_account_id),
    },
  })
}

const openPinnedMessage = async (row: ManagedGroupBinding) => {
  currentPinnedGroup.value = row
  Object.assign(pinnedForm, {
    enabled: true,
    content: '',
    parse_mode: '',
    disable_web_page_preview: false,
    disable_notification: true,
    button_text: null,
    button_url: null,
  })
  announcementChannelId.value = undefined
  const res = await guardianApi.getPinnedMessageConfig(row.id)
  Object.assign(pinnedForm, res.data.data)
  const configuredChannel = publicChannels.value.find(
    (item) => `https://t.me/${item.username}` === pinnedForm.button_url,
  )
  if (configuredChannel) {
    announcementChannelId.value = configuredChannel.id
  } else if (!pinnedForm.button_url && publicChannels.value.length > 0) {
    applyAnnouncementChannel(publicChannels.value[0].id)
  }
  pinnedDialogVisible.value = true
}

const applyAnnouncementChannel = (channelId?: number) => {
  announcementChannelId.value = channelId
  const channel = publicChannels.value.find((item) => item.id === channelId)
  if (!channel?.username) {
    pinnedForm.button_text = null
    pinnedForm.button_url = null
    return
  }
  pinnedForm.button_text = pinnedForm.button_text || '加入频道'
  pinnedForm.button_url = `https://t.me/${channel.username}`
  if (!pinnedForm.content.trim()) {
    pinnedForm.content = `📢 ${channel.title || channel.username}\n\n产品更新、使用指南和重要公告将在频道发布。`
  }
}

const savePinnedMessageConfig = async () => {
  if (!currentPinnedGroup.value) return
  savingPinnedConfig.value = true
  try {
    await guardianApi.savePinnedMessageConfig(currentPinnedGroup.value.id, { ...pinnedForm })
    ElMessage.success('默认置顶公告配置已保存')
  } finally {
    savingPinnedConfig.value = false
  }
}

const sendPinnedMessage = async () => {
  const content = pinnedForm.content.trim()
  if (!currentPinnedGroup.value || !content) {
    ElMessage.warning('请填写置顶公告内容')
    return
  }

  sendingPinned.value = true
  try {
    await guardianApi.sendPinnedMessage(currentPinnedGroup.value.id, {
      content,
      parse_mode: pinnedForm.parse_mode,
      disable_web_page_preview: pinnedForm.disable_web_page_preview,
      disable_notification: pinnedForm.disable_notification,
      button_text: pinnedForm.button_text,
      button_url: pinnedForm.button_url,
    })
    ElMessage.success('置顶公告已发送')
    pinnedDialogVisible.value = false
  } finally {
    sendingPinned.value = false
  }
}

onMounted(loadData)
</script>

<template>
  <div class="page-shell">
    <div class="page-header">
      <div>
        <h2 class="page-title">Bot管理群与频道</h2>
        <p class="page-desc">统一管理已绑定 Guardian Bot 的群和频道资产。</p>
      </div>
      <div class="header-actions">
        <el-select v-model="selectedSyncBotId" filterable placeholder="选择同步 Bot" class="sync-bot-select">
          <el-option v-for="item in bots" :key="item.account_id" :label="item.display_name || item.identifier" :value="item.account_id" />
        </el-select>
        <el-button :loading="syncingConfirmed" @click="syncConfirmedGroups">同步已确认群</el-button>
        <el-button @click="loadData">刷新</el-button>
        <el-button @click="dialogVisible = true">绑定现有资产</el-button>
        <el-button type="primary" @click="channelCreateDialogVisible = true">创建频道</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="groups" border>
      <el-table-column prop="telegram_group_id" label="Telegram ID" min-width="150" />
      <el-table-column label="类型" width="90">
        <template #default="{ row }">
          <el-tag :type="row.chat_type === 'channel' ? 'primary' : 'success'">
            {{ row.chat_type === 'channel' ? '频道' : '群组' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="title" label="名称" min-width="180" />
      <el-table-column prop="username" label="用户名" min-width="160" />
      <el-table-column prop="bot_identifier" label="主 Bot" min-width="180" />
      <el-table-column prop="bot_role" label="Bot 角色" width="110" />
      <el-table-column prop="binding_status" label="治理状态" width="120">
        <template #default="{ row }">
          <el-tag :type="row.binding_status === 'active' ? 'success' : 'warning'">{{ row.binding_status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" min-width="430">
        <template #default="{ row }">
          <template v-if="row.chat_type === 'channel'">
            <el-button type="primary" link :disabled="row.binding_status !== 'active'" @click="openChannelMessage(row as ManagedGroupBinding)">发送消息</el-button>
            <el-button type="primary" link @click="openChannelUsername(row as ManagedGroupBinding)">设置用户名</el-button>
            <el-button
              type="warning"
              link
              :loading="refreshingChannelId === row.id"
              @click="refreshChannelStatus(row as ManagedGroupBinding)"
            >
              校验 Bot 权限
            </el-button>
          </template>
          <template v-else>
            <el-button
              :type="row.all_members_muted ? 'success' : 'danger'"
              link
              :loading="mutingBindingId === row.id"
              @click="toggleMuteAll(row as ManagedGroupBinding)"
            >
              {{ row.all_members_muted ? '解除全员禁言' : '全员禁言' }}
            </el-button>
            <el-button type="primary" link @click="openPolicies(row as ManagedGroupBinding)">治理策略</el-button>
            <el-button type="success" link @click="openSensitiveKeywords(row as ManagedGroupBinding)">群管敏感词</el-button>
            <el-button type="info" link @click="openGroupCampaigns(row as ManagedGroupBinding)">群内活动</el-button>
            <el-button type="success" link @click="openPeriodicCoupons(row as ManagedGroupBinding)">周期优惠券</el-button>
            <el-button type="warning" link @click="openPinnedMessage(row as ManagedGroupBinding)">群公告</el-button>
          </template>
          <el-button v-if="row.chat_type !== 'channel'" type="primary" link @click="markDegraded(row as ManagedGroupBinding)">
            {{ row.binding_status === 'active' ? '标记降级' : '恢复治理' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" title="绑定现有群或频道" width="560px">
      <el-form label-width="120px">
        <el-form-item label="资产类型">
          <el-select v-model="form.chat_type" style="width: 100%">
            <el-option label="群组" value="group" />
            <el-option label="超级群" value="supergroup" />
            <el-option label="频道" value="channel" />
          </el-select>
        </el-form-item>
        <el-form-item label="Telegram ID">
          <el-input-number v-model="form.telegram_group_id" :precision="0" style="width: 100%" />
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="@public_name" />
        </el-form-item>
        <el-form-item label="成员数">
          <el-input-number v-model="form.member_count" :min="0" :precision="0" style="width: 100%" />
        </el-form-item>
        <el-form-item label="主 Bot">
          <el-select v-model="form.bot_account_id" filterable style="width: 100%">
            <el-option v-for="item in bots" :key="item.account_id" :label="item.display_name || item.identifier" :value="item.account_id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="createBinding">绑定</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="channelCreateDialogVisible" title="创建 Telegram 频道" width="600px">
      <el-form label-width="120px">
        <el-form-item label="创建账号">
          <el-select v-model="channelCreateForm.creator_account_id" filterable style="width: 100%">
            <el-option
              v-for="item in promoterAccounts"
              :key="item.id"
              :label="item.display_name || item.identifier"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="治理 Bot">
          <el-select v-model="channelCreateForm.bot_account_id" filterable style="width: 100%">
            <el-option v-for="item in bots" :key="item.account_id" :label="item.display_name || item.identifier" :value="item.account_id" />
          </el-select>
        </el-form-item>
        <el-form-item label="频道名称">
          <el-input v-model="channelCreateForm.title" maxlength="128" show-word-limit />
        </el-form-item>
        <el-form-item label="频道简介">
          <el-input v-model="channelCreateForm.about" type="textarea" :rows="4" maxlength="255" show-word-limit />
        </el-form-item>
        <el-form-item label="频道类型">
          <el-radio-group v-model="channelCreateForm.visibility">
            <el-radio-button label="public">公开频道</el-radio-button>
            <el-radio-button label="private">私有频道</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="channelCreateForm.visibility === 'public'" label="公开用户名">
          <el-input v-model="channelCreateForm.username" maxlength="32" placeholder="例如 pipenai_official，不含 @">
            <template #prepend>@</template>
          </el-input>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="channelCreateDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creatingChannel" @click="createChannel">创建频道</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="channelUsernameDialogVisible" title="设置频道公开用户名" width="520px">
      <el-form label-width="110px">
        <el-form-item label="目标频道">
          <el-input :model-value="currentChannel?.title || currentChannel?.telegram_group_id" disabled />
        </el-form-item>
        <el-form-item label="公开用户名">
          <el-input v-model="channelUsernameForm.username" maxlength="32" placeholder="例如 pipenai_news，不含 @">
            <template #prepend>@</template>
          </el-input>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="channelUsernameDialogVisible = false">取消</el-button>
        <el-button v-if="currentChannel?.username" :loading="savingChannelUsername" @click="makeChannelPrivate">
          改为私有
        </el-button>
        <el-button type="primary" :loading="savingChannelUsername" @click="saveChannelUsername">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="channelMessageDialogVisible" title="发送频道消息" width="620px">
      <el-form label-width="120px">
        <el-form-item label="目标频道">
          <el-input :model-value="currentChannel?.title || currentChannel?.username || currentChannel?.telegram_group_id" disabled />
        </el-form-item>
        <el-form-item label="消息内容">
          <el-input v-model="channelMessageForm.content" type="textarea" :rows="9" maxlength="4096" show-word-limit />
        </el-form-item>
        <el-form-item label="解析模式">
          <el-select v-model="channelMessageForm.parse_mode" style="width: 100%">
            <el-option label="纯文本" value="" />
            <el-option label="Markdown" value="Markdown" />
            <el-option label="HTML" value="HTML" />
          </el-select>
        </el-form-item>
        <el-form-item label="发送选项">
          <el-checkbox v-model="channelMessageForm.disable_web_page_preview">关闭链接预览</el-checkbox>
          <el-checkbox v-model="channelMessageForm.disable_notification">静默发送</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="channelMessageDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="sendingChannelMessage" @click="sendChannelMessage">发送</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="pinnedDialogVisible" title="Bot 群公告" width="620px">
      <el-form label-width="120px">
        <el-form-item label="目标群">
          <el-input :model-value="currentPinnedGroup?.title || currentPinnedGroup?.username || currentPinnedGroup?.telegram_group_id" disabled />
        </el-form-item>
        <el-form-item label="加入频道">
          <el-select
            v-model="announcementChannelId"
            clearable
            placeholder="不添加频道按钮"
            style="width: 100%"
            @change="applyAnnouncementChannel"
          >
            <el-option
              v-for="channel in publicChannels"
              :key="channel.id"
              :label="`${channel.title || channel.username} (@${channel.username})`"
              :value="channel.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-if="announcementChannelId" label="按钮文字">
          <el-input v-model="pinnedForm.button_text" maxlength="64" />
        </el-form-item>
        <el-form-item label="默认配置">
          <el-checkbox v-model="pinnedForm.enabled">启用默认置顶公告</el-checkbox>
        </el-form-item>
        <el-form-item label="公告内容">
          <el-input
            v-model="pinnedForm.content"
            type="textarea"
            :rows="8"
            maxlength="4096"
            show-word-limit
            placeholder="输入要发送并置顶到群内的公告"
          />
        </el-form-item>
        <el-form-item label="解析模式">
          <el-select v-model="pinnedForm.parse_mode" style="width: 100%">
            <el-option label="纯文本" value="" />
            <el-option label="Markdown" value="Markdown" />
            <el-option label="HTML" value="HTML" />
          </el-select>
        </el-form-item>
        <el-form-item label="发送选项">
          <el-checkbox v-model="pinnedForm.disable_web_page_preview">关闭链接预览</el-checkbox>
          <el-checkbox v-model="pinnedForm.disable_notification">静默置顶</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pinnedDialogVisible = false">取消</el-button>
        <el-button :loading="savingPinnedConfig" @click="savePinnedMessageConfig">保存默认配置</el-button>
        <el-button type="primary" :loading="sendingPinned" @click="sendPinnedMessage">发送并置顶</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.page-shell { display: grid; gap: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.page-title { margin: 0; font-size: 20px; }
.page-desc { margin: 6px 0 0; color: #606266; }
.header-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 12px; }
.sync-bot-select { width: 180px; }

@media (max-width: 900px) {
  .page-header { flex-direction: column; }
  .header-actions { width: 100%; justify-content: flex-start; }
  .sync-bot-select { width: min(100%, 260px); }
}
</style>
