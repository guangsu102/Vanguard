import { beforeEach, describe, expect, it, vi } from 'vitest'

const { get, post, remove } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  remove: vi.fn(),
}))

vi.mock('./client', () => ({
  default: { get, post, delete: remove },
}))

import { resourceSearchApi } from './resourceSearch'

describe('resourceSearchApi', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
    remove.mockReset()
  })

  it('paginates resource-search history', async () => {
    get.mockResolvedValue({ data: { data: [], total: 41 } })

    const result = await resourceSearchApi.listRuns(2, 20)

    expect(get).toHaveBeenCalledWith('/resource-search/runs', {
      params: { page: 2, page_size: 20 },
    })
    expect(result.total).toBe(41)
  })

  it('cancels and deletes a selected run', async () => {
    post.mockResolvedValue({ data: { data: { id: 12, status: 'running' } } })
    remove.mockResolvedValue({ data: {} })

    await resourceSearchApi.cancelRun(12)
    await resourceSearchApi.deleteRun(12)

    expect(post).toHaveBeenCalledWith('/resource-search/runs/12/cancel')
    expect(remove).toHaveBeenCalledWith('/resource-search/runs/12')
  })
})
