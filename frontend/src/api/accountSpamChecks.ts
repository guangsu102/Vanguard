import apiClient from './client'

export type SpamCheckOperationStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'succeeded'
  | 'partial_failed'
  | 'failed'
  | 'cancelled'

export type SpamCheckItemStatus =
  | 'pending'
  | 'in_progress'
  | 'retry_wait'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export type SpamCheckResult = 'clear' | 'restricted' | null

export interface SpamCheckOperationItem {
  id: number
  operation_id: number
  account_id: number | null
  status: SpamCheckItemStatus
  result: SpamCheckResult
  attempts: number
  reason_code?: string | null
  response_summary?: string | null
  next_retry_at?: string | null
  checked_at?: string | null
  finished_at?: string | null
}

export interface SpamCheckOperation {
  id: number
  status: SpamCheckOperationStatus
  total_accounts: number
  processed_accounts: number
  clear_accounts: number
  restricted_accounts: number
  failed_accounts: number
  cancelled_accounts: number
  last_error?: string | null
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  items?: SpamCheckOperationItem[]
}

export interface CreateSpamCheckOperationRequest {
  account_ids: number[]
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

export const createSpamCheckIdempotencyKey = (): string => {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  return 'spam-check-' + Date.now() + '-' + Math.random().toString(16).slice(2)
}

export async function createSpamCheckOperation(
  payload: CreateSpamCheckOperationRequest,
  idempotencyKey = createSpamCheckIdempotencyKey(),
): Promise<SpamCheckOperation> {
  const response = await apiClient.post('/accounts/spam-check/operations', payload, {
    headers: { 'Idempotency-Key': idempotencyKey },
  })
  return unwrap<SpamCheckOperation>(response)
}

export async function getLatestSpamCheckOperation(): Promise<SpamCheckOperation | null> {
  const response = await apiClient.get('/accounts/spam-check/operations/latest')
  return unwrap<SpamCheckOperation | null>(response)
}

export async function getSpamCheckOperation(id: number): Promise<SpamCheckOperation> {
  const response = await apiClient.get('/accounts/spam-check/operations/' + id)
  return unwrap<SpamCheckOperation>(response)
}

export async function cancelSpamCheckOperation(id: number): Promise<SpamCheckOperation> {
  const response = await apiClient.post('/accounts/spam-check/operations/' + id + '/cancel')
  return unwrap<SpamCheckOperation>(response)
}

export const accountSpamChecksApi = {
  createOperation: createSpamCheckOperation,
  getLatestOperation: getLatestSpamCheckOperation,
  getOperation: getSpamCheckOperation,
  cancelOperation: cancelSpamCheckOperation,
}

export default accountSpamChecksApi