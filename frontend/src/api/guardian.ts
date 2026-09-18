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

export interface ManagedBotOwnerOption {
  account_id: number
  identifier?: string
  display_name?: string
  status?: string
}

export interface ManagedBotManagerOption {
  profile_id: number
  account_id?: number
  bot_user_id?: number
  bot_username?: string
  display_name?: string
  can_manage_bots: boolean
  enabled: boolean
}

export interface ManagedBotProvisionCapability {
  supported: boolean
  available: boolean
  owner_accounts: ManagedBotOwnerOption[]
  manager_bot_profiles: ManagedBotManagerOption[]
  blockers: string[]
}

export interface ManagedBotProvisionInput {
  owner_account_id: number
  manager_bot_profile_id: number
  display_name: string
  username: string
}

export interface ManagedBotProvisionOperation {
  id: number
  status: string
  step?: string
  owner_account_id?: number
  manager_bot_profile_id?: number
  display_name?: string
  username?: string
  bot_user_id?: number
  guardian_bot_profile_id?: number
  owned_bot_profile_id?: number
  error_code?: string
  retry_at?: string
  created_at?: string
  started_at?: string
  finished_at?: string
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
  source_type: 'owned_group' | 'managed_group'
  owned_group_asset_id?: number | null
  owned_group_asset_title?: string | null
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

const unwrapManagedBotPayload = (payload: any): any =>
  payload?.data?.data ?? payload?.data ?? payload

const positiveNumber = (value: unknown): number | undefined => {
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : undefined
}

const MANAGED_BOT_ERROR_CODES = [
  'USERNAME_OCCUPIED',
  'USERNAME_PURCHASE_AVAILABLE',
  'USERNAME_SUFFIX_MISSING',
  'USERNAME_INVALID',
  'NAME_INVALID',
  'MANAGER_PERMISSION_MISSING',
  'MANAGER_INVALID',
  'BOT_CREATE_LIMIT_EXCEEDED',
  'FLOOD_WAIT',
  'ACCOUNT_OPERATION',
  'ACCOUNT_RISK_BLOCKED',
  'LEASE',
  'SESSION',
  'TOKEN_FETCH_FAILED',
  'MANAGED_BOT_TOKEN_FAILED',
  'MANAGED_BOT_TOKEN_UNAVAILABLE',
  'IDENTITY_MISMATCH',
  'NEEDS_ATTENTION',
  'OUTCOME_UNKNOWN',
] as const

const knownManagedBotErrorCode = (...values: unknown[]): string | undefined => {
  const text = values
    .filter((value): value is string => typeof value === 'string')
    .join(' ')
    .toUpperCase()
  return MANAGED_BOT_ERROR_CODES.find((code) => text.includes(code))
}

const normalizedBlocker = (value: unknown): string => {
  const code = knownManagedBotErrorCode(value)
  if (code) return code.toLowerCase()
  const text = typeof value === 'string' ? value.toLowerCase() : ''
  if (text.includes('unsupported') || text.includes('not_supported'))
    return 'unsupported'
  if (text.includes('owner')) return 'owner_account_unavailable'
  if (text.includes('manager')) return 'manager_bot_unavailable'
  return 'capability_unavailable'
}

const normalizeManagedBotCapability = (
  payload: any,
): ManagedBotProvisionCapability => {
  const raw = unwrapManagedBotPayload(payload) || {}
  const ownerRows =
    raw.owner_accounts ?? raw.eligible_owner_accounts ?? raw.owners ?? []
  const managerRows =
    raw.manager_bot_profiles ?? raw.manager_profiles ?? raw.managers ?? []
  const owner_accounts = (Array.isArray(ownerRows) ? ownerRows : []).flatMap(
    (item: any) => {
      const account_id = positiveNumber(item?.account_id ?? item?.id)
      return account_id
        ? [
            {
              account_id,
              identifier:
                typeof item?.identifier === 'string'
                  ? item.identifier
                  : undefined,
              display_name:
                typeof item?.display_name === 'string'
                  ? item.display_name
                  : undefined,
              status:
                typeof item?.status === 'string' ? item.status : undefined,
            },
          ]
        : []
    },
  )
  const manager_bot_profiles = (
    Array.isArray(managerRows) ? managerRows : []
  ).flatMap((item: any) => {
    const profile_id = positiveNumber(item?.profile_id ?? item?.id)
    return profile_id
      ? [
          {
            profile_id,
            account_id: positiveNumber(item?.account_id),
            bot_user_id: positiveNumber(item?.bot_user_id ?? item?.user_id),
            bot_username:
              typeof item?.bot_username === 'string'
                ? item.bot_username
                : typeof item?.username === 'string'
                  ? item.username
                  : undefined,
            display_name:
              typeof item?.display_name === 'string'
                ? item.display_name
                : undefined,
            can_manage_bots: Boolean(
              item?.can_manage_bots ?? item?.bot_can_manage_bots,
            ),
            enabled: item?.enabled === undefined ? true : Boolean(item.enabled),
          },
        ]
      : []
  })
  const blockerRows = raw.blockers ?? raw.reasons ?? []
  const blockers = (Array.isArray(blockerRows) ? blockerRows : [blockerRows])
    .map((item: any) => normalizedBlocker(item?.reason ?? item?.code ?? item))
    .filter(Boolean)
  const supported = Boolean(raw.supported ?? raw.enabled ?? true)
  const available = Boolean(
    raw.available ??
    raw.can_create ??
    (supported && owner_accounts.length && manager_bot_profiles.length),
  )
  return {
    supported,
    available,
    owner_accounts,
    manager_bot_profiles,
    blockers,
  }
}

const normalizeManagedBotProvision = (
  payload: any,
): ManagedBotProvisionOperation => {
  const raw = unwrapManagedBotPayload(payload) || {}
  return {
    id: Number(raw.id ?? raw.operation_id ?? 0),
    status: String(raw.status ?? 'queued').toLowerCase(),
    step: typeof raw.step === 'string' ? raw.step : raw.current_step,
    owner_account_id: positiveNumber(raw.owner_account_id),
    manager_bot_profile_id: positiveNumber(
      raw.manager_bot_profile_id ?? raw.manager_profile_id,
    ),
    display_name:
      typeof raw.display_name === 'string'
        ? raw.display_name
        : raw.requested_name,
    username:
      typeof raw.username === 'string' ? raw.username : raw.requested_username,
    bot_user_id: positiveNumber(raw.bot_user_id ?? raw.created_bot_user_id),
    guardian_bot_profile_id: positiveNumber(raw.guardian_bot_profile_id),
    owned_bot_profile_id: positiveNumber(raw.owned_bot_profile_id),
    error_code: knownManagedBotErrorCode(
      raw.error_code,
      raw.reason_code,
      raw.error?.code,
    ),
    retry_at:
      typeof (raw.retry_at ?? raw.next_retry_at) === 'string'
        ? (raw.retry_at ?? raw.next_retry_at)
        : undefined,
    created_at: typeof raw.created_at === 'string' ? raw.created_at : undefined,
    started_at: typeof raw.started_at === 'string' ? raw.started_at : undefined,
    finished_at:
      typeof raw.finished_at === 'string' ? raw.finished_at : undefined,
  }
}

export const managedBotProvisionErrorMessage = (value: unknown): string => {
  const payload = (value as any)?.response?.data ?? value
  const raw = unwrapManagedBotPayload(payload) ?? {}
  const detail = raw?.detail
  const code =
    knownManagedBotErrorCode(
      raw?.error_code,
      raw?.reason_code,
      raw?.error?.code,
      typeof detail === 'object' ? (detail?.reason ?? detail?.code) : detail,
      typeof value === 'string' ? value : undefined,
    ) ?? ''
  if (code.includes('USERNAME_OCCUPIED'))
    return '该 Bot 用户名已被占用，请修改后重试'
  if (code.includes('USERNAME_PURCHASE_AVAILABLE'))
    return '该 Bot 用户名只能通过 Telegram 购买，请更换普通可用用户名'
  if (code.includes('USERNAME_SUFFIX_MISSING'))
    return 'Bot 用户名必须以 bot 结尾'
  if (code.includes('USERNAME_INVALID')) return 'Bot 用户名格式无效'
  if (code.includes('NAME_INVALID')) return 'Bot 展示名称格式无效'
  if (code.includes('MANAGER_PERMISSION_MISSING'))
    return 'Manager Bot 尚未开启 Bot Management Mode'
  if (code.includes('MANAGER_INVALID')) return 'Manager Bot 无效，请重新选择'
  if (code.includes('BOT_CREATE_LIMIT_EXCEEDED'))
    return '该用户账号已达到 Telegram Bot 创建上限'
  if (code.includes('FLOOD_WAIT') || code.includes('FLOODWAIT'))
    return 'Telegram 限流，系统将在允许时间后重试'
  if (code.includes('ACCOUNT_OPERATION') || code.includes('LEASE'))
    return '用户账号正在执行其他任务，请稍后重试'
  if (code.includes('ACCOUNT_RISK_BLOCKED'))
    return '该用户账号已达到系统创建 Bot 的安全额度，请次日再试'
  if (code.includes('SESSION')) return '用户账号缺少有效 Telegram 登录会话'
  if (code.includes('TOKEN'))
    return '新 Bot 已创建，但安全获取凭证失败；请勿重复创建'
  if (code.includes('IDENTITY_MISMATCH'))
    return 'Telegram 返回身份不一致，需要管理员处理'
  if (code.includes('NEEDS_ATTENTION') || code.includes('OUTCOME_UNKNOWN'))
    return '创建结果待确认，请勿重复提交'
  return '一键创建 Bot 失败，请稍后重试'
}

export const guardianApi = {
  listBots: (params?: { enabled?: boolean; health_status?: string; search?: string; limit?: number }) =>
    apiClient.get<{ data: GuardianBot[]; total: number }>('/guardian-bots', { params }),

  getBot: async (profileId: number, signal?: AbortSignal): Promise<GuardianBot> => {
    const response = signal
      ? await apiClient.get<GuardianBot>(`/guardian-bots/${profileId}`, { signal })
      : await apiClient.get<GuardianBot>(`/guardian-bots/${profileId}`)
    return response.data
  },

  getManagedProvisionCapability:
    async (): Promise<ManagedBotProvisionCapability> =>
      normalizeManagedBotCapability(
        (await apiClient.get('/guardian-bots/managed-provisions/capability'))
          .data,
      ),

  createManagedProvision: async (
    data: ManagedBotProvisionInput,
  ): Promise<ManagedBotProvisionOperation> => {
    const idempotencyKey = crypto.randomUUID()
    return normalizeManagedBotProvision(
      (
        await apiClient.post('/guardian-bots/managed-provisions', data, {
          headers: { 'Idempotency-Key': idempotencyKey },
        })
      ).data,
    )
  },

  getManagedProvision: async (
    id: number,
  ): Promise<ManagedBotProvisionOperation> =>
    normalizeManagedBotProvision(
      (await apiClient.get(`/guardian-bots/managed-provisions/${id}`)).data,
    ),

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
