import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Proxies from './Proxies.vue'

const fetchList = vi.fn().mockResolvedValue([])
const create = vi.fn().mockResolvedValue({})
const update = vi.fn().mockResolvedValue({})
const remove = vi.fn().mockResolvedValue({})
const testLatency = vi.fn().mockResolvedValue({ latency: 20 })
const refreshStatus = vi.fn().mockResolvedValue({})
const setPage = vi.fn()
const setPageSize = vi.fn()

vi.mock('@/stores/proxy', () => ({
  useProxyStore: () => ({
    list: [
      {
        id: 1,
        address: '127.0.0.1',
        port: 8080,
        protocol: 'http',
        latency: 45,
        status: 'active',
        bindAccountPhone: '10086',
        lastCheckedAt: '2026-05-24T10:00:00Z',
        createdAt: '2026-05-20T10:00:00Z',
      },
    ],
    total: 1,
    page: 1,
    pageSize: 20,
    fetchList,
    create,
    update,
    remove,
    testLatency,
    refreshStatus,
    setPage,
    setPageSize,
  }),
}))

vi.mock('@/components/TableCard.vue', () => ({
  default: {
    props: ['data'],
    template: '<div><slot /><div v-for="row in data" :key="row.id">{{ row.address }}:{{ row.port }}</div></div>',
  },
}))
vi.mock('@/components/SearchBar.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/FormDrawer.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/StatusTag.vue', () => ({ default: { template: '<span>status</span>' } }))

const stubs = {
  'el-button': { template: '<button><slot /></button>' },
  'el-icon': { template: '<span><slot /></span>' },
  'el-tag': { template: '<span><slot /></span>' },
  'el-progress': { template: '<div />' },
}

describe('Proxies view', () => {
  beforeEach(() => {
    fetchList.mockClear()
    update.mockClear()
    refreshStatus.mockClear()
    testLatency.mockClear()
  })

  it('renders page and row', () => {
    const wrapper = mount(Proxies, { global: { stubs } })
    expect(wrapper.text()).toContain('代理管理')
    expect(wrapper.text()).toContain('127.0.0.1')
  })

  it('loads proxies on mount', () => {
    mount(Proxies, { global: { stubs } })
    expect(fetchList).toHaveBeenCalledTimes(1)
  })

  it('calls refresh and test actions', async () => {
    const wrapper = mount(Proxies, { global: { stubs } })
    const vm = wrapper.vm as any

    await vm.handleRefreshStatus()
    await vm.handleTest({ id: 1 })

    expect(refreshStatus).toHaveBeenCalledTimes(1)
    expect(testLatency).toHaveBeenCalledWith(1)
  })

  it('opens add drawer and resets form data', () => {
    const wrapper = mount(Proxies, { global: { stubs } })
    const vm = wrapper.vm as any

    vm.openAddDrawer()

    expect(vm.drawerVisible).toBe(true)
    expect(vm.editingId).toBeNull()
    expect(vm.formData.address).toBe('')
    expect(vm.formData.port).toBe(8080)
  })

  it('opens edit drawer with row data', () => {
    const wrapper = mount(Proxies, { global: { stubs } })
    const vm = wrapper.vm as any

    vm.openEditDrawer({
      id: 3,
      address: '10.0.0.1',
      port: 1080,
      protocol: 'socks5',
      username: 'user',
    })

    expect(vm.drawerVisible).toBe(true)
    expect(vm.editingId).toBe(3)
    expect(vm.formData.address).toBe('10.0.0.1')
    expect(vm.formData.protocol).toBe('socks5')
  })

  it('does not overwrite an existing password with a blank edit value', async () => {
    const wrapper = mount(Proxies, { global: { stubs } })
    const vm = wrapper.vm as any

    vm.openEditDrawer({
      id: 3,
      address: '10.0.0.1',
      port: 1080,
      protocol: 'socks5',
      username: 'user',
      country: 'US',
    })

    await vm.handleSubmit()

    expect(update).toHaveBeenCalledWith(
      3,
      expect.not.objectContaining({ password: '' }),
    )
  })

  it('sends a replacement password when one is entered', async () => {
    const wrapper = mount(Proxies, { global: { stubs } })
    const vm = wrapper.vm as any

    vm.openEditDrawer({
      id: 3,
      address: '10.0.0.1',
      port: 1080,
      protocol: 'socks5',
      country: 'US',
    })
    vm.formData.password = 'replacement-secret'

    await vm.handleSubmit()

    expect(update).toHaveBeenCalledWith(
      3,
      expect.objectContaining({ password: 'replacement-secret' }),
    )
  })
})
