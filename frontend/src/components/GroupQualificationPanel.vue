<script setup lang="ts">
import { onMounted, ref } from 'vue'
import apiClient from '@/api/client'
type Audit = {
  id: number; account_id: number; group_id: number; state: string; decision: string;
  reason: string; checked_at: string | null; expires_at: string | null;
  evidence: Record<string, any>
}
const rows = ref<Audit[]>([])
const loading = ref(false)
const selected = ref<Audit | null>(null)
const error = ref('')
const labels: Record<string, string> = {
  waiting_membership: '等待入群审批', waiting_ai: '等待模型恢复', queued: '排队', running: '检测中', completed: '已检测', manual_required: '待处理',
  allowed: '明确许可', trial: '普通成员证据', reject: '不合格', observe: '观察中',
  technical_wait: '技术等待', wait: '等待条件', protected: '保护对象', unknown: '待检测',
}
async function refresh() {
  loading.value = true
  error.value = ''
  try { rows.value = (await apiClient.get('/automation/group-reviews')).data.data }
  catch { error.value = '检测记录加载失败，请稍后刷新。' }
  finally { loading.value = false }
}
function showDetails(row: unknown) { selected.value = row as Audit }
onMounted(refresh)
</script>
<template>
  <el-card style="margin-bottom: 20px">
    <template #header>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong>逐群检测单</strong>
        <el-button :loading="loading" @click="refresh">刷新检测结果</el-button>
      </div>
    </template>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <p>普通成员广告满 24 小时仍可见即可满足广告资格；无此前例时，群规明确允许也可满足。发送前仍核实账号权限、额度、同群 24 小时间隔和前次广告存活。退群条件：完整检查最近 48 小时且无普通成员广告，或群人数少于 50，或群规明确禁广告且在线人数少于 2。</p>
    <el-table :data="rows" v-loading="loading" max-height="480" stripe>
      <el-table-column prop="account_id" label="账号" width="75" />
      <el-table-column label="群" min-width="160">
        <template #default="{ row }">{{ row.evidence.title || ('群 #' + row.group_id) }}</template>
      </el-table-column>
      <el-table-column label="人数" width="80">
        <template #default="{ row }">{{ row.evidence.member_count ?? '未知' }}</template>
      </el-table-column>
      <el-table-column label="检测时在线" width="100">
        <template #default="{ row }">{{ row.evidence.online_count ?? '未知' }}</template>
      </el-table-column>
      <el-table-column label="普通成员广告" width="125">
        <template #default="{ row }">{{ row.evidence.root_ad_history_sufficient ? '已核实' : '待核' }}</template>
      </el-table-column>
      <el-table-column label="结论" width="120">
        <template #default="{ row }">{{ labels[row.decision] || row.decision }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">{{ labels[row.state] || row.state }}</template>
      </el-table-column>
      <el-table-column prop="reason" label="原因" min-width="190" />
      <el-table-column label="依据" width="90">
        <template #default="{ row }"><el-button link @click="showDetails(row)">检测单</el-button></template>
      </el-table-column>
    </el-table>
    <el-drawer :model-value="!!selected" title="账号与群检测详情" size="65%" @close="selected = null">
      <template v-if="selected">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="账号">{{ selected.account_id }}</el-descriptions-item>
          <el-descriptions-item label="结论">{{ labels[selected.decision] || selected.decision }}</el-descriptions-item>
          <el-descriptions-item label="原因">{{ selected.reason }}</el-descriptions-item>
          <el-descriptions-item label="检测时间">{{ selected.checked_at || '尚未完成' }}</el-descriptions-item>
          <el-descriptions-item label="实际采样范围">{{ selected.evidence.observed_oldest || '未知' }} — {{ selected.evidence.observed_newest || '未知' }}</el-descriptions-item>
          <el-descriptions-item label="在线人数采集">{{ selected.evidence.online_count_source || '未知' }} · {{ selected.evidence.online_count_checked_at || '未知' }}</el-descriptions-item>
          <el-descriptions-item label="历史完整">{{ selected.evidence.history_complete ? '是' : '否；不足不能当作零' }}</el-descriptions-item>
          <el-descriptions-item label="未知项">{{ (selected.evidence.unknowns || []).join('、') || '无' }}</el-descriptions-item>
        </el-descriptions>
        <h4>权限</h4><pre>{{ JSON.stringify(selected.evidence.permissions, null, 2) }}</pre>
        <h4>规则及广告证据</h4>
        <el-table :data="selected.evidence.evidence || []">
          <el-table-column prop="source" label="来源" width="180" />
          <el-table-column prop="sender_role" label="发布者" width="100" />
          <el-table-column prop="age_hours" label="存留小时" width="100" />
          <el-table-column prop="text" label="内容" min-width="240" />
        </el-table>
        <h4>广告判定</h4><pre>{{ JSON.stringify(selected.evidence.advertising_audit, null, 2) }}</pre>
      </template>
    </el-drawer>
  </el-card>
</template>
<style scoped>pre { white-space: pre-wrap; overflow-wrap: anywhere; }</style>
