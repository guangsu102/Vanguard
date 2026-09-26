<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { automationApi, type DynamicCapacitySnapshot } from '@/api/automation'
const rows = ref<DynamicCapacitySnapshot[]>([])
const busy = ref(false)
const error = ref('')
type Listener = { state: string; connected: boolean; reason?: string | null; resume_at?: string | null }
const runtime = ref<{ status: string; checked_at: string; alerts: { code: string; severity: string; account_id?: number }[]; accounts?: { account_id: number; listener?: Listener }[] } | null>(null)
const listenerFor = (id: number) => runtime.value?.accounts?.find(item => item.account_id === id)?.listener
const listenerNames: Record<string, string> = { connected: '已连接', connected_wait: '已连接，更新同步等待读取预算', deferred: '等待恢复连接', disconnected: '未连接', error: '连接异常', unknown: '等待监听状态' }
const readLanes = [{ key: 'critical', label: '广告与存活核验' }, { key: 'routine', label: '资格审核与普通读取' }, { key: 'background', label: '搜索与候选预览' }, { key: 'sync', label: '更新同步' }]
const alertNames: Record<string, string> = { listener_not_connected: '长期监听尚未连接，见账号等待原因', listener_status_unknown: '长期监听状态尚未上报', outbound_reconciliation_required: '存在未决发送记录，需要对账', attribution_not_connected: '转化归因尚未接通，发送成功不代表注册或付费', host_disk_high: '主机磁盘超过 80%', backup_stale: '备份超过 30 小时未更新', container_unhealthy: '服务容器异常', host_monitor_stale: '主机监控未更新', telegram_cooldown: 'Telegram 冷却中', recovery_scheduler_stale: '恢复调度心跳超时', worker_stale: 'Telegram worker 心跳超时', resume_without_delivery: '冷却到期后 30 分钟仍无成功投放，请查看资格和预算', redis_memory_high: 'Redis 内存超过 80%' }
const names: Record<string, string> = { ad: '广告', verification: '验证文字', diagnostic: '诊断', other: '其他' }
const reasons: Record<string, string> = {
  frequency_group_daily_cap: '单群滚动 24 小时额度已用满', frequency_group_interval: '未到当前群频率的下次发送时间',
  frequency_survival_unresolved: '存活结果待核实，暂停续发', frequency_survival_due: '等待到期存活检测',
  frequency_first_checkpoint_required: '等待前条广告通过 2 分钟检测', frequency_group_inflight: '该群已有在途发送',
  frequency_reservation_stale: '频率已变化，等待重新调度', frequency_deleted_cooldown: '删帖降频，冷却后再复核',
  frequency_muted: '永久或超过 3 天禁言，等待退群', frequency_deleted_at_minimum: '最低频率仍删帖，等待退群',
  frequency_rejoin_blocked: '已禁止自动重加', outbound_ad_probe_budget: '试投额度用尽，成熟投放独立计算',
  outbound_ad_mature_budget: '账号本轮成熟投放额度用尽',
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
    <template #header><div class="heading"><strong>动态容量与执行条件</strong><el-button :loading="busy" @click="refresh">刷新容量</el-button></div></template>
    <p>额度是上限。实际执行量取当前合格目标、素材、审核与验证余量；时间为当前已知限制的最早解除时间，到期仍需复核资格、预算和实时权限。</p>
    <el-alert v-for="alert in runtime?.alerts || []" :key="alert.code + String(alert.account_id || '')" :title="(alert.account_id ? '#' + alert.account_id + '：' : '') + (alertNames[alert.code] || alert.code)" :type="alert.severity === 'critical' ? 'error' : 'warning'" :closable="false" />
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-empty v-if="!busy && !rows.length && !error" description="尚未配置动态容量账号" />
    <el-table :data="rows" v-loading="busy" row-key="account_id" stripe>
      <el-table-column type="expand"><template #default="{ row }">
        <div class="details">
          <p v-if="row.execution?.state === 'cooldown'">当前冷却至 {{ time(row.execution.resume_at) }}；到期自动复核后继续调度，挂起审核 {{ row.inventory.suspended_due ?? 0 }} 项。</p>
          <p v-if="row.read_rpc">读取预算：每分钟 {{ row.read_rpc.limits.minute }} / 每小时 {{ row.read_rpc.limits.hour }}；预览和资料同步每小时最多 {{ row.read_rpc.limits.background_hour }}。最近限流请求：{{ row.read_rpc.method || '尚无精确记录' }}</p>
          <p v-if="row.read_rpc?.usage">本小时已用 {{ row.read_rpc.usage.hour?.used ?? 0 }} / {{ row.read_rpc.limits.hour }} 次；今日已用 {{ row.read_rpc.usage.day?.used ?? 0 }} / {{ row.read_rpc.limits.day }} 次。<span v-if="row.read_rpc.resume_at"> 读取恢复时间：{{ time(row.read_rpc.resume_at) }}</span></p>
          <p>长期监听：{{ listenerNames[listenerFor(row.account_id)?.state || 'unknown'] || '等待监听状态' }}<span v-if="listenerFor(row.account_id)?.resume_at">；预计复核时间 {{ time(listenerFor(row.account_id)?.resume_at) }}</span></p>
          <div v-if="row.read_rpc?.lanes">
            <div v-for="lane in readLanes" :key="lane.key">
              {{ lane.label }}：当前可用 {{ row.read_rpc.lanes[lane.key]?.remaining ?? 0 }} 次
              <span v-if="row.read_rpc.lanes[lane.key]?.retry_after_seconds">；等待至 {{ time(row.read_rpc.lanes[lane.key]?.resume_at) }}</span>
              <span v-else>；读取可用</span>
            </div>
            <p>各类读取共用总预算。关键读取有余量时，审核、搜索或同步仍可能等待；广告还需满足目标资格和发送间隔。</p>
          </div>
          <div v-if="row.outbound?.ad_lanes">
            <strong>试投与成熟投放分账</strong>
            <div>试投：滚动 24 小时 {{ row.outbound.ad_lanes.probe.used_rolling_24h }} / {{ row.outbound.ad_lanes.probe.effective }} 条；成熟：{{ row.outbound.ad_lanes.mature.used_rolling_24h }} 条。两者均做存活检测并共用账号发送节奏。</div>
            <el-table :data="row.group_frequencies || []" size="small" row-key="group_id">
              <el-table-column prop="title" label="群" min-width="150" />
              <el-table-column label="阶段" width="90"><template #default="{ row: group }">{{ group.mature ? '成熟' : '试投' }}</template></el-table-column>
              <el-table-column label="单群上限" width="140"><template #default="{ row: group }">{{ group.quota }} 条 / 24 小时</template></el-table-column>
              <el-table-column label="本档存活进度" width="140"><template #default="{ row: group }">{{ group.quota === 30 ? '已达最高档' : group.successes + ' / 3' }}</template></el-table-column>
              <el-table-column label="下次最早发送" min-width="190"><template #default="{ row: group }">{{ group.next_allowed_at ? time(group.next_allowed_at) : (group.reason ? '等待条件恢复' : '等待账号调度') }}</template></el-table-column>
              <el-table-column label="状态原因" min-width="180"><template #default="{ row: group }">{{ reasons[group.reason] || group.reason || '当前群条件允许' }}</template></el-table-column>
            </el-table>
          </div>
          <strong>独立消息预算（今日／滚动 24 小时／上限）</strong>
          <div v-for="(quota, category) in row.outbound?.categories || {}" :key="category">{{ names[String(category)] || category }}：{{ quota.used_today }} / {{ quota.used_rolling_24h }} / {{ quota.effective }}，剩余 {{ quota.remaining }}</div>
          <p>库存 {{ row.inventory.total ?? 0 }}；合格 {{ row.inventory.qualified ?? 0 }}；48 小时目标 {{ row.inventory.target ?? 0 }}；活动审核 {{ row.inventory.active_backlog ?? 0 }}；待恢复审核 {{ row.inventory.manual ?? 0 }}；外发未决 {{ row.outbound?.unknown_count ?? 0 }}</p>
          <div>下次加群：{{ time(row.next_allowed_at) }}；广告时间限制最早解除：{{ time(row.ad_next_allowed_at) }}</div>
          <div v-for="(count, reason) in row.workload?.blocker_counts || {}" :key="reason">{{ reasons[String(reason)] || reason }}：{{ count }}</div>
        </div>
      </template></el-table-column>
      <el-table-column label="账号" width="85"><template #default="{ row }">#{{ row.account_id }}</template></el-table-column>
      <el-table-column label="阶段" width="110"><template #default="{ row }">{{ row.execution?.state === 'budget_wait' ? '读取预算等待' : row.execution?.state === 'unavailable' ? '预算服务异常' : row.execution?.state === 'cooldown' ? '限流冷却' : ({ paused: '暂停', pilot: '受控试投', dynamic: '动态运行' } as Record<string, string>)[row.workload?.rollout_phase || 'paused'] || '暂停' }}</template></el-table-column>
      <el-table-column label="已核实可执行" min-width="145"><template #default="{ row }">加群 {{ row.executable_now?.join ?? 0 }} / 广告 {{ row.executable_now?.ad ?? 0 }}</template></el-table-column>
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
