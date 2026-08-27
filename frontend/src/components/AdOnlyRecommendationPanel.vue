<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Check, Clock, Plus, Refresh, RefreshLeft, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  automationApi,
  type AdOnlyAssessment,
  type AdOnlyDirectAssignmentRequest,
  type AdOnlyEvent,
  type AdOnlyHandover,
  type AdOnlyHandoverOptions,
  type AdOnlyHandoverRequest,
  type AdOnlyRecommendationSettings,
} from '@/api/automation'

const defaultSettings = (): AdOnlyRecommendationSettings => ({
  recommendation_enabled: false,
  handover_execution_enabled: false,
  min_consecutive_samples: 10,
  required_send_success_percent: 100,
  required_survival_24h_percent: 100,
  peer_ad_min_messages: 1,
  peer_ad_min_senders: 1,
  peer_ad_min_survival_hours: 24,
  peer_ad_lookback_days: 14,
  risk_lookback_days: 30,
  recommendation_ttl_days: 7,
  evaluation_interval_minutes: 60,
})

const loading = ref(false)
const savingSettings = ref(false)
const evaluating = ref(false)
const actionId = ref<string | null>(null)
const activeView = ref<'candidates' | 'handovers'>('candidates')
const settings = reactive(defaultSettings())
const candidates = ref<AdOnlyAssessment[]>([])
const handovers = ref<AdOnlyHandover[]>([])
const options = ref<AdOnlyHandoverOptions>({ accounts: [], creatives: [] })
const handoverDialogVisible = ref(false)
const handoverSubmitting = ref(false)
const directDialogVisible = ref(false)
const directSubmitting = ref(false)
const selectedAssessment = ref<AdOnlyAssessment | null>(null)
const scheduleTimesText = ref('09:00,18:00')
const directScheduleTimesText = ref('09:00,18:00')
const historyVisible = ref(false)
const historyLoading = ref(false)
const historyAssessments = ref<AdOnlyAssessment[]>([])
const historyEvents = ref<AdOnlyEvent[]>([])

const form = reactive<AdOnlyHandoverRequest>({
  assessment_id: 0,
  target_account_id: 0,
  creative_id: 0,
  invite_link: '',
  send_mode: 'interval',
  interval_minutes: 180,
  scheduled_times: [],
})

const defaultPermissionExpiry = () =>
  new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString().slice(0, 19)

const directForm = reactive<AdOnlyDirectAssignmentRequest>({
  target_account_id: 0,
  creative_id: 0,
  invite_link: '',
  send_mode: 'interval',
  interval_minutes: 180,
  scheduled_times: [],
  permission_mode: 'soft_ad_allowed',
  permission_note: '',
  permission_expires_at: defaultPermissionExpiry(),
})

const recommendedCount = computed(
  () => candidates.value.filter((item) => item.status === 'recommended').length,
)
const approvedCount = computed(
  () =>
    candidates.value.filter(
      (item) => item.decision?.event_type === 'assessment_approved',
    ).length,
)
const activeHandoverCount = computed(
  () =>
    handovers.value.filter((item) =>
      ['queued', 'running', 'cleanup_pending', 'rollback_pending'].includes(item.status),
    ).length,
)

const formatTime = (value?: string) => (value ? value.replace('T', ' ').slice(0, 19) : '-')

const statusType = (value: string) => {
  if (['completed', 'recommended', 'approved'].includes(value)) return 'success'
  if (['failed', 'rollback_pending'].includes(value)) return 'danger'
  if (['cleanup_pending', 'deferred'].includes(value)) return 'warning'
  return 'info'
}

const statusLabel = (value: string) =>
  ({
    recommended: '可移交',
    observing: '观察中',
    queued: '已排队',
    running: '执行中',
    cleanup_pending: '待清理',
    rollback_pending: '回滚待处理',
    failed: '失败',
    completed: '已完成',
    rolled_back: '已回滚',
  })[value] || value

const decisionLabel = (assessment: any) => {
  const type = assessment.decision?.event_type
  if (type === 'assessment_approved') return '已批准'
  if (type === 'assessment_rejected') return '已拒绝'
  if (type === 'assessment_deferred') return '已暂缓'
  return '待审批'
}

