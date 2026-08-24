<script setup lang="ts">
import { computed, watch } from 'vue'

import ClientListPagination from '@/components/ClientListPagination.vue'
import type { Account } from '@/api/accounts'
import type { AdDynamicStatus } from '@/api/automation'
import { useClientPagination } from '@/utils/clientPagination'

type TagType = 'success' | 'warning' | 'danger' | 'info'

const props = defineProps<{
  visible: boolean
  account: Account | null
  status: AdDynamicStatus | null
}>()

const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
}>()

const title = computed(() => {
  const label = props.account?.display_name || props.account?.identifier || props.status?.account_label || '推广账号'
  return `投放阻塞详情 - ${label}`
})

const diagnostic = computed(() => props.status?.delivery_diagnostic)
const blockedGroupSamples = computed(() => diagnostic.value?.blocked_group_samples || [])
const recentErrors = computed(() => props.status?.recent_errors || [])
const blockedGroupPagination = useClientPagination(blockedGroupSamples, 5)
const recentErrorPagination = useClientPagination(recentErrors, 5)

watch(
  () => props.account?.id,
  () => {
    blockedGroupPagination.reset()
    recentErrorPagination.reset()
  },
)

function severityTagType(severity?: string): TagType {
  if (severity === 'danger') return 'danger'
  if (severity === 'warning') return 'warning'
  if (severity === 'success') return 'success'
  return 'info'
}

function deliveryTagType(status: AdDynamicStatus | null): TagType {
  if (!status?.delivery_diagnostic) return 'info'
  if (status.delivery_diagnostic.ad_delivery_allowed) return 'success'
  return severityTagType(status.delivery_diagnostic.primary_block_severity || 'danger')
}

function deliveryLabel(status: AdDynamicStatus | null) {
  if (!status?.delivery_diagnostic) return '状态评估中'
  if (status.delivery_diagnostic.ad_delivery_allowed) return '可投放'
  return status.delivery_diagnostic.primary_block_label || '投放阻塞'
}

function nextActionText(status: AdDynamicStatus | null) {
  const item = status?.delivery_diagnostic
  if (!item) return '-'
  return item.next_action_at
    ? `${item.next_action_label} · ${item.next_action_at.slice(5, 16).replace('T', ' ')}`
    : item.next_action_label
}

function pct(value?: number) {
  return `${Math.round(Number(value || 0) * 100)}%`
}
</script>

