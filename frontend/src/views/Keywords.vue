<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  ElAlert,
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
import { Check, Close, Delete, Edit, MagicStick, Plus, Refresh } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import TableCard from '@/components/TableCard.vue'
import SearchBar from '@/components/SearchBar.vue'
import FormDrawer from '@/components/FormDrawer.vue'
import StatusTag from '@/components/StatusTag.vue'
import {
  acquisitionApi,
  type GenerateKeywordTriggersResult,
  type KeywordTrigger,
  type KeywordTriggerAction,
  type KeywordTriggerFormData,
  type MessageTemplate,
  type MessageTemplateFormData,
} from '@/api/acquisition'
import { groupSearchKeywordsApi, type GenerateGroupSearchKeywordsResult, type GroupSearchKeyword } from '@/api/groupSearchKeywords'
import { GROUP_SEARCH_KEYWORD_TYPE_OPTIONS } from '@/api/keywords'
import { normalizeListPayload } from '@/utils/pagination'

type KeywordScene = 'group' | 'reply'
type ReplyTarget = 'private' | 'group'
type PrivateReplyMode = 'default' | 'template' | 'ai'
type SearchKeywordType = 'demand' | 'inquiry' | 'price' | 'competitor'
type MarketingTriggerCategory = 'intent' | 'question' | 'price' | 'pain' | 'cooperation' | 'broad'
type AIGenerationCategory = SearchKeywordType | MarketingTriggerCategory
type GeneratedKeyword = GroupSearchKeyword | KeywordTrigger
type AIGenerationResult = GenerateGroupSearchKeywordsResult | GenerateKeywordTriggersResult
type TabName = string | number

type ReplyFormData = KeywordTriggerFormData & {
  replyTarget: ReplyTarget
  privateMode: PrivateReplyMode
}

const activeTab = ref<KeywordScene>('group')
const loading = ref(false)
const drawerVisible = ref(false)
const aiDrawerVisible = ref(false)
const templateDrawerVisible = ref(false)
const batchTemplateDrawerVisible = ref(false)
const aiScene = ref<KeywordScene>('group')
const editingId = ref<number | null>(null)
const editingTemplateId = ref<number | null>(null)

const searchKeywords = ref<GroupSearchKeyword[]>([])
const searchKeywordTotal = ref(0)
const searchKeywordPage = ref(1)
const searchKeywordPageSize = ref(20)
const groupSearchParams = ref<Record<string, any>>({})
const selectedGroupKeywords = ref<GroupSearchKeyword[]>([])

const replyTriggers = ref<KeywordTrigger[]>([])
const replyTotal = ref(0)
const replyPage = ref(1)
const replyPageSize = ref(20)
const replySearchParams = ref<Record<string, any>>({})
const selectedReplyTriggers = ref<KeywordTrigger[]>([])
const replyTemplates = ref<MessageTemplate[]>([])
const templateLoading = ref(false)

const groupForm = reactive({
  text: '',
  keyword_type: 'demand' as SearchKeywordType,
  match_mode: 'fuzzy',
  requires_review: false,
  enabled: true,
})

const replyForm = reactive<ReplyFormData>({
  keyword_id: undefined,
  keyword_text: '',
  trigger_type: 'keyword',
  action: 'send_private',
  replyTarget: 'private',
  privateMode: 'template',
  template_id: undefined,
  reply_content: '',
  use_ai_reply: false,
  cooldown_seconds: 300,
  max_triggers_per_user: 5,
  max_triggers_per_group: 10,
  priority: 100,
  requires_review: false,
  enabled: true,
})

const aiFormData = reactive({
  keyword_type: 'demand' as AIGenerationCategory,
  count: 20,
  auto_approve: true,
})

const templateForm = reactive<MessageTemplateFormData>({
  name: '',
  content: '',
  template_variables: 'user_name,group_name,bot_name,register_link,keyword',
  message_type: 'guide',
  cooldown_seconds: 300,
  max_uses_per_day: 100,
  enabled: true,
})

const batchTemplateForm = reactive({
  template_id: undefined as number | undefined,
  reply_target: 'private' as ReplyTarget,
  enabled: true,
})

const aiGeneratedKeywords = ref<GeneratedKeyword[]>([])
const aiGenerationResult = ref<AIGenerationResult | null>(null)
const aiLoading = ref(false)

const keywordTypeOptions = GROUP_SEARCH_KEYWORD_TYPE_OPTIONS.map((item) => ({
  label: item.label,
  value: item.value,
  tag: item.tag,
}))

