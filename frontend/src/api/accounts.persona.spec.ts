import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({
  get: vi.fn(),
}))

vi.mock('./client', () => ({ default: client }))

import { accountsApi } from './accounts'

describe('accounts API Persona summary', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('builds the list summary from the four flat AccountResponse fields', async () => {
    client.get.mockResolvedValue({
      data: {
        data: [
          {
            id: 17,
            identifier: 'promoter-17',
            account_type: 'promoter',
            operation_mode: 'growth',
            persona_configured: true,
            persona_name: '技术型群友',
            persona_revision: 3,
            persona_applicable: true,
            persona: {
              configured: false,
              name: '不得读取的嵌套值',
              revision: 99,
              applicable: false,
              effective_enabled: false,
            },
          },
        ],
        total: 1,
        next_cursor: null,
        has_more: false,
      },
    })

    const result = await accountsApi.list({ account_type: 'promoter' })

    expect(result.list[0]?.persona).toEqual({
      account_id: 17,
      configured: true,
      name: '技术型群友',
      revision: 3,
      applicable: true,
    })
    expect(result.list[0]?.persona).not.toHaveProperty('effective_enabled')
  })

  it('does not fall back to a legacy nested persona object', async () => {
    client.get.mockResolvedValue({
      data: {
        data: [
          {
            id: 18,
            identifier: 'promoter-18',
            persona: {
              configured: true,
              name: '旧嵌套结构',
              revision: 8,
              applicable: true,
              effective_enabled: true,
            },
          },
        ],
        total: 1,
      },
    })

    const result = await accountsApi.list()

    expect(result.list[0]?.persona).toBeUndefined()
  })
})
