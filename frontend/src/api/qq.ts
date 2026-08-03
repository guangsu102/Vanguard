import apiClient from './client'

export interface QQConnectionStatus {
  configured: boolean
  provider: 'napcat_onebot11'
  account_id?: string | null
  enabled: boolean
  status: string
  display_name?: string | null
  last_heartbeat_at?: string | null
  last_connected_at?: string | null
  last_error?: string | null
}

export interface QQManagedGroup {
  id: number
  connection_id: number
  group_number: string
  local_name?: string | null
  status: 'active' | 'inactive' | 'removed'
  monitoring_enabled: boolean
  notifications_enabled: boolean
  auto_recall_enabled: boolean
  receive_all_messages_enabled?: boolean | null
  last_message_at?: string | null
  bot_added_at?: string | null
  bot_removed_at?: string | null
  created_at: string
  updated_at: string
}

export interface QQMessageAttachment {
  content_type?: string
  filename?: string
  size?: number
  url?: string
}

export interface QQGroupMessage {
  id: number
  group_id: number
  group_number: string
  group_name?: string | null
  provider_message_id: string
  member_qq?: string | null
  member_role?: string | null
  content?: string | null
  attachments: QQMessageAttachment[]
  is_at_account: boolean
  moderation_status: string
  occurred_at: string
  recalled_at?: string | null
}

export interface QQGroupCommand {
  id: string
  group_id: number
  command_type: 'notification' | 'recall'
  status: string
  provider_message_id?: string | null
  error_message?: string | null
  created_at: string
}

export const qqApi = {
  getConnection: () =>
    apiClient.get<{ data: QQConnectionStatus }>('/qq/connection'),

  listGroups: (params?: { status?: string; monitoring_enabled?: boolean; limit?: number }) =>
    apiClient.get<{ data: QQManagedGroup[]; total: number }>('/qq/groups', { params }),

  syncGroups: () =>
    apiClient.post<{ data: { total: number } }>('/qq/groups/sync'),

  createGroup: (data: { group_number: string; local_name?: string }) =>
    apiClient.post<{ data: QQManagedGroup }>('/qq/groups', data),

  updateGroup: (
    id: number,
    data: Partial<Pick<QQManagedGroup, 'local_name' | 'status' | 'monitoring_enabled' | 'notifications_enabled' | 'auto_recall_enabled'>>,
  ) => apiClient.patch<{ data: QQManagedGroup }>(`/qq/groups/${id}`, data),

  listMessages: (
    groupId: number,
    params?: { member_qq?: string; keyword?: string; offset?: number; limit?: number },
  ) => apiClient.get<{ data: QQGroupMessage[]; total: number }>(`/qq/groups/${groupId}/messages`, { params }),

  sendNotification: (groupId: number, content: string) =>
    apiClient.post<{ data: QQGroupCommand }>(`/qq/groups/${groupId}/notifications`, { content }),

  recallMessage: (messageId: number) =>
    apiClient.post<{ data: QQGroupCommand }>(`/qq/messages/${messageId}/recall`),
}