const marketingTriggerTypeOptions: Array<{ label: string; value: MarketingTriggerCategory; tag: 'success' | 'info' | 'warning' | 'danger' | 'primary' }> = [
  { label: '购买意向', value: 'intent', tag: 'success' },
  { label: '咨询提问', value: 'question', tag: 'info' },
  { label: '价格试用', value: 'price', tag: 'warning' },
  { label: '痛点求助', value: 'pain', tag: 'danger' },
  { label: '合作资源', value: 'cooperation', tag: 'primary' },
  { label: '泛需求词', value: 'broad', tag: 'success' },
]

const replyTargetOptions: Array<{ label: string; value: ReplyTarget; tag: 'primary' | 'success' }> = [
  { label: '直接私聊', value: 'private', tag: 'primary' },
  { label: '群内回复', value: 'group', tag: 'success' },
]

const privateReplyModeOptions: Array<{ label: string; value: PrivateReplyMode }> = [
  { label: '模板回复', value: 'template' },
  { label: 'AI自动回复', value: 'ai' },
  { label: '默认引导', value: 'default' },
]

const replyActionOptions: Array<{ label: string; value: KeywordTriggerAction; tag: 'success' | 'warning' | 'primary' | 'info' }> = [
  { label: '私聊引导', value: 'send_private', tag: 'primary' },
  { label: '模板回复', value: 'reply_template', tag: 'success' },
  { label: 'AI回复', value: 'reply_ai', tag: 'warning' },
  { label: '表情回应', value: 'react', tag: 'info' },
]

const groupReplyActionOptions = replyActionOptions.filter((item) => item.value !== 'send_private')

const aiTargetLabel = computed(() => (aiScene.value === 'group' ? '搜群关键词' : '营销触发词'))
const aiDrawerTitle = computed(() => (aiScene.value === 'group' ? 'AI补充搜群关键词' : 'AI生成营销触发词'))
const aiCategoryLabel = computed(() => (aiScene.value === 'group' ? '搜索词类型' : '触发词类型'))
const aiCategoryOptions = computed(() => (aiScene.value === 'group' ? keywordTypeOptions : marketingTriggerTypeOptions))
const enabledReplyTemplates = computed(() => replyTemplates.value.filter((item) => item.enabled))
const selectedReplyTemplate = computed(() => {
  if (!replyForm.template_id) return null
  return replyTemplates.value.find((item) => item.id === replyForm.template_id) || null
})

const aiGenerationNotice = computed(() => {
  const result = aiGenerationResult.value
  if (!result) return ''

  const sourceNote = result.llm_configured ? '' : '当前未配置 LLM Key，已使用本地兜底词库。'
  const skipped = result.skipped_existing + result.skipped_duplicate
  const filtered = result.skipped_invalid || 0
  const targetLabel = aiTargetLabel.value
  const reviewNote = aiScene.value === 'reply' && result.created > 0 ? '已进入待审核，审核通过后才会执行。' : ''
  if (result.created > 0) {
    const partialNote = result.candidate_exhausted ? `候选不足，未补满 ${result.requested} 个。` : ''
    const skippedNote = skipped > 0 ? `已跳过 ${skipped} 个重复候选。` : ''
    const filteredNote = filtered > 0 ? `已过滤 ${filtered} 个不合格候选。` : ''
    return `${sourceNote}已新增 ${result.created} 个${targetLabel}。${reviewNote}${skippedNote}${filteredNote}${partialNote}`
  }
  if (skipped > 0 || filtered > 0) {
    return `${sourceNote}生成了 ${result.generated} 个候选词，其中 ${skipped} 个已存在或重复，${filtered} 个不合格，没有新增。`
  }
  return `${sourceNote}没有生成可用的新候选词，请检查 LLM 配置后重试。`
})

const aiGenerationNoticeType = computed(() => (aiGenerationResult.value?.created ? 'success' : 'warning'))

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
    key: 'requires_review',
    label: '审核',
    placeholder: '全部审核',
    width: '120px',
    options: [
      { label: '全部', value: '' },
      { label: '待审核', value: true },
      { label: '已通过', value: false },
    ],
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
  { prop: 'use_count', label: '使用次数', width: '100' },
  { prop: 'used_at', label: '使用时间', width: '170', slot: 'used_at' },
  { prop: 'status', label: '状态', width: '100', slot: 'status' },
  { prop: 'requires_review', label: '审核', width: '100', slot: 'requires_review' },
  { prop: 'created_at', label: '创建时间', width: '170', slot: 'created_at' },
  { prop: 'actions', label: '操作', width: '240', fixed: 'right', slot: 'actions' },
]