const blockerLabel = (value: string) =>
  ({
    requires_exactly_one_joined_growth_account: 'Growth 账号数量不为 1',
    ad_only_account_already_joined: '已有专用账号在群',
    ad_only_owner_already_assigned: '已有专用执行账号',
    recent_ad_only_activity_exists: '近期已有专用投放',
    negative_permission_evidence: '存在禁止广告证据',
    positive_permission_evidence_required: '缺少允许广告证据',
    current_ad_policy_not_allowed: '当前广告许可不足',
    current_ad_policy_expired: '广告许可已过期',
    insufficient_consecutive_formal_ads: '连续正式样本不足',
    survival_checks_pending: '仍有 24 小时存活检查',
    send_success_threshold_not_met: '发送成功率不足',
    survival_24h_threshold_not_met: '24 小时存活率不足',
  })[value] || value

const loadData = async () => {
  loading.value = true
  try {
    const [settingsResponse, candidatesResponse, handoversResponse, optionsResponse] =
      await Promise.all([
        automationApi.getAdOnlySettings(),
        automationApi.getAdOnlyCandidates({ limit: 200 }),
        automationApi.getAdOnlyHandovers({ limit: 200 }),
        automationApi.getAdOnlyHandoverOptions(),
      ])
    Object.assign(settings, settingsResponse.data.data)
    candidates.value = candidatesResponse.data.data
    handovers.value = handoversResponse.data.data
    options.value = optionsResponse.data.data
  } finally {
    loading.value = false
  }
}

const saveSettings = async () => {
  savingSettings.value = true
  try {
    const response = await automationApi.updateAdOnlySettings({ ...settings })
    Object.assign(settings, response.data.data)
    ElMessage.success('Ad-only 设置已保存')
  } finally {
    savingSettings.value = false
  }
}

const evaluateCandidates = async () => {
  evaluating.value = true
  try {
    await automationApi.evaluateAdOnlyCandidates({ limit: 200, force: true })
    ElMessage.success('候选评估已进入队列')
  } finally {
    evaluating.value = false
  }
}

const decide = async (
  assessment: any,
  decision: 'approve' | 'reject' | 'defer',
) => {
  let note = ''
  try {
    const result = await ElMessageBox.prompt('审批备注', '候选审批', {
      confirmButtonText: '确认',
      cancelButtonText: '取消',
      inputPlaceholder: '证据复核或处理原因',
    })
    note = result.value
  } catch {
    return
  }
  const key = `decision-${assessment.id}`
  actionId.value = key
  try {
    await automationApi.decideAdOnlyAssessment(assessment.id, { decision, note })
    ElMessage.success('审批结果已记录')
    await loadData()
  } finally {
    actionId.value = null
  }
}

const openHandover = (assessment: any) => {
  selectedAssessment.value = assessment
  Object.assign(form, {
    assessment_id: assessment.id,
    target_account_id: options.value.accounts[0]?.id || 0,
    creative_id: options.value.creatives[0]?.id || 0,
    invite_link: '',
    send_mode: 'interval',
    interval_minutes: 180,
    scheduled_times: [],
  })
  scheduleTimesText.value = '09:00,18:00'
  handoverDialogVisible.value = true
}

const parseScheduleTimes = () =>
  scheduleTimesText.value
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)

const createHandover = async () => {
  if (!form.target_account_id || !form.creative_id || !form.invite_link.trim()) {
    ElMessage.warning('请选择专用账号、素材并填写群邀请链接')
    return
  }
  form.scheduled_times = form.send_mode === 'scheduled' ? parseScheduleTimes() : []
  const idempotencyKey =
    globalThis.crypto?.randomUUID?.() ||
    `handover-${Date.now()}-${Math.random().toString(16).slice(2)}`
  handoverSubmitting.value = true
  try {
    await automationApi.createAdOnlyHandover({
      ...form,
      invite_link: form.invite_link.trim(),
      idempotency_key: idempotencyKey,
    })
    form.invite_link = ''
    handoverDialogVisible.value = false
    activeView.value = 'handovers'
    ElMessage.success('交接已通过预检并进入队列')
    await loadData()
  } finally {
    handoverSubmitting.value = false
  }
}

