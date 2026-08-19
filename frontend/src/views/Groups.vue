<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElAlert, ElButton, ElDrawer, ElIcon, ElMessage, ElMessageBox, ElTable, ElTableColumn, ElTag } from 'element-plus'
import { Delete, Edit, Plus, RefreshRight, User } from '@element-plus/icons-vue'
import { useAccountStore } from '@/stores/account'
import { useGroupStore } from '@/stores/group'
import TableCard from '@/components/TableCard.vue'
import SearchBar from '@/components/SearchBar.vue'
import FormDrawer from '@/components/FormDrawer.vue'
import StatusTag from '@/components/StatusTag.vue'
import dayjs from 'dayjs'
import type { Group, GroupFormData, GroupMember } from '@/api/groups'

const groupStore = useGroupStore()
const accountStore = useAccountStore()

const loading = ref(false)
const drawerVisible = ref(false)
const detailDrawerVisible = ref(false)
const editingId = ref<number | null>(null)
const selectedGroup = ref<Group | null>(null)

const formData = reactive({
  chatId: '',
  title: '',
  username: '',
  memberCount: 0,
  status: 'active',
  discoverySource: 'manual',
  sourceKeyword: '',
  accountId: undefined as number | undefined,
  joinMethod: 'manual',
  level: 'unrated',
})

const formRules = {
  chatId: [{ required: true, message: '请输入群ID', trigger: 'blur' }],
  title: [{ required: true, message: '请输入群名称', trigger: 'blur' }],
}

const accountOptions = computed(() => [
  { label: '暂不关联推广账号', value: undefined },
  ...accountStore.list.map((account) => ({
    label: `${account.display_name || account.identifier} · ${account.status}`,
    value: account.id,
  })),
])

const levelTagType = (level: string) => {
  if (level === 'A') return 'success'
  if (level === 'B') return 'warning'
  if (level === 'C') return 'danger'
  return 'info'
}

const searchFilters = [
  {
    type: 'input' as const,
    key: 'keyword',
    label: '关键词',
    placeholder: '群名 / 群ID / 用户名 / 来源词',
    width: '240px',
  },
  {
    type: 'input' as const,
    key: 'sourceKeyword',
    label: '来源词',
    placeholder: '例如 vpn / airport / proxy',
    width: '180px',
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
      { label: '待入群', value: 'pending_join' },
      { label: '等待审核', value: 'pending' },
      { label: '入群失败', value: 'join_failed' },
      { label: '冷却中', value: 'cooling_down' },
      { label: '已拒绝', value: 'rejected' },
      { label: '停用', value: 'inactive' },
      { label: '已离开', value: 'left' },
      { label: '封禁', value: 'banned' },
    ],
  },
  {
    type: 'select' as const,
    key: 'level',
    label: '群质量',
    placeholder: '全部等级',
    width: '120px',
    options: [
      { label: '全部', value: '' },
      { label: 'A', value: 'A' },
      { label: 'B', value: 'B' },
      { label: 'C', value: 'C' },
      { label: '未评级', value: 'unrated' },
    ],
  },
]

const columns = [
  { prop: 'chatId', label: '群ID', width: '135' },
  { prop: 'title', label: '群池条目', minWidth: '220', slot: 'group' },
  { prop: 'sourceKeyword', label: '来源关键词', minWidth: '150', slot: 'source' },
  { prop: 'accountCount', label: '已入群账号', width: '120', slot: 'account' },
  { prop: 'memberCount', label: '成员数', width: '100', slot: 'memberCount' },
  { prop: 'level', label: '群质量', width: '90', slot: 'level' },
  { prop: 'status', label: '状态', width: '90', slot: 'status' },
  { prop: 'adsSent', label: '广告', width: '85', slot: 'ads' },
  { prop: 'privateMessages', label: '私聊', width: '85', slot: 'privateMessages' },
  { prop: 'repliedUsers', label: '回复用户', width: '100', slot: 'repliedUsers' },
  { prop: 'registeredUsers', label: '注册', width: '85', slot: 'registeredUsers' },
  { prop: 'paidUsers', label: '付费', width: '85', slot: 'paidUsers' },
  { prop: 'conversionRate', label: '转化率', width: '95', slot: 'conversionRate' },
  { prop: 'actions', label: '操作', width: '220', fixed: 'right', slot: 'actions' },
]

