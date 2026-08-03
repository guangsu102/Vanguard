import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Stats from './Stats.vue'

vi.mock('@/stores/stats', () => ({
  useStatsStore: () => ({
    overview: {
      totalUsers: 30,
      todayRegistered: 5,
      todayActive: 12,
      weeklyGrowth: 8,
    },
    trendData: [],
    funnelData: [],
    sourceData: [],
    fetchTrend: vi.fn().mockResolvedValue([]),
    fetchFunnel: vi.fn().mockResolvedValue([]),
    fetchSources: vi.fn().mockResolvedValue([]),
    fetchOverview: vi.fn().mockResolvedValue({}),
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
          'el-tag': { template: '<span><slot /></span>' },
          'el-progress': { template: '<div />' },
        },
      },
    })

    expect(wrapper.text()).toContain('数据统计')
  })
})
