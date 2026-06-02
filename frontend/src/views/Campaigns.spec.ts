import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Campaigns from './Campaigns.vue'

const fetchList = vi.fn().mockResolvedValue([])
const create = vi.fn().mockResolvedValue({})
const update = vi.fn().mockResolvedValue({})
const remove = vi.fn().mockResolvedValue({})
const getById = vi.fn().mockResolvedValue({})
const toggle = vi.fn().mockResolvedValue({ enabled: true })
const fetchStats = vi.fn().mockResolvedValue({})
const setPage = vi.fn()
const setPageSize = vi.fn()

vi.mock('vue-router', () => ({
  useRoute: () => ({
    query: {},
  }),
}))

vi.mock('@/stores/campaign', () => ({
  useCampaignStore: () => ({
    list: [
      {
        id: 1,
        name: 'Spring Promo',
        campaign_type: 'discount',
        campaign_scope: 'global',
        trigger_timing: 'after_register',
        trigger_event: '',
        validity_hours: 168,
        enabled: true,
        created_at: '2026-05-20T10:00:00Z',
      },
    ],
    total: 1,
    page: 1,
    pageSize: 20,
    currentCampaign: {
      id: 1,
      name: 'Spring Promo',
      campaign_type: 'discount',
      campaign_scope: 'global',
      trigger_timing: 'after_register',
      trigger_event: '',
      validity_hours: 168,
      enabled: true,
    },
    currentStats: {
      total_tracked: 10,
      registered: 8,
      converted: 6,
      conversion_rate: 25,
    },
    fetchList,
    create,
    update,
    remove,
    getById,
    toggle,
    fetchStats,
    setPage,
    setPageSize,
  }),
}))

vi.mock('@/api/guardian', () => ({
  guardianApi: {
    listManagedGroups: vi.fn().mockResolvedValue({
      data: {
        data: [
          {
            id: 1,
            telegram_group_id: 10001,
            title: 'Managed A',
            username: 'managed_a',
            bot_account_id: 99,
          },
        ],
      },
    }),
    listBots: vi.fn().mockResolvedValue({
      data: {
        data: [
          {
            id: 1,
            account_id: 99,
            identifier: '@guardian_a',
            display_name: 'Guardian A',
          },
        ],
      },
    }),
  },
}))

vi.mock('@/components/TableCard.vue', () => ({
  default: {
    props: ['data'],
    template: '<div><slot /><div v-for="row in data" :key="row.id">{{ row.name }}</div></div>',
  },
}))
vi.mock('@/components/SearchBar.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/FormDrawer.vue', () => ({ default: { template: '<div />' } }))

const stubs = {
  'el-button': { template: '<button><slot /></button>' },
  'el-icon': { template: '<span><slot /></span>' },
  'el-tag': { template: '<span><slot /></span>' },
  'el-tabs': { template: '<div><slot /></div>' },
  'el-tab-pane': { template: '<div><slot /></div>' },
  'el-dialog': { template: '<div><slot /></div>' },
  'el-form-item': { template: '<div><slot /></div>' },
  'el-input': { template: '<input />' },
}