const drawerFields = computed(() => [
  { prop: 'chatId', label: '群ID', type: 'input' as const, placeholder: 'Telegram群ID，例如 -100...' },
  { prop: 'title', label: '群名称', type: 'input' as const, placeholder: '群展示名称' },
  { prop: 'username', label: '公开用户名', type: 'input' as const, placeholder: '可选 @username' },
  { prop: 'memberCount', label: '成员数', type: 'number' as const, props: { min: 0, controlsPosition: 'right' } },
  {
    prop: 'status',
    label: '状态',
    type: 'select' as const,
    options: [
      { label: '正常', value: 'active' },
      { label: '待入群', value: 'pending_join' },
      { label: '等待审核', value: 'pending' },
      { label: '入群失败', value: 'join_failed' },
      { label: '冷却中', value: 'cooling_down' },
      { label: '已拒绝', value: 'rejected' },
      { label: '停用', value: 'inactive' },
      { label: '已离开', value: 'left' },
      { label: '封禁', value: 'banned' },
    ],
  },
  {
    prop: 'discoverySource',
    label: '发现来源',
    type: 'select' as const,
    options: [
      { label: '手动录入', value: 'manual' },
      { label: '关键词搜索', value: 'keyword_search' },
      { label: '自动搜群', value: 'auto_keyword_search' },
      { label: '相关群发现', value: 'related_group' },
      { label: '批量导入', value: 'import' },
    ],
  },
  { prop: 'sourceKeyword', label: '来源关键词', type: 'input' as const, placeholder: '发现该群时使用的搜索词' },
  {
    prop: 'level',
    label: '群质量',
    type: 'select' as const,
    options: [
      { label: 'A', value: 'A' },
      { label: 'B', value: 'B' },
      { label: 'C', value: 'C' },
      { label: '未评级', value: 'unrated' },
    ],
  },
  ...(!editingId.value
    ? [
        {
          prop: 'accountId',
          label: '首个推广账号',
          type: 'select' as const,
          placeholder: '可选，记录哪个账号先加入',
          options: accountOptions.value,
          props: { filterable: true },
        },
        {
          prop: 'joinMethod',
          label: '入群方式',
          type: 'select' as const,
          options: [
            { label: '手动记录', value: 'manual' },
            { label: '关键词自动加群', value: 'keyword_auto_join' },
            { label: '邀请链接', value: 'invite_link' },
          ],
        },
      ]
    : []),
])

const fetchData = async (params?: Record<string, any>) => {
  loading.value = true
  try {
    await groupStore.fetchList(params)
  } catch (error) {
    console.error('Failed to fetch groups:', error)
  } finally {
    loading.value = false
  }
}

const fetchAccounts = async () => {
  try {
    await accountStore.fetchList({
      account_type: 'promoter',
      limit: 100,
    })
  } catch (error) {
    console.error('Failed to fetch accounts:', error)
  }
}

const handleSearch = (values: Record<string, any>) => {
  groupStore.setPage(1)
  fetchData(values)
}

const handleReset = () => {
  groupStore.setPage(1)
  fetchData()
}

const handlePageChange = (page: number) => {
  groupStore.setPage(page)
  fetchData()
}

const handlePageSizeChange = (pageSize: number) => {
  groupStore.setPageSize(pageSize)
  fetchData()
}

const openAddDrawer = () => {
  editingId.value = null
  Object.assign(formData, {
    chatId: '',
    title: '',
    username: '',
    memberCount: 0,
    status: 'active',
    discoverySource: 'manual',
    sourceKeyword: '',
    accountId: undefined,
    joinMethod: 'manual',
    level: 'unrated',
  })
  drawerVisible.value = true
}

