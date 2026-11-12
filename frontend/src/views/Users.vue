<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElButton, ElIcon, ElMessage, ElDrawer, ElEmpty, ElTimeline, ElTimelineItem } from 'element-plus'
import { View, Mute, Unlock, RemoveFilled, CircleCheckFilled } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { isUserMuted, isUserBlocked } from '@/api/users'
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
    key: 'state',
    label: '状态',
    placeholder: '全部状态',
    width: '120px',
    options: [
      { label: '全部', value: '' },
      { label: '新用户', value: 'new' },
      { label: '待转化', value: 'pending' },
      { label: '活跃', value: 'active' },
      { label: '沉默', value: 'silent' },
      { label: '已流失', value: 'churned' },
      { label: '已封禁', value: 'blocked' },
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
  { prop: 'id', label: 'ID', width: '70' },
  { prop: 'telegramId', label: 'TG用户ID', width: '160' },
  { prop: 'username', label: '用户名', minWidth: '140' },
  { prop: 'state', label: '状态', width: '90', slot: 'state' },
  { prop: 'warningCount', label: '警告次数', width: '90' },
  { prop: 'mutedUntil', label: '禁言截止', width: '160', slot: 'mutedUntil' },
  { prop: 'createdAt', label: '注册时间', width: '160', slot: 'createdAt' },
  { prop: 'updatedAt', label: '最后更新', width: '160', slot: 'updatedAt' },
  { prop: 'actions', label: '操作', width: '200', fixed: 'right', slot: 'actions' },
]

const fetchData = async (params?: Record<string, any>) => {
  loading.value = true
  try {
    // Handle date range filter
    const query: Record<string, any> = { ...params }
    if (query.dateRange) {
      query.registeredFrom = query.dateRange[0]
      query.registeredTo = query.dateRange[1]
      delete query.dateRange
    }
    await userStore.fetchList(query)
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
  } catch (error) {
    ElMessage.error('获取用户详情失败')
  }
  // Load activity history separately so a failure here does not block the detail view
  try {
    await userStore.fetchActivities(row.id)
  } catch (error) {
    console.error('Failed to fetch user activities:', error)
  }
}

const handleMute = async (row: any) => {
  try {
    await userStore.mute(row.id, { durationHours: 24, reason: '手动禁言' })
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
    await userStore.blacklist(row.id)
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

const formatDate = (date?: string | null) => {
  return date ? dayjs(date).format('YYYY-MM-DD HH:mm') : '-'
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
      <template #state="{ row }">
        <div class="state-cell">
          <StatusTag :status="row.state" type="user" />
          <StatusTag v-if="isUserMuted(row) && !isUserBlocked(row)" status="muted" type="user" />
        </div>
      </template>

      <template #mutedUntil="{ row }">
        {{ formatDate(row.mutedUntil) }}
      </template>

      <template #createdAt="{ row }">
        {{ formatDate(row.createdAt) }}
      </template>

      <template #updatedAt="{ row }">
        {{ formatDate(row.updatedAt) }}
      </template>

      <template #actions="{ row }">
        <el-button type="primary" link size="small" @click="handleViewDetail(row)">
          <el-icon><View /></el-icon>
          详情
        </el-button>
        <el-button
          v-if="!isUserBlocked(row) && !isUserMuted(row)"
          type="warning"
          link
          size="small"
          @click="handleMute(row)"
        >
          <el-icon><Mute /></el-icon>
          禁言
        </el-button>
        <el-button
          v-else-if="isUserMuted(row)"
          type="success"
          link
          size="small"
          @click="handleUnmute(row)"
        >
          <el-icon><Unlock /></el-icon>
          解除禁言
        </el-button>
        <el-button
          v-if="!isUserBlocked(row)"
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
            <div class="user-basic">
              <h3>{{ userStore.currentUser.username ? `@${userStore.currentUser.username}` : '-' }}</h3>
              <div class="state-cell">
                <StatusTag :status="userStore.currentUser.state" type="user" />
                <StatusTag v-if="isUserMuted(userStore.currentUser) && !isUserBlocked(userStore.currentUser)" status="muted" type="user" />
              </div>
            </div>
          </div>

          <div class="detail-info">
            <div class="info-item">
              <span class="label">TG用户ID:</span>
              <span class="value">{{ userStore.currentUser.telegramId || '-' }}</span>
            </div>
            <div class="info-item">
              <span class="label">XBoard用户ID:</span>
              <span class="value">{{ userStore.currentUser.xboardUserId ?? '-' }}</span>
            </div>
            <div class="info-item">
              <span class="label">警告次数:</span>
              <span class="value">{{ userStore.currentUser.warningCount }}</span>
            </div>
            <div class="info-item">
              <span class="label">禁言截止:</span>
              <span class="value">{{ formatDate(userStore.currentUser.mutedUntil) }}</span>
            </div>
            <div class="info-item">
              <span class="label">试用开始:</span>
              <span class="value">{{ formatDate(userStore.currentUser.trialStartedAt) }}</span>
            </div>
            <div class="info-item">
              <span class="label">试用过期:</span>
              <span class="value">{{ formatDate(userStore.currentUser.trialExpiresAt) }}</span>
            </div>
            <div class="info-item">
              <span class="label">注册时间:</span>
              <span class="value">{{ formatDate(userStore.currentUser.createdAt) }}</span>
            </div>
            <div class="info-item">
              <span class="label">最后更新:</span>
              <span class="value">{{ formatDate(userStore.currentUser.updatedAt) }}</span>
            </div>
          </div>

          <div class="activity-section">
            <h4>违规记录</h4>
            <el-timeline>
              <el-timeline-item
                v-for="activity in userStore.activities"
                :key="activity.id"
                :timestamp="formatDate(activity.timestamp)"
                placement="top"
              >
                <el-card>
                  <p class="activity-type">{{ activity.type }}</p>
                  <p class="activity-desc">{{ activity.description || '-' }}</p>
                </el-card>
              </el-timeline-item>
            </el-timeline>
            <el-empty v-if="userStore.activities.length === 0" description="暂无违规记录" />
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

.state-cell {
  display: flex;
  align-items: center;
  gap: 6px;
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
      margin: 0 0 8px 0;
      font-size: 20px;
      font-weight: 600;
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
      width: 120px;
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
