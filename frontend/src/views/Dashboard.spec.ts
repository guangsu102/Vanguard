import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Dashboard from './Dashboard.vue'

const mocks = vi.hoisted(() => ({
  fetchDashboard: vi.fn().mockResolvedValue({}),
  exportStats: vi.fn().mockResolvedValue({ data: 'metric,value' }),
  downloadBlob: vi.fn(),
}))

vi.mock('@/stores/stats', () => ({
  useStatsStore: () => ({
    dashboardStats: {
      totalAccounts: 12,
      onlineAccounts: 8,
      totalGroups: 5,
      totalUsers: 42,
      dailyRegistered: 7,
      conversionRate: 16.7,
      weeklyTrend: [],
      accountDistribution: [],
      topGroups: [],
    },
    fetchDashboard: mocks.fetchDashboard,
  }),
}))

vi.mock('@/components/ECharts.vue', () => ({
  default: {
    name: 'ECharts',
    template: '<div class="echarts-stub"></div>',
  },
}))

vi.mock('@/api/stats', () => ({
  statsApi: {
    export: mocks.exportStats,
  },
}))

vi.mock('@/utils/download', () => ({
  downloadBlob: mocks.downloadBlob,
}))

describe('Dashboard view', () => {
  beforeEach(() => {
    mocks.fetchDashboard.mockClear()
    mocks.exportStats.mockClear()
    mocks.downloadBlob.mockClear()
  })

  it('renders dashboard summary cards', () => {
    const wrapper = mount(Dashboard, {
      global: {
        stubs: {
          'el-row': { template: '<div><slot /></div>' },
          'el-col': { template: '<div><slot /></div>' },
          'el-card': { template: '<div><slot name="header" /><slot /></div>' },
          'el-statistic': { template: '<div><slot name="prefix" />{{ title }}{{ value }}<slot name="suffix" /></div>', props: ['title', 'value'] },
          'el-button': { template: '<button><slot /></button>' },
          'el-icon': { template: '<span><slot /></span>' },
          'el-empty': { template: '<div>empty</div>' },
          'el-table': { template: '<table><slot /></table>' },
          'el-table-column': { template: '<div />' },
          'el-progress': { template: '<div />' },
        },
      },
    })

    expect(wrapper.text()).toContain('仪表盘')
    expect(wrapper.text()).toContain('总账号数')
  })

  it('loads dashboard data on mount', async () => {
    mount(Dashboard, {
      global: {
        stubs: {
          'el-row': { template: '<div><slot /></div>' },
          'el-col': { template: '<div><slot /></div>' },
          'el-card': { template: '<div><slot name="header" /><slot /></div>' },
          'el-statistic': { template: '<div />' },
          'el-button': { template: '<button><slot /></button>' },
          'el-icon': { template: '<span><slot /></span>' },
          'el-empty': { template: '<div />' },
          'el-table': { template: '<table><slot /></table>' },
          'el-table-column': { template: '<div />' },
          'el-progress': { template: '<div />' },
        },
      },
    })

    expect(mocks.fetchDashboard).toHaveBeenCalledTimes(1)
  })

  it('downloads dashboard export through the API client', async () => {
    const wrapper = mount(Dashboard, {
      global: {
        stubs: {
          'el-row': { template: '<div><slot /></div>' },
          'el-col': { template: '<div><slot /></div>' },
          'el-card': { template: '<div><slot name="header" /><slot /></div>' },
          'el-statistic': { template: '<div />' },
          'el-button': { template: '<button><slot /></button>' },
          'el-icon': { template: '<span><slot /></span>' },
          'el-empty': { template: '<div />' },
          'el-table': { template: '<table><slot /></table>' },
          'el-table-column': { template: '<div />' },
          'el-progress': { template: '<div />' },
        },
      },
    })

    const vm = wrapper.vm as any
    await vm.exportData()
    expect(mocks.exportStats).toHaveBeenCalledWith({ type: 'dashboard' })
    expect(mocks.downloadBlob).toHaveBeenCalledWith('metric,value', 'vanguard-dashboard.csv')
  })
})
