<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElAlert, ElButton, ElIcon, ElMessage, ElMessageBox, ElTag } from 'element-plus'
import { ChatDotRound, CircleCheck, CircleClose, Delete, Edit, Plus, UserFilled } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import TableCard from '@/components/TableCard.vue'
import SearchBar from '@/components/SearchBar.vue'
import FormDrawer from '@/components/FormDrawer.vue'
import StatusTag from '@/components/StatusTag.vue'
import AccountLoginDialog from '@/components/AccountLoginDialog.vue'
import { useAccountStore } from '@/stores/account'
import type { Account } from '@/api/accounts'
import { useRouter } from 'vue-router'

const router = useRouter()
const accountStore = useAccountStore()

const loading = ref(false)
const drawerVisible = ref(false)
const loginDialogVisible = ref(false)
const editingId = ref<number | null>(null)

const formData = reactive({
  display_name: '',
  country_code: 'US',
  fingerprint_id: '',
  is_active: true,
})

const formRules = {
  display_name: [{ required: true, message: '请输入显示名称', trigger: 'blur' }],
  country_code: [{ required: true, message: '请输入国家代码', trigger: 'blur' }],
}

const searchFilters = [
  {
    type: 'input' as const,
    key: 'search',
    label: '关键词',
    placeholder: '手机号 / 标识 / 显示名称',
    width: '220px',
  },
  {
    type: 'select' as const,
    key: 'status_filter',
    label: '状态',
    placeholder: '全部状态',
    width: '140px',
    options: [
      { label: '全部', value: '' },
      { label: '在线', value: 'online' },
      { label: '离线', value: 'offline' },
      { label: '工作中', value: 'working' },
      { label: '空闲', value: 'idle' },
      { label: '异常', value: 'error' },
      { label: '封禁', value: 'banned' },
    ],
  },
  {
    type: 'input' as const,
    key: 'country_code',
    label: '国家',
    placeholder: '如 US / HK',
    width: '120px',
  },
]

const columns = [
  { prop: 'identifier', label: '账号标识', minWidth: '220', slot: 'identifier' },
  { prop: 'status', label: '状态', width: '110', slot: 'status' },
  { prop: 'country_code', label: '国家/地区', width: '120', slot: 'country' },
  { prop: 'api_config_name', label: 'API配置', minWidth: '120' },
  { prop: 'connection_count', label: '连接数', width: '90' },
  { prop: 'error_count', label: '错误数', width: '90' },
  { prop: 'last_active_at', label: '最近活跃', width: '170', slot: 'lastActive' },
  { prop: 'created_at', label: '创建时间', width: '170', slot: 'createdAt' },
  { prop: 'actions', label: '操作', width: '220', fixed: 'right', slot: 'actions' },
]

const promoterAccounts = computed(() => accountStore.list.filter((item) => item.account_type === 'promoter'))

const fetchData = async (params?: Record<string, any>) => {
  loading.value = true
  try {
    accountStore.setAccountTypeFilter('promoter')
    await accountStore.fetchList({
      account_type: 'promoter',
      ...params,
    })
  } finally {
    loading.value = false
  }
}

const handleSearch = (values: Record<string, any>) => {
  accountStore.setPage(1)
  fetchData(values)
}

const handleReset = () => {
  accountStore.setPage(1)
  fetchData()
}

const handlePageChange = (page: number) => {
  accountStore.setPage(page)
  fetchData()
}

const handlePageSizeChange = (pageSize: number) => {
  accountStore.setPageSize(pageSize)
  fetchData()
}

const openAddDrawer = () => {
  loginDialogVisible.value = true
}

const handleLoginSuccess = () => {
  fetchData()
}

const openEditDrawer = (row: Account) => {
  editingId.value = row.id
  Object.assign(formData, {
    display_name: row.display_name || row.identifier,
    country_code: row.country_code || 'US',
    fingerprint_id: row.fingerprint_id || '',
    is_active: row.is_active,
  })
  drawerVisible.value = true
}

const handleSubmit = async () => {
  if (!editingId.value) return
  try {
    await accountStore.update(editingId.value, {
      display_name: formData.display_name.trim(),
      country_code: formData.country_code.trim().toUpperCase(),
      fingerprint_id: formData.fingerprint_id.trim() || undefined,
      is_active: formData.is_active,
    })
    ElMessage.success('推广账号已更新')
    drawerVisible.value = false
  } catch (error) {
    console.error('Failed to save account:', error)
  }
}

const handleDelete = async (row: Account) => {
  try {
    await ElMessageBox.confirm(`确定要删除推广账号 ${row.display_name || row.identifier} 吗？`, '提示', {
      type: 'warning',
    })
    await accountStore.remove(row.id)
    ElMessage.success('删除成功')
  } catch {
    // cancelled
  }
}

