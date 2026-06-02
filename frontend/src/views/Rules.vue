<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  ElAlert,
  ElButton,
  ElDialog,
  ElForm,
  ElFormItem,
  ElIcon,
  ElInput,
  ElInputNumber,
  ElMessage,
  ElMessageBox,
  ElOption,
  ElSelect,
  ElTag,
} from 'element-plus'
import { Check, Close, MagicStick, Refresh } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import TableCard from '@/components/TableCard.vue'
import SearchBar from '@/components/SearchBar.vue'
import { moderationApi, type ModerationSuggestion, type ViolationRecord, type SuggestionStatus } from '@/api/moderation'
import { normalizeListPayload } from '@/utils/pagination'

const loading = ref(false)
const generating = ref(false)
const generateDialogVisible = ref(false)
const selectedSuggestionIds = ref<number[]>([])

const suggestions = ref<ModerationSuggestion[]>([])
const suggestionTotal = ref(0)
const suggestionPage = ref(1)
const suggestionPageSize = ref(20)
const suggestionSearchParams = ref<Record<string, any>>({})

const violations = ref<ViolationRecord[]>([])
const violationTotal = ref(0)
const stats = ref({
  total: 0,
  pending: 0,
  approved: 0,
  rejected: 0,
  by_category: {} as Record<string, { pending: number; approved: number; rejected: number }>,
})

const generateForm = reactive({
  category: 'sensitive',
  match_mode: 'fuzzy',
  samplesText: '',
  sampleCount: 20,
})

const statCards = computed(() => [
  { label: '待审核', value: stats.value.pending, type: 'warning' },
  { label: '已批准', value: stats.value.approved, type: 'success' },
  { label: '已拒绝', value: stats.value.rejected, type: 'info' },
  { label: '总候选', value: stats.value.total, type: 'primary' },
])

const searchFilters = [
  {
    type: 'select' as const,
    key: 'status_filter',
    label: '状态',
    placeholder: '全部状态',
    width: '140px',
    options: [
      { label: '全部', value: '' },
      { label: '待审核', value: 'pending' },
      { label: '已批准', value: 'approved' },
      { label: '已拒绝', value: 'rejected' },
    ],
  },
  {
    type: 'input' as const,
    key: 'category',
    label: '分类',
    placeholder: '如 competitor / sensitive',
    width: '180px',
  },
]

const suggestionColumns = [
  { prop: 'keyword', label: '候选词', minWidth: '160', slot: 'keyword' },
  { prop: 'category', label: '分类', width: '130', slot: 'category' },
  { prop: 'confidence', label: '置信度', width: '100', slot: 'confidence' },
  { prop: 'source_sample', label: '来源样本', minWidth: '260', slot: 'source_sample' },
  { prop: 'status', label: '状态', width: '100', slot: 'status' },
  { prop: 'created_at', label: '生成时间', width: '170', slot: 'created_at' },
  { prop: 'actions', label: '操作', width: '170', fixed: 'right', slot: 'actions' },
]

const violationColumns = [
  { prop: 'group_id', label: '群组', width: '100' },
  { prop: 'rule_type', label: '命中类型', width: '120' },
  { prop: 'rule_pattern', label: '命中规则', minWidth: '180' },
  { prop: 'action_taken', label: '处罚动作', width: '100', slot: 'action_taken' },
  { prop: 'content', label: '违规样本', minWidth: '260', slot: 'content' },
  { prop: 'created_at', label: '时间', width: '170', slot: 'created_at' },
]

const loadStats = async () => {
  const response = await moderationApi.getStats()
  stats.value = response.data.data
}

const loadSuggestions = async (params?: Record<string, any>) => {
  loading.value = true
  try {
    if (params) suggestionSearchParams.value = params
    const response = await moderationApi.listSuggestions({
      page: suggestionPage.value,
      page_size: suggestionPageSize.value,
      ...suggestionSearchParams.value,
    })
    const payload = normalizeListPayload<ModerationSuggestion>(response.data)
    suggestions.value = payload.list
    suggestionTotal.value = payload.total
  } finally {
    loading.value = false
  }
}

const loadViolations = async () => {
  const response = await moderationApi.listViolations({ page: 1, page_size: 10 })
  const payload = normalizeListPayload<ViolationRecord>(response.data)
  violations.value = payload.list
  violationTotal.value = payload.total
}

const refreshPage = async () => {
  await Promise.all([loadStats(), loadSuggestions(), loadViolations()])
}

