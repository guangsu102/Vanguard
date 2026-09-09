import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ClientListPagination from './ClientListPagination.vue'

const ElPaginationStub = {
  name: 'ElPagination',
  props: ['currentPage', 'pageSize', 'pageSizes', 'total'],
  emits: ['update:currentPage', 'update:pageSize'],
  template: '<div />',
}

describe('ClientListPagination', () => {
  it('supports configurable options without allowing values above 100', async () => {
    const wrapper = mount(ClientListPagination, {
      props: {
        page: 1,
        pageSize: 20,
        total: 200,
        pageSizes: [20, 50, 100, 200],
      },
      global: {
        stubs: {
          'el-pagination': ElPaginationStub,
        },
      },
    })
    const pagination = wrapper.findComponent(ElPaginationStub)

    expect(pagination.props('pageSizes')).toEqual([20, 50, 100])
    pagination.vm.$emit('update:pageSize', 500)
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('update:pageSize')).toEqual([[100]])
  })
})
