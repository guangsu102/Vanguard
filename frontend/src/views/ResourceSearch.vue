<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Check,
  CircleClose,
  Delete,
  EditPen,
  Link,
  Refresh,
  Search,
} from '@element-plus/icons-vue'
import {
  resourceSearchApi,
  type ResourceReviewStatus,
  type ResourceSearchAccount,
  type ResourceSearchResult,
  type ResourceSearchRun,
  type ResourceSearchStatus,
} from '@/api/resourceSearch'
import { PAGE_SIZE_OPTIONS } from '@/utils/pagination'

const accounts = ref<ResourceSearchAccount[]>([])
const runs = ref<ResourceSearchRun[]>([])
const currentRun = ref<ResourceSearchRun | null>(null)
const results = ref<ResourceSearchResult[]>([])
const selectedRunId = ref<number>()
const accountLoading = ref(false)
const runLoading = ref(false)
const resultLoading = ref(false)
const creating = ref(false)
const cancelling = ref(false)
const deleting = ref(false)
const runTotal = ref(0)
const runPage = ref(1)
const runPageSize = 20
const resultTotal = ref(0)
const page = ref(1)
const pageSize = ref(20)
const noteDialogVisible = ref(false)
const noteSaving = ref(false)
const editingResult = ref<ResourceSearchResult | null>(null)
const editingNote = ref('')
const accountSelectionInitialized = ref(false)
let pollTimer: number | undefined

const form = reactive({
  keywordsText: '',
  accountIds: [] as number[],
  maxResultsPerKeyword: 20,
})

const filters = reactive({
  search: '',
  keyword: '',
  reviewStatus: '' as ResourceReviewStatus | '',
  minMembers: undefined as number | undefined,
})

const reviewOptions: Array<{ label: string; value: ResourceReviewStatus }> = [
  { label: '待分析', value: 'pending' },
  { label: '候选', value: 'shortlisted' },
  { label: '已确认', value: 'confirmed' },
  { label: '排除', value: 'rejected' },
]

const statusLabels: Record<ResourceSearchStatus, string> = {
  queued: '排队中',
  running: '搜索中',
  completed: '已完成',
  partial: '部分完成',
  failed: '失败',
  cancelled: '已取消',
}

const statusTagTypes: Record<ResourceSearchStatus, 'info' | 'primary' | 'success' | 'warning' | 'danger'> = {
  queued: 'info',
  running: 'primary',
  completed: 'success',
  partial: 'warning',
  failed: 'danger',
  cancelled: 'info',
}

const reviewTagTypes: Record<ResourceReviewStatus, 'info' | 'warning' | 'success' | 'danger'> = {
  pending: 'info',
  shortlisted: 'warning',
  confirmed: 'success',
  rejected: 'danger',
}

const parsedKeywords = computed(() => {
  const seen = new Set<string>()
  return form.keywordsText
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter((item) => {
      const key = item.toLocaleLowerCase()
      if (!item || seen.has(key)) return false
      seen.add(key)
      return true
    })
})

const runProgress = computed(() => {
  if (!currentRun.value?.total_accounts) return 0
  return Math.round(
    (currentRun.value.completed_accounts / currentRun.value.total_accounts) * 100,
  )
})

