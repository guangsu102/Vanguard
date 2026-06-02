<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElButton, ElIcon, ElMessage, ElMessageBox, ElTag, ElProgress } from 'element-plus'
import { Plus, Refresh, Upload, Download, Delete, Edit, Connection, DeleteSolid } from '@element-plus/icons-vue'
import { useProxyStore } from '@/stores/proxy'
import TableCard from '@/components/TableCard.vue'
import SearchBar from '@/components/SearchBar.vue'
import FormDrawer from '@/components/FormDrawer.vue'
import StatusTag from '@/components/StatusTag.vue'
import dayjs from 'dayjs'

const proxyStore = useProxyStore()

const loading = ref(false)
const drawerVisible = ref(false)
const editingId = ref<number | null>(null)
const testingId = ref<number | null>(null)

const formData = reactive({
  address: '',
  port: 8080,
  protocol: 'http' as 'http' | 'https' | 'socks5',
  username: '',
  password: '',
})

const formRules = {
  address: [{ required: true, message: '请输入代理地址', trigger: 'blur' }],
  port: [{ required: true, message: '请输入端口', trigger: 'blur' }],
  protocol: [{ required: true, message: '请选择协议', trigger: 'change' }],
}

const searchFilters = [
  {
    type: 'input' as const,
    key: 'keyword',
    label: '关键词',
    placeholder: '地址/端口',
    width: '180px',
  },
  {
    type: 'select' as const,
    key: 'protocol',
    label: '协议',
    placeholder: '全部协议',
    width: '120px',
    options: [
      { label: '全部', value: '' },
      { label: 'HTTP', value: 'http' },
      { label: 'HTTPS', value: 'https' },
      { label: 'SOCKS5', value: 'socks5' },
    ],
  },
  {
    type: 'select' as const,
    key: 'status',
    label: '状态',
    placeholder: '全部状态',
    width: '120px',
    options: [
      { label: '全部', value: '' },
      { label: '正常', value: 'active' },
      { label: '停用', value: 'inactive' },
      { label: '异常', value: 'error' },
    ],
  },
]

const columns = [
  { prop: 'id', label: 'ID', width: '80' },
  { prop: 'address', label: '地址', minWidth: '150' },
  { prop: 'port', label: '端口', width: '100' },
  { prop: 'protocol', label: '协议', width: '100', slot: 'protocol' },
  { prop: 'latency', label: '延迟', width: '150', slot: 'latency' },
  { prop: 'status', label: '状态', width: '100', slot: 'status' },
  { prop: 'bindAccountPhone', label: '绑定账号', width: '130', slot: 'bindAccount' },
  { prop: 'lastCheckedAt', label: '最后检测', width: '180', slot: 'lastChecked' },
  { prop: 'createdAt', label: '创建时间', width: '180', slot: 'createdAt' },
  { prop: 'actions', label: '操作', width: '160', fixed: 'right', slot: 'actions' },
]

const fetchData = async (params?: Record<string, any>) => {
  loading.value = true
  try {
    await proxyStore.fetchList(params)
  } catch (error) {
    console.error('Failed to fetch proxies:', error)
  } finally {
    loading.value = false
  }
}

const handleSearch = (values: Record<string, any>) => {
  proxyStore.setPage(1)
  fetchData(values)
}

const handleReset = () => {
  proxyStore.setPage(1)
  fetchData()
}

const handlePageChange = (page: number) => {
  proxyStore.setPage(page)
  fetchData()
}

const handlePageSizeChange = (pageSize: number) => {
  proxyStore.setPageSize(pageSize)
  fetchData()
}

const openAddDrawer = () => {
  editingId.value = null
  Object.assign(formData, { address: '', port: 8080, protocol: 'http', username: '', password: '' })
  drawerVisible.value = true
}

const openEditDrawer = (row: any) => {
  editingId.value = row.id
  Object.assign(formData, {
    address: row.address,
    port: row.port,
    protocol: row.protocol,
    username: row.username || '',
    password: '',
  })
  drawerVisible.value = true
}

const handleSubmit = async () => {
  try {
    if (editingId.value) {
      await proxyStore.update(editingId.value, formData)
      ElMessage.success('更新成功')
    } else {
      await proxyStore.create(formData)
      ElMessage.success('添加成功')
    }
    drawerVisible.value = false
  } catch (error) {
    console.error('Failed to save proxy:', error)
  }
}

const handleDelete = async (row: any) => {
  try {
    await ElMessageBox.confirm(`确定要删除代理 ${row.address}:${row.port} 吗？`, '提示', {
      type: 'warning',
    })
    await proxyStore.remove(row.id)
    ElMessage.success('删除成功')
  } catch {
    // cancelled
  }
}

const handleTest = async (row: any) => {
  testingId.value = row.id
  try {
    const result = await proxyStore.testLatency(row.id)
    if (result.latency < 0) {
      ElMessage.error('连接超时')
    } else {
      ElMessage.success(`延迟: ${result.latency}ms`)
    }
  } catch (error) {
    ElMessage.error('测试失败')
  } finally {
    testingId.value = null
  }
}

