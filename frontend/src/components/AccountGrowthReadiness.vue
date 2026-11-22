<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { groupReviewRuntime, type GrowthBusinessMetrics, type GrowthExecutionControls } from '@/api/groupReviews'

const props = defineProps<{ accounts: { id: number; display_name?: string | null; identifier?: string }[] }>()
type Readiness = NonNullable<GrowthBusinessMetrics['readiness']>
const selectedId = ref<number>()
const loading = ref(false)
const readiness = ref<Readiness | null>(null)
const permissions = ref<{ counts: Record<string, number>; due: number } | null>(null)
const cooldown = ref<string | null>(null)
const controls = ref<GrowthExecutionControls | null>(null)
const error = ref('')
const loaded = ref(false)
let generation = 0
const blockers = computed(() => Object.entries(readiness.value?.blocker_counts || {}).map(([code, count]) => ({ code, count })))
const maintenance = computed(() => readiness.value?.maintenance)
function count(value: unknown) { return typeof value === 'number' ? value : '未知' }
function reasonLabel(reason: string | null | undefined) {
  const labels: Record<string, string> = {
    account_status_unavailable: '账号受限或不可用', account_spam_restricted: '平台限制',
    account_risk_paused: '账号风险暂停', account_risk_level_not_allowed: '账号风险暂停',
    automation_paused: '自动任务已暂停', account_daily_cap: '当日额度已用完',
    account_total_cap: '账号总外发额度不足', permission_review_required: '等待群权限审核',
    account_cooldown: '账号间隔等待', redis_unavailable: '计数暂不可用',
    permission_failures_across_groups: '连续多群失败，等待诊断',
  }
  return reason ? (labels[reason] || reason) : '可进入下一步资格检查'
}
function localTime(value: string | null | undefined) {
  if (!value) return '暂无'
  const normalized = /Z$|[+-]\d\d:\d\d$/.test(value) ? value : `${value.replace(' ', 'T')}Z`
  const date = new Date(normalized)
  if (Number.isNaN(date.getTime())) return '未知'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}
