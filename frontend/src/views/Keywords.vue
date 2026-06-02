<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  ElButton,
  ElDrawer,
  ElForm,
  ElFormItem,
  ElIcon,
  ElInput,
  ElInputNumber,
  ElMessage,
  ElMessageBox,
  ElOption,
  ElSelect,
  ElSwitch,
  ElTabPane,
  ElTabs,
  ElTag,
} from 'element-plus'
import { Close, Delete, Edit, MagicStick, Plus, Refresh } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import TableCard from '@/components/TableCard.vue'
import SearchBar from '@/components/SearchBar.vue'
import FormDrawer from '@/components/FormDrawer.vue'
import StatusTag from '@/components/StatusTag.vue'
import { acquisitionApi, type KeywordTrigger, type KeywordTriggerAction, type KeywordTriggerFormData } from '@/api/acquisition'
import { groupSearchKeywordsApi, type GroupSearchKeyword } from '@/api/groupSearchKeywords'
import { GROUP_SEARCH_KEYWORD_TYPE_OPTIONS } from '@/api/keywords'
import { normalizeListPayload } from '@/utils/pagination'

type KeywordScene = 'group' | 'reply'
type ReplyTarget = 'private' | 'group'
type PrivateReplyMode = 'default' | 'template' | 'ai'
type SearchKeywordType = 'demand' | 'inquiry' | 'price' | 'competitor'

type ReplyFormData = KeywordTriggerFormData & {
  replyTarget: ReplyTarget
  privateMode: PrivateReplyMode
}

const activeTab = ref<KeywordScene>('group')
const loading = ref(false)
const drawerVisible = ref(false)
const aiDrawerVisible = ref(false)
const editingId = ref<number | null>(null)

const searchKeywords = ref<GroupSearchKeyword[]>([])
const searchKeywordTotal = ref(0)
const searchKeywordPage = ref(1)
const searchKeywordPageSize = ref(20)
const groupSearchParams = ref<Record<string, any>>({})

const replyTriggers = ref<KeywordTrigger[]>([])
const replyTotal = ref(0)
const replyPage = ref(1)
const replyPageSize = ref(20)
const replySearchParams = ref<Record<string, any>>({})

const groupForm = reactive({
  text: '',
  keyword_type: 'demand' as SearchKeywordType,
  match_mode: 'fuzzy',
  requires_review: true,
  enabled: true,
})

const replyForm = reactive<ReplyFormData>({
  keyword_id: undefined,
  keyword_text: '',
  trigger_type: 'keyword',
  action: 'send_private',
  replyTarget: 'private',
  privateMode: 'default',
  template_id: undefined,
  reply_content: '',
  use_ai_reply: false,
  cooldown_seconds: 300,
  max_triggers_per_user: 5,
  max_triggers_per_group: 10,
  priority: 100,
  enabled: true,
})

const aiFormData = reactive({
  keyword_type: 'demand' as SearchKeywordType,
  count: 20,
  auto_approve: false,
})

const aiGeneratedKeywords = ref<GroupSearchKeyword[]>([])
const aiLoading = ref(false)

const keywordTypeOptions = GROUP_SEARCH_KEYWORD_TYPE_OPTIONS.map((item) => ({
  label: item.label,
  value: item.value,
  tag: item.tag,
}))

const replyTargetOptions: Array<{ label: string; value: ReplyTarget; tag: 'primary' | 'success' }> = [
  { label: '直接私聊', value: 'private', tag: 'primary' },
  { label: '群内回复', value: 'group', tag: 'success' },
]

const privateReplyModeOptions: Array<{ label: string; value: PrivateReplyMode }> = [
  { label: '默认引导', value: 'default' },
  { label: '固定话术', value: 'template' },
  { label: 'AI自动回复', value: 'ai' },
]

const replyActionOptions: Array<{ label: string; value: KeywordTriggerAction; tag: 'success' | 'warning' | 'primary' | 'info' }> = [
  { label: '私聊引导', value: 'send_private', tag: 'primary' },
  { label: '固定话术', value: 'reply_template', tag: 'success' },
  { label: 'AI回复', value: 'reply_ai', tag: 'warning' },
  { label: '表情回应', value: 'react', tag: 'info' },
]

