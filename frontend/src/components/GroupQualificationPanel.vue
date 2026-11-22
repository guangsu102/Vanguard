<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import PermissionReviewPanel from './PermissionReviewPanel.vue'
import type { MembershipPermission } from '@/api/groupReviews'
import { groupReviewRuntime, listGroupReviews, type GroupReview as Audit, type GroupReviewRuntime } from '@/api/groupReviews'
const rows = ref<Audit[]>([])
const permissionRows = ref<MembershipPermission[]>([])
const simpleMode = computed(() => runtime.value?.mode === 'writable_v2')
const loading = ref(false)
const selected = ref<Audit | null>(null)
function currentMaterial(row: Audit) {
  const pending = row.evidence.pending_collection
  return pending && typeof pending === 'object' && Object.keys(pending).length ? pending : row.evidence
}
const selectedMaterial = computed(() => selected.value ? currentMaterial(selected.value) : {})
function effectiveUnknowns(row: Audit) {
  return row.read_status?.effective_unknowns ?? currentMaterial(row).unknowns ?? []
}
const error = ref('')
const accountId = ref<number>()
const accountOptions = ref<number[]>([])
const runtime = ref<GroupReviewRuntime | null>(null)
const runtimeError = ref('')
const storage = computed(() => runtime.value?.storage)
const measurement = computed(() => runtime.value?.measurement)
const measuredWork = computed(() => (measurement.value?.by_stage || []).flatMap((stage: Record<string, any>) => stage.work || []))
const zeroReadSlices = computed(() => measuredWork.value.reduce((sum: number, row: Record<string, any>) => sum + (row.zero_read_slices || 0), 0))
const schedulerCommits = computed(() => (measurement.value?.by_stage || []).reduce((sum: number, stage: Record<string, any>) => sum + (stage.scheduler_commits?.decisions || 0), 0))
const businessCompletions = computed(() => (measurement.value?.by_stage || []).reduce((sum: number, stage: Record<string, any>) => sum + (stage.business_completions?.decisions || 0), 0))
const business = computed(() => runtime.value?.business)
const readiness = computed(() => business.value?.readiness)
const maintenance = computed(() => readiness.value?.maintenance)
const readinessBlockers = computed(() => Object.entries(readiness.value?.blocker_counts || {}).map(([code, count]) => ({ code, count })))
const opportunities = computed(() => business.value?.ad_opportunities)
const opportunityRate = computed(() => opportunities.value?.complete && typeof opportunities.value.completion_rate === 'number'
  ? `${(opportunities.value.completion_rate * 100).toFixed(1)}%` : '未具备完整窗口')
const currentWork = computed(() => runtime.value?.current_work?.items || [])
const gapReasons: Record<string, string> = {
  unclassified: '未分类缺口', dirty_recovery: '写入中断恢复', source_expired: '计量来源未续期',
  source_slots_full: '来源槽位已满', semaphore_timeout: '等待写入超时', io_timeout: '磁盘写入超时',
  io_error: '磁盘写入失败', cancelled_pending: '任务取消后仍有待确认写入', batch_overflow: '批量队列已满',
}
const diagnosticGaps = computed(() => Object.entries(measurement.value?.diagnostics?.gap_reasons || {})
  .filter(([, count]) => typeof count === 'number' && count > 0)
  .map(([reason, count]) => `${gapReasons[reason] || '其他计量缺口'} ${count} 次`).join('；'))
