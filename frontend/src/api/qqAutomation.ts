import apiClient from "./client";

export interface QQAutomationAccount {
  id: number;
  app_id: string;
  display_name?: string | null;
  status: string;
  configured: boolean;
  enabled: boolean;
  automation_enabled: boolean;
  http_url?: string | null;
  join_api_url?: string | null;
  join_configured: boolean;
  join_interval_seconds: number;
  send_interval_seconds: number;
  max_joins_per_day: number;
  max_sends_per_day: number;
  last_error?: string | null;
}

export interface QQCampaignTarget {
  group_number: string;
  local_name?: string | null;
  verify_message: string;
  enabled: boolean;
}

export interface QQAdCampaign {
  id: number;
  name: string;
  enabled: boolean;
  auto_join_enabled: boolean;
  send_mode: "after_join" | "interval" | "scheduled";
  min_wait_after_join_minutes: number;
  interval_minutes: number;
  scheduled_times: string[];
  timezone: string;
  max_sends_per_group_per_day: number;
  max_sends_per_account_per_day: number;
  start_at?: string | null;
  end_at?: string | null;
  targets: QQCampaignTarget[];
}

export interface QQAdBinding {
  id: number;
  connection_id: number;
  campaign_id: number;
  creative_id: number;
  priority: number;
  enabled: boolean;
}

export interface QQJoinTask {
  id: number;
  connection_id: number;
  group_number: string;
  status: string;
  error_message?: string | null;
  attempted_at?: string | null;
  joined_at?: string | null;
}

export interface QQAdSchedule {
  id: number;
  connection_id: number;
  campaign_id: number;
  group_number: string;
  status: string;
  next_due_at?: string | null;
  last_sent_at?: string | null;
  error_message?: string | null;
}

export interface QQAutomationLog {
  id: string;
  connection_id: number;
  campaign_id?: number | null;
  creative_id?: number | null;
  group_number: string;
  operation_type: "join" | "ad";
  status: string;
  provider_message_id?: string | null;
  error_message?: string | null;
  created_at: string;
}

const root = "/qq/automation";
export const qqAutomationApi = {
  accounts: () =>
    apiClient.get<{ data: QQAutomationAccount[] }>(`${root}/accounts`),
  createAccount: (data: {
    account_number: string;
    display_name?: string;
    http_url: string;
    access_token: string;
  }) => apiClient.post(`${root}/accounts`, data),
  updateAccount: (
    id: number,
    data: Partial<QQAutomationAccount> & {
      access_token?: string;
      join_api_token?: string;
    },
  ) => apiClient.patch(`${root}/accounts/${id}`, data),
  syncAccount: (id: number) =>
    apiClient.post<{ data: { total: number } }>(`${root}/accounts/${id}/sync`),
  campaigns: () => apiClient.get<{ data: QQAdCampaign[] }>(`${root}/campaigns`),
  saveCampaign: (data: Omit<QQAdCampaign, "id">, id?: number) =>
    id
      ? apiClient.put(`${root}/campaigns/${id}`, data)
      : apiClient.post(`${root}/campaigns`, data),
  bindings: () => apiClient.get<{ data: QQAdBinding[] }>(`${root}/bindings`),
  createBindings: (data: {
    connection_id: number;
    campaign_id: number;
    creative_ids: number[];
    priority: number;
  }) => apiClient.post(`${root}/bindings`, data),
  deleteBinding: (id: number) => apiClient.delete(`${root}/bindings/${id}`),
  joinTasks: (offset = 0, limit = 50) =>
    apiClient.get<{ data: QQJoinTask[]; total: number }>(`${root}/join-tasks`, {
      params: { offset, limit },
    }),
  retryJoin: (id: number) => apiClient.post(`${root}/join-tasks/${id}/retry`),
  cancelJoin: (id: number) => apiClient.post(`${root}/join-tasks/${id}/cancel`),
  schedules: (offset = 0, limit = 50) =>
    apiClient.get<{ data: QQAdSchedule[]; total: number }>(
      `${root}/schedules`,
      { params: { offset, limit } },
    ),
  resumeSchedule: (id: number, confirmedNotSent: boolean) =>
    apiClient.post(`${root}/schedules/${id}/resume`, {
      confirmed_not_sent: confirmedNotSent,
    }),
  logs: (offset = 0, limit = 50) =>
    apiClient.get<{ data: QQAutomationLog[]; total: number }>(`${root}/logs`, {
      params: { offset, limit },
    }),
};