const groupReplyActionOptions = replyActionOptions.filter((item) => item.value !== 'send_private')

const groupSearchFilters = [
  {
    type: 'input' as const,
    key: 'keyword',
    label: '搜索词',
    placeholder: '搜索搜群词',
    width: '180px',
  },
  {
    type: 'select' as const,
    key: 'keyword_type',
    label: '类型',
    placeholder: '全部类型',
    width: '150px',
    options: [{ label: '全部', value: '' }, ...keywordTypeOptions],
  },
  {
    type: 'select' as const,
    key: 'status',
    label: '状态',
    placeholder: '全部状态',
    width: '140px',
    options: [
      { label: '全部', value: '' },
      { label: '待审核', value: 'pending' },
      { label: '已启用', value: 'approved' },
      { label: '已废弃', value: 'discarded' },
    ],
  },
]

const replySearchFilters = [
  {
    type: 'input' as const,
    key: 'keyword',
    label: '关键词',
    placeholder: '搜索营销触发词',
    width: '180px',
  },
  {
    type: 'select' as const,
    key: 'action',
    label: '触发动作',
    placeholder: '全部动作',
    width: '140px',
    options: [{ label: '全部', value: '' }, ...replyActionOptions],
  },
  {
    type: 'select' as const,
    key: 'enabled',
    label: '状态',
    placeholder: '全部状态',
    width: '120px',
    options: [
      { label: '全部', value: '' },
      { label: '启用', value: true },
      { label: '停用', value: false },
    ],
  },
]

const searchColumns = [
  { prop: 'text', label: '搜群关键词', minWidth: '180', slot: 'text' },
  { prop: 'keyword_type', label: '类型', width: '120', slot: 'keyword_type' },
  { prop: 'match_mode', label: '匹配模式', width: '110', slot: 'match_mode' },
  { prop: 'trigger_count', label: '使用次数', width: '100' },
  { prop: 'status', label: '状态', width: '100', slot: 'status' },
  { prop: 'requires_review', label: '审核', width: '100', slot: 'requires_review' },
  { prop: 'created_at', label: '创建时间', width: '170', slot: 'created_at' },
  { prop: 'actions', label: '操作', width: '180', fixed: 'right', slot: 'actions' },
]

const replyColumns = [
  { prop: 'keyword_text', label: '营销触发词', minWidth: '180', slot: 'keyword_text' },
  { prop: 'replyTarget', label: '触发位置', width: '110', slot: 'replyTarget' },
  { prop: 'action', label: '触发动作', width: '130', slot: 'action' },
  { prop: 'cooldown_seconds', label: '冷却(秒)', width: '100' },
  { prop: 'max_triggers_per_user', label: '单用户上限', width: '110' },
  { prop: 'priority', label: '优先级', width: '90' },
  { prop: 'enabled', label: '状态', width: '100', slot: 'enabled' },
  { prop: 'created_at', label: '创建时间', width: '170', slot: 'created_at' },
  { prop: 'actions', label: '操作', width: '150', fixed: 'right', slot: 'actions' },
]

const groupFields = computed(() => [
  { prop: 'text', label: '搜索词', type: 'input' as const, placeholder: '例如：机场 / vpn / 节点 / 科学上网' },
  { prop: 'keyword_type', label: '搜索词类型', type: 'select' as const, options: keywordTypeOptions },
  {
    prop: 'match_mode',
    label: '匹配模式',
    type: 'select' as const,
    options: [
      { label: '模糊匹配', value: 'fuzzy' },
      { label: '精确匹配', value: 'exact' },
      { label: '正则匹配', value: 'regex' },
    ],
  },
  { prop: 'requires_review', label: '需要审核', type: 'switch' as const },
  { prop: 'enabled', label: '启用', type: 'switch' as const },
])

const groupRules = {
  text: [{ required: true, message: '请输入搜索词', trigger: 'blur' }],
  keyword_type: [{ required: true, message: '请选择搜索词类型', trigger: 'change' }],
}

const drawerTitle = computed(() => {
  if (activeTab.value === 'group') {
    return editingId.value ? '编辑搜群关键词' : '新增搜群关键词'
  }
  return editingId.value ? '编辑营销触发词' : '新增营销触发词'
})