const formatDateTime = (value?: string) => {
  if (!value) return '-'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

const formatNumber = (value: number) => new Intl.NumberFormat('zh-CN').format(value || 0)
const asResourceResult = (row: unknown) => row as ResourceSearchResult

const loadAccounts = async () => {
  accountLoading.value = true
  try {
    accounts.value = await resourceSearchApi.listAccounts()
    const selectableIds = accounts.value
      .filter((account) => account.session_available)
      .map((account) => account.id)
    if (!accountSelectionInitialized.value) {
      form.accountIds = selectableIds
      accountSelectionInitialized.value = true
    } else {
      const selectableSet = new Set(selectableIds)
      form.accountIds = form.accountIds.filter((accountId) => selectableSet.has(accountId))
    }
  } finally {
    accountLoading.value = false
  }
}

const loadRuns = async (selectLatest = false) => {
  runLoading.value = true
  try {
    const payload = await resourceSearchApi.listRuns(runPage.value, runPageSize)
    runs.value = payload.list
    runTotal.value = payload.total
    if ((selectLatest || !selectedRunId.value) && runs.value.length) {
      selectedRunId.value = runs.value[0].id
    }
  } finally {
    runLoading.value = false
  }
}

const loadResults = async () => {
  if (!selectedRunId.value) {
    results.value = []
    resultTotal.value = 0
    return
  }
  resultLoading.value = true
  try {
    const payload = await resourceSearchApi.listResults(selectedRunId.value, {
      page: page.value,
      page_size: pageSize.value,
      search: filters.search.trim() || undefined,
      keyword: filters.keyword || undefined,
      review_status: filters.reviewStatus || undefined,
      min_members: filters.minMembers,
    })
    results.value = payload.list
    resultTotal.value = payload.total
  } finally {
    resultLoading.value = false
  }
}

const loadCurrentRun = async () => {
  if (!selectedRunId.value) {
    currentRun.value = null
    return
  }
  currentRun.value = await resourceSearchApi.getRun(selectedRunId.value)
}

const selectRun = async () => {
  page.value = 1
  await Promise.all([loadCurrentRun(), loadResults()])
}

const refresh = async () => {
  await Promise.all([loadAccounts(), loadRuns()])
  if (selectedRunId.value) {
    await Promise.all([loadCurrentRun(), loadResults()])
  }
}

const selectAllAccounts = () => {
  form.accountIds = accounts.value
    .filter((account) => account.session_available)
    .map((account) => account.id)
}

const startSearch = async () => {
  if (!parsedKeywords.value.length) {
    ElMessage.warning('请输入至少一个关键词')
    return
  }
  if (!form.accountIds.length) {
    ElMessage.warning('请选择至少一个在线账号')
    return
  }
  creating.value = true
  try {
    const run = await resourceSearchApi.createRun({
      keywords: parsedKeywords.value,
      account_ids: form.accountIds,
      max_results_per_keyword: form.maxResultsPerKeyword,
    })
    ElMessage.success('资源搜索已排队')
    runPage.value = 1
    await loadRuns(true)
    selectedRunId.value = run.id
    await selectRun()
  } finally {
    creating.value = false
  }
}

const handleRunPageChange = async (value: number) => {
  runPage.value = value
  selectedRunId.value = undefined
  currentRun.value = null
  await loadRuns(true)
  if (selectedRunId.value) await selectRun()
}

const cancelCurrentRun = async () => {
  if (!currentRun.value || !['queued', 'running'].includes(currentRun.value.status)) return
  await ElMessageBox.confirm(
    '将停止后续关键词和账号搜索，已发现的结果会保留。',
    '取消搜索批次',
    { type: 'warning', confirmButtonText: '确认取消', cancelButtonText: '继续搜索' },
  )
  cancelling.value = true
  try {
    currentRun.value = await resourceSearchApi.cancelRun(currentRun.value.id)
    ElMessage.success(
      currentRun.value.status === 'cancelled' ? '搜索已取消' : '取消请求已提交',
    )
    await Promise.all([loadRuns(), loadCurrentRun(), loadResults()])
  } finally {
    cancelling.value = false
  }
}

const deleteCurrentRun = async () => {
  if (!currentRun.value || ['queued', 'running'].includes(currentRun.value.status)) return
  await ElMessageBox.confirm(
    '将删除该批次及其全部分析结果，此操作不可撤销。',
    '删除搜索批次',
    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
  )
  deleting.value = true
  try {
    await resourceSearchApi.deleteRun(currentRun.value.id)
    ElMessage.success('搜索批次已删除')
    selectedRunId.value = undefined
    currentRun.value = null
    results.value = []
    await loadRuns(true)
    if (selectedRunId.value) await selectRun()
  } finally {
    deleting.value = false
  }
}

const applyFilters = async () => {
  page.value = 1
  await loadResults()
}

const resetFilters = async () => {
  filters.search = ''
  filters.keyword = ''
  filters.reviewStatus = ''
  filters.minMembers = undefined
  page.value = 1
  await loadResults()
}

const handlePageChange = async (value: number) => {
  page.value = value
  await loadResults()
}

const handlePageSizeChange = async (value: number) => {
  pageSize.value = value
  page.value = 1
  await loadResults()
}

const updateReviewStatus = async (
  row: ResourceSearchResult,
  reviewStatus: ResourceReviewStatus,
) => {
  try {
    await resourceSearchApi.updateResult(row.id, { review_status: reviewStatus })
    row.review_status = reviewStatus
    ElMessage.success('分析状态已保存')
  } catch {
    await loadResults()
  }
}

const openNoteDialog = (row: ResourceSearchResult) => {
  editingResult.value = row
  editingNote.value = row.note || ''
  noteDialogVisible.value = true
}

const saveNote = async () => {
  if (!editingResult.value) return
  noteSaving.value = true
  try {
    await resourceSearchApi.updateResult(editingResult.value.id, {
      note: editingNote.value,
    })
    editingResult.value.note = editingNote.value.trim() || undefined
    noteDialogVisible.value = false
    ElMessage.success('备注已保存')
  } finally {
    noteSaving.value = false
  }
}

const startPolling = () => {
  pollTimer = window.setInterval(async () => {
    if (!currentRun.value || !['queued', 'running'].includes(currentRun.value.status)) return
    await Promise.all([loadCurrentRun(), loadRuns(), loadResults()])
  }, 3000)
}

onMounted(async () => {
  await Promise.all([loadAccounts(), loadRuns()])
  if (selectedRunId.value) await selectRun()
  startPolling()
})

onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="resource-search-page">
    <header class="page-header">
      <div>
        <h2>资源搜索</h2>
        <el-tag type="info" effect="plain">只读检索</el-tag>
      </div>
      <el-tooltip content="刷新账号、批次和结果" placement="bottom">
        <el-button :icon="Refresh" circle :loading="runLoading" @click="refresh" />
      </el-tooltip>
    </header>

    <section class="search-tool">
      <div class="field keywords-field">
        <label>关键词</label>
        <el-input
          v-model="form.keywordsText"
          type="textarea"
          :rows="4"
          maxlength="2000"
          show-word-limit
          placeholder="每行一个关键词"
        />
        <span class="field-meta">{{ parsedKeywords.length }} 个关键词</span>
      </div>

      <div class="field accounts-field">
        <div class="field-label-row">
          <label>在线账号</label>
          <el-button link type="primary" :icon="Check" @click="selectAllAccounts">全选</el-button>
        </div>
        <el-select
          v-model="form.accountIds"
          multiple
          collapse-tags
          collapse-tags-tooltip
          filterable
          :loading="accountLoading"
          placeholder="选择执行搜索的账号"
        >
          <el-option
            v-for="account in accounts"
            :key="account.id"
            :label="account.display_name || account.identifier"
            :value="account.id"
            :disabled="!account.session_available"
          >
            <div class="account-option">
              <span>{{ account.display_name || account.identifier }}</span>
              <div>
                <el-tag v-if="account.operation_mode === 'ad_only'" size="small" type="warning">专投</el-tag>
                <el-tag size="small" type="success">{{ account.status === 'online' ? '在线' : '空闲' }}</el-tag>
              </div>
            </div>
          </el-option>
        </el-select>
        <span class="field-meta">{{ form.accountIds.length }} / {{ accounts.length }} 个账号</span>
      </div>

      <div class="field limit-field">
        <label>每账号/关键词结果上限</label>
        <el-input-number
          v-model="form.maxResultsPerKeyword"
          :min="5"
          :max="50"
          :step="5"
          controls-position="right"
        />
      </div>

      <el-button
        class="start-button"
        type="primary"
        :icon="Search"
        :loading="creating"
        @click="startSearch"
      >
        开始搜索
      </el-button>
    </section>

    <section class="run-section">
      <div class="run-selector">
        <label>搜索批次</label>
        <el-select
          v-model="selectedRunId"
          :loading="runLoading"
          placeholder="暂无搜索批次"
          @change="selectRun"
        >
          <el-option
            v-for="run in runs"
            :key="run.id"
            :label="`#${run.id} · ${run.keywords.join('、')} · ${formatDateTime(run.created_at)}`"
            :value="run.id"
          />
        </el-select>
        <el-tag
          v-if="currentRun"
          :type="statusTagTypes[currentRun.status]"
          effect="light"
        >
          {{ statusLabels[currentRun.status] }}
        </el-tag>
        <div v-if="currentRun" class="run-actions">
          <el-tooltip
            v-if="['queued', 'running'].includes(currentRun.status)"
            content="取消后保留已发现结果"
            placement="top"
          >
            <el-button
              :icon="CircleClose"
              circle
              type="warning"
              :loading="cancelling"
              :disabled="Boolean(currentRun.cancel_requested_at)"
              @click="cancelCurrentRun"
            />
          </el-tooltip>
          <el-tooltip v-else content="删除当前搜索批次" placement="top">
            <el-button
              :icon="Delete"
              circle
              type="danger"
              plain
              :loading="deleting"
              @click="deleteCurrentRun"
            />
          </el-tooltip>
        </div>
      </div>
      <div v-if="runTotal > runPageSize" class="run-pagination">
        <el-pagination
          small
          background
          layout="total, prev, pager, next"
          :total="runTotal"
          :current-page="runPage"
          :page-size="runPageSize"
          @update:current-page="handleRunPageChange"
        />
      </div>

      <template v-if="currentRun">
        <div class="run-metrics">
          <div class="metric">
            <span>账号进度</span>
            <b>{{ currentRun.completed_accounts }} / {{ currentRun.total_accounts }}</b>
          </div>
          <div class="metric">
            <span>原始发现</span>
            <b>{{ formatNumber(currentRun.raw_result_count) }}</b>
          </div>
          <div class="metric">
            <span>去重结果</span>
            <b>{{ formatNumber(currentRun.unique_result_count) }}</b>
          </div>
          <div class="metric">
            <span>合并重复</span>
            <b>{{ formatNumber(Math.max(0, currentRun.raw_result_count - currentRun.unique_result_count)) }}</b>
          </div>
        </div>

        <el-progress
          v-if="['queued', 'running'].includes(currentRun.status)"
          :percentage="runProgress"
          :stroke-width="8"
          :show-text="false"
        />

        <el-alert
          v-if="currentRun.error_summary"
          class="run-error"
          type="warning"
          :closable="false"
          :title="currentRun.error_summary"
          show-icon
        />

        <el-collapse v-if="currentRun.accounts?.length" class="account-progress">
          <el-collapse-item title="账号执行明细" name="accounts">
            <el-table :data="currentRun.accounts" size="small" max-height="260">
              <el-table-column prop="account_identifier" label="账号" min-width="180" />
              <el-table-column label="状态" width="110">
                <template #default="{ row }">
                  <el-tag :type="statusTagTypes[row.status as ResourceSearchStatus]" size="small">
                    {{ statusLabels[row.status as ResourceSearchStatus] }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="keywords_completed" label="已搜索词" width="100" />
              <el-table-column prop="successful_keywords" label="成功词" width="90" />
              <el-table-column prop="result_count" label="发现" width="90" />
              <el-table-column prop="error" label="异常" min-width="220" show-overflow-tooltip />
            </el-table>
          </el-collapse-item>
        </el-collapse>
      </template>
    </section>

    <section class="result-section">
      <div class="filter-bar">
        <el-input
          v-model="filters.search"
          :prefix-icon="Search"
          clearable
          placeholder="群名或用户名"
          @keyup.enter="applyFilters"
        />
        <el-select v-model="filters.keyword" clearable placeholder="命中关键词">
          <el-option
            v-for="keyword in currentRun?.keywords || []"
            :key="keyword"
            :label="keyword"
            :value="keyword"
          />
        </el-select>
        <el-input-number
          v-model="filters.minMembers"
          :min="0"
          :step="100"
          controls-position="right"
          placeholder="最少人数"
        />
        <el-select v-model="filters.reviewStatus" clearable placeholder="分析状态">
          <el-option
            v-for="option in reviewOptions"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-button type="primary" :icon="Search" @click="applyFilters">筛选</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </div>

      <el-table
        v-loading="resultLoading"
        :data="results"
        row-key="id"
        empty-text="当前批次暂无结果"
      >
        <el-table-column label="群信息" min-width="250" fixed="left">
          <template #default="{ row }">
            <div class="group-cell">
              <strong>{{ row.title || '未命名群' }}</strong>
              <span v-if="row.username">@{{ row.username }}</span>
              <span v-else>私有结果</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="成员" width="110" sortable prop="member_count">
          <template #default="{ row }">{{ formatNumber(row.member_count) }}</template>
        </el-table-column>
        <el-table-column label="命中关键词" min-width="190">
          <template #default="{ row }">
            <div class="tag-list">
              <el-tag
                v-for="keyword in row.matched_keywords"
                :key="keyword"
                size="small"
                effect="plain"
              >
                {{ keyword }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="发现账号" min-width="180">
          <template #default="{ row }">
            <div class="discovery-cell">
              <span>{{ row.discovered_by_accounts.length }} 个账号</span>
              <el-tooltip
                :content="row.discovered_by_accounts.map((item: any) => item.identifier).join('、')"
                placement="top"
              >
                <el-tag size="small" type="info">{{ row.discovery_count }} 次发现</el-tag>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="分析状态" width="140">
          <template #default="{ row }">
            <el-select
              :model-value="row.review_status"
              size="small"
              @change="(value: ResourceReviewStatus) => updateReviewStatus(asResourceResult(row), value)"
            >
              <el-option
                v-for="option in reviewOptions"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              >
                <el-tag :type="reviewTagTypes[option.value]" size="small">
                  {{ option.label }}
                </el-tag>
              </el-option>
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="180" show-overflow-tooltip prop="note" />
        <el-table-column label="操作" width="108" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-tooltip content="打开 Telegram 群" placement="top">
                <el-button
                  :icon="Link"
                  circle
                  size="small"
                  :disabled="!row.invite_link"
                  tag="a"
                  :href="row.invite_link"
                  target="_blank"
                />
              </el-tooltip>
              <el-tooltip content="编辑分析备注" placement="top">
                <el-button
                  :icon="EditPen"
                  circle
                  size="small"
                  @click="openNoteDialog(asResourceResult(row))"
                />
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination">
        <el-pagination
          background
          layout="total, sizes, prev, pager, next"
          :total="resultTotal"
          :current-page="page"
          :page-size="pageSize"
          :page-sizes="PAGE_SIZE_OPTIONS"
          @update:current-page="handlePageChange"
          @update:page-size="handlePageSizeChange"
        />
      </div>
    </section>

    <el-dialog v-model="noteDialogVisible" title="分析备注" width="min(520px, 92vw)">
      <el-input
        v-model="editingNote"
        type="textarea"
        :rows="6"
        maxlength="4000"
        show-word-limit
      />
      <template #footer>
        <el-button @click="noteDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="noteSaving" @click="saveNote">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.resource-search-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 40px;

  > div {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  h2 {
    margin: 0;
    color: #20242b;
    font-size: 24px;
    line-height: 1.4;
    letter-spacing: 0;
  }
}

.search-tool {
  display: grid;
  grid-template-columns: minmax(260px, 1.2fr) minmax(260px, 1fr) 180px 132px;
  gap: 16px;
  align-items: end;
  padding: 18px;
  border: 1px solid #dfe3e8;
  border-radius: 6px;
  background: #fff;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;

  label {
    color: #30343b;
    font-size: 14px;
    font-weight: 600;
    letter-spacing: 0;
  }
}

.field-label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 24px;
}

.field-meta {
  min-height: 18px;
  color: #717985;
  font-size: 12px;
}

.accounts-field :deep(.el-select),
.limit-field :deep(.el-input-number) {
  width: 100%;
}

.account-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;

  > div {
    display: flex;
    gap: 6px;
  }
}

.start-button {
  width: 132px;
  height: 40px;
  margin-bottom: 26px;
}

.run-section,
.result-section {
  min-width: 0;
  border-top: 1px solid #dfe3e8;
  padding-top: 16px;
}

.run-selector {
  display: grid;
  grid-template-columns: auto minmax(280px, 620px) auto auto;
  gap: 12px;
  align-items: center;

  label {
    color: #30343b;
    font-size: 14px;
    font-weight: 600;
  }
}

.run-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 40px;
}

