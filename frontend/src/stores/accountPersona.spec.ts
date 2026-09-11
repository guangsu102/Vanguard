import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const api = vi.hoisted(() => ({
  get: vi.fn(),
  replace: vi.fn(),
  reset: vi.fn(),
  preview: vi.fn(),
  listAuditEvents: vi.fn(),
}))

vi.mock('@/api/accountPersonas', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/accountPersonas')>()
  return { ...original, accountPersonasApi: api }
})

import type {
  AccountPersona,
  AccountPersonaDetail,
  AccountPersonaPreviewResult,
} from '@/api/accountPersonas'
import { useAccountPersonaStore } from './accountPersona'

const persona: AccountPersona = {
  schema_version: 1,
  name: '技术型群友',
  tone: '自然、克制、简短',
  interests: [],
  expertise: [],
  reply_length: 'short',
  preferred_topics: [],
  forbidden_topics: [],
  ad_style: 'neutral',
  catchphrases: [],
  language_style: 'zh_cn',
  system_prompt: '',
}

const personaDetail = (accountId: number, revision: number): AccountPersonaDetail => ({
  account_id: accountId,
  account_type: 'promoter',
  operation_mode: 'growth',
  persona_applicable: true,
  blocking_reason: null,
  configured: true,
  revision,
  persona_hash: `${accountId}`.padStart(64, '0'),
  persona: { ...persona, name: `Persona ${accountId}` },
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
})

const previewResult: AccountPersonaPreviewResult = {
  preview_id: 'preview-17',
  account_id: 17,
  owned_group_asset_id: 25,
  content_category: 'community',
  persona_source: 'configured',
  persona_revision: 3,
  base_account_revision: 3,
  persona_hash: 'a'.repeat(64),
  prompt_template_version: 'owned-group-persona-v1',
  prompt_hash: 'b'.repeat(64),
  governance_rules_hash: 'c'.repeat(64),
  sample_text: '样例',
  promotion_composition: null,
  effective_constraints: {
    language: 'zh_cn',
    max_chars: 80,
    allowed_topics: [],
    forbidden_topic_count: 0,
    promotion_url_locked: true,
  },
  governance: { allowed: true, reason_code: null },
  feature: { effective_enabled: true, runtime_will_apply: true },
  warnings: [],
  usage: { provider: 'openai', model: 'configured-model', input_tokens: 20, output_tokens: 5 },
}

describe('accountPersona store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('caches full configuration by account and authoritative revision', async () => {
    api.get.mockResolvedValue(personaDetail(17, 3))
    const store = useAccountPersonaStore()

    await store.fetchPersona(17, 3)
    await store.fetchPersona(17, 3)

    expect(api.get).toHaveBeenCalledTimes(1)
    expect(store.hasCachedRevision(17, 3)).toBe(true)
    expect(store.detail?.revision).toBe(3)
  })

  it('invalidates an old cache key when the account summary revision changes', async () => {
    api.get
      .mockResolvedValueOnce(personaDetail(17, 3))
      .mockResolvedValueOnce(personaDetail(17, 4))
    const store = useAccountPersonaStore()

    await store.fetchPersona(17, 3)
    await store.fetchPersona(17, 4)

    expect(api.get).toHaveBeenCalledTimes(2)
    expect(store.hasCachedRevision(17, 3)).toBe(false)
    expect(store.hasCachedRevision(17, 4)).toBe(true)
    expect(store.detail?.revision).toBe(4)
  })

  it('clears account-scoped state and ignores a response from the previous account', async () => {
    let resolveOld: (value: AccountPersonaDetail) => void = () => undefined
    const oldRequest = new Promise<AccountPersonaDetail>((resolve) => {
      resolveOld = resolve
    })
    api.get.mockReturnValueOnce(oldRequest).mockResolvedValueOnce(personaDetail(18, 1))
    const store = useAccountPersonaStore()

    const pending = store.fetchPersona(17, 3)
    await store.fetchPersona(18, 1)
    store.previewResult = previewResult
    resolveOld(personaDetail(17, 3))
    await pending

    expect(store.activeAccountId).toBe(18)
    expect(store.detail?.account_id).toBe(18)
    expect(store.hasCachedRevision(17, 3)).toBe(false)
  })

  it('uses the server response after save/reset and never increments revision locally', async () => {
    api.get.mockResolvedValue(personaDetail(17, 3))
    api.replace.mockResolvedValue(personaDetail(17, 7))
    api.reset.mockResolvedValue({
      ...personaDetail(17, 9),
      configured: false,
      persona: null,
      persona_hash: null,
    })
    const store = useAccountPersonaStore()
    await store.fetchPersona(17, 3)

    await store.replacePersona(17, 3, persona)
    expect(store.detail?.revision).toBe(7)
    expect(store.hasCachedRevision(17, 3)).toBe(false)
    expect(store.hasCachedRevision(17, 7)).toBe(true)

    await store.resetPersona(17, 7)
    expect(store.detail?.revision).toBe(9)
    expect(store.detail?.configured).toBe(false)
  })

  it('clears a previous preview immediately and keeps it empty after failure', async () => {
    const failure = { response: { status: 429, data: { error: { code: 'PERSONA_PREVIEW_RATE_LIMITED' } } } }
    api.preview.mockRejectedValue(failure)
    const store = useAccountPersonaStore()
    store.selectAccount(17)
    store.previewResult = previewResult

    const pending = store.previewPersona(
      17,
      {
        owned_group_asset_id: 25,
        content_category: 'community',
        trigger_type: 'manual',
        topic: '节点稳定性',
      },
      'preview-request',
    )

    expect(store.previewResult).toBeNull()
    await expect(pending).rejects.toBe(failure)
    expect(store.previewResult).toBeNull()
    expect(store.previewError?.code).toBe('PERSONA_PREVIEW_RATE_LIMITED')
  })
})
