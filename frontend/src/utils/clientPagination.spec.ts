import { nextTick, ref } from 'vue'
import { describe, expect, it } from 'vitest'

import { useClientPagination } from './clientPagination'

describe('useClientPagination', () => {
  it('returns only the rows for the current page', () => {
    const source = ref(Array.from({ length: 25 }, (_, index) => index + 1))
    const pagination = useClientPagination(source, 10)

    pagination.page.value = 2

    expect(pagination.total.value).toBe(25)
    expect(pagination.rows.value).toEqual([11, 12, 13, 14, 15, 16, 17, 18, 19, 20])
  })

  it('returns to the first page when the page size changes', async () => {
    const source = ref(Array.from({ length: 25 }, (_, index) => index + 1))
    const pagination = useClientPagination(source, 10)
    pagination.page.value = 3

    pagination.pageSize.value = 20
    await nextTick()

    expect(pagination.page.value).toBe(1)
  })

  it('clamps the current page when filtered rows shrink', async () => {
    const source = ref(Array.from({ length: 25 }, (_, index) => index + 1))
    const pagination = useClientPagination(source, 10)
    pagination.page.value = 3

    source.value = [1, 2]
    await nextTick()

    expect(pagination.page.value).toBe(1)
    expect(pagination.rows.value).toEqual([1, 2])
  })
})
