import apiClient from './client'

export type PrivateChatStatus = 'open' | 'closed'
export type PrivateChatHandlingMode = 'auto' | 'human'
export type PrivateChatMessageStatus = 'received' | 'pending' | 'sending' | 'sent' | 'failed' | 'unknown'

export interface PrivateChatConversation {
  id: number
  account_id: number
  account_name: string
  account_identifier?: string | null
  account_status?: string | null
  peer_telegram_id: number
  peer_username?: string | null
  peer_display_name?: string | null
  status: PrivateChatStatus
  handling_mode: PrivateChatHandlingMode
  assigned_admin_id?: number | null
  unread_count: number
  last_message_preview?: string | null
  last_message_direction?: 'inbound' | 'outbound' | null
  last_message_at?: string | null
  last_inbound_at?: string | null
  last_outbound_at?: string | null
  created_at: string
  updated_at: string
}

export interface PrivateChatMessage {
  id: number
  conversation_id: number
  account_id: number
  peer_telegram_id: number
  telegram_message_id?: number | null
  reply_to_telegram_message_id?: number | null
  direction: 'inbound' | 'outbound'
  source: 'user' | 'auto' | 'operator' | 'system' | string
  message_type: string
  content?: string | null
  media?: Record<string, unknown> | null
  status: PrivateChatMessageStatus
  operator_id?: number | null
  client_request_id?: string | null
  attempt_count: number
  error_message?: string | null
  occurred_at: string
  sent_at?: string | null
  created_at: string
  updated_at: string
}

export interface PrivateChatSummary {
  conversation_count: number
  unread_count: number
  open_count: number
}

export interface PrivateChatListParams {
  account_id?: number
  status?: PrivateChatStatus
  handling_mode?: PrivateChatHandlingMode
  unread_only?: boolean
  keyword?: string
  offset?: number
  limit?: number
}

export const privateChatsApi = {
  listConversations: (params?: PrivateChatListParams) =>
    apiClient.get<{ data: PrivateChatConversation[]; total: number }>(
      '/private-chats/conversations',
      { params },
    ),

  listMessages: (conversationId: number, params?: { before_id?: number; limit?: number }) =>
    apiClient.get<{ data: PrivateChatMessage[]; total: number }>(
      `/private-chats/conversations/${conversationId}/messages`,
      { params },
    ),

  getSummary: () =>
    apiClient.get<{ data: PrivateChatSummary }>('/private-chats/summary'),

  markRead: (conversationId: number) =>
    apiClient.post<{ data: PrivateChatConversation }>(
      `/private-chats/conversations/${conversationId}/read`,
    ),

  updateConversation: (
    conversationId: number,
    data: Partial<Pick<PrivateChatConversation, 'status' | 'handling_mode' | 'assigned_admin_id'>>,
  ) =>
    apiClient.patch<{ data: PrivateChatConversation }>(
      `/private-chats/conversations/${conversationId}`,
      data,
    ),

  sendMessage: (
    conversationId: number,
    data: { content: string; client_request_id: string },
  ) =>
    apiClient.post<{ data: PrivateChatMessage }>(
      `/private-chats/conversations/${conversationId}/messages`,
      data,
    ),
}
