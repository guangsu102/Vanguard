import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import FormDrawer from './FormDrawer.vue'

describe('FormDrawer', () => {
  it('renders drawer title and footer', () => {
    const wrapper = mount(FormDrawer, {
      props: {
        visible: true,
        title: '新增内容',
        fields: [{ prop: 'name', label: '名称', type: 'input' }],
        modelValue: { name: '' },
      },
      global: {
        stubs: {
          'el-drawer': { template: '<div><slot name="header" /><slot /><slot name="footer" /></div>' },
          'el-form': { template: '<form><slot /></form>' },
          'el-form-item': { template: '<div><slot /></div>' },
          'el-input': { template: '<input />' },
          'el-select': { template: '<select><slot /></select>' },
          'el-option': { template: '<option />' },
          'el-input-number': { template: '<input />' },
          'el-switch': { template: '<input type="checkbox" />' },
          'el-date-picker': { template: '<input />' },
          'el-button': { template: '<button><slot /></button>' },
          'el-icon': { template: '<span><slot /></span>' },
        },
      },
    })

    expect(wrapper.text()).toContain('新增内容')
    expect(wrapper.text()).toContain('取消')
    expect(wrapper.text()).toContain('确定')
  })
})
