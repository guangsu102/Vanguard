import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Settings from './Settings.vue'

vi.mock('@/stores/settings', () => ({
  useSettingsStore: () => ({
    settings: {},
    systemInfo: null,
    logs: [],
    logTotal: 0,
    page: 1,
    pageSize: 20,
    fetchSettings: vi.fn().mockResolvedValue({}),
    fetchSystemInfo: vi.fn().mockResolvedValue({}),
    fetchLogs: vi.fn().mockResolvedValue([]),
    setPage: vi.fn(),
    setPageSize: vi.fn(),
    updateSettings: vi.fn().mockResolvedValue({}),
    clearLogs: vi.fn().mockResolvedValue({}),
    backupDatabase: vi.fn().mockResolvedValue({ filename: 'backup.sql' }),
  }),
}))

describe('Settings view', () => {
  it('renders settings page title', () => {
    const wrapper = mount(Settings, {
      global: {
        stubs: {
          'el-card': { template: '<div><slot /></div>' },
          'el-descriptions': { template: '<div><slot /></div>' },
          'el-descriptions-item': { template: '<div><slot /></div>' },
          'el-divider': { template: '<hr />' },
          'el-form': { template: '<form><slot /></form>' },
          'el-form-item': { template: '<div><slot /></div>' },
          'el-icon': { template: '<span><slot /></span>' },
          'el-input': { template: '<input />' },
          'el-input-number': { template: '<input />' },
          'el-option': { template: '<option />' },
          'el-select': { template: '<select><slot /></select>' },
          'el-switch': { template: '<button />' },
          'el-button': { template: '<button><slot /></button>' },
          'el-table': { template: '<table><slot /></table>' },
          'el-table-column': { template: '<td />' },
          'el-tabs': { template: '<div><slot /></div>' },
          'el-tab-pane': { template: '<div><slot /></div>' },
          'el-tag': { template: '<span><slot /></span>' },
          'el-alert': { template: '<div><slot /></div>' },
          'el-pagination': true,
        },
      },
    })

    expect(wrapper.text()).toContain('系统设置')
  })
})