const openDirectAssignment = () => {
  Object.assign(directForm, {
    target_account_id: options.value.accounts[0]?.id || 0,
    creative_id: options.value.creatives[0]?.id || 0,
    invite_link: '',
    send_mode: 'interval',
    interval_minutes: 180,
    scheduled_times: [],
    permission_mode: 'soft_ad_allowed',
    permission_note: '',
    permission_expires_at: defaultPermissionExpiry(),
  })
  directScheduleTimesText.value = '09:00,18:00'
  directDialogVisible.value = true
}

const createDirectAssignment = async () => {
  if (!directForm.target_account_id || !directForm.creative_id || !directForm.invite_link.trim()) {
    ElMessage.warning('请选择专用账号、素材并填写群邀请链接')
    return
  }
  if (directForm.permission_note.trim().length < 3 || !directForm.permission_expires_at) {
    ElMessage.warning('请填写广告权限依据和有效期')
    return
  }
  directForm.scheduled_times =
    directForm.send_mode === 'scheduled'
      ? directScheduleTimesText.value
          .split(/[,，\s]+/)
          .map((item) => item.trim())
          .filter(Boolean)
      : []
  try {
    await ElMessageBox.confirm(
      '确认已获得该群广告投放权限，并按填写的有效期记录到审计日志？',
      '确认广告权限',
      { type: 'warning', confirmButtonText: '确认并启动' },
    )
  } catch {
    return
  }
  const idempotencyKey =
    globalThis.crypto?.randomUUID?.() ||
    `direct-${Date.now()}-${Math.random().toString(16).slice(2)}`
  directSubmitting.value = true
  try {
    await automationApi.createAdOnlyDirectAssignment({
      ...directForm,
      invite_link: directForm.invite_link.trim(),
      permission_note: directForm.permission_note.trim(),
      idempotency_key: idempotencyKey,
    })
    directForm.invite_link = ''
    directDialogVisible.value = false
    activeView.value = 'handovers'
    ElMessage.success('直接指定已通过预检并进入队列')
    await loadData()
  } finally {
    directSubmitting.value = false
  }
}

const retryHandover = async (handover: any) => {
  actionId.value = `retry-${handover.id}`
  try {
    await automationApi.retryAdOnlyHandover(handover.id)
    ElMessage.success('重试已进入队列')
    await loadData()
  } finally {
    actionId.value = null
  }
}

const rollbackHandover = async (handover: any) => {
  try {
    await ElMessageBox.confirm(
      '将停用交接活动、取消专用账号归属并尝试让专用账号退群。',
      '确认回滚',
      { type: 'warning', confirmButtonText: '开始回滚' },
    )
  } catch {
    return
  }
  actionId.value = `rollback-${handover.id}`
  try {
    await automationApi.rollbackAdOnlyHandover(handover.id)
    ElMessage.success('回滚已进入队列')
    await loadData()
  } finally {
    actionId.value = null
  }
}

const openHistory = async (assessment: any) => {
  historyVisible.value = true
  historyLoading.value = true
  try {
    const response = await automationApi.getAdOnlyHistory(assessment.group_id)
    historyAssessments.value = response.data.data.assessments
    historyEvents.value = response.data.data.events
  } finally {
    historyLoading.value = false
  }
}

onMounted(loadData)
</script>