const handleSearch = (values: Record<string, any>) => {
  suggestionPage.value = 1
  loadSuggestions(values)
}

const handleReset = () => {
  suggestionPage.value = 1
  loadSuggestions({})
}

const handlePageChange = (page: number) => {
  suggestionPage.value = page
  loadSuggestions()
}

const handlePageSizeChange = (pageSize: number) => {
  suggestionPageSize.value = pageSize
  suggestionPage.value = 1
  loadSuggestions()
}

const handleSelectionChange = (rows: ModerationSuggestion[]) => {
  selectedSuggestionIds.value = rows.map((item) => item.id)
}

const approveSuggestion = async (row: ModerationSuggestion) => {
  await moderationApi.approveSuggestion(row.id)
  ElMessage.success('候选词已写入群管敏感词库')
  await refreshPage()
}

const rejectSuggestion = async (row: ModerationSuggestion) => {
  try {
    await ElMessageBox.confirm(`确定拒绝候选词 "${row.keyword}" 吗？`, '提示', { type: 'warning' })
    await moderationApi.rejectSuggestion(row.id)
    ElMessage.success('候选词已拒绝')
    await refreshPage()
  } catch {
    // cancelled
  }
}

const batchReview = async (action: 'approve' | 'reject') => {
  if (selectedSuggestionIds.value.length === 0) {
    ElMessage.warning('请先选择候选词')
    return
  }
  await moderationApi.batchReview({
    suggestion_ids: selectedSuggestionIds.value,
    action,
  })
  ElMessage.success(action === 'approve' ? '批量批准完成' : '批量拒绝完成')
  selectedSuggestionIds.value = []
  await refreshPage()
}

const openGenerateDialog = () => {
  Object.assign(generateForm, {
    category: 'sensitive',
    match_mode: 'fuzzy',
    samplesText: violations.value
      .slice(0, 5)
      .map((item) => item.content)
      .filter(Boolean)
      .join('\n'),
    sampleCount: 20,
  })
  generateDialogVisible.value = true
}

const handleGenerate = async () => {
  const samples = generateForm.samplesText
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, generateForm.sampleCount)

  if (samples.length === 0) {
    ElMessage.warning('请先输入违规样本')
    return
  }

  generating.value = true
  try {
    await moderationApi.generateSuggestions({
      samples,
      category: generateForm.category,
      match_mode: generateForm.match_mode,
    })
    ElMessage.success('候选词已生成并进入审核队列')
    generateDialogVisible.value = false
    await refreshPage()
  } finally {
    generating.value = false
  }
}

const formatDate = (date?: string) => (date ? dayjs(date).format('YYYY-MM-DD HH:mm') : '-')

const statusTagType = (status: SuggestionStatus) => {
  if (status === 'pending') return 'warning'
  if (status === 'approved') return 'success'
  return 'info'
}

const actionTagType = (action: string) => {
  if (action === 'ban') return 'danger'
  if (action === 'mute') return 'warning'
  if (action === 'kick') return 'info'
  return 'success'
}

onMounted(() => {
  refreshPage()
})
</script>

