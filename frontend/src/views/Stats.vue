<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { ElButton, ElIcon, ElCard, ElRow, ElCol, ElDatePicker, ElTable, ElTableColumn, ElSelect, ElOption, ElEmpty, ElMessage, ElTag } from 'element-plus'
import { Download, Refresh } from '@element-plus/icons-vue'
import { useStatsStore } from '@/stores/stats'
import { statsApi } from '@/api/stats'
import type { P4Summary } from '@/api/stats'
import { downloadBlob } from '@/utils/download'
import ECharts from '@/components/ECharts.vue'
import dayjs from 'dayjs'

const statsStore = useStatsStore()

const loading = ref(false)
const dateType = ref('week')
const customDateRange = ref<[string, string] | null>(null)
const weeklyGrowth = computed(() => statsStore.overview?.weeklyGrowth || 0)
const trendData = computed(() => statsStore.trendData || [])
const funnelData = computed(() => statsStore.funnelData || [])
const sourceData = computed(() => statsStore.sourceData || [])

// P4 广告公平 pacing / 管理指标 / 历史治理摘要（只读）
const p4Summary = ref<P4Summary | null>(null)

const fetchP4Summary = async () => {
  try {
    const response = await statsApi.p4Summary()
    p4Summary.value = response.data.data
  } catch (error) {
    console.error('Failed to fetch P4 summary:', error)
  }
}

const p4PacingMode = computed(() => {
  const flags = p4Summary.value?.flags
  if (!flags?.ad_pacing_enabled) return '关闭'
  if (flags.ad_pacing_shadow) return '影子模式'
  return flags.canary_account_ids.length ? `灰度(${flags.canary_account_ids.length}账号)` : '全量'
})

const p4HistoryMode = computed(() => {
  const flags = p4Summary.value?.flags
  if (!flags?.history_governance_enabled) return '关闭'
  return flags.history_governance_dry_run ? 'Dry-run' : '归档中'
})

const formatSeconds = (value: number | null | undefined) => {
  if (value === null || value === undefined) return '-'
  if (value < 60) return `${Math.round(value)}秒`
  if (value < 3600) return `${Math.floor(value / 60)}分${Math.round(value % 60)}秒`
  return `${Math.floor(value / 3600)}时${Math.round((value % 3600) / 60)}分`
}

const formatComponent = (value: number | undefined) =>
  value === undefined || value === null ? '-' : value.toFixed(2)

const gateReasonText: Record<string, string> = {
  gated_account_state: '账号状态',
  gated_risk_pause: '风控暂停',
  gated_budget_wait: '预算等待',
  gated_survival_backlog: '存活积压',
  gated_unresolved_backlog: '未决发送',
}

const riskTagType = (level: string) => {
  if (level === 'normal') return 'success'
  if (level === 'watch') return 'warning'
  return 'danger'
}

const dateRange = computed(() => {
  const now = dayjs()
  switch (dateType.value) {
    case 'today':
      return [now.startOf('day').format('YYYY-MM-DD'), now.endOf('day').format('YYYY-MM-DD')]
    case 'week':
      return [now.subtract(7, 'day').format('YYYY-MM-DD'), now.format('YYYY-MM-DD')]
    case 'month':
      return [now.subtract(30, 'day').format('YYYY-MM-DD'), now.format('YYYY-MM-DD')]
    case 'custom':
      return customDateRange.value || [now.subtract(7, 'day').format('YYYY-MM-DD'), now.format('YYYY-MM-DD')]
    default:
      return [now.subtract(7, 'day').format('YYYY-MM-DD'), now.format('YYYY-MM-DD')]
  }
})

const trendChartOption = computed(() => {
  const data = trendData.value
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
    },
    legend: {
      data: ['注册用户', '转化用户', '活跃用户'],
      bottom: 0,
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '15%',
      top: '10%',
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: data.map((d) => dayjs(d.date).format('MM-DD')),
    },
    yAxis: {
      type: 'value',
    },
    series: [
      {
        name: '注册用户',
        type: 'bar',
        data: data.map((d) => d.registered),
        itemStyle: { color: '#409eff' },
      },
      {
        name: '转化用户',
        type: 'bar',
        data: data.map((d) => d.converted),
        itemStyle: { color: '#67c23a' },
      },
      {
        name: '活跃用户',
        type: 'line',
        smooth: true,
        data: data.map((d) => d.active),
        itemStyle: { color: '#e6a23c' },
        lineStyle: { width: 2 },
      },
    ],
  }
})

