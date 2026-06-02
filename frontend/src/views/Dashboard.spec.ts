import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Dashboard from './Dashboard.vue'

const fetchDashboard = vi.fn().mockResolvedValue({})
const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)

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
    fetchDashboard,
  }),
}))

vi.mock('@/components/ECharts.vue', () => ({
  default: {
    name: 'ECharts',
    template: '<div class="echarts-stub"></div>',
  },
}))

describe('Dashboard view', () => {
  beforeEach(() => {
    fetchDashboard.mockClear()
    openSpy.mockClear()
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

    expect(fetchDashboard).toHaveBeenCalledTimes(1)
  })

  it('opens export URL when clicking export', () => {
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
    vm.exportData()
    expect(openSpy).toHaveBeenCalledWith('/api/stats/export?type=dashboard', '_blank')
  })
})