const formatDate = (date?: string) => (date ? dayjs(date).format('YYYY-MM-DD HH:mm') : '-')

const getKeywordTypeOption = (type: string) => keywordTypeOptions.find((item) => item.value === type)
const getReplyTarget = (action: KeywordTriggerAction): ReplyTarget => (action === 'send_private' ? 'private' : 'group')
const getPrivateMode = (row: KeywordTrigger): PrivateReplyMode => {
  if (row.action !== 'send_private') return 'default'
  if (row.use_ai_reply) return 'ai'
  if (row.reply_content?.trim()) return 'template'
  return 'default'
}
const getReplyTargetOption = (action: KeywordTriggerAction) => replyTargetOptions.find((item) => item.value === getReplyTarget(action))
const getReplyActionOption = (row: KeywordTrigger) => {
  if (row.action === 'send_private') {
    const mode = getPrivateMode(row)
    if (mode === 'ai') return { label: '私聊AI回复', tag: 'warning' as const }
    if (mode === 'template') return { label: '私聊固定话术', tag: 'success' as const }
    return { label: '私聊默认引导', tag: 'primary' as const }
  }
  return replyActionOptions.find((item) => item.value === row.action)
}

const resetGroupForm = () => {
  Object.assign(groupForm, {
    text: '',
    keyword_type: 'demand',
    match_mode: 'fuzzy',
    requires_review: true,
    enabled: true,
  })
}

const resetReplyForm = () => {
  Object.assign(replyForm, {
    keyword_id: undefined,
    keyword_text: '',
    trigger_type: 'keyword',
    action: 'send_private',
    replyTarget: 'private',
    privateMode: 'default',
    template_id: undefined,
    reply_content: '',
    use_ai_reply: false,
    cooldown_seconds: 300,
    max_triggers_per_user: 5,
    max_triggers_per_group: 10,
    priority: 100,
    enabled: true,
  })
}

const fetchGroupKeywords = async (params?: Record<string, any>) => {
  loading.value = true
  try {
    if (params) groupSearchParams.value = params
    const response = await groupSearchKeywordsApi.list({
      page: searchKeywordPage.value,
      page_size: searchKeywordPageSize.value,
      ...groupSearchParams.value,
    })
    const payload = normalizeListPayload<GroupSearchKeyword>(response.data)
    searchKeywords.value = payload.list
    searchKeywordTotal.value = payload.total
  } finally {
    loading.value = false
  }
}

const fetchReplyTriggers = async (params?: Record<string, any>) => {
  loading.value = true
  try {
    if (params) replySearchParams.value = params
    const response = await acquisitionApi.getKeywordTriggers({
      page: replyPage.value,
      page_size: replyPageSize.value,
      ...replySearchParams.value,
    })
    const payload = normalizeListPayload<KeywordTrigger>(response.data)
    replyTriggers.value = payload.list
    replyTotal.value = payload.total
  } finally {
    loading.value = false
  }
}

const refreshCurrentTab = () => {
  if (activeTab.value === 'group') return fetchGroupKeywords()
  return fetchReplyTriggers()
}

const handleTabChange = (tab: string) => {
  activeTab.value = tab as KeywordScene
  refreshCurrentTab()
}

const handleGroupSearch = (values: Record<string, any>) => {
  searchKeywordPage.value = 1
  fetchGroupKeywords(values)
}

const handleGroupReset = () => {
  searchKeywordPage.value = 1
  fetchGroupKeywords({})
}

const handleGroupPageChange = (page: number) => {
  searchKeywordPage.value = page
  fetchGroupKeywords()
}

const handleGroupPageSizeChange = (pageSize: number) => {
  searchKeywordPageSize.value = pageSize
  searchKeywordPage.value = 1
  fetchGroupKeywords()
}

const handleReplySearch = (values: Record<string, any>) => {
  replyPage.value = 1
  fetchReplyTriggers(values)
}

const handleReplyReset = () => {
  replyPage.value = 1
  fetchReplyTriggers({})
}

const handleReplyPageChange = (page: number) => {
  replyPage.value = page
  fetchReplyTriggers()
}

