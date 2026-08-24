<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Check, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { automationApi, type GroupAdProfile } from '@/api/automation'
import ClientListPagination from '@/components/ClientListPagination.vue'
import { useClientPagination } from '@/utils/clientPagination'

const loading = ref(false)
const savingGroupId = ref<number | null>(null)
const profiles = ref<GroupAdProfile[]>([])

const adPolicyModeOptions = [
  { label: '待确认', value: 'unknown' },
  { label: '探测中', value: 'unknown_probe' },
  { label: '允许投放', value: 'allowed' },
  { label: '禁止投放', value: 'forbidden' },
]

const sortedProfiles = computed(() =>
  [...profiles.value].sort((left, right) => Number(right.daily_capacity || 0) - Number(left.daily_capacity || 0)),
)
const { page, pageSize, total, rows: pagedProfiles } = useClientPagination(sortedProfiles, 20)

const pct = (value?: number) => `${Math.round(Number(value || 0) * 100)}%`

const loadProfiles = async () => {
  loading.value = true
  try {
    const response = await automationApi.getGroupAdProfiles()
    profiles.value = response.data.data
  } finally {
    loading.value = false
  }
}

const saveGroupAdPolicy = async (row: any) => {
  let note = ''
  try {
    const result = await ElMessageBox.prompt('请填写许可依据、管理员确认或禁止原因', '确认群广告策略', {
      confirmButtonText: '确认保存',
      cancelButtonText: '取消',
      inputPattern: /\S{2,}/,
      inputErrorMessage: '至少填写 2 个字符',
    })
    note = result.value
  } catch {
    await loadProfiles()
    return
  }

  savingGroupId.value = row.group_id
  try {
    const response = await automationApi.updateGroupAdPolicy(row.group_id, {
      mode: row.ad_policy_mode,
      confidence: row.ad_policy_mode === 'unknown' ? 0 : 100,
      note,
    })
    Object.assign(row, response.data.data)
    ElMessage.success('群广告许可已更新')
  } finally {
    savingGroupId.value = null
  }
}

onMounted(loadProfiles)
</script>

<template>
  <section class="group-operation-panel" v-loading="loading">
    <div class="panel-toolbar">
      <div>
        <h3>群广告许可与档位</h3>
        <p>维护每个群的广告许可，查看系统根据投放证据计算的档位和日容量。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadProfiles">刷新</el-button>
    </div>

    <el-table :data="pagedProfiles" row-key="group_id" border>
      <el-table-column label="群" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.group_title || row.telegram_group_id }}</template>
      </el-table-column>
      <el-table-column label="广告许可" min-width="180">
        <template #default="{ row }">
          <el-select v-model="row.ad_policy_mode" size="small">
            <el-option v-for="item in adPolicyModeOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="档位" width="110">
        <template #default="{ row }"
          ><el-tag effect="plain">{{ row.ad_tier }}</el-tag></template
        >
      </el-table-column>
      <el-table-column prop="daily_capacity" label="日容量" width="100" />
      <el-table-column label="24h样本" width="100">
        <template #default="{ row }">{{ row.metrics?.completed_samples || 0 }}</template>
      </el-table-column>
      <el-table-column label="24h存活" width="100">
        <template #default="{ row }">{{ pct(row.metrics?.survival_rate_24h) }}</template>
      </el-table-column>
      <el-table-column label="转化" width="80">
        <template #default="{ row }">{{ row.metrics?.conversions || 0 }}</template>
      </el-table-column>
      <el-table-column label="无删除" width="90">
        <template #default="{ row }">{{ row.metrics?.clean_days || 0 }}天</template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button
            :icon="Check"
            type="primary"
            size="small"
            :loading="savingGroupId === row.group_id"
            @click="saveGroupAdPolicy(row)"
            >保存</el-button
          >
        </template>
      </el-table-column>
    </el-table>
    <ClientListPagination v-model:page="page" v-model:page-size="pageSize" :total="total" />
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
  margin: 0 0 6px;
  font-size: 17px;
}
.panel-toolbar p {
  margin: 0;
  color: #606266;
}
</style>
