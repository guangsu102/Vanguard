import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import GrowthLogs from './GrowthLogs.vue'

const { getAutoJoinAttempts, getAutoJoinVerificationLogs, getDeliveryLogs, getCampaigns, listAccounts } = vi.hoisted(
  () => ({
    getAutoJoinAttempts: vi.fn().mockResolvedValue({ data: { data: [] } }),
    getAutoJoinVerificationLogs: vi.fn().mockResolvedValue({ data: { data: [] } }),
    getDeliveryLogs: vi.fn().mockResolvedValue({
      data: {
        data: [
          {
            id: 1,
            account_id: 7,
            campaign_id: 9,
            campaign_name: '定向计划',
            group_title: '目标群',
            status: 'sent',
            created_at: '2026-08-25T01:00:00Z',
          },
        ],
        total: 1,
      },
    }),
    getCampaigns: vi.fn().mockResolvedValue({ data: { data: [{ id: 9, name: '定向计划' }] } }),
    listAccounts: vi.fn().mockResolvedValue({
      list: [{ id: 7, account_type: 'promoter', display_name: '专用广告号' }],
    }),
  }),
)

vi.mock('vue-router', () => ({
  useRoute: () => ({
    query: {
      tab: 'delivery',
      account_id: '7',
      campaign_id: '9',
    },
  }),
}))

vi.mock('@/api/accounts', () => ({
  accountsApi: {
    list: listAccounts,
  },
}))

vi.mock('@/api/automation', () => ({
  automationApi: {
    getAutoJoinAttempts,
    getAutoJoinVerificationLogs,
    getDeliveryLogs,
    getCampaigns,
  },
}))

vi.mock('@/components/ClientListPagination.vue', () => ({
  default: {
    name: 'ClientListPagination',
    template: '<div />',
  },
}))

const globalConfig = {
  stubs: {
    'el-button': { template: '<button><slot /></button>' },
    'el-tabs': { template: '<div><slot /></div>' },
    'el-tab-pane': { template: '<section><slot /></section>' },
    'el-table': { template: '<div><slot /></div>' },
    'el-table-column': { template: '<div />' },
    'el-tag': { template: '<span><slot /></span>' },
    'el-select': { template: '<div><slot /></div>' },
    'el-option': { template: '<div />' },
    'el-date-picker': { template: '<div />' },
    'el-pagination': { template: '<div />' },
  },
  directives: {
    loading: { mounted: () => undefined },
  },
}

describe('GrowthLogs view', () => {
  beforeEach(() => {
    getAutoJoinAttempts.mockClear()
    getAutoJoinVerificationLogs.mockClear()
    getDeliveryLogs.mockClear()
    getCampaigns.mockClear()
    listAccounts.mockClear()
  })

  it('opens delivery logs with account and campaign filters from the route', async () => {
    const wrapper = mount(GrowthLogs, { global: globalConfig })
    await flushPromises()

    const vm = wrapper.vm as any
    expect(vm.activeTab).toBe('delivery')
    expect(getDeliveryLogs).toHaveBeenCalledWith(
      expect.objectContaining({
        account_id: 7,
        campaign_id: 9,
        page: 1,
        page_size: 20,
      }),
    )
    expect(wrapper.text()).toContain('增长日志')
    expect(vm.accountLabel(7)).toBe('专用广告号')
  })

  it('applies and resets delivery filters', async () => {
    const wrapper = mount(GrowthLogs, { global: globalConfig })
    await flushPromises()
    const vm = wrapper.vm as any

    vm.deliveryStatus = 'failed'
    vm.deliveryTimeRange = ['2026-08-24T00:00:00', '2026-08-25T00:00:00']
    await vm.searchDeliveryLogs()

    expect(getDeliveryLogs).toHaveBeenLastCalledWith(
      expect.objectContaining({
        status: 'failed',
        start_at: '2026-08-24T00:00:00',
        end_at: '2026-08-25T00:00:00',
      }),
    )

    await vm.resetDeliveryLogs()
    expect(getDeliveryLogs).toHaveBeenLastCalledWith(
      expect.objectContaining({
        account_id: undefined,
        campaign_id: undefined,
        status: undefined,
      }),
    )
  })
})
