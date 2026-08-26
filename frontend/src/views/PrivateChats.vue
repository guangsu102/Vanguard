<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import dayjs from 'dayjs'
import { ElMessage } from 'element-plus'
import {
  ArrowLeft,
  Check,
  ChatDotRound,
  CircleClose,
  Position,
  Refresh,
  Search,
  Switch,
  User,
} from '@element-plus/icons-vue'
import {
  privateChatsApi,
  type PrivateChatConversation,
  type PrivateChatMessage,
  type PrivateChatStatus,
} from '@/api/privateChats'
import wsClient from '@/utils/websocket'

const REALTIME_CHANNEL = 'telegram:private-chats'

const conversations = ref<PrivateChatConversation[]>([])
const conversationTotal = ref(0)
const currentConversation = ref<PrivateChatConversation | null>(null)
const messages = ref<PrivateChatMessage[]>([])
const messageTotal = ref(0)
const conversationsLoading = ref(false)
const messagesLoading = ref(false)
const sending = ref(false)
const draft = ref('')
const messagesViewport = ref<HTMLElement | null>(null)
const isMobile = ref(false)
const mobileChatOpen = ref(false)

const filters = reactive<{
  status: PrivateChatStatus | ''
  accountId?: number
  unreadOnly: boolean
  keyword: string
}>({
  status: 'open',
  accountId: undefined,
  unreadOnly: false,
  keyword: '',
})

let syncTimer: ReturnType<typeof setInterval> | null = null

const accountOptions = computed(() => {
  const options = new Map<number, string>()
  conversations.value.forEach((item) => {
    options.set(item.account_id, item.account_name)
  })
  return Array.from(options, ([value, label]) => ({ value, label }))
})

const currentPeerName = computed(() => {
  const conversation = currentConversation.value
  if (!conversation) return ''
  return (
    conversation.peer_display_name
    || (conversation.peer_username ? `@${conversation.peer_username}` : '')
    || String(conversation.peer_telegram_id)
  )
})

const currentPeerInitial = computed(() => currentPeerName.value.slice(0, 1).toUpperCase() || '?')

const formatTime = (value?: string | null) => {
  if (!value) return '-'
  const date = dayjs(value)
  return date.isSame(dayjs(), 'day') ? date.format('HH:mm') : date.format('MM-DD HH:mm')
}

const fullTime = (value?: string | null) => {
  if (!value) return '-'
  return dayjs(value).format('YYYY-MM-DD HH:mm:ss')
}

const conversationName = (conversation: PrivateChatConversation) => (
  conversation.peer_display_name
  || (conversation.peer_username ? `@${conversation.peer_username}` : '')
  || String(conversation.peer_telegram_id)
)

const conversationInitial = (conversation: PrivateChatConversation) => (
  conversationName(conversation).slice(0, 1).toUpperCase() || '?'
)

const messageContent = (message: PrivateChatMessage) => {
  if (message.content?.trim()) return message.content
  const labels: Record<string, string> = {
    photo: '[图片]',
    video: '[视频]',
    voice: '[语音]',
    audio: '[音频]',
    document: '[文件]',
    sticker: '[贴纸]',
  }
  return labels[message.message_type] || `[${message.message_type}]`
}

const messageStatus = (message: PrivateChatMessage) => {
  if (message.direction !== 'outbound') return ''
  const labels: Record<string, string> = {
    pending: '等待发送',
    sending: '发送中',
    sent: '已发送',
    failed: '发送失败',
    unknown: '状态未知',
  }
  return labels[message.status] || message.status
}

const scrollToBottom = async () => {
  await nextTick()
  if (messagesViewport.value) {
    messagesViewport.value.scrollTop = messagesViewport.value.scrollHeight
  }
}

const matchesConversationFilters = (conversation: PrivateChatConversation) => {
  if (filters.status && conversation.status !== filters.status) return false
  if (filters.accountId && conversation.account_id !== filters.accountId) return false
  if (filters.unreadOnly && conversation.unread_count === 0) return false
  const keyword = filters.keyword.trim().toLowerCase()
  if (!keyword) return true
  return [
    conversation.peer_display_name,
    conversation.peer_username,
    conversation.last_message_preview,
    String(conversation.peer_telegram_id),
  ].some((value) => value?.toLowerCase().includes(keyword))
}

