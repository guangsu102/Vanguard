import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { ElMessage } from 'element-plus'

const mocks = vi.hoisted(() => ({
  role: 'admin',
  settings: {
    ownedGroupAiPersona: {
      enabled: true,
      revision: 3,
      updatedAt: '2026-09-10T10:00:00Z',
      updatedBy: 2,
      staticEnabled: false,
      effectiveEnabled: false,
    },
  },
  updateFeature: vi.fn(),
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ userInfo: { get role() { return mocks.role } } }),
}))

vi.mock('@/stores/settings', () => ({
  useSettingsStore: () => ({
    settings: mocks.settings,
    systemInfo: null,
    logs: [],
    logTotal: 0,
    page: 1,
    pageSize: 20,
    fetchSettings: vi.fn().mockResolvedValue(mocks.settings),
    fetchSystemInfo: vi.fn().mockResolvedValue({}),
    fetchLogs: vi.fn().mockResolvedValue([]),
    setPage: vi.fn(),
    setPageSize: vi.fn(),
    updateSettings: vi.fn().mockResolvedValue({}),
    updateOwnedGroupAiPersonaFeature: mocks.updateFeature,
    clearLogs: vi.fn().mockResolvedValue({}),
    backupDatabase: vi.fn().mockResolvedValue({ filename: 'backup.sql' }),
  }),
}))

import Settings from './Settings.vue'

const mountSettings = () => mount(Settings, {
  global: {
    stubs: {
      'el-card': { template: '<div><slot /></div>' },
      'el-descriptions': { template: '<div><slot /></div>' },
      'el-descriptions-item': {
        props: ['label'],
        template: '<div>{{ label }}<slot /></div>',
      },
      'el-divider': {
        props: ['contentPosition'],
        template: '<div><hr /><slot /></div>',
      },
      'el-form': { template: '<form><slot /></form>' },
      'el-form-item': { template: '<div><slot /></div>' },
      'el-icon': { template: '<span><slot /></span>' },
      'el-input': { template: '<input />' },
      'el-switch': { template: '<button data-testid="persona-switch" />' },
      'el-button': {
        inheritAttrs: false,
        template: '<button v-bind="$attrs"><slot /></button>',
      },
      'el-table': { template: '<table><slot /></table>' },
      'el-table-column': { template: '<td />' },
      'el-tabs': { template: '<div><slot /></div>' },
      'el-tab-pane': { template: '<div><slot /></div>' },
      'el-tag': { template: '<span><slot /></span>' },
      'el-alert': {
        props: ['title'],
        template: '<div>{{ title }}</div>',
      },
      'el-pagination': true,
    },
  },
})

describe('Settings Persona feature section', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.role = 'admin'
    mocks.updateFeature.mockResolvedValue(mocks.settings.ownedGroupAiPersona)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows static, runtime and effective states with the restart warning', async () => {
    const wrapper = mountSettings()
    await flushPromises()

    expect(wrapper.text()).toContain('静态开关')
    expect(wrapper.text()).toContain('运行时开关')
    expect(wrapper.text()).toContain('最终生效')
    expect(wrapper.text()).toContain('需启用环境开关并重启后端才会生效')
    expect(wrapper.text()).toContain('保存 Persona 开关')
  })

  it('renders a read-only runtime state without Persona save controls for non-admins', async () => {
    mocks.role = 'auditor'
    const wrapper = mountSettings()
    await flushPromises()

    expect(wrapper.text()).toContain('运行时开关')
    expect(wrapper.text()).not.toContain('保存 Persona 开关')
  })

  it('uses the fixed conflict prompt and leaves retry to the admin', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockImplementation(() => undefined as never)
    mocks.updateFeature.mockRejectedValue({
      response: {
        status: 409,
        data: { error: { code: 'PERSONA_FEATURE_REVISION_CONFLICT' } },
      },
    })
    const wrapper = mountSettings()
    await flushPromises()

    await wrapper.get('[data-testid="save-persona-feature"]').trigger('click')
    await flushPromises()

    expect(mocks.updateFeature).toHaveBeenCalledTimes(1)
    expect(warning).toHaveBeenCalledWith('设置已被其他管理员修改，请确认后重试')
    expect(wrapper.text()).toContain('确认并重试')
  })

  it.each([
    [
      { status: 403, code: 'ADMIN_REQUIRED' },
      '仅管理员可以修改 Persona 功能开关',
    ],
    [
      { status: 503, code: 'PERSONA_FEATURE_SETTING_NOT_INITIALIZED' },
      'Persona 运行时设置尚未初始化，请先完成数据库迁移',
    ],
  ])('shows a fixed message for feature switch failures', async (failure, message) => {
    const notify = vi.spyOn(ElMessage, 'error').mockImplementation(() => undefined as never)
    mocks.updateFeature.mockRejectedValue({
      response: {
        status: failure.status,
        data: { error: { code: failure.code } },
      },
    })
    const wrapper = mountSettings()
    await flushPromises()

    await wrapper.get('[data-testid="save-persona-feature"]').trigger('click')
    await flushPromises()

    expect(notify).toHaveBeenCalledWith(message)
  })
})
