<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElButton, ElIcon, ElMessage, ElMessageBox, ElTag, ElDrawer, ElTable, ElEmpty, ElTimeline, ElTimelineItem } from 'element-plus'
import { View, UserDelete, Mute, Unlock, RemoveFilled, CircleCheckFilled, CircleCloseFilled } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import TableCard from '@/components/TableCard.vue'
import SearchBar from '@/components/SearchBar.vue'
import StatusTag from '@/components/StatusTag.vue'
import dayjs from 'dayjs'

const userStore = useUserStore()

const loading = ref(false)
const detailDrawerVisible = ref(false)
const currentUserId = ref<number | null>(null)

const searchFilters = [
  {
    type: 'input' as const,
    key: 'keyword',
    label: '关键词',
    placeholder: '用户名/TG ID',
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
      { label: '已禁言', value: 'muted' },
      { label: '已封禁', value: 'banned' },
    ],
  },
  {
    type: 'date-range' as const,
    key: 'dateRange',
    label: '注册时间',
    width: '320px',
  },
]

const columns = [
  { prop: 'id', label: 'ID', width: '80' },
  { prop: 'tgUserId', label: 'TG用户ID', width: '120' },
  { prop: 'displayName', label: '昵称', minWidth: '120', slot: 'displayName' },
  { prop: 'username', label: '用户名', width: '150' },
  { prop: 'status', label: '状态', width: '100', slot: 'status' },
  { prop: 'sourceGroupName', label: '来源群组', minWidth: '150' },
  { prop: 'registeredAt', label: '注册时间', width: '180', slot: 'registeredAt' },
  { prop: 'lastActiveAt', label: '最后活跃', width: '180', slot: 'lastActive' },
  { prop: 'actions', label: '操作', width: '200', fixed: 'right', slot: 'actions' },
]

const fetchData = async (params?: Record<string, any>) => {
  loading.value = true
  try {
    // Handle date range filter
    if (params?.dateRange) {
      params.registeredFrom = params.dateRange[0]
      params.registeredTo = params.dateRange[1]
      delete params.dateRange
    }
    await userStore.fetchList(params)
  } catch (error) {
    console.error('Failed to fetch users:', error)
  } finally {
    loading.value = false
  }
}

const handleSearch = (values: Record<string, any>) => {
  userStore.setPage(1)
  fetchData(values)
}

const handleReset = () => {
  userStore.setPage(1)
  fetchData()
}

const handlePageChange = (page: number) => {
  userStore.setPage(page)
  fetchData()
}

const handlePageSizeChange = (pageSize: number) => {
  userStore.setPageSize(pageSize)
  fetchData()
}

const handleViewDetail = async (row: any) => {
  currentUserId.value = row.id
  detailDrawerVisible.value = true
  try {
    await userStore.getById(row.id)
    await userStore.fetchActivities(row.id)
  } catch (error) {
    ElMessage.error('获取用户详情失败')
  }
}

const handleMute = async (row: any) => {
  try {
    await userStore.mute(row.id, { reason: '手动禁言' })
    ElMessage.success('禁言成功')
  } catch (error) {
    ElMessage.error('禁言失败')
  }
}

const handleUnmute = async (row: any) => {
  try {
    await userStore.unmute(row.id)
    ElMessage.success('解除禁言成功')
  } catch (error) {
    ElMessage.error('操作失败')
  }
}

const handleBlacklist = async (row: any) => {
  try {
    await userStore.blacklist(row.id, { reason: '手动封禁' })
    ElMessage.success('加入黑名单成功')
  } catch (error) {
    ElMessage.error('操作失败')
  }
}

const handleRemoveBlacklist = async (row: any) => {
  try {
    await userStore.removeBlacklist(row.id)
    ElMessage.success('移出黑名单成功')
  } catch (error) {
    ElMessage.error('操作失败')
  }
}

const formatDate = (date: string) => {
  return date ? dayjs(date).format('YYYY-MM-DD HH:mm') : '-'
}

const getActivityIcon = (type: string) => {
  switch (type) {
    case 'register':
      return 'User'
    case 'message':
      return 'ChatLineSquare'
    case 'campaign':
      return 'Gift'
    case 'punishment':
      return 'Warning'
    default:
      return 'Document'
  }
}

onMounted(() => {
  fetchData()
})
</script>