.run-pagination {
  display: flex;
  justify-content: flex-start;
  padding-top: 12px;
  overflow-x: auto;
}

.run-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(120px, 1fr));
  gap: 1px;
  margin: 16px 0;
  overflow: hidden;
  border: 1px solid #dfe3e8;
  border-radius: 6px;
  background: #dfe3e8;
}

.metric {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 70px;
  padding: 13px 16px;
  background: #fff;

  span {
    color: #717985;
    font-size: 12px;
  }

  b {
    color: #20242b;
    font-size: 20px;
    letter-spacing: 0;
  }
}

.run-error {
  margin-top: 12px;
}

.account-progress {
  margin-top: 12px;
}

.filter-bar {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) 180px 160px 150px auto auto;
  gap: 10px;
  margin-bottom: 14px;
}

.group-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;

  strong {
    overflow: hidden;
    color: #20242b;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  span {
    overflow: hidden;
    color: #717985;
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.tag-list,
.row-actions,
.discovery-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
  overflow-x: auto;
}

@media (max-width: 1180px) {
  .search-tool {
    grid-template-columns: 1fr 1fr;
  }

  .start-button {
    width: 100%;
    margin-bottom: 26px;
  }

  .filter-bar {
    grid-template-columns: repeat(3, minmax(140px, 1fr));
  }
}

@media (max-width: 720px) {
  .search-tool,
  .filter-bar {
    grid-template-columns: 1fr;
  }

  .start-button {
    margin-bottom: 0;
  }

  .run-selector {
    grid-template-columns: 1fr;
  }

  .run-metrics {
    grid-template-columns: 1fr 1fr;
  }

  .pagination {
    justify-content: flex-start;
  }
}
</style>
