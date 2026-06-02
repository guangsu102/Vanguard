<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { ElCard, ElRow, ElCol, ElStatistic, ElButton, ElIcon, ElEmpty } from 'element-plus'
import { Refresh, Download } from '@element-plus/icons-vue'
import { useStatsStore } from '@/stores/stats'
import ECharts from '@/components/ECharts.vue'
import dayjs from 'dayjs'

const statsStore = useStatsStore()

const loading = ref(true)
const autoRefresh = ref(false)
let refreshInterval: ReturnType<typeof setInterval> | null = null

const stats = computed(() => statsStore.dashboardStats || {
  totalAccounts: 0,
  onlineAccounts: 0,
  totalGroups: 0,
  totalUsers: 0,
  dailyRegistered: 0,
  conversionRate: 0,
})

const trendChartOption = computed(() => {
  const data = statsStore.dashboardStats?.weeklyTrend || []
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
    },
    legend: {
      data: ['注册', '转化'],
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
        name: '注册',
        type: 'line',
        smooth: true,
        data: data.map((d) => d.registered),
        itemStyle: { color: '#409eff' },
        areaStyle: { color: 'rgba(64, 158, 255, 0.1)' },
      },
      {
        name: '转化',
        type: 'line',
        smooth: true,
        data: data.map((d) => d.converted),
        itemStyle: { color: '#67c23a' },
        areaStyle: { color: 'rgba(103, 194, 58, 0.1)' },
      },
    ],
  }
})

const distributionChartOption = computed(() => {
  const data = statsStore.dashboardStats?.accountDistribution || []
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
          name: d.status === 'online' ? '在线' : d.status === 'offline' ? '离线' : '封禁',
        })),
        itemStyle: {
          borderRadius: 6,
          borderColor: '#fff',
          borderWidth: 2,
        },
      },
    ],
    color: ['#67c23a', '#909399', '#f56c6c'],
  }
})

const fetchData = async () => {
  try {
    await statsStore.fetchDashboard()
  } catch (error) {
    console.error('Failed to load dashboard:', error)
  } finally {
    loading.value = false
  }
}

const toggleAutoRefresh = () => {
  autoRefresh.value = !autoRefresh.value
  if (autoRefresh.value) {
    refreshInterval = setInterval(fetchData, 30000)
  } else if (refreshInterval) {
    clearInterval(refreshInterval)
    refreshInterval = null
  }
}

const exportData = () => {
  window.open('/api/stats/export?type=dashboard', '_blank')
}

onMounted(() => {
  fetchData()
})

onUnmounted(() => {
  if (refreshInterval) {
    clearInterval(refreshInterval)
  }
})
</script>

