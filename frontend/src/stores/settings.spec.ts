import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const api = vi.hoisted(() => ({
  get: vi.fn(),
  update: vi.fn(),
  updateOwnedGroupAiPersonaFeature: vi.fn(),
  getSystemInfo: vi.fn(),
  getLogs: vi.fn(),
  exportLogs: vi.fn(),
  clearLogs: vi.fn(),
  backupDatabase: vi.fn(),
  restartService: vi.fn(),
}))

vi.mock('@/api/settings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/settings')>()
  return { ...actual, settingsApi: api }
})

import type {
  OwnedGroupAiPersonaFeatureSettings,
  SettingsReadData,
  SettingsUpdatePayload,
} from '@/api/settings'
import { useSettingsStore } from './settings'

const feature = (
  overrides: Partial<OwnedGroupAiPersonaFeatureSettings> = {},
): OwnedGroupAiPersonaFeatureSettings => ({
  enabled: false,
  revision: 2,
  updatedAt: null,
  updatedBy: null,
  staticEnabled: false,
  effectiveEnabled: false,
  ...overrides,
})

const readData = (
  ownedGroupAiPersona: OwnedGroupAiPersonaFeatureSettings = feature(),
): SettingsReadData => ({
  aiReply: { enabled: false },
  ownedGroupAiPersona,
})

describe('settings Store Persona feature switch', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
    api.get.mockResolvedValue({ data: { data: readData() } })
    api.update.mockResolvedValue({ data: { data: readData() } })
  })

  it('whitelists the generic payload and never forwards a read-only Persona node', async () => {
    const store = useSettingsStore()
    const runtimeInjected = {
      aiReply: { enabled: true },
      ownedGroupMessaging: { enabled: false },
      ownedGroupAiPersona: feature({ enabled: true }),
    } as unknown as SettingsUpdatePayload

    await store.updateSettings(runtimeInjected)

    expect(api.update).toHaveBeenCalledWith({
      aiReply: { enabled: true },
      ownedGroupMessaging: { enabled: false },
    })
  })

  it('uses the current revision and replaces the node with the server response', async () => {
    const store = useSettingsStore()
    await store.fetchSettings()
    const saved = feature({
      enabled: true,
      revision: 7,
      staticEnabled: true,
      effectiveEnabled: true,
    })
    api.updateOwnedGroupAiPersonaFeature.mockResolvedValue({ data: { data: saved } })

    await store.updateOwnedGroupAiPersonaFeature(true)

    expect(api.updateOwnedGroupAiPersonaFeature).toHaveBeenCalledWith({
      expectedRevision: 2,
      enabled: true,
    })
    expect(store.settings?.ownedGroupAiPersona).toEqual(saved)
    expect(store.ownedGroupAiPersonaPendingIntent).toBeNull()
  })

  it('refreshes once on conflict, keeps the intent, and never automatically replays PUT', async () => {
    const store = useSettingsStore()
    await store.fetchSettings()
    const conflict = {
      response: {
        status: 409,
        data: { error: { code: 'PERSONA_FEATURE_REVISION_CONFLICT' } },
      },
    }
    api.updateOwnedGroupAiPersonaFeature.mockRejectedValue(conflict)
    api.get.mockResolvedValueOnce({
      data: { data: readData(feature({ revision: 3 })) },
    })

    await expect(store.updateOwnedGroupAiPersonaFeature(true)).rejects.toBe(conflict)

    expect(api.updateOwnedGroupAiPersonaFeature).toHaveBeenCalledTimes(1)
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(store.settings?.ownedGroupAiPersona.revision).toBe(3)
    expect(store.ownedGroupAiPersonaPendingIntent).toBe(true)
  })
})
