<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Close, Refresh, Select, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import dayjs from 'dayjs'

import { accountsApi, type Account } from '@/api/accounts'
import { automationApi, type GroupFailoverStatus, type GroupFailoverTask } from '@/api/automation'

const loading = ref(false)
const running = ref('')
const tasks = ref<GroupFailoverTask[]>([])
const accounts = ref<Account[]>([])
const summary = ref<Partial<Record<GroupFailoverStatus, number>>>({})
const total = ref(0)
const statusFilter = ref<GroupFailoverStatus | ''>('')
const page = ref(1)
const pageSize = ref(20)
const form = reactive({
  max_tasks: 20,
  dry_run: false,
  target_account_ids: [] as number[],
})

const statusOptions: Array<{ label: string; value: GroupFailoverStatus }> = [
  { label: '待接管', value: 'queued' },
  { label: '接管中', value: 'joining' },
  { label: '等待重试', value: 'retry' },
  { label: '已恢复', value: 'succeeded' },
  { label: '需人工处理', value: 'manual_required' },
  { label: '失败', value: 'failed' },
  { label: '已取消', value: 'cancelled' },
]

const availableAccounts = computed(() =>
  accounts.value.filter((account) => account.is_active && !['banned', 'error'].includes(account.status || '')),
)
const pendingTotal = computed(() =>
  (['queued', 'joining', 'retry', 'manual_required', 'failed'] as GroupFailoverStatus[]).reduce(
    (sum, status) => sum + (summary.value[status] || 0),
    0,
  ),
)

const accountLabel = (account: Account) =>
  account.display_name || account.phone || account.identifier || `#${account.id}`
const statusText = (value: GroupFailoverStatus) => statusOptions.find((item) => item.value === value)?.label || value
const statusType = (value: GroupFailoverStatus) => {
  if (value === 'succeeded') return 'success'
  if (value === 'failed') return 'danger'
  if (value === 'retry' || value === 'manual_required') return 'warning'
  if (value === 'joining') return 'primary'
  return 'info'
}
const formatTime = (value?: string) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-')