const handleReplyPageSizeChange = (pageSize: number) => {
  replyPageSize.value = pageSize
  replyPage.value = 1
  fetchReplyTriggers()
}

watch(
  () => replyForm.replyTarget,
  (target) => {
    if (target === 'private') {
      replyForm.action = 'send_private'
      replyForm.use_ai_reply = replyForm.privateMode === 'ai'
      return
    }
    if (replyForm.action === 'send_private') {
      replyForm.action = 'reply_template'
    }
  },
)

watch(
  () => replyForm.privateMode,
  (mode) => {
    if (replyForm.replyTarget === 'private') {
      replyForm.action = 'send_private'
      replyForm.use_ai_reply = mode === 'ai'
    }
  },
)

const openAddDrawer = () => {
  editingId.value = null
  if (activeTab.value === 'group') resetGroupForm()
  else resetReplyForm()
  drawerVisible.value = true
}

const openEditGroupDrawer = (row: GroupSearchKeyword) => {
  editingId.value = row.id
  Object.assign(groupForm, {
    text: row.text,
    keyword_type: row.keyword_type,
    match_mode: row.match_mode,
    requires_review: row.requires_review,
    enabled: row.enabled,
  })
  drawerVisible.value = true
}

const openEditReplyDrawer = (row: KeywordTrigger) => {
  editingId.value = row.id
  Object.assign(replyForm, {
    keyword_id: row.keyword_id,
    keyword_text: row.keyword_text,
    trigger_type: row.trigger_type,
    action: row.action,
    replyTarget: getReplyTarget(row.action),
    privateMode: getPrivateMode(row),
    template_id: row.template_id,
    reply_content: row.reply_content || '',
    use_ai_reply: row.use_ai_reply,
    cooldown_seconds: row.cooldown_seconds,
    max_triggers_per_user: row.max_triggers_per_user,
    max_triggers_per_group: row.max_triggers_per_group,
    priority: row.priority,
    enabled: row.enabled,
  })
  drawerVisible.value = true
}

const handleGroupSubmit = async () => {
  const payload = {
    text: groupForm.text.trim(),
    keyword_type: groupForm.keyword_type,
    match_mode: groupForm.match_mode,
    requires_review: groupForm.requires_review,
    enabled: groupForm.enabled,
    status: groupForm.requires_review ? 'pending' : 'approved',
  }
  if (editingId.value) {
    await groupSearchKeywordsApi.update(editingId.value, payload)
    ElMessage.success('搜群关键词已更新')
  } else {
    await groupSearchKeywordsApi.create(payload)
    ElMessage.success('搜群关键词已添加')
  }
  drawerVisible.value = false
  fetchGroupKeywords()
}

const handleReplySubmit = async () => {
  if (!replyForm.keyword_text.trim()) {
    ElMessage.warning('请输入触发关键词')
    return
  }
  const action = replyForm.replyTarget === 'private'
    ? 'send_private'
    : replyForm.action === 'send_private'
      ? 'reply_template'
      : replyForm.action
  const replyContent = replyForm.reply_content?.trim() || ''
  if (replyForm.replyTarget === 'private' && replyForm.privateMode === 'template' && !replyContent) {
    ElMessage.warning('请输入私聊固定话术')
    return
  }
  if (replyForm.replyTarget === 'group' && action === 'reply_template' && !replyContent) {
    ElMessage.warning('请输入群内固定话术')
    return
  }
  const payload = {
    keyword_id: replyForm.keyword_id,
    keyword_text: replyForm.keyword_text.trim(),
    trigger_type: replyForm.trigger_type,
    action,
    template_id: replyForm.template_id,
    reply_content: replyForm.replyTarget === 'private'
      ? replyForm.privateMode === 'template'
        ? replyContent
        : ''
      : action === 'reply_template'
        ? replyContent
        : '',
    use_ai_reply: replyForm.replyTarget === 'private'
      ? replyForm.privateMode === 'ai'
      : action === 'reply_ai' ? true : replyForm.use_ai_reply,
    cooldown_seconds: replyForm.cooldown_seconds,
    max_triggers_per_user: replyForm.max_triggers_per_user,
    max_triggers_per_group: replyForm.max_triggers_per_group,
    priority: replyForm.priority,
    enabled: replyForm.enabled,
  }

  if (editingId.value) {
    await acquisitionApi.updateKeywordTrigger(editingId.value, payload)
    ElMessage.success('营销触发词已更新')
  } else {
    await acquisitionApi.createKeywordTrigger(payload)
    ElMessage.success('营销触发词已添加')
  }
  drawerVisible.value = false
  fetchReplyTriggers()
}