describe('Campaigns view', () => {
  beforeEach(() => {
    fetchList.mockClear()
    toggle.mockClear()
    getById.mockClear()
    fetchStats.mockClear()
  })

  it('renders page and campaign row', () => {
    const wrapper = mount(Campaigns, { global: { stubs } })

    expect(wrapper.text()).toContain('活动管理')
    expect(wrapper.text()).toContain('Spring Promo')
  })

  it('loads campaigns on mount', () => {
    mount(Campaigns, { global: { stubs } })
    expect(fetchList).toHaveBeenCalledTimes(1)
    expect(fetchList).toHaveBeenCalledWith({ campaign_scope: 'global' })
  })

  it('calls toggle action', async () => {
    const wrapper = mount(Campaigns, { global: { stubs } })
    const vm = wrapper.vm as any

    await vm.handleToggle({ id: 1, enabled: false })

    expect(toggle).toHaveBeenCalledWith(1)
  })

  it('opens details and fetches stats', async () => {
    const wrapper = mount(Campaigns, { global: { stubs } })
    const vm = wrapper.vm as any

    await vm.handleViewDetail({ id: 1 })

    expect(getById).toHaveBeenCalledWith(1)
    expect(fetchStats).toHaveBeenCalledWith(1)
  })

  it('opens add drawer and initializes default form values', async () => {
    const wrapper = mount(Campaigns, { global: { stubs } })
    const vm = wrapper.vm as any

    vm.openAddDrawer()

    expect(vm.drawerVisible).toBe(true)
    expect(vm.editingId).toBeNull()
    expect(vm.formData.name).toBe('')
    expect(vm.formData.campaign_type).toBe('discount')
    expect(vm.formData.campaign_scope).toBe('global')
  })

  it('uses select options for managed-group trigger events', () => {
    const wrapper = mount(Campaigns, { global: { stubs } })
    const vm = wrapper.vm as any

    vm.formData.campaign_scope = 'managed_group'
    const triggerField = vm.drawerFields.find((field: any) => field.prop === 'trigger_event')

    expect(triggerField?.type).toBe('select')
    expect(triggerField?.options?.map((item: any) => item.value)).toEqual([
      'user_joined',
      'verification_passed',
      'new_member_delay',
      'scheduled',
      'manual_broadcast',
      'periodic',
    ])
  })

  it('uses enum select options for global trigger timing', () => {
    const wrapper = mount(Campaigns, { global: { stubs } })
    const vm = wrapper.vm as any

    vm.formData.campaign_scope = 'global'
    const triggerField = vm.drawerFields.find((field: any) => field.prop === 'trigger_timing')

    expect(triggerField?.type).toBe('select')
    expect(triggerField?.options?.map((item: any) => item.value)).toEqual([
      'after_register',
      'immediate',
      'delayed',
      'scheduled',
      'manual',
      'periodic',
    ])
  })

  it('opens edit drawer with row data', async () => {
    const wrapper = mount(Campaigns, { global: { stubs } })
    const vm = wrapper.vm as any
    const row = {
      id: 2,
      name: 'Summer Promo',
      campaign_type: 'discount',
      campaign_scope: 'managed_group',
      trigger_timing: 'immediate',
      trigger_event: 'user_joined',
      validity_hours: 72,
      enabled: false,
    }

    vm.openEditDrawer(row)

    expect(vm.drawerVisible).toBe(true)
    expect(vm.editingId).toBe(2)
    expect(vm.formData.name).toBe('Summer Promo')
    expect(vm.formData.campaign_type).toBe('discount')
    expect(vm.formData.campaign_scope).toBe('managed_group')
  })

  it('omits managed-group fields when building a global payload', () => {
    const wrapper = mount(Campaigns, { global: { stubs } })
    const vm = wrapper.vm as any

    vm.formData.campaign_scope = 'global'
    vm.formData.target_group_ids = [10001, 10002]
    vm.formData.bot_account_id = 99

    const payload = vm.buildPayload()

    expect(payload.target_group_ids).toBeUndefined()
    expect(payload.bot_account_id).toBeUndefined()
  })

  it('formats managed-group trigger event labels for display', () => {
    const wrapper = mount(Campaigns, { global: { stubs } })
    const vm = wrapper.vm as any

    expect(vm.formatTriggerEvent('user_joined')).toBe('用户入群')
    expect(vm.formatTriggerEvent('manual_broadcast')).toBe('手动广播')
    expect(vm.formatTriggerEvent()).toBe('-')
  })

  it('builds delayed managed-group broadcast policy from structured fields', () => {
    const wrapper = mount(Campaigns, { global: { stubs } })
    const vm = wrapper.vm as any

    vm.formData.campaign_scope = 'managed_group'
    vm.formData.trigger_event = 'new_member_delay'
    vm.formData.delay_minutes = 15
    vm.formData.target_group_ids = [10001]
    vm.formData.bot_account_id = 99

    const payload = vm.buildPayload()

    expect(payload.trigger_timing).toBe('delayed')
    expect(payload.broadcast_policy_json).toEqual({ delay_minutes: 15 })
  })

  it('builds scheduled managed-group broadcast policy from schedule text', () => {
    const wrapper = mount(Campaigns, { global: { stubs } })
    const vm = wrapper.vm as any

    vm.formData.campaign_scope = 'managed_group'
    vm.formData.trigger_event = 'scheduled'
    vm.formData.schedule_times_text = '09:00, 14:30\n21:00'
    vm.formData.target_group_ids = [10001]
    vm.formData.bot_account_id = 99

    const payload = vm.buildPayload()

    expect(payload.trigger_timing).toBe('scheduled')
    expect(payload.broadcast_policy_json).toEqual({ schedule_times: ['09:00', '14:30', '21:00'] })
  })
})