const handleEnable = async (row: Account) => {
  try {
    await accountStore.enable(row.id)
    ElMessage.success('账号已连接')
  } catch (error) {
    console.error('Failed to connect account:', error)
  }
}

const handleDisable = async (row: Account) => {
  try {
    await accountStore.disable(row.id)
    ElMessage.success('账号已断开')
  } catch (error) {
    console.error('Failed to disconnect account:', error)
  }
}

const formatDate = (date?: string) => (date ? dayjs(date).format('YYYY-MM-DD HH:mm') : '-')

const goToGuardianBots = () => {
  router.push('/guardian/bots')
}

onMounted(() => {
  fetchData()
})
</script>

<template>
  <div class="accounts-page">
    <div class="page-header">
      <div>
        <h2 class="page-title">推广账号</h2>
        <p class="page-desc">这里只管理用于搜群、加群、广告投放和私聊引导的推广账号。</p>
      </div>
      <div class="header-actions">
        <el-button @click="goToGuardianBots">
          <el-icon><ChatDotRound /></el-icon>
          查看Bot账号
        </el-button>
        <el-button type="primary" @click="openAddDrawer">
          <el-icon><Plus /></el-icon>
          添加推广账号
        </el-button>
      </div>
    </div>

    <el-alert
      title="群治理 Bot 已独立到“群治理中心”，不会再和推广账号混用。"
      type="info"
      :closable="false"
      show-icon
      class="page-alert"
    />

    <SearchBar
      :filters="searchFilters"
      :loading="loading"
      @search="handleSearch"
      @reset="handleReset"
    />

    <TableCard
      :columns="columns"
      :data="promoterAccounts"
      :total="accountStore.total"
      :loading="loading"
      :page="accountStore.page"
      :page-size="accountStore.pageSize"
      row-key="id"
      @page-change="handlePageChange"
      @page-size-change="handlePageSizeChange"
    >
      <template #identifier="{ row }">
        <div class="identifier-cell">
          <div class="primary-line">
            <el-icon><UserFilled /></el-icon>
            <span>{{ row.display_name || row.identifier }}</span>
          </div>
          <div class="secondary-line">
            <span>{{ row.phone || row.identifier }}</span>
            <span>{{ row.session_name }}</span>
          </div>
        </div>
      </template>

      <template #status="{ row }">
        <div class="status-cell">
          <StatusTag :status="row.status" type="account" />
          <el-tag :type="row.is_active ? 'success' : 'info'" effect="plain">
            {{ row.is_active ? '已启用' : '已停用' }}
          </el-tag>
        </div>
      </template>

      <template #country="{ row }">
        <span>{{ row.country_code }}{{ row.country_name ? ` / ${row.country_name}` : '' }}</span>
      </template>

      <template #lastActive="{ row }">
        {{ formatDate(row.last_active_at || row.last_connected_at) }}
      </template>

      <template #createdAt="{ row }">
        {{ formatDate(row.created_at) }}
      </template>

      <template #actions="{ row }">
        <el-button type="primary" link size="small" @click="openEditDrawer(row)">
          <el-icon><Edit /></el-icon>
          编辑
        </el-button>
        <el-button
          v-if="row.is_active"
          type="warning"
          link
          size="small"
          @click="handleDisable(row)"
        >
          <el-icon><CircleClose /></el-icon>
          断开
        </el-button>
        <el-button
          v-else
          type="success"
          link
          size="small"
          @click="handleEnable(row)"
        >
          <el-icon><CircleCheck /></el-icon>
          连接
        </el-button>
        <el-button type="danger" link size="small" @click="handleDelete(row)">
          <el-icon><Delete /></el-icon>
          删除
        </el-button>
      </template>
    </TableCard>

    <AccountLoginDialog
      v-model:visible="loginDialogVisible"
      @success="handleLoginSuccess"
    />

    <FormDrawer
      v-model:visible="drawerVisible"
      title="编辑推广账号"
      :fields="[
        { prop: 'display_name', label: '显示名称', type: 'input', placeholder: '便于运营识别的名称' },
        { prop: 'country_code', label: '国家代码', type: 'input', placeholder: '如 US / SG / HK' },
        { prop: 'fingerprint_id', label: '指纹ID', type: 'input', placeholder: '可选' },
        { prop: 'is_active', label: '启用', type: 'switch' },
      ]"
      :model-value="formData"
      :rules="formRules"
      width="520px"
      @confirm="handleSubmit"
    />
  </div>
</template>

<style scoped lang="scss">
.accounts-page {
  padding: 0;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 16px;
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

.page-alert {
  margin-bottom: 16px;
}

.header-actions {
  display: flex;
  gap: 12px;
}

.identifier-cell,
.status-cell {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.primary-line,
.secondary-line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.primary-line {
  font-weight: 600;
  color: #303133;
}

.secondary-line {
  color: #909399;
  font-size: 12px;
  flex-wrap: wrap;
}
</style>