const replyColumns = [
  { prop: 'keyword_text', label: '营销触发词', minWidth: '180', slot: 'keyword_text' },
  { prop: 'replyTarget', label: '触发位置', width: '110', slot: 'replyTarget' },
  { prop: 'action', label: '触发动作', width: '130', slot: 'action' },
  { prop: 'reply_content', label: '模板话术', minWidth: '160', slot: 'reply_content' },
  { prop: 'cooldown_seconds', label: '冷却(秒)', width: '100' },
  { prop: 'max_triggers_per_user', label: '单用户上限', width: '110' },
  { prop: 'priority', label: '优先级', width: '90' },
  { prop: 'requires_review', label: '审核', width: '100', slot: 'requires_review' },
  { prop: 'enabled', label: '状态', width: '100', slot: 'enabled' },
  { prop: 'created_at', label: '创建时间', width: '170', slot: 'created_at' },
  { prop: 'actions', label: '操作', width: '270', fixed: 'right', slot: 'actions' },
]

const templateColumns = [
  { prop: 'name', label: '模板名称', minWidth: '150', slot: 'name' },
  { prop: 'content', label: '模板内容', minWidth: '260', slot: 'content' },
  { prop: 'usage_count', label: '绑定数', width: '90' },
  { prop: 'enabled', label: '状态', width: '90', slot: 'enabled' },
  { prop: 'updated_at', label: '更新时间', width: '160', slot: 'updated_at' },
  { prop: 'actions', label: '操作', width: '150', fixed: 'right', slot: 'actions' },
]

const groupFields = computed(() => [
  { prop: 'text', label: '搜索词', type: 'input' as const, placeholder: '例如：华人群 / 招聘群 / 餐饮群 / AI群' },
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
    requires_review: false,
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
    privateMode: 'template',
    template_id: undefined,
    reply_content: '',
    use_ai_reply: false,
    cooldown_seconds: 300,
    max_triggers_per_user: 5,
    max_triggers_per_group: 10,
    priority: 100,
    requires_review: false,
    enabled: true,
  })
}