const openEditDrawer = (row: Group) => {
  editingId.value = row.id
  Object.assign(formData, {
    chatId: row.chatId,
    title: row.title || '',
    username: row.username || '',
    memberCount: row.memberCount || 0,
    status: row.status || 'active',
    discoverySource: row.discoverySource || 'manual',
    sourceKeyword: row.sourceKeyword || '',
    accountId: undefined,
    joinMethod: 'manual',
    level: row.level || 'unrated',
  })
  drawerVisible.value = true
}

const handleSubmit = async () => {
  const payload = { ...formData } as GroupFormData
  try {
    if (editingId.value) {
      await groupStore.update(editingId.value, payload)
      ElMessage.success('群池条目已更新')
    } else {
      await groupStore.create(payload)
      ElMessage.success('群池条目已添加')
    }
    drawerVisible.value = false
  } catch (error) {
    console.error('Failed to save group:', error)
  }
}

const handleDelete = async (row: Group) => {
  try {
    await ElMessageBox.confirm(`确定要删除群池条目 ${row.title || row.chatId} 吗？`, '提示', {
      type: 'warning',
    })
    await groupStore.remove(row.id)
    ElMessage.success('删除成功')
  } catch {
    // cancelled
  }
}

const handleViewMembers = async (row: Group) => {
  try {
    selectedGroup.value = row
    await groupStore.fetchMembers(row.id)
    detailDrawerVisible.value = true
  } catch {
    ElMessage.error('获取入群账号失败')
  }
}

const handleSyncMetrics = async (row: Group) => {
  try {
    await groupStore.syncMetrics(row.id)
    ElMessage.success('群池指标已同步')
  } catch {
    ElMessage.error('同步失败')
  }
}

const formatDate = (date?: string) => (date ? dayjs(date).format('YYYY-MM-DD HH:mm') : '-')
const formatNumber = (value?: number) => Number(value || 0).toLocaleString()

const sourceLabel = (source: string) => {
  const map: Record<string, string> = {
    manual: '手动',
    keyword_search: '关键词',
    auto_keyword_search: '自动搜群',
    related_group: '相关群',
    import: '导入',
    keyword_auto_join: '关键词自动加群',
    invite_link: '邀请链接',
  }
  return map[source] || source || '-'
}

const joinMethodLabel = (member: GroupMember) => sourceLabel(member.joinMethod)

onMounted(() => {
  fetchAccounts()
  fetchData()
})
</script>

