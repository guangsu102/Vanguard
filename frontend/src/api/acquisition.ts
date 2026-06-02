import apiClient from './client'

export interface SearchRecord {
  id: number
  keyword: string
  group_id: number
  group_title?: string
  member_count?: number
  found_at: string
}

export interface MessageRecord {
  id: number
  account_id: number
  group_id: number
  content?: string
  message_type: string
  message_id?: number
  sent_at: string
}

export interface TriggerRecord {
  id: number
  trigger_id: number
  user_id: number
  group_id?: number
  matched_keyword: string
  action_taken: string
  reply_content?: string
  created_at: string
}

export interface GuideFlow {
  user_id: number
  state: 'init' | 'awaiting_registration' | 'registered' | 'closed'
  current_step: number
  steps_completed: string[]
  started_at: string
  last_message_at: string
}

export interface TrackingRecord {
  tracking_code: string
  user_id?: number
  source_type?: string
  campaign_name?: string
  group_id?: number
  keyword?: string
  converted: boolean
  click_at?: string
  registered_at?: string
  converted_at?: string
  created_at: string
}

export interface AcquisitionStats {
  total_tracking: number
  converted_tracking: number
  conversion_rate: number
  total_messages: number
  total_triggers: number
}

export type KeywordTriggerAction = 'reply_template' | 'reply_ai' | 'send_private' | 'react' | 'pin_message'
export type KeywordTriggerType = 'keyword' | 'command' | 'member_join' | 'bot_mention'

export interface KeywordTrigger {
  id: number
  keyword_id?: number
  keyword_text: string
  trigger_type: KeywordTriggerType
  action: KeywordTriggerAction
  template_id?: number
  reply_content?: string
  use_ai_reply: boolean
  cooldown_seconds: number
  max_triggers_per_user: number
  max_triggers_per_group: number
  priority: number
  enabled: boolean
  created_at: string
}

export interface KeywordTriggerFormData {
  keyword_id?: number
  keyword_text: string
  trigger_type: KeywordTriggerType
  action: KeywordTriggerAction
  template_id?: number
  reply_content?: string
  use_ai_reply: boolean
  cooldown_seconds: number
  max_triggers_per_user: number
  max_triggers_per_group: number
  priority: number
  enabled: boolean
}

export const acquisitionApi = {
  // Search Records
  createSearchRecord: (data: { keyword: string; group_id: number; group_title?: string; member_count?: number }) => {
    return apiClient.post<{ data: SearchRecord }>('/acquisition/search', data)
  },

  getSearchRecords: (params?: { keyword?: string; limit?: number }) => {
    return apiClient.get<{ data: SearchRecord[] }>('/acquisition/search', { params })
  },

  // Messages
  createMessageRecord: (data: {
    account_id: number
    group_id: number
    content?: string
    message_type?: string
    message_id?: number
  }) => {
    return apiClient.post<{ data: MessageRecord }>('/acquisition/messages', data)
  },

  getMessageStats: (params?: { start_date?: string; end_date?: string }) => {
    return apiClient.get<{ data: { total: number; by_type: Record<string, number> } }>('/acquisition/messages/stats', { params })
  },

  // Triggers
  createTriggerRecord: (data: {
    trigger_id: number
    user_id: number
    group_id?: number
    message_id?: number
    matched_keyword: string
    user_message?: string
    action_taken: string
    reply_content?: string
  }) => {
    return apiClient.post<{ data: TriggerRecord }>('/acquisition/triggers', data)
  },

  getTriggerRecords: (params?: { trigger_id?: number; user_id?: number; limit?: number }) => {
    return apiClient.get<{ data: TriggerRecord[] }>('/acquisition/triggers', { params })
  },

  getKeywordTriggers: (params?: {
    page?: number
    page_size?: number
    keyword?: string
    action?: KeywordTriggerAction | ''
    enabled?: boolean
  }) => {
    return apiClient.get<{ data: KeywordTrigger[]; total: number }>('/acquisition/keyword-triggers', { params })
  },

  createKeywordTrigger: (data: KeywordTriggerFormData) => {
    return apiClient.post<{ data: KeywordTrigger }>('/acquisition/keyword-triggers', data)
  },

  updateKeywordTrigger: (id: number, data: Partial<KeywordTriggerFormData>) => {
    return apiClient.put<{ data: KeywordTrigger }>(`/acquisition/keyword-triggers/${id}`, data)
  },

  deleteKeywordTrigger: (id: number) => {
    return apiClient.delete(`/acquisition/keyword-triggers/${id}`)
  },

  // Guide Flow
  updateGuideFlow: (data: {
    user_id: number
    state: string
    step?: number
    steps_completed?: string[]
  }) => {
    return apiClient.put<{ data: GuideFlow }>('/acquisition/guide-flow', data)
  },

  getGuideFlow: (userId: number) => {
    return apiClient.get<{ data: GuideFlow }>(`/acquisition/guide-flow/${userId}`)
  },

  // Tracking
  createTracking: (data: {
    tracking_code: string
    user_id?: number
    source_type?: string
    campaign_name?: string
    group_id?: number
    keyword?: string
    bot_id?: string
  }) => {
    return apiClient.post<{ data: TrackingRecord }>('/acquisition/track', data)
  },

  updateTracking: (trackingCode: string, data: Partial<TrackingRecord>) => {
    return apiClient.put<{ data: TrackingRecord }>(`/acquisition/track/${trackingCode}`, data)
  },

  getTracking: (trackingCode: string) => {
    return apiClient.get<{ data: TrackingRecord }>(`/acquisition/track/${trackingCode}`)
  },

  getTrackingList: (params?: { converted?: boolean; campaign?: string; limit?: number }) => {
    return apiClient.get<{ data: TrackingRecord[] }>('/acquisition/track', { params })
  },

  // Statistics
  getStats: () => {
    return apiClient.get<{ data: AcquisitionStats }>('/acquisition/stats/overview')
  },
}
