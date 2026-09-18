import apiClient from './client'

export type AccountProfileUpdateOperationStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'succeeded'
  | 'partial_failed'
  | 'failed'
  | 'cancelled'

export type AccountProfileUpdateItemStatus =
  | 'pending'
  | 'in_progress'
  | 'retry_wait'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'skipped'

export interface AccountProfileUpdateItem {
  id: number
  account_id: number | null
  status: AccountProfileUpdateItemStatus
  attempts: number
  reason_code?: string | null
  error_message?: string | null
  next_retry_at?: string | null
  remote_attempted_at?: string | null
  started_at?: string | null
  finished_at?: string | null
}

export interface AccountProfileUpdateOperation {
  id: number
  status: AccountProfileUpdateOperationStatus
  profile_bio: string
  total_accounts: number
  processed_accounts: number
  succeeded_accounts: number
  failed_accounts: number
  cancelled_accounts: number
  skipped_accounts: number
  max_attempts: number
  last_error?: string | null
  cancel_requested_at?: string | null
  heartbeat_at?: string | null
  started_at?: string | null
  finished_at?: string | null
  created_at: string
  items?: AccountProfileUpdateItem[]
}

export interface CreateAccountProfileUpdateOperationRequest {
  account_ids: number[]
  profile_bio: string
}

interface ApiEnvelope<T> {
  data: T
}

function unwrap<T>(response: { data: T | ApiEnvelope<T> }): T {
  const payload = response.data
  if (payload !== null && typeof payload === 'object' && 'data' in payload) {
    return (payload as ApiEnvelope<T>).data
  }
  return payload as T
}

/**
 * Generate a stable-enough client key for one submit attempt. The server
 * couples the key to a canonical account/bio snapshot, so retried submits
 * cannot queue a different bulk profile update.
 */
export const createAccountProfileUpdateIdempotencyKey = (): string => {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID()
  }
  return 'account-profile-update-' + Date.now() + '-' + Math.random().toString(16).slice(2)
}

export async function createAccountProfileUpdateOperation(
  payload: CreateAccountProfileUpdateOperationRequest,
  idempotencyKey = createAccountProfileUpdateIdempotencyKey(),
): Promise<AccountProfileUpdateOperation> {
  const response = await apiClient.post('/accounts/profile-updates/operations', payload, {
    headers: { 'Idempotency-Key': idempotencyKey },
  })
  return unwrap<AccountProfileUpdateOperation>(response)
}

export async function getLatestAccountProfileUpdateOperation(): Promise<AccountProfileUpdateOperation | null> {
  const response = await apiClient.get('/accounts/profile-updates/operations/latest')
  return unwrap<AccountProfileUpdateOperation | null>(response)
}

export async function getAccountProfileUpdateOperation(id: number): Promise<AccountProfileUpdateOperation> {
  const response = await apiClient.get('/accounts/profile-updates/operations/' + id)
  return unwrap<AccountProfileUpdateOperation>(response)
}

export async function cancelAccountProfileUpdateOperation(id: number): Promise<AccountProfileUpdateOperation> {
  const response = await apiClient.post('/accounts/profile-updates/operations/' + id + '/cancel')
  return unwrap<AccountProfileUpdateOperation>(response)
}

export const accountProfileUpdatesApi = {
  createOperation: createAccountProfileUpdateOperation,
  getLatestOperation: getLatestAccountProfileUpdateOperation,
  getOperation: getAccountProfileUpdateOperation,
  cancelOperation: cancelAccountProfileUpdateOperation,
}

export default accountProfileUpdatesApi
