import { describe, expect, it } from 'vitest'

import { getApiErrorMessage } from './client'

describe('getApiErrorMessage', () => {
  it('reads the standard API message', () => {
    expect(getApiErrorMessage({ message: 'Operation failed' })).toBe('Operation failed')
  })

  it('reads FastAPI HTTPException detail', () => {
    expect(getApiErrorMessage({ detail: 'risk_guard_blocked:content_similar_target' })).toBe(
      'risk_guard_blocked:content_similar_target',
    )
  })

  it('joins FastAPI validation messages', () => {
    expect(
      getApiErrorMessage({
        detail: [{ msg: 'Field required' }, { msg: 'Invalid channel URL' }],
      }),
    ).toBe('Field required; Invalid channel URL')
  })

  it('falls back for unknown response bodies', () => {
    expect(getApiErrorMessage({ detail: { reason: 'unknown' } })).toBe('Request failed')
  })
})