const handleRefreshStatus = async () => {
  loading.value = true
  try {
    await proxyStore.refreshStatus()
    ElMessage.success('刷新成功')
  } catch (error) {
    ElMessage.error('刷新失败')
  } finally {
    loading.value = false
  }
}

const handleExport = () => {
  window.open('/api/proxies/export', '_blank')
}

const formatDate = (date: string) => {
  return date ? dayjs(date).format('YYYY-MM-DD HH:mm') : '-'
}

const getLatencyColor = (latency: number | undefined) => {
  if (latency === undefined || latency < 0) return '#f56c6c'
  if (latency < 100) return '#67c23a'
  if (latency < 500) return '#e6a23c'
  return '#f56c6c'
}

const getLatencyText = (latency: number | undefined) => {
  if (latency === undefined || latency < 0) return '超时'
  return `${latency}ms`
}

onMounted(() => {
  fetchData()
})
</script>

<template>
  <div class="proxies-page">
    <div class="page-header">
      <h2 class="page-title">代理管理</h2>
      <div class="header-actions">
        <el-button @click="handleExport">
          <el-icon><Download /></el-icon>
          导出
        </el-button>
        <el-button @click="handleRefreshStatus">
          <el-icon><Refresh /></el-icon>
          刷新状态
        </el-button>
        <el-button type="primary" @click="openAddDrawer">
          <el-icon><Plus /></el-icon>
          添加代理
        </el-button>
      </div>
    </div>

    <SearchBar
      :filters="searchFilters"
      :loading="loading"
      @search="handleSearch"
      @reset="handleReset"
    />

    <TableCard
      :columns="columns"
      :data="proxyStore.list"
      :total="proxyStore.total"
      :loading="loading"
      :page="proxyStore.page"
      :page-size="proxyStore.pageSize"
      row-key="id"
      @page-change="handlePageChange"
      @page-size-change="handlePageSizeChange"
    >
      <template #protocol="{ row }">
        <el-tag :type="row.protocol === 'socks5' ? 'success' : row.protocol === 'https' ? 'warning' : 'primary'" effect="plain">
          {{ row.protocol?.toUpperCase() || 'N/A' }}
        </el-tag>
      </template>

      <template #latency="{ row }">
        <div class="latency-cell">
          <span :style="{ color: getLatencyColor(row.latency) }">
            {{ getLatencyText(row.latency) }}
          </span>
          <el-progress
            v-if="row.latency !== undefined && row.latency >= 0"
            :percentage="Math.max(100 - row.latency, 0)"
            :stroke-width="4"
            :show-text="false"
            :color="getLatencyColor(row.latency)"
            style="width: 60px; margin-left: 8px;"
          />
        </div>
      </template>

      <template #status="{ row }">
        <StatusTag :status="row.status" type="proxy" />
      </template>

      <template #bindAccount="{ row }">
        <span v-if="row.bindAccountPhone">{{ row.bindAccountPhone }}</span>
        <span v-else class="text-muted">未绑定</span>
      </template>

      <template #lastChecked="{ row }">
        {{ formatDate(row.lastCheckedAt) }}
      </template>

      <template #createdAt="{ row }">
        {{ formatDate(row.createdAt) }}
      </template>

      <template #actions="{ row }">
        <el-button type="primary" link size="small" @click="openEditDrawer(row)">
          <el-icon><Edit /></el-icon>
          编辑
        </el-button>
        <el-button
          type="success"
          link
          size="small"
          :loading="testingId === row.id"
          @click="handleTest(row)"
        >
          <el-icon><Connection /></el-icon>
          测试
        </el-button>
        <el-button type="danger" link size="small" @click="handleDelete(row)">
          <el-icon><Delete /></el-icon>
          删除
        </el-button>
      </template>
    </TableCard>

    <FormDrawer
      v-model:visible="drawerVisible"
      :title="editingId ? '编辑代理' : '添加代理'"
      :fields="[
        { prop: 'address', label: '地址', type: 'input', placeholder: '例如: 192.168.1.1' },
        { prop: 'port', label: '端口', type: 'number', placeholder: '例如: 8080', props: { min: 1, max: 65535 } },
        {
          prop: 'protocol',
          label: '协议',
          type: 'select',
          options: [
            { label: 'HTTP', value: 'http' },
            { label: 'HTTPS', value: 'https' },
            { label: 'SOCKS5', value: 'socks5' },
          ]
        },
        { prop: 'username', label: '用户名', type: 'input', placeholder: '可选' },
        { prop: 'password', label: '密码', type: 'input', placeholder: '可选' },
      ]"
      :model-value="formData"
      :rules="formRules"
      @confirm="handleSubmit"
    />
  </div>
</template>

<style scoped lang="scss">
.proxies-page {
  padding: 0;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.page-title {
  font-size: 20px;
  font-weight: 600;
  margin: 0;
  color: #303133;
}

.header-actions {
  display: flex;
  gap: 12px;
}

.latency-cell {
  display: flex;
  align-items: center;
}

.text-muted {
  color: #c0c4cc;
}
</style>