<template>
  <div class="rules-page">
    <div class="page-header">
      <div>
        <h2 class="page-title">审核规则</h2>
        <p class="page-desc">这里只审核群治理候选敏感词，不再维护营销关键词或搜群关键词。</p>
      </div>
      <div class="header-actions">
        <el-button @click="refreshPage">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button @click="openGenerateDialog">
          <el-icon><MagicStick /></el-icon>
          AI生成候选
        </el-button>
      </div>
    </div>

    <el-alert
      title="批准后将写入“群管敏感词库”，不会写入增长中心的搜群词或营销触发词。"
      type="info"
      :closable="false"
      show-icon
      class="page-alert"
    />

    <div class="stat-grid">
      <div v-for="card in statCards" :key="card.label" class="stat-card">
        <div class="stat-label">{{ card.label }}</div>
        <div class="stat-value">{{ card.value }}</div>
      </div>
    </div>

    <SearchBar
      :filters="searchFilters"
      :loading="loading"
      @search="handleSearch"
      @reset="handleReset"
    />

    <div class="section-header">
      <div class="section-title">候选审核队列</div>
      <div class="section-actions">
        <el-button type="success" plain @click="batchReview('approve')">批量批准</el-button>
        <el-button type="warning" plain @click="batchReview('reject')">批量拒绝</el-button>
      </div>
    </div>

    <TableCard
      :columns="suggestionColumns"
      :data="suggestions"
      :total="suggestionTotal"
      :loading="loading"
      :page="suggestionPage"
      :page-size="suggestionPageSize"
      :selection="true"
      row-key="id"
      @page-change="handlePageChange"
      @page-size-change="handlePageSizeChange"
      @selection-change="handleSelectionChange"
    >
      <template #keyword="{ row }">
        <span class="keyword-chip">{{ row.keyword }}</span>
      </template>

      <template #category="{ row }">
        <el-tag effect="plain">{{ row.category }}</el-tag>
      </template>

      <template #confidence="{ row }">
        {{ Math.round((row.confidence || 0) * 100) }}%
      </template>

      <template #source_sample="{ row }">
        <span class="sample-text">{{ row.source_sample || '-' }}</span>
      </template>

      <template #status="{ row }">
        <el-tag :type="statusTagType(row.status)" effect="plain">
          {{ row.status }}
        </el-tag>
      </template>

      <template #created_at="{ row }">
        {{ formatDate(row.created_at) }}
      </template>

      <template #actions="{ row }">
        <template v-if="row.status === 'pending'">
          <el-button type="success" link size="small" @click="approveSuggestion(row)">
            <el-icon><Check /></el-icon>
            批准
          </el-button>
          <el-button type="danger" link size="small" @click="rejectSuggestion(row)">
            <el-icon><Close /></el-icon>
            拒绝
          </el-button>
        </template>
        <span v-else class="muted-text">已处理</span>
      </template>
    </TableCard>

    <div class="section-header">
      <div class="section-title">最近违规样本</div>
      <div class="section-subtitle">可直接复制样本到 AI 候选生成流程。</div>
    </div>

    <TableCard
      :columns="violationColumns"
      :data="violations"
      :total="violationTotal"
      :loading="loading"
      :page="1"
      :page-size="10"
      row-key="id"
    >
      <template #action_taken="{ row }">
        <el-tag :type="actionTagType(row.action_taken)" effect="plain">{{ row.action_taken }}</el-tag>
      </template>

      <template #content="{ row }">
        <span class="sample-text">{{ row.content || '-' }}</span>
      </template>

      <template #created_at="{ row }">
        {{ formatDate(row.created_at) }}
      </template>
    </TableCard>

    <el-dialog v-model="generateDialogVisible" title="AI生成敏感词候选" width="720px">
      <el-form label-width="120px">
        <el-form-item label="候选分类">
          <el-input v-model="generateForm.category" placeholder="如 competitor / sensitive" />
        </el-form-item>
        <el-form-item label="匹配模式">
          <el-select v-model="generateForm.match_mode" style="width: 100%">
            <el-option label="模糊匹配" value="fuzzy" />
            <el-option label="精确匹配" value="exact" />
            <el-option label="正则建议" value="regex" />
          </el-select>
        </el-form-item>
        <el-form-item label="样本上限">
          <el-input-number v-model="generateForm.sampleCount" :min="1" :max="100" />
        </el-form-item>
        <el-form-item label="违规样本">
          <el-input
            v-model="generateForm.samplesText"
            type="textarea"
            :rows="12"
            placeholder="每行一条违规样本，AI 会从这些样本里提炼候选敏感词。"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="generateDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="generating" @click="handleGenerate">生成候选</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.rules-page {
  padding: 0;
}

.page-header,
.section-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}

.page-header {
  margin-bottom: 16px;
}

.page-title {
  margin: 0;
  color: #303133;
  font-size: 20px;
  font-weight: 600;
}

.page-desc,
.section-subtitle,
.muted-text {
  color: #606266;
}

.page-desc {
  margin: 6px 0 0;
}

.page-alert,
.stat-grid,
.section-header {
  margin-bottom: 16px;
}

.header-actions,
.section-actions {
  display: flex;
  gap: 12px;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(160px, 1fr));
  gap: 12px;
}

.stat-card {
  padding: 16px;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
}

.stat-label {
  color: #909399;
  font-size: 13px;
}

.stat-value {
  margin-top: 8px;
  color: #303133;
  font-size: 24px;
  font-weight: 700;
}

.section-title {
  color: #303133;
  font-size: 16px;
  font-weight: 600;
}

.keyword-chip {
  display: inline-block;
  padding: 2px 8px;
  background: #f5f7fa;
  border-radius: 4px;
  font-family: 'Monaco', 'Menlo', monospace;
}

.sample-text {
  color: #606266;
  line-height: 1.6;
}

@media (max-width: 1100px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(160px, 1fr));
  }
}
</style>
