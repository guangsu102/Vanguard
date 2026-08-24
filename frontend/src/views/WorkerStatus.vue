<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { guardianApi, type TelegramWorkerStatus } from '@/api/guardian'
import ClientListPagination from '@/components/ClientListPagination.vue'
import { useClientPagination } from '@/utils/clientPagination'

const loading = ref(false)
const workers = ref<TelegramWorkerStatus[]>([])
const role = ref('')
const workerPagination = useClientPagination(workers, 20)

const fetchWorkers = async () => {
  loading.value = true
  try {
    const res = await guardianApi.listWorkers({ role: role.value || undefined, limit: 500 })
    workers.value = res.data.data
  } catch (error) {
    ElMessage.error('加载执行器状态失败')
  } finally {
    loading.value = false
  }
}

const statusType = (status: string, stale = false) => {
  if (stale) return 'danger'
  if (status === 'online') return 'success'
  if (status === 'starting' || status === 'degraded') return 'warning'
  if (status === 'error' || status === 'offline') return 'danger'
  return 'info'
}

onMounted(fetchWorkers)
</script>

<template>
  <div class="worker-status">
    <div class="toolbar">
      <el-segmented
        v-model="role"
        :options="[
          { label: '全部', value: '' },
          { label: '引流账号', value: 'growth_user_worker' },
          { label: '群管机器人', value: 'guardian_bot_worker' },
          { label: 'NapCat OneBot', value: 'qq_onebot_worker' },
        ]"
        @change="fetchWorkers"
      />
      <el-button :icon="Refresh" :loading="loading" @click="fetchWorkers">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="workerPagination.rows.value" border>
      <el-table-column prop="worker_id" label="Worker ID" min-width="220" />
      <el-table-column prop="role" label="角色" width="170" />
      <el-table-column prop="status" label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status, row.is_stale)" effect="plain">
            {{ row.is_stale ? 'stale' : row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="account_id" label="账号ID" width="100" />
      <el-table-column prop="bot_profile_id" label="Bot Profile" width="120" />
      <el-table-column prop="last_heartbeat_at" label="最近心跳" min-width="180">
        <template #default="{ row }">
          <span>{{ row.last_heartbeat_at || '-' }}</span>
          <span v-if="row.heartbeat_age_seconds !== null && row.heartbeat_age_seconds !== undefined" class="age">
            {{ row.heartbeat_age_seconds }}s
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="last_error" label="最近错误" min-width="220" show-overflow-tooltip />
      <el-table-column label="配置快照" min-width="220">
        <template #default="{ row }">
          <span>{{ JSON.stringify(row.metadata || {}) }}</span>
        </template>
      </el-table-column>
    </el-table>
    <ClientListPagination
      v-model:page="workerPagination.page.value"
      v-model:page-size="workerPagination.pageSize.value"
      :total="workerPagination.total.value"
    />
  </div>
</template>

<style scoped>
.worker-status {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.age {
  margin-left: 8px;
  color: #909399;
  font-size: 12px;
}
</style>