const funnelChartOption = computed(() => {
  const data = funnelData.value
  return {
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c} ({d}%)',
    },
    legend: {
      orient: 'vertical',
      left: 'left',
      top: 'center',
    },
    series: [
      {
        type: 'funnel',
        left: '20%',
        top: '10%',
        bottom: '10%',
        width: '60%',
        min: 0,
        max: 100,
        gap: 2,
        label: {
          show: true,
          position: 'inside',
        },
        labelLine: {
          show: false,
        },
        itemStyle: {
          borderColor: '#fff',
          borderWidth: 1,
        },
        data: data.map((d, i) => ({
          value: d.count,
          name: d.stage,
          itemStyle: {
            color: ['#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#909399'][i] || '#409eff',
          },
        })),
      },
    ],
  }
})

const sourceChartOption = computed(() => {
  const data = sourceData.value
  return {
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c} ({d}%)',
    },
    legend: {
      orient: 'vertical',
      right: '5%',
      top: 'center',
    },
    series: [
      {
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['35%', '50%'],
        avoidLabelOverlap: false,
        itemStyle: {
          borderRadius: 10,
          borderColor: '#fff',
          borderWidth: 2,
        },
        label: {
          show: false,
        },
        emphasis: {
          label: {
            show: true,
            fontSize: 14,
            fontWeight: 'bold',
          },
        },
        data: data.map((d) => ({
          value: d.count,
          name: d.source,
        })),
      },
    ],
  }
})

const groupColumns = [
  { prop: 'id', label: 'ID', width: '80' },
  { prop: 'title', label: '群组名称', minWidth: '200' },
  { prop: 'memberCount', label: '成员数', width: '120', align: 'right' },
  { prop: 'dailyActive', label: '日活跃', width: '120', align: 'right' },
  { prop: 'weeklyGrowth', label: '周增长', width: '120', slot: 'growth' },
]

const keywordColumns = [
  { prop: 'keyword', label: '关键词', minWidth: '200' },
  { prop: 'type', label: '类型', width: '100', slot: 'type' },
  { prop: 'hitCount', label: '命中次数', width: '120', align: 'right' },
  { prop: 'lastHitAt', label: '最后命中', width: '180', slot: 'lastHit' },
]

const fetchAllStats = async () => {
  loading.value = true
  try {
    const [startDate, endDate] = dateRange.value
    await Promise.all([
      statsStore.fetchTrend({ startDate, endDate }),
      statsStore.fetchFunnel({ startDate, endDate }),
      statsStore.fetchSources({ startDate, endDate }),
      statsStore.fetchOverview({ startDate, endDate }),
      fetchP4Summary(),
    ])
  } catch (error) {
    console.error('Failed to fetch stats:', error)
  } finally {
    loading.value = false
  }
}

const handleExport = async (type: string) => {
  const [startDate, endDate] = dateRange.value
  try {
    const response = await statsApi.export({ type, startDate, endDate })
    downloadBlob(response.data, `vanguard-${type}.csv`)
  } catch (error) {
    console.error('Failed to export stats:', error)
    ElMessage.error('导出失败')
  }
}

const handleDateTypeChange = () => {
  fetchAllStats()
}

watch(dateType, handleDateTypeChange)

onMounted(() => {
  fetchAllStats()
})
</script>