const loadTasks = async () => {
  loading.value = true
  try {
    const response = await automationApi.getGroupFailoverTasks({
      status: statusFilter.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    tasks.value = response.data.data
    total.value = response.data.total
    summary.value = response.data.summary || {}
  } finally {
    loading.value = false
  }
}

const loadAccounts = async () => {
  const response = await accountsApi.list({
    account_type: 'promoter',
    limit: 100,
  })
  accounts.value = response.list
}

const runRecovery = async (withSelectedAccounts = false) => {
  if (withSelectedAccounts && !form.target_account_ids.length) {
    ElMessage.warning('请先选择接管账号')
    return
  }
  if (withSelectedAccounts) {
    try {
      await ElMessageBox.confirm(
        `将待接管群按负载均衡分配给 ${form.target_account_ids.length} 个账号，确认继续？`,
        '一键分配接管账号',
        {
          type: 'warning',
          confirmButtonText: '确认分配',
          cancelButtonText: '取消',
        },
      )
    } catch {
      return
    }
  }

  running.value = withSelectedAccounts ? 'assign' : 'scan'
  try {
    await automationApi.runGroupFailover({
      max_tasks: form.max_tasks,
      dry_run: form.dry_run,
      target_account_ids: withSelectedAccounts ? [...form.target_account_ids] : undefined,
    })
    ElMessage.success('群资源恢复任务已下发')
    await loadTasks()
  } finally {
    running.value = ''
  }
}

const retryTask = async (task: any) => {
  await automationApi.retryGroupFailoverTask(task.id)
  ElMessage.success('恢复任务已重新排队')
  await loadTasks()
}

const cancelTask = async (task: any) => {
  try {
    await ElMessageBox.confirm(`确认取消「${task.group_title || task.telegram_group_id}」的恢复任务？`, '取消恢复', {
      type: 'warning',
    })
  } catch {
    return
  }
  await automationApi.cancelGroupFailoverTask(task.id)
  ElMessage.success('恢复任务已取消')
  await loadTasks()
}

const handleStatusChange = async () => {
  page.value = 1
  await loadTasks()
}
const handlePageChange = async (value: number) => {
  page.value = value
  await loadTasks()
}
const handlePageSizeChange = async (value: number) => {
  pageSize.value = value
  page.value = 1
  await loadTasks()
}

onMounted(() => Promise.all([loadAccounts(), loadTasks()]))
</script>

<template>
  <section class="group-operation-panel" v-loading="loading">
    <div class="panel-toolbar">
      <div>
        <h3>封号群资源恢复</h3>
        <div class="summary-tags">
          <el-tag type="warning" effect="plain">待处理 {{ pendingTotal }}</el-tag>
          <el-tag type="success" effect="plain">已恢复 {{ summary.succeeded || 0 }}</el-tag>
          <el-tag v-if="summary.manual_required" type="warning" effect="plain">
            人工 {{ summary.manual_required }}
          </el-tag>
        </div>
      </div>
      <el-select
        v-model="statusFilter"
        clearable
        placeholder="全部状态"
        class="status-filter"
        @change="handleStatusChange"
      >
        <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
      </el-select>
    </div>

    <el-form inline class="failover-toolbar">
      <el-form-item label="接管账号">
        <el-select
          v-model="form.target_account_ids"
          multiple
          filterable
          clearable
          collapse-tags
          collapse-tags-tooltip
          placeholder="选择可用账号"
          class="account-select"
        >
          <el-option
            v-for="account in availableAccounts"
            :key="account.id"
            :label="accountLabel(account)"
            :value="account.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="单轮任务数">
        <el-input-number v-model="form.max_tasks" :min="1" :max="100" />
      </el-form-item>
      <el-form-item label="预演">
        <el-switch v-model="form.dry_run" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :icon="VideoPlay" :loading="running === 'scan'" @click="runRecovery(false)">
          执行恢复扫描
        </el-button>
        <el-button :icon="Select" :loading="running === 'assign'" @click="runRecovery(true)"> 分配所选账号 </el-button>
        <el-button :icon="Refresh" :loading="loading" @click="loadTasks">刷新</el-button>
      </el-form-item>
    </el-form>

    <el-table :data="tasks" row-key="id" border>
      <el-table-column label="群" min-width="190">
        <template #default="{ row }">
          <div>{{ row.group_title || row.telegram_group_id }}</div>
          <small v-if="row.group_username">@{{ row.group_username }}</small>
        </template>
      </el-table-column>
      <el-table-column label="源账号" min-width="150">
        <template #default="{ row }">{{ row.source_account_label || `#${row.source_account_id}` }}</template>
      </el-table-column>
      <el-table-column label="接管账号" min-width="150">
        <template #default="{ row }">{{
          row.target_account_label || (row.target_account_id ? `#${row.target_account_id}` : '-')
        }}</template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" effect="plain">{{ statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="reason" label="原因" min-width="170" show-overflow-tooltip />
      <el-table-column prop="attempt_count" label="尝试" width="70" />
      <el-table-column label="下次处理" width="180">
        <template #default="{ row }">{{ formatTime(row.next_retry_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="!['succeeded', 'cancelled', 'joining'].includes(row.status)"
            link
            type="primary"
            :icon="Refresh"
            @click="retryTask(row)"
            >重试</el-button
          >
          <el-button
            v-if="!['succeeded', 'cancelled'].includes(row.status)"
            link
            type="danger"
            :icon="Close"
            @click="cancelTask(row)"
            >取消</el-button
          >
        </template>
      </el-table-column>
    </el-table>

    <div class="pagination-bar">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50, 100]"
        background
        layout="total, sizes, prev, pager, next, jumper"
        @current-change="handlePageChange"
        @size-change="handlePageSizeChange"
      />
    </div>
  </section>
</template>

<style scoped>
.panel-toolbar {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 16px;
}
.panel-toolbar h3 {
  margin: 0 0 8px;
  font-size: 17px;
}
.summary-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.status-filter {
  width: 180px;
}
.account-select {
  width: 320px;
}
.pagination-bar {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
  overflow-x: auto;
}
@media (max-width: 768px) {
  .panel-toolbar {
    flex-direction: column;
  }
  .account-select {
    width: min(100%, 320px);
  }
  .pagination-bar {
    justify-content: flex-start;
  }
}
</style>
