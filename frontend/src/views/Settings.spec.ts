import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import Settings from './Settings.vue'

describe('Settings view', () => {
  it('renders settings page title', () => {
    const wrapper = mount(Settings, {
      global: {
        stubs: {
          'el-card': { template: '<div><slot /></div>' },
          'el-form': { template: '<form><slot /></form>' },
          'el-form-item': { template: '<div><slot /></div>' },
          'el-input': { template: '<input />' },
          'el-switch': { template: '<button />' },
          'el-button': { template: '<button><slot /></button>' },
          'el-tabs': { template: '<div><slot /></div>' },
          'el-tab-pane': { template: '<div><slot /></div>' },
          'el-alert': { template: '<div><slot /></div>' },
        },
      },
    })

    expect(wrapper.text()).toContain('系统设置')
  })
})
