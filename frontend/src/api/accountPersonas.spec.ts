import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
}))

vi.mock('./client', () => ({ default: client }))

import {
  accountPersonasApi,
  createNeutralAccountPersona,
  getAccountPersonaError,
  getAccountPersonaFieldErrors,
  normalizeAccountPersona,
  validateAccountPersona,
  type AccountPersona,
  type AccountPersonaDetail,
} from './accountPersonas'

const persona: AccountPersona = {
  schema_version: 1,
  name: '技术型群友',
  tone: '自然、克制、简短',
  interests: ['网络稳定性'],
  expertise: ['技术排障'],
  reply_length: 'short',
  preferred_topics: ['使用体验'],
  forbidden_topics: ['过度营销'],
  ad_style: 'soft_share',
  catchphrases: [],
  language_style: 'zh_cn',
  system_prompt: '',
}

const detail: AccountPersonaDetail = {
  account_id: 17,
  account_type: 'promoter',
  operation_mode: 'growth',
  persona_applicable: true,
  blocking_reason: null,
  configured: true,
  revision: 3,
  persona_hash: 'a'.repeat(64),
  persona,
  updated_at: '2026-09-10T10:00:00Z',
  updated_by: { id: 2, username: 'admin' },
  feature: {
    static_enabled: true,
    runtime_enabled: true,
    effective_enabled: true,
    runtime_will_apply: true,
  },
  limits: { max_total_bytes: 16384, max_system_prompt_chars: 1000 },
  preview_targets: [],
}

describe('account persona API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('uses the account-scoped full GET and PUT contracts', async () => {
    client.get.mockResolvedValue({ data: { data: detail, correlation_id: 'persona-get' } })
    client.put.mockResolvedValue({
      data: { data: { ...detail, revision: 4 }, correlation_id: 'persona-put' },
    })

    await accountPersonasApi.get(17)
    const saved = await accountPersonasApi.replace(17, {
      expected_revision: 3,
      persona,
    })

    expect(client.get).toHaveBeenCalledWith('/accounts/17/ai-persona')
    expect(client.put).toHaveBeenCalledWith('/accounts/17/ai-persona', {
      expected_revision: 3,
      persona,
    })
    expect(saved.revision).toBe(4)
  })

  it('uses reset, preview request id and audit cursor without key conversion', async () => {
    client.post
      .mockResolvedValueOnce({ data: { data: { ...detail, configured: false, persona: null } } })
      .mockResolvedValueOnce({
        data: {
          data: {
            preview_id: 'preview-1',
            account_id: 17,
            owned_group_asset_id: 25,
            content_category: 'community',
          },
        },
      })
    client.get.mockResolvedValue({ data: { data: { items: [], next_cursor: null } } })

    await accountPersonasApi.reset(17, { expected_revision: 3 })
    await accountPersonasApi.preview(
      17,
      {
        owned_group_asset_id: 25,
        content_category: 'community',
        trigger_type: 'manual',
        topic: '节点稳定性',
      },
      'persona-request-1',
    )
    await accountPersonasApi.listAuditEvents(17, {
      cursor: 'opaque',
      limit: 50,
      event_type: 'account_persona_updated',
    })

    expect(client.post).toHaveBeenNthCalledWith(
      1,
      '/accounts/17/ai-persona/reset',
      { expected_revision: 3 },
    )
    expect(client.post).toHaveBeenNthCalledWith(
      2,
      '/accounts/17/ai-persona/preview',
      expect.not.objectContaining({ sample_context: expect.anything() }),
      { headers: { 'X-Request-ID': 'persona-request-1' } },
    )
    expect(client.get).toHaveBeenCalledWith('/accounts/17/ai-persona/audit-events', {
      params: {
        cursor: 'opaque',
        limit: 50,
        event_type: 'account_persona_updated',
      },
    })
  })

  it('normalizes NFKC strings and reports explicit field boundaries', () => {
    const normalized = normalizeAccountPersona({
      ...persona,
      name: '  技术型群友  ',
      interests: ['节点', '节点', ''],
      preferred_topics: ['配置建议'],
      forbidden_topics: ['配置建议'],
      system_prompt: 'a'.repeat(1001),
    })
    const errors = validateAccountPersona(normalized)

    expect(normalized.name).toBe('技术型群友')
    expect(normalized.interests).toEqual(['节点'])
    expect(createNeutralAccountPersona().language_style).toBe('auto')
    expect(errors.system_prompt).toContain('1000')
    expect(errors.forbidden_topics).toContain('不能重复')
  })

  it('extracts revision codes and safe field locations without relying on messages', () => {
    const error = getAccountPersonaError({
      response: {
        status: 409,
        data: {
          error: {
            code: 'PERSONA_REVISION_CONFLICT',
            message: 'variable server text',
            details: {
              current_revision: 4,
              field_errors: [{ field: 'persona.tone', error_type: 'string_too_long' }],
            },
            retryable: false,
          },
          correlation_id: 'persona-conflict',
        },
      },
    })

    expect(error).toMatchObject({
      code: 'PERSONA_REVISION_CONFLICT',
      status: 409,
      correlation_id: 'persona-conflict',
    })
    expect(getAccountPersonaFieldErrors(error)).toEqual({
      tone: '字段校验失败（string_too_long）',
    })
  })
})