const replaceConversation = (conversation: PrivateChatConversation) => {
  const index = conversations.value.findIndex((item) => item.id === conversation.id)
  if (index >= 0) {
    conversations.value.splice(index, 1)
  }
  if (matchesConversationFilters(conversation)) {
    conversations.value.unshift(conversation)
    conversations.value.sort((left, right) => {
      const leftTime = left.last_message_at ? dayjs(left.last_message_at).valueOf() : 0
      const rightTime = right.last_message_at ? dayjs(right.last_message_at).valueOf() : 0
      return rightTime - leftTime
    })
  }
  if (currentConversation.value?.id === conversation.id) {
    currentConversation.value = conversation
  }
}

const loadConversations = async (preserveSelection = true) => {
  conversationsLoading.value = true
  try {
    const response = await privateChatsApi.listConversations({
      account_id: filters.accountId,
      status: filters.status || undefined,
      unread_only: filters.unreadOnly || undefined,
      keyword: filters.keyword.trim() || undefined,
      limit: 100,
    })
    conversations.value = response.data.data
    conversationTotal.value = response.data.total

    if (!preserveSelection && conversations.value.length) {
      await openConversation(conversations.value[0])
    } else if (currentConversation.value) {
      const refreshed = conversations.value.find(
        (item) => item.id === currentConversation.value?.id,
      )
      if (refreshed) currentConversation.value = refreshed
    }
  } finally {
    conversationsLoading.value = false
  }
}

const loadMessages = async (conversationId: number) => {
  messagesLoading.value = true
  try {
    const response = await privateChatsApi.listMessages(conversationId, { limit: 100 })
    if (currentConversation.value?.id !== conversationId) return
    messages.value = response.data.data
    messageTotal.value = response.data.total
    await scrollToBottom()
  } finally {
    messagesLoading.value = false
  }
}

const markCurrentRead = async () => {
  const conversation = currentConversation.value
  if (!conversation || conversation.unread_count === 0) return
  conversation.unread_count = 0
  try {
    const response = await privateChatsApi.markRead(conversation.id)
    replaceConversation(response.data.data)
  } catch {
    await loadConversations()
  }
}

const openConversation = async (conversation: PrivateChatConversation) => {
  currentConversation.value = conversation
  mobileChatOpen.value = true
  messages.value = []
  await loadMessages(conversation.id)
  await markCurrentRead()
}

const loadOlderMessages = async () => {
  const conversation = currentConversation.value
  const firstMessage = messages.value[0]
  if (!conversation || !firstMessage || messages.value.length >= messageTotal.value) return

  const viewport = messagesViewport.value
  const previousHeight = viewport?.scrollHeight || 0
  const response = await privateChatsApi.listMessages(conversation.id, {
    before_id: firstMessage.id,
    limit: 50,
  })
  messages.value = [...response.data.data, ...messages.value]
  await nextTick()
  if (viewport) {
    viewport.scrollTop = viewport.scrollHeight - previousHeight
  }
}

const updateHandlingMode = async (handlingMode: 'auto' | 'human') => {
  const conversation = currentConversation.value
  if (!conversation) return
  const response = await privateChatsApi.updateConversation(conversation.id, {
    handling_mode: handlingMode,
  })
  replaceConversation(response.data.data)
  ElMessage.success(handlingMode === 'human' ? '已切换为人工接管' : '已恢复自动处理')
}

const toggleConversationStatus = async () => {
  const conversation = currentConversation.value
  if (!conversation) return
  const nextStatus: PrivateChatStatus = conversation.status === 'open' ? 'closed' : 'open'
  const response = await privateChatsApi.updateConversation(conversation.id, {
    status: nextStatus,
  })
  replaceConversation(response.data.data)
  ElMessage.success(nextStatus === 'closed' ? '会话已关闭' : '会话已重新打开')
}

