<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { automationApi, type DynamicCapacitySnapshot } from '@/api/automation'
const rows = ref<DynamicCapacitySnapshot[]>([])
const busy = ref(false)
const error = ref('')
type Listener = { raw_journal?: { available?: boolean; source?: string; states?: Record<string, number>; oldest_pending_seconds?: number; storage_error?: string | null; checkpoint?: { count: number; latest_at?: string | null; digest?: string } }; sync_wait_scope?: string | null; update_queue_size?: number; queue_nonempty_seconds?: number; read_wait_seconds?: number; state: string; connected: boolean; reason?: string | null; resume_at?: string | null }
const runtime = ref<{ status: string; checked_at: string; alerts: { code: string; severity: string; account_id?: number }[]; accounts?: { account_id: number; listener?: Listener }[] } | null>(null)
const listenerFor = (id: number) => runtime.value?.accounts?.find(item => item.account_id === id)?.listener
const listenerNames: Record<string, string> = { pausing: '正在暂停连接，等待当前操作结束', connected: '已连接', connected_wait: '已连接，补读等待预算，见原始更新落盘状态', deferred: '等待恢复连接', disconnected: '未连接', error: '连接异常', unknown: '等待监听状态' }
const readLanes = [{ key: 'ad', label: '广告检查与结果核对（35% 独立保底）' }, { key: 'survival', label: '广告存活核查（15% 独立保底）' }, { key: 'critical', label: '必要维护与事件处理' }, { key: 'routine', label: '资格审核与普通读取' }, { key: 'background', label: '搜索与候选预览' }, { key: 'sync', label: '更新同步' }]
const alertNames: Record<string, string> = { ad_survival_overdue: '广告存活核查已逾期，重试时间延后不消除逾期状态', listener_raw_storage_failed: '原始更新落盘失败，需立即检查存储', listener_sync_delayed: '监听仍连接，部分频道缺口补读持续等待预算', listener_event_reconciliation_required: '存在待对账的历史监听事件', listener_business_backlog: '监听事件处理延迟超过 2 分钟', growth_memory_high: '增长服务内存较高，请查看积压和运行趋势', listener_update_backlog: '监听更新持续积压，正在等待读取恢复', listener_pause_failed: '监听暂停失败，需要检查服务状态', listener_not_connected: '长期监听尚未连接，见账号等待原因', listener_status_unknown: '长期监听状态尚未上报', outbound_reconciliation_required: '存在未决发送记录，需要对账', attribution_not_connected: '转化归因尚未接通，发送成功不代表注册或付费', host_disk_high: '主机磁盘超过 80%', backup_stale: '备份超过 30 小时未更新', container_unhealthy: '服务容器异常', host_monitor_stale: '主机监控未更新', telegram_cooldown: 'Telegram 冷却中', recovery_scheduler_stale: '恢复调度心跳超时', worker_stale: 'Telegram worker 心跳超时', resume_without_delivery: '冷却到期后 30 分钟仍无成功投放，请查看资格和预算', redis_memory_high: 'Redis 内存超过 80%' }
const names: Record<string, string> = { ad: '广告', verification: '验证文字', diagnostic: '诊断', other: '其他' }
const reasons: Record<string, string> = {
  account_risk_quarantined: '账号已隔离，等待平台限制核实',
  join_ad_account_unavailable: '账号广告暂停，加群同步让路',
  join_ad_material_missing: '缺少可投放素材，暂停新增群',
  join_wait_ad_delivery: '先处理到期广告，再补群',
  join_wait_ad_survival: '先完成到期广告存活核查',
  join_wait_ad_reconciliation: '先核实未决发送结果',
  join_ad_capacity_unavailable: '广告读取与存活容量不足，暂停新增群',
  join_ad_delivery_paused: '广告调度暂停，暂不补群',
  join_inventory_target_met: '现有可用群与审核队列已覆盖产能目标',
  join_wait_inventory_review: '待审核群可覆盖缺口，先完成审核再决定补群',
  qualification_group_identity_unknown: '历史群身份待核对，暂不计入可用投放群',
  frequency_group_daily_cap: '单群滚动 24 小时额度已用满', frequency_group_interval: '未到当前群频率的下次发送时间',
  frequency_survival_unresolved: '存活结果待核实，暂停续发', frequency_survival_due: '等待到期存活检测',
  frequency_first_checkpoint_required: '等待前条广告通过 2 分钟检测', frequency_group_inflight: '该群已有在途发送',
  frequency_reservation_stale: '频率已变化，等待重新调度', frequency_deleted_cooldown: '删帖降频，冷却后再复核',
  frequency_daily_review_due: '等待核验上一周期最后一条广告', frequency_daily_review_unknown: '日核验结果待确认，暂停续发',
  frequency_daily_deleted: '上一周期最后一条已确认不存在，额度减半', frequency_daily_target_changed: '日核验目标变化，等待重新核验',
  frequency_muted: '永久或超过 3 天禁言，等待退群', frequency_deleted_at_minimum: '最低频率下仍确认不存在，等待退群',
  frequency_rejoin_blocked: '已禁止自动重加', outbound_ad_probe_budget: '试投额度用尽，成熟投放独立计算',
  outbound_ad_mature_budget: '账号本轮成熟投放额度用尽',
  telegram_ad_read_budget: '广告专属读取份额等待恢复',
  telegram_preview_read_budget: '候选预检读取份额等待恢复',
  telegram_join_read_budget: '加群所需普通读取预算等待恢复',
  telegram_read_budget: '读取预算已用满，等待窗口恢复', telegram_rpc_guard_unavailable: '读取预算服务暂不可用',
  telegram_rpc_cooldown: 'Telegram 读取限流，等待冷却后自动复核', account_risk_pause: '账号风险暂停',
  account_ad_cooldown: '账号广告随机冷却中', account_ad_delivery_interval: '账号广告最小间隔未到',
  account_ad_cooldown_unavailable: '账号冷却状态读取失败，等待恢复',
  delivery_schedule_not_due: '群广告任务尚未到期', delivery_tuple_inflight: '该群有发送任务执行中',
  delivery_schedule_paused: '群广告调度暂停', group_manual_ad_hold: '该群投放已暂停',
  join_candidate_preview_due: '候选群等待刷新预览', join_candidate_quality_pending: '候选群人数或广告前例待核实',
  prejoin_members_below_50: '入群前确认不足 50 人', prejoin_rules_ban_and_online_below_2: '群规禁广告且在线不足 2 人',
  join_candidate_identity_pending: '候选群等待实时核实身份', join_candidates_unavailable: '没有待加群候选',
  qualification_group_daily_cap: '该群未满 24 小时间隔', qualification_previous_survival_unresolved: '等待前次广告存活确认',
  qualification_review_required: '等待自动资格审核', ad_schedule_not_due: '未到广告调度时间',
  ad_delivery_paused: '推广总开关暂停', auto_join_disabled: '加群总开关暂停',
  account_auto_ads_disabled: '账号广告暂停', account_auto_join_disabled: '账号加群暂停',
  account_capacity_unavailable: '账号状态或风险暂停', join_interval: '未到下次加群时间',
  join_review_backlog: '活动审核队列已满', qualification_disabled: '审核调度暂停',
  account_age_not_verified_six_months: '缺少满 180 天的有效证据',
  account_age_evidence_conflict: '号龄声明与注册日期冲突',
  outbound_total_budget: '总外发额度用尽', rollout_paused: '尚未启用试投',
}
const time = (value?: string | null) => value ? new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z').toLocaleString('zh-CN') : '暂无确定时间，见阻挡原因'
const dailyReviewNames: Record<string, string> = { idle: '等待本周期首次发送', waiting: '等待周期结束', due: '等待日核验', checking: '正在核验', retry: '待核实，暂停续发', blocked: '已停止投放' }
async function refresh() {
  busy.value = true; error.value = ''
  try {
    const [capacity, health] = await Promise.all([automationApi.getCapacity(), automationApi.getRuntime()])
    rows.value = capacity.data.data; runtime.value = health.data.data
  }
  catch { error.value = '容量读取失败，请刷新重试。' }
  finally { busy.value = false }
}
onMounted(refresh)
</script>
<template>
  <el-card class="capacity-panel">
    <template #header><div class="heading"><strong>广告产出与执行条件</strong><el-button :loading="busy" @click="refresh">刷新容量</el-button></div></template>
    <p>额度是上限。实际执行量取当前合格目标、素材、审核与验证余量；时间为当前已知限制的最早解除时间，到期仍需复核资格、预算和实时权限。</p>
    <el-alert v-for="alert in runtime?.alerts || []" :key="alert.code + String(alert.account_id || '')" :title="(alert.account_id ? '#' + alert.account_id + '：' : '') + (alertNames[alert.code] || alert.code)" :type="alert.severity === 'critical' ? 'error' : 'warning'" :closable="false" />
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-empty v-if="!busy && !rows.length && !error" description="尚未配置动态容量账号" />
    <el-table :data="rows" v-loading="busy" row-key="account_id" stripe>
      <el-table-column type="expand"><template #default="{ row }">
        <div class="details">
          <p v-if="row.execution?.state === 'cooldown'">当前冷却至 {{ time(row.execution.resume_at) }}；到期自动复核后继续调度，挂起审核 {{ row.inventory.suspended_due ?? 0 }} 项。</p>
          <p v-if="row.read_rpc">读取预算：每分钟 {{ row.read_rpc.limits.minute }} / 每小时 {{ row.read_rpc.limits.hour }}；预览和资料同步每小时最多 {{ row.read_rpc.limits.background_hour }}。最近限流请求：{{ row.read_rpc.method || '尚无精确记录' }}</p>
          <p v-if="row.read_rpc?.usage">本小时已用 {{ row.read_rpc.usage.hour?.used ?? 0 }} / {{ row.read_rpc.limits.hour }} 次；当前日窗口已用 {{ row.read_rpc.usage.day?.used ?? 0 }} / {{ row.read_rpc.limits.day }} 次。<span v-if="row.read_rpc.resume_at"> 账号总预算复核时间：{{ time(row.read_rpc.resume_at) }}</span></p>
          <p v-if="row.read_rpc?.limits.ad_hour != null">广告专属读取：每小时 {{ row.read_rpc.limits.ad_hour }} / 每日 {{ row.read_rpc.limits.ad_day }} 次；其余用途合计每小时最多 {{ row.read_rpc.limits.non_ad_hour }} / 每日 {{ row.read_rpc.limits.non_ad_day }} 次。读取次数不等于广告条数，总预算和 Telegram 冷却仍生效。</p>
          <p>实际监听连接：{{ listenerFor(row.account_id)?.connected ? "已连接" : "未连接" }}；{{ listenerNames[listenerFor(row.account_id)?.state || 'unknown'] || '等待监听状态' }}<span v-if="listenerFor(row.account_id)?.resume_at">；预计复核时间 {{ time(listenerFor(row.account_id)?.resume_at) }}</span></p>
          <p v-if="listenerFor(row.account_id)?.update_queue_size != null">待处理更新 {{ listenerFor(row.account_id)?.update_queue_size }}；连续观测到积压 {{ listenerFor(row.account_id)?.queue_nonempty_seconds ?? 0 }} 秒；读取等待 {{ listenerFor(row.account_id)?.read_wait_seconds ?? 0 }} 秒</p>
          <p v-if="row.read_rpc?.limits.survival_hour != null">存活核查专属读取：每小时 {{ row.read_rpc.limits.survival_hour }} / 每日 {{ row.read_rpc.limits.survival_day }} 次，占总额度 15%；广告保底之外的其他用途合计最多 50%。旧总窗口计数和 Telegram 冷却继续生效。</p>
          <template v-if="listenerFor(row.account_id)?.raw_journal">
            <p v-if="listenerFor(row.account_id)?.raw_journal?.available !== false">原始更新已落盘待处理 {{ listenerFor(row.account_id)?.raw_journal?.states?.pending ?? 0 }}；已随检查点确认 {{ listenerFor(row.account_id)?.raw_journal?.states?.checkpointed ?? 0 }}；需对账 {{ listenerFor(row.account_id)?.raw_journal?.states?.reconciliation_required ?? 0 }}。{{ listenerFor(row.account_id)?.raw_journal?.source === 'disk' ? '当前显示磁盘记录，连接恢复后继续处理。' : '' }}</p>
            <p v-else>{{ listenerFor(row.account_id)?.raw_journal?.storage_error ? '磁盘积压状态读取失败，不能视为积压为零。' : '尚未发现当前监听会话的持久化记录。' }}</p>
            <p v-if="listenerFor(row.account_id)?.raw_journal?.checkpoint">持久化检查点 {{ listenerFor(row.account_id)?.raw_journal?.checkpoint?.count }} 个；最近检查点时间 {{ time(listenerFor(row.account_id)?.raw_journal?.checkpoint?.latest_at) }}。积压清空并确认检查点前进后，才算补读恢复。</p>
          </template>
          <div v-if="row.read_rpc?.lanes">
            <div v-for="lane in readLanes" :key="lane.key">
              {{ lane.label }}：当前可用 {{ row.read_rpc.lanes[lane.key]?.remaining ?? 0 }} 次；每小时份额 {{ row.read_rpc.limits[lane.key + "_hour"] }} / 每日份额 {{ row.read_rpc.limits[lane.key + "_day"] }}
              <span v-if="row.read_rpc.lanes[lane.key]?.retry_after_seconds">；等待至 {{ time(row.read_rpc.lanes[lane.key]?.resume_at) }}</span>
              <span v-else>；读取可用</span>
            </div>
            <p>各类读取共用总预算。关键读取有余量时，审核、搜索或同步仍可能等待；广告还需满足目标资格和发送间隔。</p>
          </div>
          <div v-if="row.outbound?.ad_lanes">
            <strong>试投与成熟投放分账</strong>
            <div>试投：滚动 24 小时 {{ row.outbound.ad_lanes.probe.used_rolling_24h }} / {{ row.outbound.ad_lanes.probe.effective }} 条；成熟：{{ row.outbound.ad_lanes.mature.used_rolling_24h }} 条。两者均做存活检测并共用账号发送节奏。</div>
            <p>每 24 小时核验上一周期最后一条广告：存活则额度翻倍，最高 30；确认不存在则减半，最低 1；最低频率下仍不存在则退群；结果未知先等待核实。</p>
            <el-table :data="row.group_frequencies || []" size="small" row-key="group_id">
              <el-table-column prop="title" label="群" min-width="150" />
              <el-table-column label="阶段" width="90"><template #default="{ row: group }">{{ group.mature ? '成熟' : '试投' }}</template></el-table-column>
              <el-table-column label="单群上限" width="140"><template #default="{ row: group }">{{ group.quota }} 条 / 24 小时</template></el-table-column>
              <el-table-column label="每日最后一条核验" min-width="190"><template #default="{ row: group }">{{ dailyReviewNames[group.daily_review_status] || '等待日核验状态' }}<div v-if="group.daily_review_due_at">{{ time(group.daily_review_due_at) }}</div></template></el-table-column>
              <el-table-column label="下次最早发送" min-width="190"><template #default="{ row: group }">{{ group.next_allowed_at ? time(group.next_allowed_at) : (group.reason ? '等待条件恢复' : '等待账号调度') }}</template></el-table-column>
              <el-table-column label="状态原因" min-width="180"><template #default="{ row: group }">{{ reasons[group.reason] || group.reason || '当前群条件允许' }}</template></el-table-column>
            </el-table>
          </div>
          <div v-if="row.ad_plan">
            <strong>广告优先补群计划</strong>
            <p>已有可用群 {{ row.ad_plan.usable_groups }}；单日群频次容量 {{ row.ad_plan.available_slots_24h }} 条；保守资源产能 {{ row.ad_plan.sustainable_ads_24h }} 条/日；当前计划参考 {{ row.ad_plan.planned_ads_24h }} 条/日。</p>
            <p>补群目标 {{ row.ad_plan.target_groups }}；合格可用群缺口 {{ row.ad_plan.qualified_group_deficit ?? Math.max(0, row.ad_plan.target_groups - row.ad_plan.usable_groups) }}；待审核 {{ row.ad_plan.pending_review_groups ?? row.inventory.active_backlog ?? 0 }}；预计仍需新增 {{ row.ad_plan.group_deficit }}。待审核群不等于当前可投放群。到期可投放 {{ row.ad_plan.ad_due }}；到期存活检查 {{ row.ad_plan.survival_due }}；未决发送 {{ row.ad_plan.unresolved_sends }}。</p>
            <p>读取成本：每次投放 {{ row.ad_plan.delivery_read_cost }} 次、完整存活周期 {{ row.ad_plan.survival_read_cost }} 次；{{ row.ad_plan.read_cost_source === 'measured' ? '依据实测样本' : row.ad_plan.read_cost_source === 'mixed' ? '部分环节已有实测，其余使用保守估算' : '样本不足，使用保守估算' }}。按广告、存活各自份额预留 20% 处理重试。计划用于控制补群，不保证发送成功，也不提高群频次。</p>
            <p v-if="row.ad_plan.join_blocker">加群等待原因：{{ reasons[row.ad_plan.join_blocker] || row.ad_plan.join_blocker }}</p>
          </div>
          <div v-if="row.ad_output">
            <strong>真实广告产出</strong>
            <p v-if="row.ad_output.survival_overdue">存活检查逾期 {{ row.ad_output.survival_overdue }} 条，其中延期等待 {{ row.ad_output.survival_deferred_overdue ?? 0 }} 条；最久逾期 {{ Math.ceil((row.ad_output.survival_oldest_overdue_seconds ?? 0) / 60) }} 分钟；下次尝试 {{ time(row.ad_output.survival_next_attempt_at) }}。</p>
            <p>最近 24 小时发送回执 {{ row.ad_output.sent_24h }}；其中已确认 1 小时存活 {{ row.ad_output.confirmed_1h }}、已判定删除 {{ row.ad_output.deleted }}、存活待核实 {{ row.ad_output.unresolved }}。</p>
            <p>最近 72 小时发送且已经满 24 小时的 {{ row.ad_output.matured_sends_72h }} 条中，确认 24 小时存活 {{ row.ad_output.confirmed_24h }}，待核实 {{ row.ad_output.matured_unresolved }}。未到观察期限的不计入分母。</p>
          </div>
          <strong>独立消息预算（今日／滚动 24 小时／上限）</strong>
          <div v-for="(quota, category) in row.outbound?.categories || {}" :key="category">{{ names[String(category)] || category }}：{{ quota.used_today }} / {{ quota.used_rolling_24h }} / {{ quota.effective }}，剩余 {{ quota.remaining }}</div>
          <p>库存 {{ row.inventory.total ?? 0 }}；资格通过 {{ row.inventory.qualified ?? 0 }}；可用投放群 {{ row.inventory.usable ?? 0 }}；历史身份待核实 {{ row.inventory.identity_blocked ?? 0 }}；48 小时目标 {{ row.inventory.target ?? 0 }}；活动审核 {{ row.inventory.active_backlog ?? 0 }}；待恢复审核 {{ row.inventory.manual ?? 0 }}；外发未决 {{ row.outbound?.unknown_count ?? 0 }}</p>
          <div>下次加群：{{ time(row.next_allowed_at) }}；广告时间限制最早解除：{{ time(row.ad_next_allowed_at) }}</div>
          <div v-for="(count, reason) in row.workload?.blocker_counts || {}" :key="reason">{{ reasons[String(reason)] || reason }}：{{ count }}</div>
        </div>
      </template></el-table-column>
      <el-table-column label="账号" width="85"><template #default="{ row }">#{{ row.account_id }}</template></el-table-column>
      <el-table-column label="阶段" width="110"><template #default="{ row }">{{ row.execution?.state === 'quarantined' ? '账号已隔离' : row.execution?.state === 'paused' ? '广告已暂停' : row.execution?.state === 'budget_wait' ? '读取预算等待' : row.execution?.state === 'unavailable' ? '预算服务异常' : row.execution?.state === 'cooldown' ? '限流冷却' : ({ paused: '暂停', pilot: '受控试投', dynamic: '动态运行' } as Record<string, string>)[row.workload?.rollout_phase || 'paused'] || '暂停' }}</template></el-table-column>
      <el-table-column label="已核实可执行" min-width="145"><template #default="{ row }">广告 {{ row.executable_now?.ad ?? 0 }} / 加群 {{ row.executable_now?.join ?? 0 }}</template></el-table-column>
      <el-table-column label="24 小时广告回执" min-width="145"><template #default="{ row }">{{ row.ad_output?.sent_24h ?? 0 }}；1h 存活 {{ row.ad_output?.confirmed_1h ?? 0 }}</template></el-table-column>
      <el-table-column label="待加群候选" min-width="240"><template #default="{ row }">共 {{ row.workload?.join_candidates_total ?? 0 }}；新鲜优质 {{ row.workload?.join_candidates ?? 0 }}；待核实 {{ row.workload?.join_candidates_preview_pending ?? 0 }}；已排除 {{ row.workload?.join_candidates_excluded ?? 0 }}</template></el-table-column>
      <el-table-column label="今日／滚动广告" min-width="140"><template #default="{ row }">{{ row.used_today.ad ?? 0 }} / {{ row.used_rolling_24h.ad ?? 0 }}（{{ row.outbound?.ad_lanes ? "账号节奏容量" : "上限" }} {{ row.effective.ad ?? 0 }}）</template></el-table-column>
      <el-table-column label="总外发剩余" width="110"><template #default="{ row }">{{ row.remaining.total ?? 0 }}</template></el-table-column>
      <el-table-column label="为什么现在不能执行" min-width="240"><template #default="{ row }">{{ row.blockers.map((reason: string) => reasons[reason] || reason).join('；') || '缓存条件已满足，等待执行前复核' }}</template></el-table-column>
    </el-table>
  </el-card>
</template>
<style scoped>
.capacity-panel { margin-bottom: 20px; }
.heading { display: flex; align-items: center; justify-content: space-between; }
p { color: var(--el-text-color-secondary); line-height: 1.6; }
.details { padding: 16px 40px; line-height: 1.8; }
</style>