const handleSubmit = async () => {
  try {
    if (activeTab.value === 'group') await handleGroupSubmit()
    else await handleReplySubmit()
  } catch (error) {
    console.error('Failed to save keyword config:', error)
  }
}

const updateGroupForm = (value: Record<string, any>) => {
  Object.assign(groupForm, value)
}

const handleDeleteGroup = async (row: GroupSearchKeyword) => {
  try {
    await ElMessageBox.confirm(`确定删除搜群关键词 "${row.text}" 吗？`, '提示', { type: 'warning' })
    await groupSearchKeywordsApi.remove(row.id)
    ElMessage.success('删除成功')
    fetchGroupKeywords()
  } catch {
    // cancelled
  }
}

const toggleGroupKeywordStatus = async (row: GroupSearchKeyword) => {
  const nextEnabled = !row.enabled
  await groupSearchKeywordsApi.update(row.id, {
    enabled: nextEnabled,
    status: nextEnabled ? (row.requires_review ? 'pending' : 'approved') : 'discarded',
  })
  ElMessage.success(nextEnabled ? '已启用' : '已停用')
  fetchGroupKeywords()
}

const handleDeleteReply = async (row: KeywordTrigger) => {
  try {
    await ElMessageBox.confirm(`确定删除营销触发词 "${row.keyword_text}" 吗？`, '提示', { type: 'warning' })
    await acquisitionApi.deleteKeywordTrigger(row.id)
    ElMessage.success('删除成功')
    fetchReplyTriggers()
  } catch {
    // cancelled
  }
}

const openAIDrawer = () => {
  aiGeneratedKeywords.value = []
  Object.assign(aiFormData, {
    keyword_type: 'demand',
    count: 20,
    auto_approve: false,
  })
  aiDrawerVisible.value = true
}

const handleAIGenerate = async () => {
  aiLoading.value = true
  try {
    const response = await groupSearchKeywordsApi.generate({
      keyword_type: aiFormData.keyword_type,
      count: aiFormData.count,
      auto_approve: aiFormData.auto_approve,
    })
    aiGeneratedKeywords.value = response.data.data.keywords
    ElMessage.success(`生成了 ${response.data.data.created} 个搜群关键词`)
  } finally {
    aiLoading.value = false
  }
}

onMounted(() => {
  fetchGroupKeywords()
})
</script>