function metricValue(value: unknown, available: boolean) {
  return available && typeof value === 'number' ? value : '暂无完整计量'
}
const stageLabels: Record<string, string> = {
  initial_review: '首次取证', collection_continuation: '继续取证', maturity_wait: '等待满 24 小时',
  event_recheck: '变化复核', ai_wait: '等待 AI', budget_wait: '等待读取额度',
  technical_wait: '技术等待', storage_fallback: '证据补采', evidence_wait: '等待补充证据', complete: '已完成',
  technical_evidence_wait: '等待新证据恢复',
}
const labels: Record<string, string> = {
  waiting_membership: '等待入群审批', waiting_ai: '等待 AI', queued: '排队', running: '检测中', completed: '已检测', manual_required: '待处理',
  allowed: '明确许可', trial: '普通成员证据', reject: '不合格', observe: '观察中',
  technical_wait: '技术等待', wait: '等待条件', protected: '保护对象', unknown: '待检测',
}
const authorizationLabels: Record<string, string> = {
  active: '授权有效', invalidated: '授权已失效', missing: '尚无授权', unavailable: '授权状态暂不可用',
}
const jobLabels: Record<string, string> = {
  READY: '等待执行', RUNNING: '执行中', WAIT_BUDGET: '等待读取额度', WAIT_PROVIDER: '等待服务恢复',
  WAIT_MATURITY: '等待证据满 24 小时', WAIT_EVENT: '等待新证据', DONE: '已完成', STALE: '范围已变化',
}
function authorizationLabel(value: unknown) {
  const row = value as Audit
  return row.authorization ? authorizationLabels[row.authorization.state] || '授权状态待确认' : '原审核路径'
}
function blockerLabel(row: Audit) {
  if (!row.blocker) return row.job ? (jobLabels[row.job.state] || '待确认') : stageLabel(row)
  if (row.blocker.code === 'runtime_unavailable') return '进度暂不可用'
  return jobLabels[row.job?.state || ''] || '等待所需条件'
}
function recordedCount(value: unknown) {
  return typeof value === 'number' ? value : '未记录'
}
async function refresh() {
  loading.value = true
  error.value = ''
  runtimeError.value = ''
  runtime.value = null
  try {
    const [reviews, status] = await Promise.allSettled([
      listGroupReviews(accountId.value),
      groupReviewRuntime(accountId.value),
    ])
    if (reviews.status === 'fulfilled') {
      rows.value = reviews.value.data.data
      if (!accountId.value) accountOptions.value = [...new Set(rows.value.map(row => row.account_id))].slice(0, 100)
    } else error.value = '检测记录加载失败，请稍后刷新。'
    if (status.status === 'fulfilled') runtime.value = status.value.data.data
    if (simpleMode.value && reviews.status === 'fulfilled') {
      permissionRows.value = reviews.value.data.data as unknown as MembershipPermission[]
      rows.value = []
    }
    else runtimeError.value = '读取与存储状态暂不可用。'
  }
  finally { loading.value = false }
}
function verdictLabel(value: unknown) {
  const row = value as Audit
  const verdict = row.evidence.advertising_audit?.ad_allowed
  return verdict === true ? '通过' : verdict === false ? '不通过' : '待确认'
}
function stageLabel(value: unknown) {
  const row = value as Audit
  if (row.job) return jobLabels[row.job.state] || '待确认'
  return stageLabels[String(row.read_status?.stage || '')] || labels[row.state] || row.state
}
function ordinaryAdsVerified(value: unknown) {
  const row = value as Audit
  const material = currentMaterial(row)
  return material.root_ad_history_sufficient === true || (
    material.decision_dependencies_complete === true &&
    material.sampling_stopped_reason === 'sufficient_ordinary_root_trial_proof'
  )
}
function formatTime(value: unknown) {
  if (typeof value !== 'string' || !value.trim()) return '未知'
  const iso = value.trim()
  const date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}Z`)
  if (Number.isNaN(date.getTime())) return '时间格式无效'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).format(date) + '（北京时间 UTC+8）'
}
function bytes(value: unknown) {
  return typeof value === 'number' ? `${(value / 1024 / 1024).toFixed(1)} MiB` : '未知'
}
function showDetails(row: unknown) { selected.value = row as Audit }
onMounted(refresh)
</script>
<template>
  <el-card style="margin-bottom: 20px">
    <template #header>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong>{{ simpleMode ? '成员投放权限' : '逐群检测单' }}</strong>
        <el-select v-model="accountId" clearable placeholder="全部账号" style="width:150px" @change="refresh">
          <el-option v-for="id in accountOptions" :key="id" :label="`账号 ${id}`" :value="id" />
        </el-select>
        <el-button :loading="loading" @click="refresh">刷新检测结果</el-button>
      </div>
    </template>
    <PermissionReviewPanel v-if="simpleMode" :rows="permissionRows" />
    <template v-else>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-alert v-if="runtimeError" :title="runtimeError" type="warning" :closable="false" />
    <template v-if="readiness">
      <el-descriptions :column="3" border class="runtime-overview">
        <el-descriptions-item label="当前可执行广告目标">{{ readiness.available ? recordedCount(readiness.ad_targets) : '未知' }}</el-descriptions-item>
        <el-descriptions-item label="缓存已就绪加群候选">{{ readiness.available ? recordedCount(readiness.join_candidates) : '未知' }}</el-descriptions-item>
        <el-descriptions-item label="待预览 / 待确认身份">{{ readiness.available ? `${recordedCount(readiness.join_candidates_preview_pending)} / ${recordedCount(readiness.join_candidates_pending_identity)}` : '未知' }}</el-descriptions-item>
      </el-descriptions>
      <p class="metric-note">以上使用现有本地准入判断，不触发 Telegram 取证或预留额度；实际执行仍会原子复核频率、预算、权限和目标围栏。候选仅统计缓存已满足条件的目标，不含尚未预览的群。</p>
      <el-alert v-if="readiness.available && !readiness.sample_complete" title="本地库存摘要为有界采样，数量不是全量；未覆盖的目标保持未知。" type="info" :closable="false" />
      <h4 v-if="readinessBlockers.length">当前阻塞原因</h4>
      <el-table v-if="readinessBlockers.length" :data="readinessBlockers" max-height="180" stripe>
        <el-table-column prop="code" label="原因码" min-width="250" />
        <el-table-column prop="count" label="受影响项 / 检查数" width="180" />
      </el-table>
    </template>
    <el-descriptions v-if="business?.available" :column="3" border class="runtime-overview">
      <el-descriptions-item label="近 24 小时广告成功 / 尝试">{{ business.ads?.succeeded ?? '未记录' }} / {{ business.ads?.attempted ?? '未记录' }}</el-descriptions-item>
      <el-descriptions-item label="加群意图 / 进入 SDK">{{ recordedCount(business.joins?.intent) }} / {{ recordedCount(business.joins?.sdk_entered) }}</el-descriptions-item>
      <el-descriptions-item label="平台已响应 / 成员已确认">{{ recordedCount(business.joins?.platform_responded) }} / {{ recordedCount(business.joins?.membership_confirmed) }}</el-descriptions-item>
      <el-descriptions-item label="广告 / 加群结果待确认">{{ recordedCount(business.ads?.unknown) }} / {{ recordedCount(business.joins?.unknown) }}</el-descriptions-item>
      <el-descriptions-item label="SDK 进入 / 平台响应未记录">{{ recordedCount(business.joins?.sdk_entry_unrecorded) }} / {{ recordedCount(business.joins?.platform_response_unrecorded) }}</el-descriptions-item>
      <el-descriptions-item label="到期广告机会 / 回执完成">{{ opportunities?.available ? `${opportunities.due} / ${opportunities.completed}` : '未开始记录' }}</el-descriptions-item>
      <el-descriptions-item label="到期机会完成率">{{ opportunityRate }}</el-descriptions-item>
      <el-descriptions-item label="机会采集覆盖">{{ opportunities?.complete ? '完整窗口' : '未具备完整窗口' }}</el-descriptions-item>
      <el-descriptions-item label="到期未完成 / 更早积压">{{ opportunities?.available ? `${opportunities.pending} / ${opportunities.pending_before_window}` : '未开始记录' }}</el-descriptions-item>
    </el-descriptions>
    <p v-if="business?.available" class="metric-note">广告成功仅计入有消息回执的发送；加群意图、进入 SDK、平台响应及成员确认分别计数。历史未记录的阶段不推算为成功或零调用。</p>
    <p v-if="business?.available" class="metric-note">到期机会包含预算等待和软件排队，同一义务重试只计一次；有真实广告消息回执才算完成。采集未覆盖完整 24 小时或调度断档时不计算完成率。</p>
    <el-descriptions v-if="runtime" :column="3" border class="runtime-overview">
      <el-descriptions-item label="账本窗口预算已扣">
        {{ metricValue(runtime.metrics?.budget_charged_reads, runtime.metrics?.coverage === 'settled_selection') }}
        <small>（24 小时内创建、已结算，最多 1000 片）</small>
      </el-descriptions-item>
      <el-descriptions-item label="完整小时窗口读取调用">
        {{ metricValue(measurement?.actual_reads, measurement?.complete === true) }}
        <small>（进入 SDK，最近 24 个完整 UTC 小时）</small>
      </el-descriptions-item>
      <el-descriptions-item label="获批尝试 / 已观测调用">
        {{ measurement?.available ? `${measurement.attempted_reads ?? 0} / ${measurement.known_entered_sdk ?? 0}` : '暂不可用' }}
      </el-descriptions-item>
      <el-descriptions-item label="零读取完成片 / 调度已提交判定">
        {{ measurement?.available ? `${zeroReadSlices} / ${schedulerCommits}` : '暂不可用' }}
        <small>（判定提交仅覆盖审核调度入口）</small>
      </el-descriptions-item>
      <el-descriptions-item label="业务已提交完成">
        {{ measurement?.available ? businessCompletions : '暂不可用' }}
        <small>（审核及续授权，按业务提交计数；完整窗口后用于对比）</small>
      </el-descriptions-item>
      <el-descriptions-item label="已结算 / 执行中工作片">
        {{ runtime.metrics?.settled_work_units ?? 0 }} / {{ runtime.metrics?.inflight_work_units ?? 0 }}
      </el-descriptions-item>
      <el-descriptions-item label="消耗待核对工作片">{{ runtime.metrics?.unknown_consumption_work_units ?? 0 }}</el-descriptions-item>
      <el-descriptions-item label="近 7 日样本预算已扣">
        {{ runtime.samples?.available ? runtime.samples.budget_charged_reads : (accountId ? '暂不可用' : '选择账号查看') }}
        <small>（审核 / 续授权各最近 200 片，非全量）</small>
      </el-descriptions-item>
      <el-descriptions-item label="已采集样本工作片">{{ runtime.samples?.available ? runtime.samples.work_units : '—' }}</el-descriptions-item>
      <el-descriptions-item label="当前可用读取">
        <template v-if="runtime.budget?.state === 'disabled'">已停用，使用 RPC 节流</template>
        <template v-else-if="runtime.budget?.state === 'unavailable'">暂不可用</template>
        <template v-else-if="runtime.budget">审核 {{ runtime.budget.review?.remaining ?? '未知' }} / 续授权 {{ runtime.budget.renewal?.remaining ?? '未知' }}</template>
        <template v-else>选择账号查看</template>
      </el-descriptions-item>
      <el-descriptions-item label="观察证据保护槽">
        {{ storage?.enabled ? `${storage.protected_slots_used} / ${storage.protected_slots_limit}` : '未启用或不可用' }}
      </el-descriptions-item>
      <el-descriptions-item label="缓存磁盘占用 / 上限">
        {{ storage?.enabled ? `${bytes(storage.physical_file_size)} / ${bytes(storage.physical_hard_limit_bytes)}` : '—' }}
      </el-descriptions-item>
      <el-descriptions-item label="全部新增组件占用 / 最坏上限">
        {{ runtime.component_storage?.ok ? `${bytes(runtime.component_storage.allocated_bytes)} / ${bytes(runtime.component_storage.declared_peak_bytes)}` : '尚未通过容量核对' }}
      </el-descriptions-item>
    </el-descriptions>
    <p v-if="runtime" class="metric-note">预算已扣与进入 SDK 的调用分开统计；预留额度不是消耗，工作片数不是独立完成审核数。计量起始前、缺失或中断窗口显示暂无完整计量，不能当作零。SDK 内部重试未在本层逐次计数，样本不是全量，当前未计算节省比例。</p>
    <p v-if="diagnosticGaps" class="metric-note">计量文件累计诊断（全部账号、非本窗口）：{{ diagnosticGaps }}。历史缺口无原因细分时不补算。</p>
    <el-alert v-if="measurement?.available && !measurement.complete" title="计量尚未覆盖整个窗口，已观测调用仅为部分数据；需固定同等业务量及复杂度后比较阶段收益。" type="info" :closable="false" />
    <el-alert v-if="runtime?.metrics?.truncated" title="账本窗口超过 1000 片，当前仅显示最近 1000 片的读取。" type="info" :closable="false" />
    <template v-if="runtime?.current_work?.available">
      <h4>当前授权与任务</h4>
      <p class="metric-note">新入群尚未形成检测结论的任务也在这里显示。</p>
      <el-alert v-if="runtime.current_work.truncated" title="当前成员关系超过 500 条，此处显示前 500 条。" type="info" :closable="false" />
      <el-table :data="currentWork" max-height="300" stripe>
        <el-table-column prop="group_id" label="群 ID" width="100" />
        <el-table-column label="当前授权" width="150"><template #default="{ row }">{{ authorizationLabel(row) }}</template></el-table-column>
        <el-table-column label="任务进度" min-width="180"><template #default="{ row }">{{ row.job ? (jobLabels[row.job.state] || '待确认') : '尚无任务' }}</template></el-table-column>
        <el-table-column label="下一步所需 / 当前可用读取" min-width="220"><template #default="{ row }">{{ recordedCount(row.job?.next_step_reads) }} / {{ recordedCount(row.blocker?.available_reads) }}</template></el-table-column>
        <el-table-column label="预计可重试" min-width="250"><template #default="{ row }">{{ row.blocker?.retry_at ? formatTime(row.blocker.retry_at) : '等待条件或事件' }}</template></el-table-column>
      </el-table>
    </template>
    <template v-if="maintenance?.available">
      <h4>经营维护</h4>
      <p class="metric-note">人数、在线数和经营价值维护独立排队；这里的等待不阻塞已有有效授权或新增群。重试时间是预测，不是恢复承诺。</p>
      <el-alert v-if="maintenance.truncated" title="维护列表仅显示最近 100 项。" type="info" :closable="false" />
      <el-table :data="maintenance.items" max-height="220" stripe>
        <el-table-column prop="group_id" label="群 ID" width="100" />
        <el-table-column label="维护进度" min-width="150"><template #default="{ row }">{{ jobLabels[row.state] || row.state }}</template></el-table-column>
        <el-table-column prop="reason" label="原因" min-width="180" />
        <el-table-column label="预计可重试" min-width="250"><template #default="{ row }">{{ row.ready_at ? formatTime(row.ready_at) : '等待条件或事件' }}</template></el-table-column>
      </el-table>
    </template>
    <p v-else-if="maintenance" class="metric-note">经营维护状态暂不可用；广告目标和加群候选摘要独立显示。</p>
    <p>普通成员广告满 24 小时仍可见即可满足广告资格；无此前例时，群规明确允许也可满足。发送前仍核实账号权限、额度、同群 24 小时间隔和前次广告存活。启用新流程后，人数、在线人数及 48 小时无广告等经营价值检查由低优先级维护执行；缺少这些数据不会阻塞广告和加群。有效授权与审核任务进度分别展示。</p>
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
        <template #default="{ row }">{{ ordinaryAdsVerified(row) ? '已核实' : '待核' }}</template>
      </el-table-column>
      <el-table-column label="结论" width="120">
        <template #default="{ row }">{{ labels[row.decision] || row.decision }}</template>
      </el-table-column>
      <el-table-column label="广告审核" width="100">
        <template #default="{ row }">{{ verdictLabel(row) }}</template>
      </el-table-column>
      <el-table-column label="当前授权" width="130">
        <template #default="{ row }">{{ authorizationLabel(row) }}</template>
      </el-table-column>
      <el-table-column label="当前阶段" width="150">
        <template #default="{ row }">{{ stageLabel(row) }}</template>
      </el-table-column>
      <el-table-column label="本片 SDK 读取" width="115">
        <template #default="{ row }">{{ row.read_status?.actual_reads ?? '未知' }}</template>
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
          <el-descriptions-item label="当前授权">{{ authorizationLabel(selected) }}<template v-if="selected.authorization?.grant_audit_id">；依据检测单 #{{ selected.authorization.grant_audit_id }}</template></el-descriptions-item>
          <el-descriptions-item label="原因">{{ selected.reason }}</el-descriptions-item>
          <el-descriptions-item label="当前阶段">{{ stageLabel(selected) }}</el-descriptions-item>
          <el-descriptions-item v-if="selected.blocker" label="任务等待">{{ blockerLabel(selected) }}；下一步所需读取 {{ recordedCount(selected.blocker.required_reads) }} / 当前可用 {{ recordedCount(selected.blocker.available_reads) }}<template v-if="selected.blocker.retry_at">；预计可重试 {{ formatTime(selected.blocker.retry_at) }}（以执行时可用条件为准）</template></el-descriptions-item>
          <el-descriptions-item v-if="selected.job?.missing_dependencies?.length" label="下一步缺少的证据">{{ selected.job.missing_dependencies.join('、') }}</el-descriptions-item>
          <el-descriptions-item label="尚待核验 / 按需暂缓的消息">{{ selected.read_status?.pending_message_count ?? '未知' }} / {{ selected.read_status?.not_evaluated_message_count ?? '未知' }}</el-descriptions-item>
          <el-descriptions-item v-if="selected.read_status?.technical_recovery_exhausted" label="技术恢复">本轮重试已用完；出现新的有效证据后自动重新排队。{{ selected.read_status.event_pending ? '当前有待处理变化，恢复器将核对是否属于新一代。' : '' }}</el-descriptions-item>
          <el-descriptions-item label="本片 SDK / 预算已扣 / 预留读取">{{ selected.read_status?.actual_reads ?? '未知' }} / {{ selected.read_status?.budget_attempted_reads ?? '未知' }} / {{ selected.read_status?.reserved_reads ?? '未知' }}</el-descriptions-item>
          <el-descriptions-item v-if="selected.read_status?.mature_at" label="证据满 24 小时时刻">{{ formatTime(selected.read_status.mature_at) }}</el-descriptions-item>
          <el-descriptions-item v-if="selected.read_status?.retry_at" label="预计恢复时间">{{ formatTime(selected.read_status.retry_at) }}</el-descriptions-item>
          <el-descriptions-item v-if="selected.wait?.first_blocked_at" label="首次等待">{{ formatTime(selected.wait.first_blocked_at) }}</el-descriptions-item>
          <el-descriptions-item v-if="selected.wait?.required_reads" label="本次所需读取">{{ selected.wait.required_reads }} 次；预计可重试：{{ formatTime(selected.wait.retry_at) }}</el-descriptions-item>
          <el-descriptions-item label="检测时间">{{ selected.checked_at ? formatTime(selected.checked_at) : '尚未完成' }}</el-descriptions-item>
          <el-descriptions-item label="实际采样范围">{{ formatTime(selectedMaterial.observed_oldest) }} — {{ formatTime(selectedMaterial.observed_newest) }}</el-descriptions-item>
          <el-descriptions-item label="在线人数采集">{{ selectedMaterial.online_count_source || '未知' }} · {{ formatTime(selectedMaterial.online_count_checked_at) }}</el-descriptions-item>
          <el-descriptions-item label="历史完整">{{ selectedMaterial.history_complete ? '是' : '否；不足不能当作零' }}</el-descriptions-item>
          <el-descriptions-item label="群规采集">{{ selected.read_status?.rules_evaluation_status === 'NOT_EVALUATED' ? '未采集；需要时补证' : (selectedMaterial.rules_incomplete ? '尚不完整' : '按原路径核验') }}</el-descriptions-item>
          <el-descriptions-item label="未知项">{{ effectiveUnknowns(selected).join('、') || '无' }}</el-descriptions-item>
        </el-descriptions>
        <h4>权限</h4><pre>{{ JSON.stringify(selectedMaterial.permissions, null, 2) }}</pre>
        <h4>规则及广告证据</h4>
        <el-table :data="selectedMaterial.evidence || []">
          <el-table-column prop="source" label="来源" width="180" />
          <el-table-column prop="sender_role" label="发布者" width="100" />
          <el-table-column prop="age_hours" label="存留小时" width="100" />
          <el-table-column prop="text" label="内容" min-width="240" />
        </el-table>
        <p>广告审核：{{ verdictLabel(selected) }}。等待 AI 或证据不足均保持待确认。</p>
        <h4>广告判定依据</h4><pre>{{ JSON.stringify(selected.evidence.advertising_audit, null, 2) }}</pre>
      </template>
    </el-drawer>
    </template>
  </el-card>
</template>
<style scoped>
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.runtime-overview { margin-top: 12px; }
.metric-note, small { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