const createClientRequestId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `private-chat-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

const sendReply = async () => {
  const conversation = currentConversation.value
  const content = draft.value.trim()
  if (!conversation || !content || sending.value) return

  sending.value = true
  try {
    if (conversation.handling_mode !== 'human') {
      const takeover = await privateChatsApi.updateConversation(conversation.id, {
        handling_mode: 'human',
      })
      replaceConversation(takeover.data.data)
    }
    const response = await privateChatsApi.sendMessage(conversation.id, {
      content,
      client_request_id: createClientRequestId(),
    })
    if (!messages.value.some((item) => item.id === response.data.data.id)) {
      messages.value.push(response.data.data)
    }
    draft.value = ''
    await scrollToBottom()
  } finally {
    sending.value = false
  }
}

const retryMessage = async (message: PrivateChatMessage) => {
  if (!message.content) return
  draft.value = message.content
  await sendReply()
}

const handleRealtimeMessage = (payload: unknown) => {
  const message = payload as PrivateChatMessage
  if (!message?.id || !message.conversation_id) return

  if (currentConversation.value?.id === message.conversation_id) {
    const index = messages.value.findIndex((item) => item.id === message.id)
    if (index >= 0) {
      messages.value[index] = message
    } else {
      messages.value.push(message)
      messageTotal.value += 1
    }
    scrollToBottom()
    if (message.direction === 'inbound') {
      markCurrentRead()
    }
  }
}

const handleRealtimeConversation = (payload: unknown) => {
  const conversation = payload as PrivateChatConversation
  if (!conversation?.id) return
  replaceConversation(conversation)
  if (
    !currentConversation.value
    && matchesConversationFilters(conversation)
  ) {
    openConversation(conversation)
  }
}

const handleRealtimeConnected = () => {
  loadConversations().catch(() => undefined)
  if (currentConversation.value) {
    loadMessages(currentConversation.value.id).catch(() => undefined)
  }
}

const setupRealtime = async () => {
  wsClient.on('telegram:private-message', handleRealtimeMessage)
  wsClient.on('telegram:private-message-status', handleRealtimeMessage)
  wsClient.on('telegram:private-conversation', handleRealtimeConversation)
  wsClient.on('connected', handleRealtimeConnected)

  if (!wsClient.isConnected()) {
    const token = localStorage.getItem('token')
    if (!token) return
    await wsClient.connect(
      import.meta.env.VITE_API_BASE_URL || '/api',
      Date.now(),
      token,
    )
  }
  wsClient.subscribe(REALTIME_CHANNEL)
}

const updateViewportMode = () => {
  isMobile.value = window.innerWidth <= 900
  if (!isMobile.value) mobileChatOpen.value = false
}

const refreshAll = async () => {
  await loadConversations()
  if (currentConversation.value) {
    await loadMessages(currentConversation.value.id)
  }
}

onMounted(async () => {
  updateViewportMode()
  window.addEventListener('resize', updateViewportMode)
  await loadConversations(false)
  setupRealtime().catch(() => undefined)
  syncTimer = setInterval(() => {
    loadConversations().catch(() => undefined)
  }, 30000)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', updateViewportMode)
  if (syncTimer) clearInterval(syncTimer)
  wsClient.unsubscribe(REALTIME_CHANNEL)
  wsClient.off('telegram:private-message', handleRealtimeMessage)
  wsClient.off('telegram:private-message-status', handleRealtimeMessage)
  wsClient.off('telegram:private-conversation', handleRealtimeConversation)
  wsClient.off('connected', handleRealtimeConnected)
})
</script>

<template>
  <div class="private-chats-page">
    <header class="page-heading">
      <div>
        <h1>私聊工作台</h1>
        <p>{{ conversationTotal }} 个会话，消息实时同步</p>
      </div>
      <el-tooltip content="刷新会话" placement="bottom">
        <el-button :icon="Refresh" circle :loading="conversationsLoading" @click="refreshAll" />
      </el-tooltip>
    </header>

    <section class="chat-workspace">
      <aside v-show="!isMobile || !mobileChatOpen" class="conversation-panel">
        <div class="conversation-filters">
          <el-input
            v-model="filters.keyword"
            :prefix-icon="Search"
            clearable
            placeholder="搜索用户或消息"
            @keyup.enter="loadConversations(false)"
            @clear="loadConversations(false)"
          />
          <div class="filter-row">
            <el-select
              v-model="filters.status"
              aria-label="会话状态"
              @change="loadConversations(false)"
            >
              <el-option label="处理中" value="open" />
              <el-option label="已关闭" value="closed" />
              <el-option label="全部状态" value="" />
            </el-select>
            <el-select
              v-model="filters.accountId"
              clearable
              aria-label="接收账号"
              placeholder="全部账号"
              @change="loadConversations(false)"
            >
              <el-option
                v-for="option in accountOptions"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
          </div>
          <el-checkbox v-model="filters.unreadOnly" @change="loadConversations(false)">
            只看未读
          </el-checkbox>
        </div>

        <el-scrollbar v-loading="conversationsLoading" class="conversation-scroll">
          <button
            v-for="conversation in conversations"
            :key="conversation.id"
            type="button"
            class="conversation-item"
            :class="{ active: currentConversation?.id === conversation.id }"
            @click="openConversation(conversation)"
          >
            <el-badge :value="conversation.unread_count" :hidden="conversation.unread_count === 0">
              <el-avatar :size="40">{{ conversationInitial(conversation) }}</el-avatar>
            </el-badge>
            <span class="conversation-copy">
              <span class="conversation-line">
                <strong>{{ conversationName(conversation) }}</strong>
                <time>{{ formatTime(conversation.last_message_at) }}</time>
              </span>
              <span class="conversation-line">
                <span class="message-preview">{{ conversation.last_message_preview || '暂无消息' }}</span>
                <el-tag
                  v-if="conversation.handling_mode === 'human'"
                  type="warning"
                  effect="plain"
                  size="small"
                >
                  人工
                </el-tag>
              </span>
              <span class="account-label">{{ conversation.account_name }}</span>
            </span>
          </button>
          <el-empty v-if="!conversationsLoading && conversations.length === 0" description="暂无私聊会话" />
        </el-scrollbar>
      </aside>

      <main v-show="!isMobile || mobileChatOpen" class="chat-panel">
        <template v-if="currentConversation">
          <div class="chat-header">
            <el-button
              v-if="isMobile"
              :icon="ArrowLeft"
              text
              circle
              aria-label="返回会话列表"
              @click="mobileChatOpen = false"
            />
            <el-avatar :size="38">{{ currentPeerInitial }}</el-avatar>
            <div class="chat-identity">
              <strong>{{ currentPeerName }}</strong>
              <span>
                通过 {{ currentConversation.account_name }}
                <i :class="['status-dot', currentConversation.account_status]" />
              </span>
            </div>
            <div class="chat-actions">
              <el-tooltip
                :content="currentConversation.handling_mode === 'human' ? '恢复自动处理' : '人工接管'"
                placement="bottom"
              >
                <el-button
                  :icon="currentConversation.handling_mode === 'human' ? Switch : User"
                  circle
                  @click="updateHandlingMode(currentConversation.handling_mode === 'human' ? 'auto' : 'human')"
                />
              </el-tooltip>
              <el-tooltip
                :content="currentConversation.status === 'open' ? '关闭会话' : '重新打开'"
                placement="bottom"
              >
                <el-button
                  :icon="currentConversation.status === 'open' ? CircleClose : Check"
                  circle
                  @click="toggleConversationStatus"
                />
              </el-tooltip>
            </div>
          </div>

          <div ref="messagesViewport" v-loading="messagesLoading" class="message-stream">
            <button
              v-if="messages.length < messageTotal"
              type="button"
              class="load-older"
              @click="loadOlderMessages"
            >
              查看更早消息
            </button>

            <div
              v-for="message in messages"
              :key="message.id"
              class="message-row"
              :class="message.direction"
            >
              <div class="message-bubble" :class="{ failed: message.status === 'failed' }">
                <span v-if="message.source === 'auto'" class="message-source">自动回复</span>
                <p>{{ messageContent(message) }}</p>
                <span class="message-meta">
                  {{ fullTime(message.occurred_at) }}
                  <template v-if="messageStatus(message)"> · {{ messageStatus(message) }}</template>
                </span>
                <div v-if="message.status === 'failed'" class="message-error">
                  <span>{{ message.error_message || '发送失败' }}</span>
                  <el-button link type="danger" @click="retryMessage(message)">重试</el-button>
                </div>
              </div>
            </div>
            <el-empty v-if="!messagesLoading && messages.length === 0" description="暂无消息" />
          </div>

          <div class="composer">
            <el-input
              v-model="draft"
              type="textarea"
              resize="none"
              :rows="3"
              maxlength="4000"
              show-word-limit
              placeholder="输入回复内容"
              :disabled="currentConversation.status === 'closed'"
              @keydown.enter.exact.prevent="sendReply"
            />
            <el-tooltip content="发送回复" placement="top">
              <el-button
                type="primary"
                :icon="Position"
                circle
                :loading="sending"
                :disabled="!draft.trim() || currentConversation.status === 'closed'"
                @click="sendReply"
              />
            </el-tooltip>
          </div>
        </template>

        <div v-else class="chat-empty">
          <el-icon :size="38"><ChatDotRound /></el-icon>
          <strong>选择一个会话</strong>
        </div>
      </main>

      <aside v-if="!isMobile" class="detail-panel">
        <template v-if="currentConversation">
          <div class="detail-heading">
            <span>会话资料</span>
            <el-tag
              :type="currentConversation.status === 'open' ? 'success' : 'info'"
              effect="plain"
            >
              {{ currentConversation.status === 'open' ? '处理中' : '已关闭' }}
            </el-tag>
          </div>

          <dl class="detail-list">
            <dt>Telegram ID</dt>
            <dd>{{ currentConversation.peer_telegram_id }}</dd>
            <dt>用户名</dt>
            <dd>{{ currentConversation.peer_username ? `@${currentConversation.peer_username}` : '-' }}</dd>
            <dt>接收账号</dt>
            <dd>{{ currentConversation.account_name }}</dd>
            <dt>账号标识</dt>
            <dd>{{ currentConversation.account_identifier || '-' }}</dd>
            <dt>处理方式</dt>
            <dd>
              <el-tag
                :type="currentConversation.handling_mode === 'human' ? 'warning' : 'info'"
                effect="plain"
              >
                {{ currentConversation.handling_mode === 'human' ? '人工接管' : '自动规则' }}
              </el-tag>
            </dd>
            <dt>首次进入</dt>
            <dd>{{ fullTime(currentConversation.created_at) }}</dd>
            <dt>最近入站</dt>
            <dd>{{ fullTime(currentConversation.last_inbound_at) }}</dd>
          </dl>

          <el-button
            v-if="currentConversation.handling_mode === 'auto'"
            :icon="User"
            class="detail-command"
            @click="updateHandlingMode('human')"
          >
            人工接管
          </el-button>
          <el-button
            v-else
            :icon="Switch"
            class="detail-command"
            @click="updateHandlingMode('auto')"
          >
            恢复自动处理
          </el-button>
        </template>
      </aside>
    </section>
  </div>
</template>

<style scoped>
.private-chats-page {
  min-width: 0;
}

.page-heading {
  min-height: 60px;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.page-heading h1 {
  margin: 0;
  color: #1f2933;
  font-size: 22px;
  font-weight: 650;
  letter-spacing: 0;
}

.page-heading p {
  margin: 6px 0 0;
  color: #7a828c;
  font-size: 13px;
}

.chat-workspace {
  height: calc(100vh - 164px);
  min-height: 600px;
  display: grid;
  grid-template-columns: 320px minmax(420px, 1fr) 260px;
  overflow: hidden;
  border: 1px solid #dfe3e8;
  border-radius: 6px;
  background: #ffffff;
}

.conversation-panel,
.detail-panel {
  min-width: 0;
  background: #fafbfc;
}

.conversation-panel {
  display: flex;
  flex-direction: column;
  border-right: 1px solid #e4e7eb;
}

.conversation-filters {
  padding: 14px;
  border-bottom: 1px solid #e4e7eb;
  display: grid;
  gap: 10px;
}

.filter-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.conversation-scroll {
  flex: 1;
}

.conversation-item {
  width: 100%;
  min-height: 88px;
  padding: 13px 14px;
  border: 0;
  border-bottom: 1px solid #edf0f2;
  background: transparent;
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr);
  gap: 10px;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.conversation-item:hover {
  background: #f1f5f4;
}

.conversation-item.active {
  background: #e8f3ef;
  box-shadow: inset 3px 0 #16856b;
}

.conversation-copy {
  min-width: 0;
  display: grid;
  gap: 5px;
}

.conversation-line {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.conversation-line strong,
.message-preview,
.account-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conversation-line strong {
  flex: 1;
  color: #27313a;
  font-size: 14px;
}

.conversation-line time,
.account-label {
  color: #8a929b;
  font-size: 12px;
}

.message-preview {
  flex: 1;
  color: #66707a;
  font-size: 13px;
}

.chat-panel {
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: #f5f6f7;
}

.chat-header {
  min-height: 68px;
  padding: 0 18px;
  border-bottom: 1px solid #e1e5e8;
  background: #ffffff;
  display: flex;
  align-items: center;
  gap: 11px;
}

.chat-identity {
  min-width: 0;
  flex: 1;
  display: grid;
  gap: 4px;
}

.chat-identity strong {
  overflow: hidden;
  color: #232b32;
  font-size: 15px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-identity span {
  color: #7a828b;
  font-size: 12px;
}

.status-dot {
  width: 7px;
  height: 7px;
  margin-left: 4px;
  border-radius: 50%;
  display: inline-block;
  background: #9aa1a8;
}

.status-dot.online,
.status-dot.working,
.status-dot.idle {
  background: #1c9b72;
}

.chat-actions {
  display: flex;
  gap: 8px;
}

.message-stream {
  min-height: 0;
  padding: 22px clamp(16px, 4vw, 52px);
  flex: 1;
  overflow-y: auto;
}

.load-older {
  margin: 0 auto 18px;
  padding: 6px 12px;
  border: 0;
  background: transparent;
  display: block;
  color: #66717b;
  cursor: pointer;
}

.message-row {
  margin: 10px 0;
  display: flex;
}

.message-row.outbound {
  justify-content: flex-end;
}

.message-bubble {
  max-width: min(72%, 680px);
  padding: 10px 13px 8px;
  border: 1px solid #e0e4e7;
  border-radius: 6px;
  background: #ffffff;
  color: #26313a;
  box-shadow: 0 1px 2px rgb(31 41 51 / 5%);
}

.message-row.outbound .message-bubble {
  border-color: #b9ddd2;
  background: #def1eb;
}

.message-bubble.failed {
  border-color: #dfb8b8;
  background: #fff3f3;
}

.message-bubble p {
  margin: 0;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.message-source {
  margin-bottom: 4px;
  display: block;
  color: #6f7780;
  font-size: 11px;
}

.message-meta {
  margin-top: 5px;
  display: block;
  color: #858d95;
  font-size: 11px;
  text-align: right;
}

.message-error {
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
  color: #b64040;
  font-size: 12px;
}

.composer {
  min-height: 104px;
  padding: 12px 16px;
  border-top: 1px solid #dfe3e6;
  background: #ffffff;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 42px;
  align-items: end;
  gap: 10px;
}

.composer :deep(.el-textarea__inner) {
  min-height: 78px;
  border-radius: 4px;
  box-shadow: none;
}

.detail-panel {
  padding: 18px;
  border-left: 1px solid #e4e7eb;
}

.detail-heading {
  margin-bottom: 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: #2c343c;
  font-size: 14px;
  font-weight: 600;
}

.detail-list {
  margin: 0;
}

.detail-list dt {
  margin-top: 15px;
  color: #8a929a;
  font-size: 12px;
}

.detail-list dd {
  margin: 5px 0 0;
  color: #333d46;
  font-size: 13px;
  line-height: 1.5;
  word-break: break-word;
}

.detail-command {
  width: 100%;
  margin-top: 24px;
}

.chat-empty {
  min-height: 100%;
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 10px;
  color: #8a929a;
}

@media (max-width: 1180px) {
  .chat-workspace {
    grid-template-columns: 290px minmax(380px, 1fr) 220px;
  }
}

@media (max-width: 900px) {
  .page-heading {
    min-height: 50px;
  }

  .chat-workspace {
    height: calc(100vh - 150px);
    min-height: 520px;
    grid-template-columns: 1fr;
  }

  .conversation-panel,
  .chat-panel {
    width: 100%;
    border: 0;
  }

  .conversation-item {
    min-height: 82px;
  }

  .message-bubble {
    max-width: 86%;
  }

  .message-stream {
    padding: 16px 12px;
  }

  .chat-header {
    padding: 0 10px;
  }

  .composer {
    padding: 10px;
  }
}
</style>