<template>
  <div class="dashboard">
    <div class="page-header">
      <div class="header-left">
        <h2 class="page-title">仪表盘</h2>
        <span class="page-desc">系统运营数据概览</span>
      </div>
      <div class="header-actions">
        <el-button :type="autoRefresh ? 'success' : 'default'" @click="toggleAutoRefresh">
          <el-icon><Refresh /></el-icon>
          {{ autoRefresh ? '实时刷新中' : '自动刷新' }}
        </el-button>
        <el-button @click="fetchData" :loading="loading">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button type="primary" @click="exportData">
          <el-icon><Download /></el-icon>
          导出
        </el-button>
      </div>
    </div>

    <el-row :gutter="20" class="stats-row">
      <el-col :span="6">
        <el-card class="stat-card" shadow="hover">
          <div class="stat-icon accounts">
            <el-icon size="24"><User /></el-icon>
          </div>
          <el-statistic title="总账号数" :value="stats.totalAccounts">
            <template #suffix>
              <span class="stat-unit">个</span>
            </template>
          </el-statistic>
        </el-card>
      </el-col>

      <el-col :span="6">
        <el-card class="stat-card" shadow="hover">
          <div class="stat-icon online">
            <el-icon size="24"><CircleCheck /></el-icon>
          </div>
          <el-statistic title="在线账号" :value="stats.onlineAccounts">
            <template #suffix>
              <span class="stat-unit">个</span>
            </template>
          </el-statistic>
        </el-card>
      </el-col>

      <el-col :span="6">
        <el-card class="stat-card" shadow="hover">
          <div class="stat-icon groups">
            <el-icon size="24"><ChatDotRound /></el-icon>
          </div>
          <el-statistic title="群组数量" :value="stats.totalGroups">
            <template #suffix>
              <span class="stat-unit">个</span>
            </template>
          </el-statistic>
        </el-card>
      </el-col>

      <el-col :span="6">
        <el-card class="stat-card" shadow="hover">
          <div class="stat-icon users">
            <el-icon size="24"><UserFilled /></el-icon>
          </div>
          <el-statistic title="总用户数" :value="stats.totalUsers">
            <template #suffix>
              <span class="stat-unit">人</span>
            </template>
          </el-statistic>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="stats-row secondary">
      <el-col :span="8">
        <el-card class="stat-card small" shadow="hover">
          <el-statistic title="今日注册" :value="stats.dailyRegistered">
            <template #suffix>
              <span class="stat-unit">人</span>
            </template>
          </el-statistic>
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card class="stat-card small" shadow="hover">
          <el-statistic title="转化率" :value="stats.conversionRate" suffix="%">
            <template #prefix>
              <span class="stat-prefix">≈</span>
            </template>
          </el-statistic>
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card class="stat-card small" shadow="hover">
          <el-statistic title="活跃用户" :value="stats.totalUsers" suffix="人">
          </el-statistic>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>注册与转化趋势</span>
              <span class="card-tip">近7天数据</span>
            </div>
          </template>
          <ECharts
            v-if="statsStore.dashboardStats?.weeklyTrend?.length"
            :option="trendChartOption"
            height="320px"
          />
          <el-empty v-else description="暂无数据" />
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>账号状态分布</span>
            </div>
          </template>
          <ECharts
            v-if="statsStore.dashboardStats?.accountDistribution?.length"
            :option="distributionChartOption"
            height="320px"
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
              <span>热门群组 TOP 10</span>
            </div>
          </template>
          <el-table :data="statsStore.dashboardStats?.topGroups || []" stripe>
            <el-table-column type="index" label="排名" width="80" align="center" />
            <el-table-column prop="title" label="群组名称" min-width="200" />
            <el-table-column prop="memberCount" label="成员数" width="150" align="right">
              <template #default="{ row }">
                {{ row.memberCount.toLocaleString() }} 人
              </template>
            </el-table-column>
            <el-table-column label="活跃度" width="200">
              <template #default="{ row }">
                <el-progress
                  :percentage="Math.min((row.memberCount / (statsStore.dashboardStats?.topGroups?.[0]?.memberCount || 1)) * 100, 100)"
                  :stroke-width="8"
                />
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script lang="ts">
import { User, CircleCheck, ChatDotRound, UserFilled } from '@element-plus/icons-vue'
export default {
  components: { User, CircleCheck, ChatDotRound, UserFilled },
}
</script>

<style scoped lang="scss">
.dashboard {
  padding: 0;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
}

.header-left {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
  color: #303133;
}

.page-desc {
  font-size: 14px;
  color: #909399;
}

.header-actions {
  display: flex;
  gap: 12px;
}

.stats-row {
  margin-bottom: 20px;

  &.secondary {
    :deep(.el-card) {
      height: 100px;
    }
  }
}

.stat-card {
  position: relative;
  overflow: hidden;

  :deep(.el-statistic__head) {
    font-size: 14px;
    color: #909399;
  }

  :deep(.el-statistic__content) {
    font-size: 28px;
    font-weight: 600;
  }
}

.stat-icon {
  position: absolute;
  top: 16px;
  right: 16px;
  width: 48px;
  height: 48px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;

  &.accounts {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  }

  &.online {
    background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
  }

  &.groups {
    background: linear-gradient(135deg, #fc4a1a 0%, #f7b733 100%);
  }

  &.users {
    background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
  }
}

.stat-unit {
  font-size: 14px;
  color: #909399;
  margin-left: 4px;
}

.stat-prefix {
  font-size: 16px;
  color: #67c23a;
}

.charts-row {
  margin-bottom: 20px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-tip {
  font-size: 12px;
  color: #909399;
}
</style>