const resetTemplateForm = () => {
  editingTemplateId.value = null
  Object.assign(templateForm, {
    name: '',
    content: '',
    template_variables: 'user_name,group_name,bot_name,register_link,keyword',
    message_type: 'guide',
    cooldown_seconds: 300,
    max_uses_per_day: 100,
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
    selectedGroupKeywords.value = []
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
    selectedReplyTriggers.value = []
  } finally {
    loading.value = false
  }
}

const fetchMessageTemplates = async () => {
  templateLoading.value = true
  try {
    const response = await acquisitionApi.getMessageTemplates({
      message_type: 'guide',
      include_inline: false,
    })
    replyTemplates.value = response.data.data || []
    if (!batchTemplateForm.template_id && enabledReplyTemplates.value.length > 0) {
      batchTemplateForm.template_id = enabledReplyTemplates.value[0].id
    }
  } finally {
    templateLoading.value = false
  }
}

const refreshCurrentTab = () => {
  if (activeTab.value === 'group') return fetchGroupKeywords()
  fetchReplyTriggers()
  fetchMessageTemplates()
}

const handleTabChange = (tab: TabName) => {
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
  else {
    resetReplyForm()
    fetchMessageTemplates()
  }
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

const openEditReplyDrawer = (row: KeywordTrigger, forceTemplate = false) => {
  editingId.value = row.id
  fetchMessageTemplates()
  const nextReplyTarget = forceTemplate && row.action !== 'send_private' ? 'group' : getReplyTarget(row.action)
  const nextAction = forceTemplate && nextReplyTarget === 'group' ? 'reply_template' : row.action
  Object.assign(replyForm, {
    keyword_id: row.keyword_id,
    keyword_text: row.keyword_text,
    trigger_type: row.trigger_type,
    action: nextAction,
    replyTarget: nextReplyTarget,
    privateMode: forceTemplate && nextReplyTarget === 'private' ? 'template' : getPrivateMode(row),
    template_id: row.template_id,
    reply_content: row.reply_content || '',
    use_ai_reply: row.use_ai_reply,
    cooldown_seconds: row.cooldown_seconds,
    max_triggers_per_user: row.max_triggers_per_user,
    max_triggers_per_group: row.max_triggers_per_group,
    priority: row.priority,
    requires_review: row.requires_review,
    enabled: row.enabled,
  })
  drawerVisible.value = true
}

const openTemplateReplyDrawer = (row: KeywordTrigger) => {
  openEditReplyDrawer(row, true)
}

const openTemplateDrawer = async () => {
  resetTemplateForm()
  templateDrawerVisible.value = true
  await fetchMessageTemplates()
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
  const action: KeywordTriggerAction = replyForm.replyTarget === 'private'
    ? 'send_private'
    : replyForm.action === 'send_private'
      ? 'reply_template'
      : replyForm.action
  const replyContent = replyForm.reply_content?.trim() || ''
  const usesTemplateReply = (
    (replyForm.replyTarget === 'private' && replyForm.privateMode === 'template') ||
    (replyForm.replyTarget === 'group' && action === 'reply_template')
  )
  const selectedTemplateId = usesTemplateReply ? replyForm.template_id : undefined
  if (replyForm.replyTarget === 'private' && replyForm.privateMode === 'template' && !selectedTemplateId && !replyContent) {
    ElMessage.warning('请选择统一模板或输入私聊模板话术')
    return
  }
  if (replyForm.replyTarget === 'group' && action === 'reply_template' && !selectedTemplateId && !replyContent) {
    ElMessage.warning('请选择统一模板或输入群内模板话术')
    return
  }
  const payload: KeywordTriggerFormData = {
    keyword_id: replyForm.keyword_id,
    keyword_text: replyForm.keyword_text.trim(),
    trigger_type: replyForm.trigger_type,
    action,
    template_id: selectedTemplateId,
    reply_content: usesTemplateReply
      ? selectedTemplateId ? undefined : replyContent
      : '',
    use_ai_reply: replyForm.replyTarget === 'private'
      ? replyForm.privateMode === 'ai'
      : action === 'reply_ai' ? true : replyForm.use_ai_reply,
    cooldown_seconds: replyForm.cooldown_seconds,
    max_triggers_per_user: replyForm.max_triggers_per_user,
    max_triggers_per_group: replyForm.max_triggers_per_group,
    priority: replyForm.priority,
    requires_review: replyForm.requires_review,
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

const isGroupKeywordPendingReview = (row: GroupSearchKeyword) => {
  return row.status === 'pending' || row.requires_review
}

const selectedPendingGroupKeywords = computed(() => {
  return selectedGroupKeywords.value.filter(isGroupKeywordPendingReview)
})

const getGroupReviewLabel = (row: GroupSearchKeyword) => {
  if (isGroupKeywordPendingReview(row)) return '待审核'
  if (row.status === 'approved') return '已通过'
  if (row.status === 'discarded') return '已废弃'
  return '免审核'
}

const getGroupReviewTagType = (row: GroupSearchKeyword) => {
  if (isGroupKeywordPendingReview(row)) return 'warning'
  if (row.status === 'approved') return 'success'
  return 'info'
}

const handleApproveGroupKeyword = async (row: GroupSearchKeyword) => {
  await groupSearchKeywordsApi.update(row.id, {
    requires_review: false,
    status: 'approved',
    enabled: true,
  })
  ElMessage.success('已审核通过')
  fetchGroupKeywords()
}

const handleGroupSelectionChange = (rows: GroupSearchKeyword[]) => {
  selectedGroupKeywords.value = rows
}

const handleBatchApproveGroupKeywords = async () => {
  const rows = selectedPendingGroupKeywords.value
  if (selectedGroupKeywords.value.length === 0) {
    ElMessage.warning('请先选择要审核的搜群关键词')
    return
  }
  if (rows.length === 0) {
    ElMessage.warning('选中的搜群关键词没有待审核项')
    return
  }
  await Promise.all(
    rows.map((row) =>
      groupSearchKeywordsApi.update(row.id, {
        requires_review: false,
        status: 'approved',
        enabled: true,
      }),
    ),
  )
  ElMessage.success(`已批量通过 ${rows.length} 个搜群关键词`)
  fetchGroupKeywords()
}

const handleBatchDeleteGroupKeywords = async () => {
  const rows = [...selectedGroupKeywords.value]
  if (rows.length === 0) {
    ElMessage.warning('请先选择要删除的搜群关键词')
    return
  }
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${rows.length} 个搜群关键词吗？`, '批量删除确认', { type: 'warning' })
  } catch {
    return
  }
  await Promise.all(rows.map((row) => groupSearchKeywordsApi.remove(row.id)))
  ElMessage.success(`已删除 ${rows.length} 个搜群关键词`)
  fetchGroupKeywords()
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

const isReplyTriggerPendingReview = (row: KeywordTrigger) => row.requires_review

const selectedPendingReplyTriggers = computed(() => {
  return selectedReplyTriggers.value.filter(isReplyTriggerPendingReview)
})

const getReplyReviewLabel = (row: KeywordTrigger) => (row.requires_review ? '待审核' : '已通过')

const getReplyReviewTagType = (row: KeywordTrigger) => (row.requires_review ? 'warning' : 'success')

const getReplyTemplatePreview = (row: KeywordTrigger) => {
  const content = row.reply_content?.trim()
  const prefix = row.template_name ? `${row.template_name}: ` : ''
  if (content) return content.length > 24 ? `${content.slice(0, 24)}...` : content
  if (row.action === 'reply_ai' || row.use_ai_reply) return 'AI自动回复'
  return prefix ? `${prefix}未加载内容` : '未配置'
}

const handleApproveReplyTrigger = async (row: KeywordTrigger) => {
  await acquisitionApi.updateKeywordTrigger(row.id, {
    requires_review: false,
    enabled: true,
  })
  ElMessage.success('营销触发词已审核通过')
  fetchReplyTriggers()
}

const handleReplySelectionChange = (rows: KeywordTrigger[]) => {
  selectedReplyTriggers.value = rows
}

const handleBatchApproveReplyTriggers = async () => {
  const rows = selectedPendingReplyTriggers.value
  if (selectedReplyTriggers.value.length === 0) {
    ElMessage.warning('请先选择要审核的营销触发词')
    return
  }
  if (rows.length === 0) {
    ElMessage.warning('选中的营销触发词没有待审核项')
    return
  }
  await Promise.all(
    rows.map((row) =>
      acquisitionApi.updateKeywordTrigger(row.id, {
        requires_review: false,
        enabled: true,
      }),
    ),
  )
  ElMessage.success(`已批量通过 ${rows.length} 个营销触发词`)
  fetchReplyTriggers()
}

const handleBatchDeleteReplyTriggers = async () => {
  const rows = [...selectedReplyTriggers.value]
  if (rows.length === 0) {
    ElMessage.warning('请先选择要删除的营销触发词')
    return
  }
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${rows.length} 个营销触发词吗？`, '批量删除确认', { type: 'warning' })
  } catch {
    return
  }
  await Promise.all(rows.map((row) => acquisitionApi.deleteKeywordTrigger(row.id)))
  ElMessage.success(`已删除 ${rows.length} 个营销触发词`)
  fetchReplyTriggers()
}

const openBatchTemplateDrawer = async () => {
  if (selectedReplyTriggers.value.length === 0) {
    ElMessage.warning('请先选择要绑定模板的营销触发词')
    return
  }
  await fetchMessageTemplates()
  batchTemplateForm.template_id = enabledReplyTemplates.value[0]?.id
  batchTemplateForm.reply_target = 'private'
  batchTemplateForm.enabled = true
  batchTemplateDrawerVisible.value = true
}

const handleBatchBindTemplate = async () => {
  if (selectedReplyTriggers.value.length === 0) {
    ElMessage.warning('请先选择营销触发词')
    return
  }
  if (!batchTemplateForm.template_id) {
    ElMessage.warning('请选择统一模板')
    return
  }
  const response = await acquisitionApi.batchBindKeywordTriggerTemplate({
    trigger_ids: selectedReplyTriggers.value.map((row) => row.id),
    template_id: batchTemplateForm.template_id,
    reply_target: batchTemplateForm.reply_target,
    enabled: batchTemplateForm.enabled,
  })
  ElMessage.success(`已绑定 ${response.data.data.updated} 个营销触发词`)
  batchTemplateDrawerVisible.value = false
  await fetchReplyTriggers()
  await fetchMessageTemplates()
}

const editMessageTemplate = (row: MessageTemplate) => {
  editingTemplateId.value = row.id
  Object.assign(templateForm, {
    name: row.name,
    content: row.content,
    template_variables: row.template_variables || 'user_name,group_name,bot_name,register_link,keyword',
    message_type: row.message_type || 'guide',
    cooldown_seconds: row.cooldown_seconds,
    max_uses_per_day: row.max_uses_per_day,
    enabled: row.enabled,
  })
}

const saveMessageTemplate = async () => {
  if (!templateForm.name.trim()) {
    ElMessage.warning('请输入模板名称')
    return
  }
  if (!templateForm.content.trim()) {
    ElMessage.warning('请输入模板内容')
    return
  }
  const payload = {
    ...templateForm,
    name: templateForm.name.trim(),
    content: templateForm.content.trim(),
  }
  if (editingTemplateId.value) {
    await acquisitionApi.updateMessageTemplate(editingTemplateId.value, payload)
    ElMessage.success('模板已更新')
  } else {
    await acquisitionApi.createMessageTemplate(payload)
    ElMessage.success('模板已创建')
  }
  resetTemplateForm()
  await fetchMessageTemplates()
}

const deleteMessageTemplate = async (row: MessageTemplate) => {
  try {
    await ElMessageBox.confirm(
      `确定删除统一模板 "${row.name}" 吗？已绑定的关键词会解除模板绑定。`,
      '删除模板确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  await acquisitionApi.deleteMessageTemplate(row.id)
  ElMessage.success('模板已删除')
  if (replyForm.template_id === row.id) replyForm.template_id = undefined
  await fetchMessageTemplates()
  await fetchReplyTriggers()
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
  aiScene.value = activeTab.value
  aiGeneratedKeywords.value = []
  aiGenerationResult.value = null
  Object.assign(aiFormData, {
    keyword_type: activeTab.value === 'group' ? 'demand' : 'intent',
    count: 20,
    auto_approve: activeTab.value === 'group',
  })
  aiDrawerVisible.value = true
}

const handleAIGenerate = async () => {
  aiLoading.value = true
  try {
    if (aiScene.value === 'group') {
      const response = await groupSearchKeywordsApi.generate({
        keyword_type: aiFormData.keyword_type as SearchKeywordType,
        count: aiFormData.count,
        auto_approve: aiFormData.auto_approve,
      })
      const result = response.data.data
      aiGenerationResult.value = result
      aiGeneratedKeywords.value = result.keywords
      if (result.created > 0) {
        ElMessage.success(`生成了 ${result.created} 个搜群关键词`)
        await fetchGroupKeywords()
      } else if (result.skipped_existing + result.skipped_duplicate + result.skipped_invalid > 0) {
        ElMessage.warning('候选词重复或不合格，未新增搜群关键词')
      } else {
        ElMessage.warning('没有生成可用的新搜群关键词')
      }
      return
    }

    const response = await acquisitionApi.generateKeywordTriggers({
      category: aiFormData.keyword_type,
      count: aiFormData.count,
      action: 'send_private',
      use_ai_reply: false,
      cooldown_seconds: 300,
      max_triggers_per_user: 5,
      max_triggers_per_group: 10,
      priority: 100,
    })
    const result = response.data.data
    aiGenerationResult.value = result
    aiGeneratedKeywords.value = result.keywords
    if (result.created > 0) {
      ElMessage.success(`生成了 ${result.created} 个营销触发词，已进入待审核`)
      await fetchReplyTriggers()
    } else if (result.skipped_existing + result.skipped_duplicate + result.skipped_invalid > 0) {
      ElMessage.warning('候选词重复或不合格，未新增营销触发词')
    } else {
      ElMessage.warning('没有生成可用的新营销触发词')
    }
  } finally {
    aiLoading.value = false
  }
}

const getGeneratedKeywordText = (item: GeneratedKeyword) => {
  return 'text' in item ? item.text : item.keyword_text
}

const getGeneratedKeywordTagType = (item: GeneratedKeyword) => {
  return item.requires_review ? 'warning' : 'success'
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
        <el-button @click="openAIDrawer">
          <el-icon><MagicStick /></el-icon>
          {{ activeTab === 'group' ? 'AI补词' : 'AI生成' }}
        </el-button>
        <el-button v-if="activeTab === 'reply'" @click="openTemplateDrawer">
          <el-icon><Edit /></el-icon>
          模板库
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

        <div class="bulk-actions">
          <span class="bulk-summary">
            已选 {{ selectedGroupKeywords.length }} 个
            <template v-if="selectedGroupKeywords.length > 0">
              ，待审核 {{ selectedPendingGroupKeywords.length }} 个
            </template>
          </span>
          <div class="bulk-buttons">
            <el-button
              type="success"
              plain
              :disabled="selectedPendingGroupKeywords.length === 0"
              @click="handleBatchApproveGroupKeywords"
            >
              <el-icon><Check /></el-icon>
              批量通过
            </el-button>
            <el-button
              type="danger"
              plain
              :disabled="selectedGroupKeywords.length === 0"
              @click="handleBatchDeleteGroupKeywords"
            >
              <el-icon><Delete /></el-icon>
              批量删除
            </el-button>
          </div>
        </div>

        <TableCard
          :columns="searchColumns"
          :data="searchKeywords"
          :total="searchKeywordTotal"
          :loading="loading"
          :page="searchKeywordPage"
          :page-size="searchKeywordPageSize"
          :selection="true"
          row-key="id"
          @page-change="handleGroupPageChange"
          @page-size-change="handleGroupPageSizeChange"
          @selection-change="handleGroupSelectionChange"
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
            <el-tag :type="getGroupReviewTagType(row)" effect="plain">
              {{ getGroupReviewLabel(row) }}
            </el-tag>
          </template>

          <template #used_at="{ row }">
            {{ row.used_at ? formatDate(row.used_at) : '未使用' }}
          </template>

          <template #created_at="{ row }">
            {{ formatDate(row.created_at) }}
          </template>

          <template #actions="{ row }">
            <el-button
              v-if="isGroupKeywordPendingReview(row)"
              type="success"
              link
              size="small"
              @click="handleApproveGroupKeyword(row)"
            >
              <el-icon><Check /></el-icon>
              通过
            </el-button>
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

        <div class="bulk-actions">
          <span class="bulk-summary">
            已选 {{ selectedReplyTriggers.length }} 个
            <template v-if="selectedReplyTriggers.length > 0">
              ，待审核 {{ selectedPendingReplyTriggers.length }} 个
            </template>
          </span>
          <div class="bulk-buttons">
            <el-button
              type="success"
              plain
              :disabled="selectedPendingReplyTriggers.length === 0"
              @click="handleBatchApproveReplyTriggers"
            >
              <el-icon><Check /></el-icon>
              批量通过
            </el-button>
            <el-button
              type="primary"
              plain
              :disabled="selectedReplyTriggers.length === 0"
              @click="openBatchTemplateDrawer"
            >
              <el-icon><Edit /></el-icon>
              绑定模板
            </el-button>
            <el-button
              type="danger"
              plain
              :disabled="selectedReplyTriggers.length === 0"
              @click="handleBatchDeleteReplyTriggers"
            >
              <el-icon><Delete /></el-icon>
              批量删除
            </el-button>
          </div>
        </div>

        <TableCard
          :columns="replyColumns"
          :data="replyTriggers"
          :total="replyTotal"
          :loading="loading"
          :page="replyPage"
          :page-size="replyPageSize"
          :selection="true"
          row-key="id"
          @page-change="handleReplyPageChange"
          @page-size-change="handleReplyPageSizeChange"
          @selection-change="handleReplySelectionChange"
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

          <template #reply_content="{ row }">
            <span class="muted-text">{{ getReplyTemplatePreview(row) }}</span>
          </template>

          <template #enabled="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag>
          </template>

          <template #requires_review="{ row }">
            <el-tag :type="getReplyReviewTagType(row)" effect="plain">
              {{ getReplyReviewLabel(row) }}
            </el-tag>
          </template>

          <template #created_at="{ row }">
            {{ formatDate(row.created_at) }}
          </template>

          <template #actions="{ row }">
            <el-button
              v-if="isReplyTriggerPendingReview(row)"
              type="success"
              link
              size="small"
              @click="handleApproveReplyTrigger(row)"
            >
              <el-icon><Check /></el-icon>
              通过
            </el-button>
            <el-button type="primary" link size="small" @click="openEditReplyDrawer(row)">
              <el-icon><Edit /></el-icon>
              编辑
            </el-button>
            <el-button type="success" link size="small" @click="openTemplateReplyDrawer(row)">
              <el-icon><Edit /></el-icon>
              模板
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
        <el-form-item v-if="replyForm.replyTarget === 'private' && replyForm.privateMode === 'template'" label="统一模板">
          <el-select
            v-model="replyForm.template_id"
            clearable
            filterable
            placeholder="选择统一模板，或留空后填写自定义话术"
            style="width: 100%;"
          >
            <el-option v-for="item in enabledReplyTemplates" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="replyForm.replyTarget === 'private' && replyForm.privateMode === 'template' && selectedReplyTemplate" label="模板预览">
          <div class="template-preview">{{ selectedReplyTemplate.content }}</div>
        </el-form-item>
        <el-form-item v-if="replyForm.replyTarget === 'private' && replyForm.privateMode === 'template' && !replyForm.template_id" label="私聊模板话术" required>
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
        <el-form-item v-if="replyForm.replyTarget === 'group' && replyForm.action === 'reply_template'" label="统一模板">
          <el-select
            v-model="replyForm.template_id"
            clearable
            filterable
            placeholder="选择统一模板，或留空后填写自定义话术"
            style="width: 100%;"
          >
            <el-option v-for="item in enabledReplyTemplates" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="replyForm.replyTarget === 'group' && replyForm.action === 'reply_template' && selectedReplyTemplate" label="模板预览">
          <div class="template-preview">{{ selectedReplyTemplate.content }}</div>
        </el-form-item>
        <el-form-item v-if="replyForm.replyTarget === 'group' && replyForm.action === 'reply_template' && !replyForm.template_id" label="群内模板话术" required>
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
        <el-form-item label="需要审核">
          <el-switch v-model="replyForm.requires_review" />
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
      v-model="templateDrawerVisible"
      title="营销回复模板库"
      size="760px"
      :show-close="false"
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="drawer-header">
          <span class="drawer-title">营销回复模板库</span>
          <el-icon class="close-icon" @click="templateDrawerVisible = false"><Close /></el-icon>
        </div>
      </template>

      <el-form :model="templateForm" label-width="110px" class="template-form">
        <el-form-item label="模板名称" required>
          <el-input v-model="templateForm.name" placeholder="例如：默认私聊引导" />
        </el-form-item>
        <el-form-item label="模板内容" required>
          <el-input
            v-model="templateForm.content"
            type="textarea"
            :rows="5"
            maxlength="5000"
            show-word-limit
            placeholder="支持变量：{{user_name}}、{{group_name}}、{{register_link}}、{{keyword}}"
          />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="templateForm.enabled" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="templateLoading" @click="saveMessageTemplate">
            {{ editingTemplateId ? '更新模板' : '创建模板' }}
          </el-button>
          <el-button @click="resetTemplateForm">清空</el-button>
          <el-button @click="fetchMessageTemplates">刷新</el-button>
        </el-form-item>
      </el-form>

      <TableCard
        :columns="templateColumns"
        :data="replyTemplates"
        :total="0"
        :loading="templateLoading"
        row-key="id"
      >
        <template #name="{ row }">
          <span class="keyword-text">{{ row.name }}</span>
        </template>

        <template #content="{ row }">
          <span class="muted-text">{{ row.content.length > 42 ? `${row.content.slice(0, 42)}...` : row.content }}</span>
        </template>

        <template #enabled="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag>
        </template>

        <template #updated_at="{ row }">
          {{ formatDate(row.updated_at) }}
        </template>

        <template #actions="{ row }">
          <el-button type="primary" link size="small" @click="editMessageTemplate(row)">
            <el-icon><Edit /></el-icon>
            编辑
          </el-button>
          <el-button type="danger" link size="small" @click="deleteMessageTemplate(row)">
            <el-icon><Delete /></el-icon>
            删除
          </el-button>
        </template>
      </TableCard>
    </el-drawer>

    <el-drawer
      v-model="batchTemplateDrawerVisible"
      title="批量绑定模板"
      size="460px"
      :show-close="false"
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="drawer-header">
          <span class="drawer-title">批量绑定模板</span>
          <el-icon class="close-icon" @click="batchTemplateDrawerVisible = false"><Close /></el-icon>
        </div>
      </template>

      <el-form :model="batchTemplateForm" label-width="110px">
        <el-form-item label="已选关键词">
          <el-tag effect="plain">{{ selectedReplyTriggers.length }} 个</el-tag>
        </el-form-item>
        <el-form-item label="统一模板" required>
          <el-select
            v-model="batchTemplateForm.template_id"
            filterable
            placeholder="选择要绑定的模板"
            style="width: 100%;"
          >
            <el-option v-for="item in enabledReplyTemplates" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="回复位置">
          <el-select v-model="batchTemplateForm.reply_target" style="width: 100%;">
            <el-option v-for="item in replyTargetOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="绑定后启用">
          <el-switch v-model="batchTemplateForm.enabled" />
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="drawer-footer">
          <el-button @click="batchTemplateDrawerVisible = false">取消</el-button>
          <el-button type="primary" :loading="templateLoading" @click="handleBatchBindTemplate">确定绑定</el-button>
        </div>
      </template>
    </el-drawer>

    <el-drawer
      v-model="aiDrawerVisible"
      :title="aiDrawerTitle"
      size="500px"
      :show-close="false"
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="drawer-header">
          <span class="drawer-title">{{ aiDrawerTitle }}</span>
          <el-icon class="close-icon" @click="aiDrawerVisible = false"><Close /></el-icon>
        </div>
      </template>

      <el-form :model="aiFormData" label-width="110px">
        <el-form-item :label="aiCategoryLabel">
          <el-select v-model="aiFormData.keyword_type" style="width: 100%;">
            <el-option v-for="item in aiCategoryOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="生成数量">
          <el-input-number v-model="aiFormData.count" :min="1" :max="50" />
        </el-form-item>
        <el-form-item v-if="aiScene === 'group'" label="生成后状态">
          <el-switch
            v-model="aiFormData.auto_approve"
            active-text="免审核直接启用"
            inactive-text="进入待审核"
          />
        </el-form-item>
        <el-form-item v-else label="生成后状态">
          <el-tag type="warning" effect="plain">进入待审核</el-tag>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="aiLoading" @click="handleAIGenerate">
            <el-icon><MagicStick /></el-icon>
            生成关键词
          </el-button>
        </el-form-item>
      </el-form>

      <el-alert
        v-if="aiGenerationNotice"
        class="generation-alert"
        :title="aiGenerationNotice"
        :type="aiGenerationNoticeType"
        show-icon
        :closable="false"
      />

      <div v-if="aiGeneratedKeywords.length > 0" class="generated-list">
        <div class="generated-header">
          <span>生成结果 ({{ aiGeneratedKeywords.length }})</span>
        </div>
        <div class="generated-words">
          <el-tag
            v-for="item in aiGeneratedKeywords"
            :key="item.id"
            class="word-tag"
            :type="getGeneratedKeywordTagType(item)"
          >
            {{ getGeneratedKeywordText(item) }}
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

.bulk-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  margin-bottom: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: #fafafa;
}

.bulk-summary {
  color: #606266;
  font-size: 13px;
}

.bulk-buttons {
  display: flex;
  gap: 8px;
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

.template-form {
  margin-bottom: 16px;
}

.template-preview {
  width: 100%;
  max-height: 140px;
  overflow: auto;
  padding: 10px 12px;
  color: #606266;
  line-height: 1.6;
  white-space: pre-wrap;
  background: #f5f7fa;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
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

.generation-alert {
  margin-bottom: 16px;
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
