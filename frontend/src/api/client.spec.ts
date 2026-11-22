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

  it('reads the nested stage-two error envelope', () => {
    expect(
      getApiErrorMessage({
        error: {
          code: 'POLICY_REVISION_CONFLICT',
          message: '策略已被其他管理员修改',
          details: { revision: 3 },
          retryable: false,
        },
        correlation_id: 'msg-conflict',
      }),
    ).toBe('策略已被其他管理员修改')
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

  it('explains recovery failures without exposing arbitrary server details', () => {
    expect(getApiErrorMessage({ detail: { reason: 'session_reauthentication_required', status: 'error' } }))
      .toBe('登录授权已失效，请重新登录绑定')
    expect(getApiErrorMessage({ detail: { reason: 'read_budget_wait', status: 'error' } }))
      .toBe('正在等待读取预算，请到期后重试')
    expect(getApiErrorMessage({ detail: { reason: 'identity_mismatch', status: 'error' } }))
      .toBe('会话手机号与账号不一致，请重新绑定正确账号')
    expect(getApiErrorMessage({ detail: { reason: 'private raw credential value' } })).toBe('Request failed')
  })
})
