import apiClient from './client'

export interface GuardianBot {
  id: number
  account_id: number
  identifier: string
  display_name?: string
  account_type: 'guardian_bot'
  status: string
  is_active: boolean
  bot_username?: string
  bot_user_id?: number
  health_status: string
  sync_status: string
  permissions_snapshot?: Record<string, any>
  last_heartbeat_at?: string
  last_synced_at?: string
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface ManagedGroupBinding {
  id: number
  group_id: number
  telegram_group_id: number
  title?: string
  username?: string
  member_count: number
  bot_account_id: number
  bot_identifier: string
  bot_display_name?: string
  binding_status: string
  bot_role: string
  permissions_snapshot?: Record<string, any>
  bound_at: string
  last_synced_at?: string
  chat_type: 'group' | 'supergroup' | 'channel'
  all_members_muted: boolean
}

export interface ManagedGroupPinnedMessageConfig {
  enabled: boolean
  content: string
  parse_mode: '' | 'Markdown' | 'HTML'
  disable_web_page_preview: boolean
  disable_notification: boolean
  button_text?: string | null
  button_url?: string | null
}

export interface ModerationSensitiveKeyword {
  id: number
  text: string
  category: string
  source: string
  level: string
  action: string
  group_id?: number | null
  enabled: boolean
  confidence: number
  source_sample?: string
  created_at: string
  updated_at: string
}

export interface TelegramWorkerStatus {
  id: number
  worker_id: string
  role: 'growth_user_worker' | 'guardian_bot_worker'
  account_id?: number
  bot_profile_id?: number
  status: string
  last_heartbeat_at?: string
  heartbeat_age_seconds?: number
  is_stale: boolean
  last_error?: string
  metadata: Record<string, any>
  created_at: string
  updated_at: string
}

export const guardianApi = {
  listBots: (params?: { enabled?: boolean; health_status?: string; search?: string; limit?: number }) =>
    apiClient.get<{ data: GuardianBot[]; total: number }>('/guardian-bots', { params }),

  createBot: (data: {
    identifier: string
    display_name?: string
    bot_token: string
    bot_username?: string
    bot_user_id?: number
    country_code?: string
    country_name?: string
    api_config_name?: string
    enabled?: boolean
  }) => apiClient.post<{ data: GuardianBot }>('/guardian-bots', data),

  updateBot: (id: number, data: Record<string, any>) =>
    apiClient.put<{ data: GuardianBot }>(`/guardian-bots/${id}`, data),

  listManagedGroups: (params?: { bot_account_id?: number; binding_status?: string; limit?: number }) =>
    apiClient.get<{ data: ManagedGroupBinding[]; total: number }>('/managed-groups', { params }),

  createManagedGroup: (data: Record<string, any>) =>
    apiClient.post<{ data: ManagedGroupBinding }>('/managed-groups', data),

  updateManagedGroup: (id: number, data: Record<string, any>) =>
    apiClient.put<{ data: ManagedGroupBinding }>(`/managed-groups/${id}`, data),

  syncConfirmedGroups: (data: { bot_account_id: number; statuses?: string[]; limit?: number }) =>
    apiClient.post<{ data: { checked: number; synced: number; skipped: number; errors: Array<Record<string, any>> } }>(
      '/managed-groups/sync-confirmed',
      data
    ),

  getPinnedMessageConfig: (id: number) =>
    apiClient.get<{ data: ManagedGroupPinnedMessageConfig }>(`/managed-groups/${id}/pinned-message-config`),

  savePinnedMessageConfig: (id: number, data: ManagedGroupPinnedMessageConfig) =>
    apiClient.put<{ data: ManagedGroupPinnedMessageConfig }>(`/managed-groups/${id}/pinned-message-config`, data),

  sendPinnedMessage: (id: number, data: {
    content: string
    parse_mode?: 'Markdown' | 'HTML' | ''
    disable_web_page_preview?: boolean
    disable_notification?: boolean
    button_text?: string | null
    button_url?: string | null
  }) => apiClient.post<{ data: { binding_id: number; telegram_group_id: number; message_id: number; pinned: boolean } }>(
    `/managed-groups/${id}/pinned-message`,
    data
  ),

  setMuteAll: (id: number, muted: boolean) =>
    apiClient.post<{ data: { binding_id: number; telegram_group_id?: number; all_members_muted: boolean } }>(
      `/managed-groups/${id}/mute-all`,
      { muted }
    ),

  sendChannelMessage: (id: number, data: {
    content: string
    parse_mode?: 'Markdown' | 'HTML' | ''
    disable_web_page_preview?: boolean
    disable_notification?: boolean
  }) => apiClient.post<{ data: { binding_id: number; telegram_channel_id: number; message_id: number } }>(
    `/managed-groups/${id}/channel-message`,
    data
  ),

  createChannel: (data: {
    creator_account_id: number
    bot_account_id: number
    title: string
    about?: string
    is_public?: boolean
    username?: string
  }) => apiClient.post<{
      data: {
        binding: ManagedGroupBinding
        warnings: string[]
        bot_assignment_complete: boolean
        public_username_complete: boolean
      }
  }>('/managed-groups/channels', data),

  updateChannelUsername: (id: number, username: string) =>
    apiClient.put<{
      data: {
        binding_id: number
        telegram_channel_id: number
        username?: string | null
        channel_visibility: 'public' | 'private'
      }
    }>(`/managed-groups/${id}/channel-username`, { username }),

  refreshChannelStatus: (id: number) =>
    apiClient.post<{
      data: { binding: ManagedGroupBinding; bot_assignment_complete: boolean }
    }>(`/managed-groups/${id}/channel-status/refresh`),

  listSensitiveKeywords: (params?: { group_id?: number; category?: string; enabled?: boolean; search?: string; page?: number; page_size?: number }) =>
    apiClient.get<{ data: ModerationSensitiveKeyword[]; total: number }>('/moderation-sensitive-keywords', { params }),

  createSensitiveKeyword: (data: Record<string, any>) =>
    apiClient.post<{ data: ModerationSensitiveKeyword }>('/moderation-sensitive-keywords', data),

  updateSensitiveKeyword: (id: number, data: Record<string, any>) =>
    apiClient.put<{ data: ModerationSensitiveKeyword }>(`/moderation-sensitive-keywords/${id}`, data),

  deleteSensitiveKeyword: (id: number) =>
    apiClient.delete(`/moderation-sensitive-keywords/${id}`),

  listWorkers: (params?: { role?: string; status?: string; limit?: number }) =>
    apiClient.get<{ data: TelegramWorkerStatus[]; total: number }>('/workers', { params }),

  getVerificationPolicy: (groupId: number) =>
    apiClient.get<{ data: Record<string, any> }>(`/group-governance/verification/${groupId}`),

  saveVerificationPolicy: (data: Record<string, any>) =>
    apiClient.put<{ data: Record<string, any> }>('/group-governance/verification', data),

  getModerationPolicy: (groupId: number) =>
    apiClient.get<{ data: Record<string, any> }>(`/group-governance/moderation/${groupId}`),

  saveModerationPolicy: (data: Record<string, any>) =>
    apiClient.put<{ data: Record<string, any> }>('/group-governance/moderation', data),

  getPunishmentPolicy: (groupId: number) =>
    apiClient.get<{ data: Record<string, any> }>(`/group-governance/punishment/${groupId}`),

  savePunishmentPolicy: (data: Record<string, any>) =>
    apiClient.put<{ data: Record<string, any> }>('/group-governance/punishment', data),
}
