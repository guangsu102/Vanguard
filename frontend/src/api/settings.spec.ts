import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
}))

vi.mock('./client', () => ({ default: client }))

import {
  getOwnedGroupAiPersonaFeatureErrorMessage,
  getSettingsApiError,
  settingsApi,
  type SettingsUpdatePayload,
} from './settings'

describe('settings API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('uses a dedicated camelCase contract for the Persona feature switch', async () => {
    client.put.mockResolvedValue({ data: { data: { enabled: true, revision: 4 } } })

    await settingsApi.updateOwnedGroupAiPersonaFeature({
      expectedRevision: 3,
      enabled: true,
    })

    expect(client.put).toHaveBeenCalledWith('/settings/owned-group-ai-persona', {
      expectedRevision: 3,
      enabled: true,
    })
  })

  it('keeps the generic update payload free of the read-only Persona node', async () => {
    const payload: SettingsUpdatePayload = { aiReply: { enabled: true } }
    await settingsApi.update(payload)

    expect(client.put).toHaveBeenCalledWith('/settings', {
      aiReply: { enabled: true },
    })
  })

  it('maps feature switch errors by stable code and status', () => {
    const conflict = getSettingsApiError({
      response: {
        status: 409,
        data: { error: { code: 'PERSONA_FEATURE_REVISION_CONFLICT' } },
      },
    })
    const unavailable = getSettingsApiError({ response: { status: 503, data: {} } })

    expect(getOwnedGroupAiPersonaFeatureErrorMessage(conflict)).toBe(
      '设置已被其他管理员修改，请确认后重试',
    )
    expect(getOwnedGroupAiPersonaFeatureErrorMessage(unavailable)).toBe(
      'Persona 功能设置暂时不可用，请稍后重试',
    )
  })
})
