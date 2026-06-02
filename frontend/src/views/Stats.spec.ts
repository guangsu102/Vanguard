import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Stats from './Stats.vue'

vi.mock('@/stores/stats', () => ({
  useStatsStore: () => ({
    dashboardStats: {
      totalAccounts: 10,
      onlineAccounts: 6,
      totalGroups: 4,
      totalUsers: 30,
      dailyRegistered: 5,
      conversionRate: 12.5,
      weeklyTrend: [],
      accountDistribution: [],
      topGroups: [],
    },
    fetchDashboard: vi.fn().mockResolvedValue({}),
  }),
}))

describe('Stats view', () => {
  it('renders stats page title', () => {
    const wrapper = mount(Stats, {
      global: {
        stubs: {
          'el-card': { template: '<div><slot /></div>' },
          'el-row': { template: '<div><slot /></div>' },
          'el-col': { template: '<div><slot /></div>' },
          'el-statistic': { template: '<div />' },
          'el-empty': { template: '<div />' },
          'el-button': { template: '<button><slot /></button>' },
          'el-icon': { template: '<span><slot /></span>' },
          'el-table': { template: '<table><slot /></table>' },
          'el-table-column': { template: '<div />' },
          'el-progress': { template: '<div />' },
        },
      },
    })

    expect(wrapper.text()).toContain('数据统计')
  })
})