<template>
  <section v-loading="loading" class="ad-only-panel">
    <div class="settings-band">
      <div class="settings-main">
        <div class="setting-control">
          <span>自动评估</span>
          <el-switch v-model="settings.recommendation_enabled" />
        </div>
        <div class="setting-control">
          <span>允许执行交接</span>
          <el-switch v-model="settings.handover_execution_enabled" />
        </div>
        <div class="setting-control numeric">
          <span>连续样本</span>
          <el-input-number v-model="settings.min_consecutive_samples" :min="1" :max="100" />
        </div>
        <div class="setting-control numeric">
          <span>建议有效期</span>
          <el-input-number v-model="settings.recommendation_ttl_days" :min="1" :max="30" />
          <small>天</small>
        </div>
      </div>
      <div class="settings-actions">
        <el-button :icon="Refresh" :loading="loading" @click="loadData" />
        <el-button :icon="VideoPlay" :loading="evaluating" @click="evaluateCandidates">
          立即评估
        </el-button>
        <el-button
          type="warning"
          :icon="Plus"
          :disabled="!settings.handover_execution_enabled"
          @click="openDirectAssignment"
        >
          直接指定群
        </el-button>
        <el-button type="primary" :icon="Check" :loading="savingSettings" @click="saveSettings">
          保存设置
        </el-button>
      </div>
    </div>

    <div class="summary-strip">
      <div><span>当前候选</span><strong>{{ recommendedCount }}</strong></div>
      <div><span>已批准</span><strong>{{ approvedCount }}</strong></div>
      <div><span>活动交接</span><strong>{{ activeHandoverCount }}</strong></div>
      <div>
        <span>执行开关</span>
        <el-tag :type="settings.handover_execution_enabled ? 'success' : 'info'" effect="plain">
          {{ settings.handover_execution_enabled ? '已开启' : '已关闭' }}
        </el-tag>
      </div>
    </div>

    <el-tabs v-model="activeView" class="workflow-tabs">
      <el-tab-pane label="候选与审批" name="candidates">
        <el-table :data="candidates" row-key="id" class="candidate-table">
          <el-table-column type="expand" width="44">
            <template #default="{ row }">
              <div class="evidence-detail">
                <div>
                  <span>阻断原因</span>
                  <div class="tag-list">
                    <el-tag
                      v-for="reason in row.blocking_reasons"
                      :key="reason"
                      type="warning"
                      effect="plain"
                    >
                      {{ blockerLabel(reason) }}
                    </el-tag>
                    <span v-if="!row.blocking_reasons.length">无</span>
                  </div>
                </div>
                <div>
                  <span>证据哈希</span>
                  <code>{{ row.evidence_hash }}</code>
                </div>
                <div>
                  <span>快照时间</span>
                  <strong>{{ formatTime(row.created_at) }}</strong>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="群" min-width="190">
            <template #default="{ row }">
              <div class="primary-cell">
                <strong>{{ row.group_title || row.telegram_group_id }}</strong>
                <small>#{{ row.group_id }} · {{ row.telegram_group_id }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="Growth 账号" min-width="150">
            <template #default="{ row }">
              {{ row.source_growth_account_label || row.source_growth_account_id || '-' }}
            </template>
          </el-table-column>
          <el-table-column label="连续正式样本" width="120" align="center">
            <template #default="{ row }">
              <strong>{{ row.consecutive_success_count }}</strong>
              <small class="table-subline">待检查 {{ row.pending_sample_count }}</small>
            </template>
          </el-table-column>
          <el-table-column label="发送 / 24h" width="130" align="center">
            <template #default="{ row }">
              {{ row.send_success_percent }}% / {{ row.survival_24h_percent }}%
            </template>
          </el-table-column>
          <el-table-column label="建议" width="105">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" effect="plain">
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="审批" width="100">
            <template #default="{ row }">{{ decisionLabel(row) }}</template>
          </el-table-column>
          <el-table-column label="有效期" width="170">
            <template #default="{ row }">{{ formatTime(row.valid_until) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="310" fixed="right">
            <template #default="{ row }">
              <el-button link @click="openHistory(row)">历史</el-button>
              <el-button
                v-if="row.status === 'recommended'"
                link
                type="success"
                :loading="actionId === `decision-${row.id}`"
                @click="decide(row, 'approve')"
              >
                批准
              </el-button>
              <el-button
                v-if="row.status === 'recommended'"
                link
                type="warning"
                @click="decide(row, 'defer')"
              >
                暂缓
              </el-button>
              <el-button
                v-if="row.status === 'recommended'"
                link
                type="danger"
                @click="decide(row, 'reject')"
              >
                拒绝
              </el-button>
              <el-button
                v-if="row.decision?.event_type === 'assessment_approved'"
                link
                type="primary"
                :disabled="!settings.handover_execution_enabled"
                @click="openHandover(row)"
              >
                启动交接
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="交接任务" name="handovers">
        <el-table :data="handovers" row-key="id">
          <el-table-column label="类型" width="100">
            <template #default="{ row }">
              <el-tag :type="row.workflow_type === 'direct' ? 'warning' : 'info'" effect="plain">
                {{ row.workflow_type === 'direct' ? '直接指定' : '评估接管' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="群" min-width="180">
            <template #default="{ row }">
              <div class="primary-cell">
                <strong>{{ row.group_title || row.telegram_group_id || '等待解析群链接' }}</strong>
                <small>交接 #{{ row.id }}</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="账号交接" min-width="240">
            <template #default="{ row }">
              {{ row.workflow_type === 'direct' ? '管理员指定' : (row.source_growth_account_label || row.source_growth_account_id) }}
              →
              {{ row.target_ad_only_account_label || row.target_ad_only_account_id }}
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" effect="plain">
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="当前步骤" min-width="150" prop="current_step" />
          <el-table-column label="频率" width="130">
            <template #default="{ row }">
              {{ row.send_mode === 'scheduled' ? row.scheduled_times.join(', ') : `${row.interval_minutes} 分钟` }}
            </template>
          </el-table-column>
          <el-table-column label="重试" width="70" align="center" prop="retry_count" />
          <el-table-column label="错误" min-width="190" show-overflow-tooltip prop="last_error" />
          <el-table-column label="更新时间" width="170">
            <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="165" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="['failed', 'cleanup_pending'].includes(row.status)"
                link
                type="primary"
                :icon="RefreshLeft"
                :loading="actionId === `retry-${row.id}`"
                @click="retryHandover(row)"
              >
                重试
              </el-button>
              <el-button
                v-if="!['completed', 'rolled_back', 'cancelled', 'running'].includes(row.status)"
                link
                type="warning"
                :loading="actionId === `rollback-${row.id}`"
                @click="rollbackHandover(row)"
              >
                回滚
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-dialog
      v-model="handoverDialogVisible"
      title="启动 Ad-only 交接"
      width="min(620px, calc(100vw - 32px))"
    >
      <el-form label-position="top">
        <div class="dialog-grid">
          <el-form-item label="目标专用账号" required>
            <el-select v-model="form.target_account_id" filterable>
              <el-option
                v-for="account in options.accounts"
                :key="account.id"
                :label="account.label"
                :value="account.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="广告素材" required>
            <el-select v-model="form.creative_id" filterable>
              <el-option
                v-for="creative in options.creatives"
                :key="creative.id"
                :label="creative.name"
                :value="creative.id"
              />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="群邀请链接" required>
          <el-input
            v-model="form.invite_link"
            type="password"
            show-password
            autocomplete="off"
            placeholder="t.me 公共群或私有邀请链接"
          />
        </el-form-item>
        <el-form-item label="发送模式">
          <el-radio-group v-model="form.send_mode">
            <el-radio-button value="interval">固定间隔</el-radio-button>
            <el-radio-button value="scheduled">每日定时</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.send_mode === 'interval'" label="发送间隔">
          <el-input-number v-model="form.interval_minutes" :min="30" :max="10080" :step="30" />
          <span class="unit-label">分钟</span>
        </el-form-item>
        <el-form-item v-else label="每日时间">
          <el-input v-model="scheduleTimesText" placeholder="09:00,18:00" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="handoverDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :icon="VideoPlay"
          :loading="handoverSubmitting"
          @click="createHandover"
        >
          预检并启动
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="directDialogVisible"
      title="直接指定 Ad-only 群"
      width="min(680px, calc(100vw - 32px))"
    >
      <el-form label-position="top">
        <div class="dialog-grid">
          <el-form-item label="Ad-only 账号" required>
            <el-select v-model="directForm.target_account_id" filterable>
              <el-option
                v-for="account in options.accounts"
                :key="account.id"
                :label="account.label"
                :value="account.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="广告素材" required>
            <el-select v-model="directForm.creative_id" filterable>
              <el-option
                v-for="creative in options.creatives"
                :key="creative.id"
                :label="creative.name"
                :value="creative.id"
              />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="群邀请链接" required>
          <el-input
            v-model="directForm.invite_link"
            type="password"
            show-password
            autocomplete="off"
            placeholder="t.me 公共群或私有邀请链接"
          />
        </el-form-item>
        <div class="dialog-grid">
          <el-form-item label="广告权限" required>
            <el-select v-model="directForm.permission_mode">
              <el-option label="允许常规软广" value="soft_ad_allowed" />
              <el-option label="允许高频广告" value="high_volume_ad_allowed" />
            </el-select>
          </el-form-item>
          <el-form-item label="权限有效期" required>
            <el-date-picker
              v-model="directForm.permission_expires_at"
              type="datetime"
              value-format="YYYY-MM-DDTHH:mm:ss"
              placeholder="选择失效时间"
            />
          </el-form-item>
        </div>
        <el-form-item label="权限依据" required>
          <el-input
            v-model="directForm.permission_note"
            type="textarea"
            :rows="2"
            maxlength="500"
            show-word-limit
            placeholder="管理员授权、群规或合作约定"
          />
        </el-form-item>
        <el-form-item label="发送模式">
          <el-radio-group v-model="directForm.send_mode">
            <el-radio-button value="interval">固定间隔</el-radio-button>
            <el-radio-button value="scheduled">每日定时</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="directForm.send_mode === 'interval'" label="发送间隔">
          <el-input-number
            v-model="directForm.interval_minutes"
            :min="30"
            :max="10080"
            :step="30"
          />
          <span class="unit-label">分钟</span>
        </el-form-item>
        <el-form-item v-else label="每日时间">
          <el-input v-model="directScheduleTimesText" placeholder="09:00,18:00" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="directDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :icon="VideoPlay"
          :loading="directSubmitting"
          @click="createDirectAssignment"
        >
          预检并启动
        </el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="historyVisible"
      title="候选与交接审计"
      size="min(720px, 92vw)"
    >
      <div v-loading="historyLoading" class="history-drawer">
        <el-timeline>
          <el-timeline-item
            v-for="event in historyEvents"
            :key="event.id"
            :timestamp="formatTime(event.created_at)"
            :type="statusType(event.status || '')"
            :icon="event.event_type.includes('defer') ? Clock : undefined"
          >
            <strong>{{ event.event_type }}</strong>
            <p>{{ event.message || '-' }}</p>
          </el-timeline-item>
        </el-timeline>
        <el-divider>不可变评估快照</el-divider>
        <div v-for="item in historyAssessments" :key="item.id" class="snapshot-row">
          <span>#{{ item.id }} · {{ formatTime(item.created_at) }}</span>
          <el-tag :type="statusType(item.status)" effect="plain">{{ statusLabel(item.status) }}</el-tag>
          <strong>{{ item.consecutive_success_count }} 个连续样本</strong>
        </div>
      </div>
    </el-drawer>
  </section>
</template>

<style scoped lang="scss">
.ad-only-panel {
  min-height: 360px;
}

.settings-band,
.summary-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.settings-main,
.settings-actions,
.setting-control,
.summary-strip > div,
.tag-list {
  display: flex;
  align-items: center;
  gap: 10px;
}

.settings-main {
  flex-wrap: wrap;
}

.setting-control {
  min-height: 36px;
  color: var(--el-text-color-regular);
}

.setting-control.numeric :deep(.el-input-number) {
  width: 112px;
}

.setting-control small,
.table-subline,
.primary-cell small {
  color: var(--el-text-color-secondary);
}

.summary-strip {
  justify-content: flex-start;
}

.summary-strip > div {
  min-width: 150px;
}

.summary-strip strong {
  font-size: 22px;
  line-height: 1;
}

.workflow-tabs {
  margin-top: 14px;
}

.primary-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.table-subline {
  display: block;
  margin-top: 3px;
}

.evidence-detail {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(280px, 1fr) 180px;
  gap: 18px;
  padding: 12px 18px;
  background: var(--el-fill-color-light);
}

.evidence-detail > div {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.evidence-detail code {
  overflow-wrap: anywhere;
  color: var(--el-text-color-regular);
}

.tag-list {
  flex-wrap: wrap;
}

.dialog-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.unit-label {
  margin-left: 10px;
  color: var(--el-text-color-secondary);
}

.history-drawer {
  padding-right: 12px;
}

.history-drawer p {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
}

.snapshot-row {
  display: grid;
  grid-template-columns: 1fr 90px 140px;
  align-items: center;
  gap: 12px;
  min-height: 44px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

@media (max-width: 900px) {
  .settings-band,
  .settings-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .settings-actions :deep(.el-button + .el-button) {
    margin-left: 0;
  }

  .summary-strip {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }

  .summary-strip > div {
    min-width: 0;
  }

  .evidence-detail,
  .dialog-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 600px) {
  .snapshot-row {
    grid-template-columns: 1fr auto;
  }

  .snapshot-row strong {
    grid-column: 1 / -1;
  }
}
</style>