<template>
  <div class="groups-page">
    <div class="page-header">
      <div>
        <h2 class="page-title">群池管理</h2>
        <p class="page-desc">增长中心维护的候选群与已入群池，用于搜群、加群、广告投放和转化分析。</p>
      </div>
      <div class="header-actions">
        <el-button type="primary" @click="openAddDrawer">
          <el-icon><Plus /></el-icon>
          添加群池条目
        </el-button>
      </div>
    </div>

    <el-alert
      title="Bot 管理群已独立到“群治理中心 / Bot管理群”，这里仅保留增长侧群池。"
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
      :data="groupStore.list"
      :total="groupStore.total"
      :loading="loading"
      :page="groupStore.page"
      :page-size="groupStore.pageSize"
      row-key="id"
      @page-change="handlePageChange"
      @page-size-change="handlePageSizeChange"
    >
      <template #group="{ row }">
        <div class="group-cell">
          <span class="group-title">{{ row.title || '-' }}</span>
          <span class="group-username">{{ row.username || '无公开用户名' }}</span>
        </div>
      </template>

      <template #source="{ row }">
        <div class="source-cell">
          <el-tag size="small" effect="plain">{{ sourceLabel(row.discoverySource) }}</el-tag>
          <span>{{ row.sourceKeyword || '-' }}</span>
        </div>
      </template>

      <template #account="{ row }">
        <div class="account-cell">
          <span>{{ row.accountCount }} 个推广账号</span>
          <small>{{ row.primaryAccountPhone || '暂无主账号' }}</small>
        </div>
      </template>

      <template #memberCount="{ row }">
        <span class="metric-number">{{ formatNumber(row.memberCount) }}</span>
      </template>

      <template #level="{ row }">
        <el-tag :type="levelTagType(row.level)" effect="plain">
          {{ row.level === 'unrated' ? '未评级' : row.level }}
        </el-tag>
      </template>

      <template #status="{ row }">
        <StatusTag :status="row.status" type="group" />
      </template>

      <template #ads="{ row }">
        <span class="metric-number">{{ formatNumber(row.metrics?.adsSent) }}</span>
      </template>

      <template #privateMessages="{ row }">
        <span class="metric-number">{{ formatNumber(row.metrics?.privateMessages) }}</span>
      </template>

      <template #repliedUsers="{ row }">
        <span class="metric-number">{{ formatNumber(row.metrics?.repliedUsers) }}</span>
      </template>

      <template #registeredUsers="{ row }">
        <span class="metric-number">{{ formatNumber(row.metrics?.registeredUsers) }}</span>
      </template>

      <template #paidUsers="{ row }">
        <span class="metric-number">{{ formatNumber(row.metrics?.paidUsers) }}</span>
      </template>

      <template #conversionRate="{ row }">
        <span class="conversion-rate">{{ row.metrics?.conversionRate || 0 }}%</span>
      </template>

      <template #actions="{ row }">
        <el-button type="primary" link size="small" @click="openEditDrawer(row)">
          <el-icon><Edit /></el-icon>
          编辑
        </el-button>
        <el-button type="info" link size="small" @click="handleViewMembers(row)">
          <el-icon><User /></el-icon>
          入群账号
        </el-button>
        <el-button type="success" link size="small" @click="handleSyncMetrics(row)">
          <el-icon><RefreshRight /></el-icon>
          同步指标
        </el-button>
        <el-button type="danger" link size="small" @click="handleDelete(row)">
          <el-icon><Delete /></el-icon>
          删除
        </el-button>
      </template>
    </TableCard>

    <FormDrawer
      v-model:visible="drawerVisible"
      :title="editingId ? '编辑群池条目' : '新增群池条目'"
      :fields="drawerFields"
      :model-value="formData"
      :rules="formRules"
      width="560px"
      @confirm="handleSubmit"
    />

    <el-drawer
      v-model="detailDrawerVisible"
      :title="selectedGroup ? `${selectedGroup.title || selectedGroup.chatId} 的入群账号` : '入群账号'"
      direction="rtl"
      size="680px"
      :show-close="false"
    >
      <el-table :data="groupStore.members" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="accountPhone" label="推广账号" min-width="160" />
        <el-table-column prop="joinMethod" label="入群方式" width="130">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ joinMethodLabel(row as GroupMember) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="sourceKeyword" label="来源词" min-width="120">
          <template #default="{ row }">
            {{ row.sourceKeyword || '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="joinedAt" label="加入时间" width="160">
          <template #default="{ row }">
            {{ formatDate(row.joinedAt) }}
          </template>
        </el-table-column>
      </el-table>

      <div class="member-statistics">
        <span>已记录推广账号数: {{ groupStore.memberTotal }}</span>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped lang="scss">
.groups-page {
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

.group-cell,
.source-cell,
.account-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.group-title {
  font-weight: 600;
  color: #303133;
}

.group-username,
.account-cell small {
  color: #909399;
  font-size: 12px;
}

.metric-number {
  font-weight: 600;
  color: #409eff;
}

.conversion-rate {
  font-weight: 600;
  color: #67c23a;
}

.member-statistics {
  margin-top: 16px;
  padding: 16px;
  background: #f5f7fa;
  border-radius: 4px;
  color: #606266;
}
</style>
