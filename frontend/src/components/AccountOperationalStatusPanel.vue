<script setup lang="ts">
import { computed } from 'vue'
import { Refresh } from '@element-plus/icons-vue'

import type { AdDynamicStatus } from '@/api/automation'
import ClientListPagination from '@/components/ClientListPagination.vue'
import { useClientPagination } from '@/utils/clientPagination'

type TagType = 'success' | 'warning' | 'danger' | 'info'

const props = defineProps<{
  statuses: AdDynamicStatus[]
  loading: boolean
}>()

const emit = defineEmits<{
  (event: 'refresh'): void
}>()

const statusSource = computed(() => props.statuses)
const {
  page: statusPage,
  pageSize: statusPageSize,
  total: statusTotal,
  rows: pagedStatuses,
} = useClientPagination(statusSource, 10)

function pct(value?: number) {
  return `${Math.round(Number(value || 0) * 100)}%`
}

function accountLabel(row: any) {
  return row.account_label || `#${row.account_id}`
}

function riskTagType(level: string): TagType {
  if (['frozen', 'quarantined'].includes(level)) return 'danger'
  if (['limited', 'watch'].includes(level)) return 'warning'
  return 'success'
}

function riskLabel(level: string) {
  const labels: Record<string, string> = {
    normal: '正常',
    watch: '观察',
    limited: '限流',
    frozen: '冻结',
    quarantined: '隔离',
  }
  return labels[level] || level || '-'
}

function warmupStageLabel(stage?: string) {
  const labels: Record<string, string> = {
    observe: '观察',
    seed: '起步',
    soft: '低频',
    ramp: '提量',
    normal: '正常',
    cooldown: '冷却',
  }
  return labels[stage || ''] || stage || '-'
}

function severityTagType(severity?: string): TagType {
  if (severity === 'danger') return 'danger'
  if (severity === 'warning') return 'warning'
  if (severity === 'success') return 'success'
  return 'info'
}

function dynamicHealthTagType(row: any): TagType {
  return severityTagType(row.dynamic_health_diagnostic?.primary_severity)
}

function diagnosticTagType(row: any): TagType {
  return severityTagType(row.delivery_diagnostic?.primary_block_severity)
}

function diagnosticLabel(row: any) {
  return row.delivery_diagnostic?.primary_block_label || '-'
}

function nextActionText(row: any) {
  const diagnostic = row.delivery_diagnostic
  if (!diagnostic) return '-'
  return diagnostic.next_action_at
    ? `${diagnostic.next_action_label} · ${diagnostic.next_action_at.slice(5, 16).replace('T', ' ')}`
    : diagnostic.next_action_label
}

function groupDiagnosticText(row: any) {
  const item = row.delivery_diagnostic?.group_diagnostics
  if (!item) return '-'
  const probeState = row.delivery_diagnostic?.probe_execution_allowed ? '探针可运行' : '探针阻断'
  const adState = row.delivery_diagnostic?.ad_delivery_allowed ? '广告可发送' : '广告暂停'
  return `${probeState} · ${adState} · 就绪 ${item.ready} · Premium ${item.premium || 0} · 待许可 ${(item.ad_permission_unknown || 0) + (item.ad_policy_expired || 0)} · 待探针 ${item.pending_probe} · 阻断 ${item.probe_failed + item.blocked + (item.ad_permission_forbidden || 0)}`
}

function compactError(row: any) {
  const error = row.recent_errors?.[0]
  return error ? `${error.error || '-'} (${error.count})` : '-'
}
</script>

<template>
  <section class="operational-status-panel" v-loading="loading">
    <div class="panel-toolbar">
      <div>
        <h3>账号运营态</h3>
        <p>集中查看推广账号的自动化开关、风险、健康度和实时投放能力。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="emit('refresh')">刷新</el-button>
    </div>

    <el-table :data="pagedStatuses" border size="small" empty-text="暂无推广账号运营态数据" class="status-table">
      <el-table-column label="账号" min-width="160" fixed>
        <template #default="{ row }">
          <div class="account-cell">
            <span>{{ accountLabel(row) }}</span>
            <small>#{{ row.account_id }}</small>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="自动化开关" min-width="145">
        <template #default="{ row }">
          <div class="tag-list">
            <el-tag :type="row.auto_join_enabled ? 'success' : 'info'" size="small">加群</el-tag>
            <el-tag :type="row.auto_ads_enabled ? 'success' : 'info'" size="small">广告</el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="风控" min-width="130">
        <template #default="{ row }">
          <el-tag :type="riskTagType(row.risk_level)" size="small">{{ riskLabel(row.risk_level) }}</el-tag>
          <span class="inline-score">{{ row.risk_score }}</span>
        </template>
      </el-table-column>
      <el-table-column label="健康诊断" min-width="210" show-overflow-tooltip>
        <template #default="{ row }">
          <el-tag :type="dynamicHealthTagType(row)" size="small">
            {{ row.dynamic_health_diagnostic?.primary_label || '-' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="暖号" min-width="150">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">{{ warmupStageLabel(row.warmup_stage) }}</el-tag>
          <span class="muted-text">{{ row.warmup_remaining_days }} 天</span>
        </template>
      </el-table-column>
      <el-table-column label="探针 / 发言" min-width="150">
        <template #default="{ row }">{{ pct(row.probe_success_rate_24h) }} / {{ pct(row.writable_rate) }}</template>
      </el-table-column>
      <el-table-column prop="ad_eligible_groups" label="可投放群" width="100" />
      <el-table-column label="实时广告额度（日 / 轮）" min-width="190">
        <template #default="{ row }">{{ row.dynamic_daily_limit }} / {{ row.dynamic_run_limit }}</template>
      </el-table-column>
      <el-table-column label="投放阻塞" min-width="170">
        <template #default="{ row }">
          <el-tag :type="diagnosticTagType(row)" size="small">{{ diagnosticLabel(row) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="下一步" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ nextActionText(row) }}</template>
      </el-table-column>
      <el-table-column label="群状态" min-width="260" show-overflow-tooltip>
        <template #default="{ row }">{{ groupDiagnosticText(row) }}</template>
      </el-table-column>
      <el-table-column label="近期错误" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ compactError(row) }}</template>
      </el-table-column>
    </el-table>

    <ClientListPagination v-model:page="statusPage" v-model:page-size="statusPageSize" :total="statusTotal" />
  </section>
</template>

<style scoped>
.operational-status-panel {
  min-width: 0;
}

.panel-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.panel-toolbar h3 {
  margin: 0;
  color: #303133;
  font-size: 16px;
}

.panel-toolbar p {
  margin: 6px 0 0;
  color: #606266;
  font-size: 13px;
}

.status-table {
  width: 100%;
}

.account-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.account-cell small,
.muted-text {
  color: #909399;
  font-size: 12px;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.inline-score,
.muted-text {
  margin-left: 6px;
}

@media (max-width: 768px) {
  .panel-toolbar {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