<template>
  <div class="stats-page">
    <div class="page-header">
      <h2 class="page-title">数据统计</h2>
      <div class="header-actions">
        <el-date-picker
          v-if="dateType === 'custom'"
          v-model="customDateRange"
          type="daterange"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          value-format="YYYY-MM-DD"
          @change="fetchAllStats"
        />
        <el-select v-model="dateType" style="width: 120px;" @change="handleDateTypeChange">
          <el-option label="今日" value="today" />
          <el-option label="近7天" value="week" />
          <el-option label="近30天" value="month" />
          <el-option label="自定义" value="custom" />
        </el-select>
        <el-button @click="fetchAllStats" :loading="loading">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
      </div>
    </div>

    <el-row :gutter="20" class="overview-row">
      <el-col :span="6">
        <el-card shadow="hover" class="overview-card">
          <div class="overview-item">
            <span class="label">总用户数</span>
            <span class="value">{{ statsStore.overview?.totalUsers || 0 }}</span>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="overview-card">
          <div class="overview-item">
            <span class="label">今日注册</span>
            <span class="value highlight">{{ statsStore.overview?.todayRegistered || 0 }}</span>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="overview-card">
          <div class="overview-item">
            <span class="label">今日活跃</span>
            <span class="value highlight">{{ statsStore.overview?.todayActive || 0 }}</span>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="overview-card">
          <div class="overview-item">
            <span class="label">周增长率</span>
            <span class="value" :class="{ positive: weeklyGrowth > 0 }">
              {{ weeklyGrowth > 0 ? '+' : '' }}{{ weeklyGrowth }}%
            </span>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>用户趋势</span>
              <el-button type="primary" link @click="handleExport('trend')">
                <el-icon><Download /></el-icon>
                导出
              </el-button>
            </div>
          </template>
          <ECharts
            v-if="trendData.length"
            :option="trendChartOption"
            height="350px"
          />
          <el-empty v-else description="暂无数据" />
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>转化漏斗</span>
            </div>
          </template>
          <ECharts
            v-if="funnelData.length"
            :option="funnelChartOption"
            height="350px"
          />
          <el-empty v-else description="暂无数据" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>群组统计 TOP 10</span>
              <el-button type="primary" link @click="handleExport('groups')">
                <el-icon><Download /></el-icon>
                导出
              </el-button>
            </div>
          </template>
          <el-table :data="[]" stripe>
            <el-table-column v-for="col in groupColumns" :key="col.prop" v-bind="col">
              <template v-if="col.slot" #default="{ row }">
                <span v-if="col.prop === 'weeklyGrowth'">
                  <span :class="{ positive: row.weeklyGrowth > 0, negative: row.weeklyGrowth < 0 }">
                    {{ row.weeklyGrowth > 0 ? '+' : '' }}{{ row.weeklyGrowth }}%
                  </span>
                </span>
              </template>
            </el-table-column>
          </el-table>
          <el-empty description="暂无数据" />
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>关键词命中率 TOP 10</span>
              <el-button type="primary" link @click="handleExport('keywords')">
                <el-icon><Download /></el-icon>
                导出
              </el-button>
            </div>
          </template>
          <el-table :data="[]" stripe>
            <el-table-column v-for="col in keywordColumns" :key="col.prop" v-bind="col">
              <template v-if="col.slot === 'type'" #default="{ row }">
                <el-tag :type="row.type === 'whitelist' ? 'success' : 'danger'" size="small">
                  {{ row.type === 'whitelist' ? '白' : '黑' }}
                </el-tag>
              </template>
              <template v-else-if="col.slot === 'lastHit'" #default="{ row }">
                {{ row.lastHitAt ? dayjs(row.lastHitAt).format('MM-DD HH:mm') : '-' }}
              </template>
            </el-table-column>
          </el-table>
          <el-empty description="暂无数据" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="24">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>用户来源分布</span>
            </div>
          </template>
          <ECharts
            v-if="sourceData.length"
            :option="sourceChartOption"
            height="300px"
          />
          <el-empty v-else description="暂无数据" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="24">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>P4 广告公平调度与治理摘要</span>
              <el-tag size="small" :type="p4PacingMode === '关闭' ? 'info' : 'success'">
                Pacing: {{ p4PacingMode }}
              </el-tag>
              <el-tag size="small" :type="p4HistoryMode === '关闭' ? 'info' : 'warning'">
                历史治理: {{ p4HistoryMode }}
              </el-tag>
            </div>
          </template>

          <el-row :gutter="12" class="p4-cards">
            <el-col :span="4">
              <div class="p4-item">
                <span class="label">广告 Outbox 待处理</span>
                <span class="value">{{ p4Summary?.outbox?.pending ?? '-' }}</span>
                <span class="sub">最老 {{ formatSeconds(p4Summary?.outbox?.oldest_age_seconds ?? null) }}</span>
              </div>
            </el-col>
            <el-col :span="4">
              <div class="p4-item">
                <span class="label">存活积压</span>
                <span class="value">{{ p4Summary?.survival?.backlog ?? '-' }}</span>
                <span class="sub">到期待检 {{ p4Summary?.survival?.check_due ?? 0 }}</span>
              </div>
            </el-col>
            <el-col :span="4">
              <div class="p4-item">
                <span class="label">未决发送</span>
                <span class="value" :class="{ negative: (p4Summary?.survival?.unresolved_sends ?? 0) > 0 }">
                  {{ p4Summary?.survival?.unresolved_sends ?? '-' }}
                </span>
                <span class="sub">pending/unknown 目标为 0 是发布门禁</span>
              </div>
            </el-col>
            <el-col :span="4">
              <div class="p4-item">
                <span class="label">回执延迟 P95</span>
                <span class="value">{{ formatSeconds(p4Summary?.receipts?.latency_p95_seconds ?? null) }}</span>
                <span class="sub">SLO 900 秒</span>
              </div>
            </el-col>
            <el-col :span="4">
              <div class="p4-item">
                <span class="label">对账逾期</span>
                <span class="value" :class="{ negative: (p4Summary?.reconciliation?.overdue ?? 0) > 0 }">
                  {{ p4Summary?.reconciliation?.overdue ?? '-' }}
                </span>
                <span class="sub">任务待处理</span>
              </div>
            </el-col>
            <el-col :span="4">
              <div class="p4-item">
                <span class="label">可归档操作</span>
                <span class="value">{{ p4Summary?.history?.candidates?.operations ?? '-' }}</span>
                <span class="sub">归档 {{ p4Summary?.history?.archive_count ?? 0 }} 条</span>
              </div>
            </el-col>
          </el-row>

          <el-table
            v-if="p4Summary?.pacing?.accounts?.length"
            :data="p4Summary.pacing.accounts"
            stripe
            max-height="360"
            class="p4-account-table"
          >
            <el-table-column prop="account_id" label="账号 ID" width="90" />
            <el-table-column label="风险等级" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="riskTagType(row.risk_level)">
                  {{ row.risk_level || 'normal' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="Pacing 评分" width="110" align="right">
              <template #default="{ row }">{{ formatComponent(row.score) }}</template>
            </el-table-column>
            <el-table-column label="门禁" width="110">
              <template #default="{ row }">
                <el-tag v-if="row.gate_reason" size="small" type="danger">
                  {{ gateReasonText[row.gate_reason] || row.gate_reason }}
                </el-tag>
                <el-tag v-else size="small" type="success">通过</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="预算余量" width="100" align="right">
              <template #default="{ row }">{{ formatComponent(row.budget_headroom) }}</template>
            </el-table-column>
            <el-table-column label="读取恢复" width="100" align="right">
              <template #default="{ row }">{{ formatComponent(row.read_recovery_factor) }}</template>
            </el-table-column>
            <el-table-column label="群冷却就绪" width="110" align="right">
              <template #default="{ row }">{{ formatComponent(row.cooldown_readiness) }}</template>
            </el-table-column>
            <el-table-column label="存活健康" width="100" align="right">
              <template #default="{ row }">{{ formatComponent(row.survival_health) }}</template>
            </el-table-column>
            <el-table-column label="近期成功率" width="110" align="right">
              <template #default="{ row }">{{ formatComponent(row.recent_success_rate) }}</template>
            </el-table-column>
            <el-table-column label="公平份额 / 实际份额" min-width="180">
              <template #default="{ row }">
                {{ formatComponent(row.fair_share) }} / {{ formatComponent(row.actual_share) }}
              </template>
            </el-table-column>
            <el-table-column label="调度序" width="80" align="right">
              <template #default="{ row }">{{ row.rank || '-' }}</template>
            </el-table-column>
          </el-table>
          <el-empty
            v-else
            description="暂无 P4 决策数据（开启 P4_AD_PACING 后此处显示每次调度窗口的账号候选、门禁与评分）"
          />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped lang="scss">
.stats-page {
  padding: 0;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.page-title {
  font-size: 20px;
  font-weight: 600;
  margin: 0;
  color: #303133;
}

.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

.overview-row {
  margin-bottom: 20px;
}

.overview-card {
  :deep(.el-card__body) {
    padding: 20px;
  }
}

.overview-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;

  .label {
    font-size: 14px;
    color: #909399;
  }

  .value {
    font-size: 28px;
    font-weight: 600;
    color: #303133;

    &.highlight {
      color: #409eff;
    }

    &.positive {
      color: #67c23a;
    }

    &.negative {
      color: #f56c6c;
    }
  }
}

.charts-row {
  margin-bottom: 20px;
}

.p4-cards {
  margin-bottom: 16px;
}

.p4-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px;
  background: #f5f7fa;
  border-radius: 6px;

  .label {
    font-size: 12px;
    color: #909399;
  }

  .value {
    font-size: 22px;
    font-weight: 600;
    color: #303133;
  }

  .sub {
    font-size: 12px;
    color: #c0c4cc;
  }
}

.p4-account-table {
  margin-top: 4px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.positive {
  color: #67c23a;
}

.negative {
  color: #f56c6c;
}
</style>
