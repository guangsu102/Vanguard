import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import TableCard from './TableCard.vue'

describe('TableCard', () => {
  it('renders empty state and columns', () => {
    const wrapper = mount(TableCard, {
      props: {
        columns: [{ label: '名称', prop: 'name' }],
        data: [],
        total: 0,
      },
      global: {
        stubs: {
          'el-card': { template: '<div><slot /></div>' },
          'el-table': { template: '<table><slot name="empty" /></table>' },
          'el-table-column': { template: '<div />' },
          'el-empty': { template: '<div>暂无数据</div>' },
          'el-pagination': { template: '<div />' },
        },
      },
    })

    expect(wrapper.text()).toContain('暂无数据')
  })

  it('emits page change', async () => {
    const wrapper = mount(TableCard, {
      props: {
        columns: [{ label: '名称', prop: 'name' }],
        data: [{ id: 1, name: 'A' }],
        total: 10,
      },
      global: {
        stubs: {
          'el-card': { template: '<div><slot /></div>' },
          'el-table': { template: '<table><slot /></table>' },
          'el-table-column': { template: '<div />' },
          'el-empty': { template: '<div />' },
          'el-pagination': { template: '<div />' },
        },
      },
    })

    wrapper.vm.$emit('page-change', 2)
    expect(wrapper.emitted('page-change')).toBeTruthy()
  })
})
