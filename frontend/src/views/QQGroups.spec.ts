import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import QQGroups from './QQGroups.vue'

const qqMocks = vi.hoisted(() => ({
  getConnection: vi.fn(),
  listGroups: vi.fn(),
  syncGroups: vi.fn(),
}))

const wsMocks = vi.hoisted(() => ({
  on: vi.fn(),
  off: vi.fn(),
  subscribe: vi.fn(),
  unsubscribe: vi.fn(),
  isConnected: vi.fn(() => true),
  connect: vi.fn(),
}))

vi.mock('@/api/qq', () => ({
  qqApi: {
    getConnection: qqMocks.getConnection,
    listGroups: qqMocks.listGroups,
    syncGroups: qqMocks.syncGroups,
    createGroup: vi.fn(),
    updateGroup: vi.fn(),
    listMessages: vi.fn(),
    sendNotification: vi.fn(),
    recallMessage: vi.fn(),
  },
}))

vi.mock('@/utils/websocket', () => ({ default: wsMocks }))

describe('QQGroups view', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    qqMocks.getConnection.mockResolvedValue({
      data: {
        data: {
          configured: true,
          enabled: true,
          status: 'online',
          provider: 'napcat_onebot11',
          account_id: '10001',
        },
      },
    })
    qqMocks.listGroups.mockResolvedValue({
      data: {
        data: [],
        total: 0,
      },
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('loads NapCat OneBot connection and group state', async () => {
    const wrapper = mount(QQGroups, {
      global: {
        stubs: {
          ElButton: true,
          ElDialog: true,
          ElDrawer: true,
          ElForm: true,
          ElFormItem: true,
          ElInput: true,
          ElSwitch: true,
          ElTable: true,
          ElTableColumn: true,
          ElTag: { template: '<span><slot /></span>' },
          ElTooltip: true,
        },
        directives: {
          loading: {},
        },
      },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('NapCat QQ 群')
    expect(wrapper.text()).toContain('在线')
    expect(qqMocks.getConnection).toHaveBeenCalledTimes(1)
    expect(qqMocks.listGroups).toHaveBeenCalledWith({ limit: 500 })
    expect(wsMocks.subscribe).toHaveBeenCalledWith('qq:messages')

    wrapper.unmount()
  })
})