<template>
  <div class="users-page">
    <div class="page-header">
      <h2 class="page-title">用户管理</h2>
    </div>

    <SearchBar
      :filters="searchFilters"
      :loading="loading"
      @search="handleSearch"
      @reset="handleReset"
    />

    <TableCard
      :columns="columns"
      :data="userStore.list"
      :total="userStore.total"
      :loading="loading"
      :page="userStore.page"
      :page-size="userStore.pageSize"
      row-key="id"
      @page-change="handlePageChange"
      @page-size-change="handlePageSizeChange"
    >
      <template #displayName="{ row }">
        <div class="user-info">
          <el-avatar v-if="row.avatar" :src="row.avatar" :size="32" />
          <span>{{ row.displayName || '-' }}</span>
        </div>
      </template>

      <template #status="{ row }">
        <StatusTag :status="row.status" type="user" />
      </template>

      <template #registeredAt="{ row }">
        {{ formatDate(row.registeredAt) }}
      </template>

      <template #lastActive="{ row }">
        {{ formatDate(row.lastActiveAt) }}
      </template>

      <template #actions="{ row }">
        <el-button type="primary" link size="small" @click="handleViewDetail(row)">
          <el-icon><View /></el-icon>
          详情
        </el-button>
        <el-button
          v-if="row.status === 'active'"
          type="warning"
          link
          size="small"
          @click="handleMute(row)"
        >
          <el-icon><Mute /></el-icon>
          禁言
        </el-button>
        <el-button
          v-else-if="row.status === 'muted'"
          type="success"
          link
          size="small"
          @click="handleUnmute(row)"
        >
          <el-icon><Unlock /></el-icon>
          解除禁言
        </el-button>
        <el-button
          v-if="row.status !== 'banned'"
          type="danger"
          link
          size="small"
          @click="handleBlacklist(row)"
        >
          <el-icon><RemoveFilled /></el-icon>
          黑名单
        </el-button>
        <el-button
          v-else
          type="success"
          link
          size="small"
          @click="handleRemoveBlacklist(row)"
        >
          <el-icon><CircleCheckFilled /></el-icon>
          移出黑名单
        </el-button>
      </template>
    </TableCard>

    <el-drawer
      v-model="detailDrawerVisible"
      title="用户详情"
      direction="rtl"
      size="50%"
    >
      <template v-if="userStore.currentUser">
        <div class="user-detail">
          <div class="detail-header">
            <el-avatar :src="userStore.currentUser.avatar" :size="80" />
            <div class="user-basic">
              <h3>{{ userStore.currentUser.displayName || '-' }}</h3>
              <p>@{{ userStore.currentUser.username || '-' }}</p>
              <StatusTag :status="userStore.currentUser.status" type="user" />
            </div>
          </div>

          <div class="detail-info">
            <div class="info-item">
              <span class="label">TG用户ID:</span>
              <span class="value">{{ userStore.currentUser.tgUserId }}</span>
            </div>
            <div class="info-item">
              <span class="label">来源群组:</span>
              <span class="value">{{ userStore.currentUser.sourceGroupName || '-' }}</span>
            </div>
            <div class="info-item">
              <span class="label">注册时间:</span>
              <span class="value">{{ formatDate(userStore.currentUser.registeredAt) }}</span>
            </div>
            <div class="info-item">
              <span class="label">最后活跃:</span>
              <span class="value">{{ formatDate(userStore.currentUser.lastActiveAt) }}</span>
            </div>
          </div>

          <div class="activity-section">
            <h4>行为记录</h4>
            <el-timeline>
              <el-timeline-item
                v-for="activity in userStore.activities"
                :key="activity.id"
                :timestamp="formatDate(activity.timestamp)"
                placement="top"
              >
                <el-card>
                  <p class="activity-type">{{ activity.type }}</p>
                  <p class="activity-desc">{{ activity.description }}</p>
                </el-card>
              </el-timeline-item>
            </el-timeline>
            <el-empty v-if="userStore.activities.length === 0" description="暂无行为记录" />
          </div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped lang="scss">
.users-page {
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

.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
}

.user-detail {
  padding: 0 16px;
}

.detail-header {
  display: flex;
  align-items: center;
  gap: 20px;
  padding-bottom: 20px;
  border-bottom: 1px solid #ebeef5;
  margin-bottom: 20px;

  .user-basic {
    h3 {
      margin: 0 0 4px 0;
      font-size: 20px;
      font-weight: 600;
    }

    p {
      margin: 0 0 8px 0;
      color: #909399;
    }
  }
}

.detail-info {
  margin-bottom: 24px;

  .info-item {
    display: flex;
    padding: 8px 0;
    border-bottom: 1px solid #f5f7fa;

    .label {
      width: 100px;
      color: #909399;
    }

    .value {
      flex: 1;
      color: #303133;
    }
  }
}

.activity-section {
  h4 {
    margin: 0 0 16px 0;
    font-size: 16px;
    font-weight: 600;
  }

  .activity-type {
    font-weight: 600;
    color: #409eff;
    margin: 0 0 4px 0;
  }

  .activity-desc {
    color: #606266;
    margin: 0;
  }
}
</style>