watch(selectedId, () => { generation++; readiness.value = null; permissions.value = null; controls.value = null; cooldown.value = null; error.value = ''; loaded.value = false })
watch(() => props.accounts, accounts => {
  if (selectedId.value && !accounts.some(account => account.id === selectedId.value)) selectedId.value = undefined
})
onBeforeUnmount(() => { generation++ })
async function refresh() {
  if (!selectedId.value || loading.value) return
  const account = selectedId.value
  const request = ++generation
  loading.value = true
  readiness.value = null
  permissions.value = null
  controls.value = null
  cooldown.value = null
  loaded.value = false
  error.value = ''
  try {
    const response = await groupReviewRuntime(account)
    if (request !== generation || selectedId.value !== account) return
    readiness.value = response.data.data.business?.readiness || null
    controls.value = response.data.data.controls || null
    if (response.data.data.mode === 'writable_v2') {
      permissions.value = response.data.data.permissions
      cooldown.value = response.data.data.cooldown_reason
    }
    loaded.value = true
  } catch {
    if (request === generation) error.value = '摘要加载失败，请稍后手动刷新。'
  } finally { loading.value = false }
}
</script>
<template>
  <el-card class="growth-readiness" shadow="never">
    <template #header><strong>广告与加群就绪摘要</strong></template>
    <div class="summary-actions">
      <el-select v-model="selectedId" clearable filterable placeholder="选择当前列表中的一个账号" style="width: 280px">
        <el-option v-for="account in accounts" :key="account.id" :value="account.id"
          :label="`#${account.id} ${account.display_name || account.identifier || ''}`" />
      </el-select>
      <el-button :disabled="!selectedId || loading" :loading="loading" @click="refresh">刷新该账号摘要</el-button>
    </div>
    <p class="summary-note">展示所选账号上次刷新的结果。候选仍需满足实际加群条件；需要最新状态时请刷新。</p>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" />
    <template v-if="controls?.available">
      <el-descriptions :column="2" border>
        <el-descriptions-item label="加群配置 / 有效上限">{{ controls.configured.join_daily }} / {{ controls.effective.join_daily }} 次/天</el-descriptions-item>
        <el-descriptions-item label="广告配置 / 有效上限">{{ controls.configured.ad_daily }} / {{ controls.effective.ad_daily }} 条/天</el-descriptions-item>
        <el-descriptions-item label="加群已用 / 剩余">{{ count(controls.execution?.join?.used) }} / {{ count(controls.execution?.join?.remaining) }}</el-descriptions-item>
        <el-descriptions-item label="广告已用 / 剩余">{{ count(controls.execution?.ads?.used) }} / {{ count(controls.execution?.ads?.remaining) }}</el-descriptions-item>
        <el-descriptions-item label="最小加群 / 广告间隔">{{ controls.effective.join_interval_seconds / 60 }} / {{ controls.effective.ad_interval_seconds / 60 }} 分钟</el-descriptions-item>
        <el-descriptions-item label="广告成功 / 结果未知">{{ controls.usage.ad_success }} / {{ controls.usage.ad_unknown }}</el-descriptions-item>
        <el-descriptions-item label="加群当前状态">{{ reasonLabel(controls.execution?.join?.reason || controls.account_reason || controls.execution?.reason) }}</el-descriptions-item>
        <el-descriptions-item label="广告当前状态">{{ reasonLabel(controls.execution?.ads?.reason || controls.account_reason || controls.execution?.reason) }}</el-descriptions-item>
        <el-descriptions-item label="下次加群检查">{{ localTime(controls.execution?.join?.next_allowed_at) }}</el-descriptions-item>
        <el-descriptions-item label="下次广告检查">{{ localTime(controls.execution?.ads?.next_allowed_at) }}</el-descriptions-item>
      </el-descriptions>
      <p class="summary-note">北京时间：加群每日 08:00 重置，广告每日 00:00 重置。结果未知的广告继续占用额度，明确失败保留冷却；实际执行还需通过群权限与计划检查。</p>
      <el-alert v-if="controls.diagnostic_pause" :title="reasonLabel(controls.diagnostic_pause.reason)" type="warning" :closable="false" />
    </template>
    <template v-if="permissions">
      <el-descriptions :column="3" border>
        <el-descriptions-item label="权限已通过">{{ permissions.counts.approved || 0 }}</el-descriptions-item>
        <el-descriptions-item label="待检查">{{ permissions.counts.pending || 0 }}</el-descriptions-item>
        <el-descriptions-item label="已到投放时间">{{ permissions.due }}</el-descriptions-item>
      </el-descriptions>
      <p class="summary-note">投放还需满足启用计划、纯文字素材、人工开关和频率要求。{{ cooldown ? `当前冷却：${cooldown}` : '当前无读取冷却。' }}</p>
    </template>
    <el-alert v-else-if="loaded && !readiness && !controls" title="此账号暂未提供新流程摘要，请确认启用范围。" type="info" :closable="false" />
    <template v-if="readiness">
      <el-descriptions :column="3" border>
        <el-descriptions-item label="当前可执行广告目标">{{ readiness.available ? count(readiness.ad_targets) : '未知' }}</el-descriptions-item>
        <el-descriptions-item label="缓存已就绪加群候选">{{ readiness.available ? count(readiness.join_candidates) : '未知' }}</el-descriptions-item>
        <el-descriptions-item label="待预览 / 待确认身份">{{ readiness.available ? `${count(readiness.join_candidates_preview_pending)} / ${count(readiness.join_candidates_pending_identity)}` : '未知' }}</el-descriptions-item>
      </el-descriptions>
      <p v-if="!readiness.available" class="summary-note">本地库存摘要暂不可用，不能视为零。</p>
      <p v-else-if="!readiness.sample_complete" class="summary-note">本摘要为有界采样，数量不是全量。</p>
      <h4 v-if="blockers.length">当前阻塞原因</h4>
      <el-table v-if="blockers.length" :data="blockers" size="small" max-height="160">
        <el-table-column prop="code" label="原因" min-width="240" />
        <el-table-column prop="count" label="受影响项 / 检查数" width="180" />
      </el-table>
      <p v-if="maintenance?.available" class="summary-note">经营维护记录：{{ maintenance.items.length }} 项{{ maintenance.truncated ? '（仅最近 100 项）' : '' }}。维护独立排队，不作为广告和加群资格。</p>
      <p v-else-if="maintenance" class="summary-note">经营维护状态暂不可用；广告和候选摘要独立显示。</p>
    </template>
  </el-card>
</template>
<style scoped>
.growth-readiness { margin-bottom: 16px; }
.summary-actions { display: flex; flex-wrap: wrap; gap: 12px; }
.summary-note { color: #606266; font-size: 13px; line-height: 1.6; }
</style>