<template>
  <div class="keywords-page">
    <div class="page-header">
      <div>
        <h2 class="page-title">关键词管理</h2>
        <p class="page-desc">增长中心只管理搜群关键词和营销触发词，群管敏感词已独立到群治理中心。</p>
      </div>
      <div class="header-actions">
        <el-button @click="refreshCurrentTab">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button v-if="activeTab === 'group'" @click="openAIDrawer">
          <el-icon><MagicStick /></el-icon>
          AI补词
        </el-button>
        <el-button type="primary" @click="openAddDrawer">
          <el-icon><Plus /></el-icon>
          {{ activeTab === 'group' ? '新增搜群词' : '新增营销触发词' }}
        </el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" @tab-change="handleTabChange">
      <el-tab-pane label="搜群关键词" name="group">
        <SearchBar
          :filters="groupSearchFilters"
          :loading="loading"
          @search="handleGroupSearch"
          @reset="handleGroupReset"
        />

        <TableCard
          :columns="searchColumns"
          :data="searchKeywords"
          :total="searchKeywordTotal"
          :loading="loading"
          :page="searchKeywordPage"
          :page-size="searchKeywordPageSize"
          row-key="id"
          @page-change="handleGroupPageChange"
          @page-size-change="handleGroupPageSizeChange"
        >
          <template #text="{ row }">
            <span class="keyword-text">{{ row.text }}</span>
          </template>

          <template #keyword_type="{ row }">
            <el-tag :type="getKeywordTypeOption(row.keyword_type)?.tag || 'info'" effect="plain">
              {{ getKeywordTypeOption(row.keyword_type)?.label || row.keyword_type }}
            </el-tag>
          </template>

          <template #match_mode="{ row }">
            <span class="muted-text">{{ row.match_mode }}</span>
          </template>

          <template #status="{ row }">
            <StatusTag :status="row.status" type="keyword" />
          </template>

          <template #requires_review="{ row }">
            <el-tag :type="row.requires_review ? 'warning' : 'success'" effect="plain">
              {{ row.requires_review ? '待审核' : '免审核' }}
            </el-tag>
          </template>

          <template #created_at="{ row }">
            {{ formatDate(row.created_at) }}
          </template>

          <template #actions="{ row }">
            <el-button type="primary" link size="small" @click="openEditGroupDrawer(row)">
              <el-icon><Edit /></el-icon>
              编辑
            </el-button>
            <el-button type="warning" link size="small" @click="toggleGroupKeywordStatus(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </el-button>
            <el-button type="danger" link size="small" @click="handleDeleteGroup(row)">
              <el-icon><Delete /></el-icon>
              删除
            </el-button>
          </template>
        </TableCard>
      </el-tab-pane>

      <el-tab-pane label="营销触发词" name="reply">
        <SearchBar
          :filters="replySearchFilters"
          :loading="loading"
          @search="handleReplySearch"
          @reset="handleReplyReset"
        />

        <TableCard
          :columns="replyColumns"
          :data="replyTriggers"
          :total="replyTotal"
          :loading="loading"
          :page="replyPage"
          :page-size="replyPageSize"
          row-key="id"
          @page-change="handleReplyPageChange"
          @page-size-change="handleReplyPageSizeChange"
        >
          <template #keyword_text="{ row }">
            <span class="keyword-text">{{ row.keyword_text }}</span>
          </template>

          <template #replyTarget="{ row }">
            <el-tag :type="getReplyTargetOption(row.action)?.tag || 'info'" effect="plain">
              {{ getReplyTargetOption(row.action)?.label || '群内回复' }}
            </el-tag>
          </template>

          <template #action="{ row }">
            <el-tag :type="getReplyActionOption(row)?.tag || 'info'" effect="plain">
              {{ getReplyActionOption(row)?.label || row.action }}
            </el-tag>
          </template>

          <template #enabled="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag>
          </template>

          <template #created_at="{ row }">
            {{ formatDate(row.created_at) }}
          </template>

          <template #actions="{ row }">
            <el-button type="primary" link size="small" @click="openEditReplyDrawer(row)">
              <el-icon><Edit /></el-icon>
              编辑
            </el-button>
            <el-button type="danger" link size="small" @click="handleDeleteReply(row)">
              <el-icon><Delete /></el-icon>
              删除
            </el-button>
          </template>
        </TableCard>
      </el-tab-pane>
    </el-tabs>

    <FormDrawer
      v-if="activeTab === 'group'"
      v-model:visible="drawerVisible"
      :title="drawerTitle"
      :fields="groupFields"
      :model-value="groupForm"
      :rules="groupRules"
      @update:model-value="updateGroupForm"
      @confirm="handleSubmit"
    />

    <el-drawer
      v-if="activeTab === 'reply'"
      v-model="drawerVisible"
      :title="drawerTitle"
      size="560px"
      :show-close="false"
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="drawer-header">
          <span class="drawer-title">{{ drawerTitle }}</span>
          <el-icon class="close-icon" @click="drawerVisible = false"><Close /></el-icon>
        </div>
      </template>

      <el-form :model="replyForm" label-width="130px">
        <el-form-item label="触发关键词" required>
          <el-input v-model="replyForm.keyword_text" placeholder="用户消息命中后触发回复或私聊" />
        </el-form-item>
        <el-form-item label="回复位置">
          <el-select v-model="replyForm.replyTarget" style="width: 100%;">
            <el-option v-for="item in replyTargetOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="replyForm.replyTarget === 'private'" label="私聊回复">
          <el-select v-model="replyForm.privateMode" style="width: 100%;">
            <el-option v-for="item in privateReplyModeOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="replyForm.replyTarget === 'private' && replyForm.privateMode === 'template'" label="私聊话术" required>
          <el-input
            v-model="replyForm.reply_content"
            type="textarea"
            :rows="5"
            maxlength="5000"
            show-word-limit
            placeholder="支持变量：{{user_name}}、{{group_name}}、{{register_link}}、{{keyword}}"
          />
        </el-form-item>
        <el-form-item v-if="replyForm.replyTarget === 'group'" label="回复方式">
          <el-select v-model="replyForm.action" style="width: 100%;">
            <el-option v-for="item in groupReplyActionOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="replyForm.replyTarget === 'group' && replyForm.action === 'reply_template'" label="群内话术" required>
          <el-input
            v-model="replyForm.reply_content"
            type="textarea"
            :rows="5"
            maxlength="5000"
            show-word-limit
            placeholder="支持变量：{{user_name}}、{{group_name}}、{{register_link}}、{{keyword}}"
          />
        </el-form-item>
        <el-form-item v-if="replyForm.replyTarget === 'group'" label="AI回复">
          <el-switch v-model="replyForm.use_ai_reply" :disabled="replyForm.action === 'reply_ai'" />
        </el-form-item>
        <el-form-item label="冷却时间">
          <el-input-number v-model="replyForm.cooldown_seconds" :min="0" :max="86400" />
        </el-form-item>
        <el-form-item label="单用户上限">
          <el-input-number v-model="replyForm.max_triggers_per_user" :min="1" :max="999" />
        </el-form-item>
        <el-form-item label="单群上限">
          <el-input-number v-model="replyForm.max_triggers_per_group" :min="1" :max="999" />
        </el-form-item>
        <el-form-item label="优先级">
          <el-input-number v-model="replyForm.priority" :min="0" :max="999" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="replyForm.enabled" />
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="drawer-footer">
          <el-button @click="drawerVisible = false">取消</el-button>
          <el-button type="primary" @click="handleSubmit">确定</el-button>
        </div>
      </template>
    </el-drawer>

    <el-drawer
      v-model="aiDrawerVisible"
      title="AI补充搜群关键词"
      size="500px"
      :show-close="false"
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="drawer-header">
          <span class="drawer-title">AI补充搜群关键词</span>
          <el-icon class="close-icon" @click="aiDrawerVisible = false"><Close /></el-icon>
        </div>
      </template>

      <el-form :model="aiFormData" label-width="110px">
        <el-form-item label="搜索词类型">
          <el-select v-model="aiFormData.keyword_type" style="width: 100%;">
            <el-option v-for="item in keywordTypeOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="生成数量">
          <el-input-number v-model="aiFormData.count" :min="1" :max="50" />
        </el-form-item>
        <el-form-item label="生成后状态">
          <el-switch
            v-model="aiFormData.auto_approve"
            active-text="免审核直接启用"
            inactive-text="进入待审核"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="aiLoading" @click="handleAIGenerate">
            <el-icon><MagicStick /></el-icon>
            生成关键词
          </el-button>
        </el-form-item>
      </el-form>

      <div v-if="aiGeneratedKeywords.length > 0" class="generated-list">
        <div class="generated-header">
          <span>生成结果 ({{ aiGeneratedKeywords.length }})</span>
        </div>
        <div class="generated-words">
          <el-tag
            v-for="item in aiGeneratedKeywords"
            :key="item.id"
            class="word-tag"
            :type="item.requires_review ? 'warning' : 'success'"
          >
            {{ item.text }}
          </el-tag>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped lang="scss">
.keywords-page {
  padding: 0;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 20px;
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

.header-actions {
  display: flex;
  gap: 12px;
}

.keyword-text {
  font-family: 'Monaco', 'Menlo', monospace;
  background: #f5f7fa;
  padding: 2px 8px;
  border-radius: 4px;
}

.muted-text {
  color: #909399;
}

.drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.drawer-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}

.close-icon {
  cursor: pointer;
  font-size: 18px;
  color: #909399;
}

.drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}

.generated-list {
  margin-top: 16px;
}

.generated-header {
  margin-bottom: 12px;
  color: #303133;
  font-weight: 600;
}

.generated-words {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
</style>
