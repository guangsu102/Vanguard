<script setup lang="ts">
import { ref, reactive, onMounted, computed, watch } from 'vue'
import { ElButton, ElIcon, ElCard, ElRow, ElCol, ElDatePicker, ElTable, ElSelect, ElOption, ElEmpty } from 'element-plus'
import { Download, Refresh, Calendar } from '@element-plus/icons-vue'
import { useStatsStore } from '@/stores/stats'
import ECharts from '@/components/ECharts.vue'
import dayjs from 'dayjs'

const statsStore = useStatsStore()

const loading = ref(false)
const dateType = ref('week')
const customDateRange = ref<[string, string] | null>(null)

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
  const data = statsStore.trendData || []
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
  const data = statsStore.funnelData || []
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
  const data = statsStore.sourceData || []
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
    ])
  } catch (error) {
    console.error('Failed to fetch stats:', error)
  } finally {
    loading.value = false
  }
}

const handleExport = (type: string) => {
  const [startDate, endDate] = dateRange.value
  window.open(`/api/stats/export?type=${type}&startDate=${startDate}&endDate=${endDate}`, '_blank')
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
            <span class="value" :class="{ positive: (statsStore.overview?.weeklyGrowth || 0) > 0 }">
              {{ statsStore.overview?.weeklyGrowth > 0 ? '+' : '' }}{{ statsStore.overview?.weeklyGrowth || 0 }}%
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
            v-if="statsStore.trendData.length"
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
            v-if="statsStore.funnelData.length"
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
            v-if="statsStore.sourceData.length"
            :option="sourceChartOption"
            height="300px"
          />
          <el-empty v-else description="暂无数据" />
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
