import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import SearchBar from './SearchBar.vue'

describe('SearchBar', () => {
  it('renders filter labels', () => {
    const wrapper = mount(SearchBar, {
      props: {
        filters: [
          { type: 'input', key: 'keyword', label: '关键词', placeholder: '请输入关键词' },
          { type: 'select', key: 'status', label: '状态', options: [] },
        ],
      },
      global: {
        stubs: {
          'el-card': { template: '<div><slot /></div>' },
          'el-form': { template: '<form><slot /></form>' },
          'el-form-item': { template: '<div><slot /></div>' },
          'el-input': { template: '<input />' },
          'el-select': { template: '<select><slot /></select>' },
          'el-option': { template: '<option />' },
          'el-date-picker': { template: '<input />' },
          'el-button': { template: '<button><slot /></button>' },
          'el-icon': { template: '<span><slot /></span>' },
        },
      },
    })

    expect(wrapper.text()).toContain('关键词')
    expect(wrapper.text()).toContain('状态')
  })
})