<template>
  <el-drawer
    :model-value="visible"
    :title="title"
    size="760px"
    class="delivery-block-drawer"
    @update:model-value="emit('update:visible', $event)"
  >
    <div v-if="status" class="drawer-content">
      <div class="delivery-summary">
        <div class="summary-state">
          <span>当前投放状态</span>
          <el-tag :type="deliveryTagType(status)" effect="dark" size="large">
            {{ deliveryLabel(status) }}
          </el-tag>
        </div>
        <div class="summary-metrics">
          <div>
            <span>风险分</span>
            <strong>{{ status.risk_score }}</strong>
          </div>
          <div>
            <span>健康分</span>
            <strong>{{ Math.round(status.health_score || 0) }}</strong>
          </div>
          <div>
            <span>可投放群</span>
            <strong>{{ status.ad_eligible_groups }}</strong>
          </div>
          <div>
            <span>日 / 轮额度</span>
            <strong>{{ status.dynamic_daily_limit }} / {{ status.dynamic_run_limit }}</strong>
          </div>
        </div>
      </div>

      <el-descriptions :column="2" border class="detail-descriptions">
        <el-descriptions-item label="下一步动作">{{ nextActionText(status) }}</el-descriptions-item>
        <el-descriptions-item label="探针 / 发言成功率">
          {{ pct(status.probe_success_rate_24h) }} /
          {{ pct(status.writable_rate) }}
        </el-descriptions-item>
        <el-descriptions-item label="探针执行">
          {{ diagnostic?.probe_execution_allowed ? '允许' : '阻断' }}
        </el-descriptions-item>
        <el-descriptions-item label="广告发送">
          {{ diagnostic?.ad_delivery_allowed ? '允许' : '暂停' }}
        </el-descriptions-item>
        <el-descriptions-item label="活动" :span="2">
          {{
            diagnostic?.active_campaign_name ||
            (diagnostic?.active_campaign_id ? `#${diagnostic.active_campaign_id}` : '无启用活动')
          }}
        </el-descriptions-item>
      </el-descriptions>

      <section class="detail-section">
        <h4>阻塞原因</h4>
        <div v-if="diagnostic?.block_reasons?.length" class="diagnostic-tags">
          <el-tag
            v-for="reason in diagnostic.block_reasons"
            :key="reason.reason"
            :type="severityTagType(reason.severity)"
            effect="plain"
          >
            {{ reason.label }}{{ reason.detail ? `：${reason.detail}` : '' }}
          </el-tag>
        </div>
        <el-empty v-else description="当前没有投放阻塞原因" :image-size="64" />
      </section>

      <section class="detail-section">
        <h4>健康扣分</h4>
        <div v-if="status.dynamic_health_diagnostic?.negative_adjustments?.length" class="diagnostic-tags">
          <el-tag
            v-for="item in status.dynamic_health_diagnostic.negative_adjustments"
            :key="item.reason"
            :type="severityTagType(item.severity)"
            effect="plain"
          >
            {{ item.label }} {{ item.delta }}
          </el-tag>
        </div>
        <el-empty v-else description="当前没有健康扣分项" :image-size="64" />
      </section>

      <section class="detail-section">
        <h4>阻塞群样本</h4>
        <el-table
          :data="blockedGroupPagination.rows.value"
          border
          size="small"
          empty-text="暂无阻塞群样本"
        >
          <el-table-column label="群" min-width="170" show-overflow-tooltip>
            <template #default="{ row }">{{ row.title || row.telegram_group_id }}</template>
          </el-table-column>
          <el-table-column label="原因" min-width="150">
            <template #default="{ row }">
              <el-tag :type="severityTagType(row.severity)" size="small">{{ row.label }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="暖号 / 探针 / 广告" min-width="220" show-overflow-tooltip>
            <template #default="{ row }"
              >{{ row.warmup_status }} / {{ row.probe_status }} / {{ row.ad_status }}</template
            >
          </el-table-column>
        </el-table>
        <ClientListPagination
          v-model:page="blockedGroupPagination.page.value"
          v-model:page-size="blockedGroupPagination.pageSize.value"
          :total="blockedGroupPagination.total.value"
          :page-sizes="[5, 10, 20]"
        />
      </section>

      <section class="detail-section">
        <h4>近期错误</h4>
        <el-table :data="recentErrorPagination.rows.value" border size="small" empty-text="暂无近期错误">
          <el-table-column prop="error" label="错误" min-width="260" show-overflow-tooltip />
          <el-table-column prop="count" label="次数" width="100" />
        </el-table>
        <ClientListPagination
          v-model:page="recentErrorPagination.page.value"
          v-model:page-size="recentErrorPagination.pageSize.value"
          :total="recentErrorPagination.total.value"
          :page-sizes="[5, 10, 20]"
        />
      </section>
    </div>

    <el-empty v-else description="该账号的运营状态仍在评估中，请稍后刷新" />
  </el-drawer>
</template>

<style scoped>
.drawer-content {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.delivery-summary {
  display: grid;
  grid-template-columns: minmax(180px, 0.7fr) minmax(0, 2fr);
  gap: 14px;
}

.summary-state,
.summary-metrics > div {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 7px;
  min-height: 72px;
  padding: 12px;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
}

.summary-state span,
.summary-metrics span {
  color: #909399;
  font-size: 12px;
}

.summary-state .el-tag {
  align-self: flex-start;
}

.summary-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.summary-metrics strong {
  color: #303133;
  font-size: 18px;
}

.detail-descriptions {
  width: 100%;
}

.detail-section h4 {
  margin: 0 0 10px;
  color: #303133;
  font-size: 14px;
}

.diagnostic-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

@media (max-width: 768px) {
  .delivery-summary,
  .summary-metrics {
    grid-template-columns: 1fr;
  }
}
</style>
