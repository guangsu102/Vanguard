import apiClient from './client'

export type AccountPersonaReplyLength = 'short' | 'medium' | 'long'
export type AccountPersonaAdStyle =
  | 'neutral'
  | 'soft_share'
  | 'experience_share'
  | 'problem_solution'
export type AccountPersonaLanguageStyle = 'auto' | 'zh_cn'
export type AccountPersonaCategory = 'community' | 'promotion'
export type AccountPersonaTriggerType = 'manual'
export type AccountPersonaSource = 'draft' | 'configured' | 'neutral_default'
export type AccountPersonaAuditEventType =
  | 'account_persona_created'
  | 'account_persona_updated'
  | 'account_persona_reset'
  | 'account_persona_preview_generated'
  | 'account_persona_preview_failed'

export type AccountPersonaField =
  | 'name'
  | 'tone'
  | 'interests'
  | 'expertise'
  | 'reply_length'
  | 'preferred_topics'
  | 'forbidden_topics'
  | 'ad_style'
  | 'catchphrases'
  | 'language_style'
  | 'system_prompt'
  | '_form'

export type AccountPersonaFieldErrors = Partial<Record<AccountPersonaField, string>>

export interface AccountPersona {
  schema_version: 1
  name: string
  tone: string
  interests: string[]
  expertise: string[]
  reply_length: AccountPersonaReplyLength
  preferred_topics: string[]
  forbidden_topics: string[]
  ad_style: AccountPersonaAdStyle
  catchphrases: string[]
  language_style: AccountPersonaLanguageStyle
  system_prompt: string
}

export interface AccountPersonaActor {
  id: number
  username: string
}

export interface AccountPersonaFeature {
  static_enabled: boolean
  runtime_enabled: boolean
  effective_enabled: boolean
  runtime_will_apply: boolean
}

export interface AccountPersonaLimits {
  max_total_bytes: number
  max_system_prompt_chars: number
}

export interface AccountPersonaPreviewTarget {
  owned_group_asset_id: number
  group_name: string
  policy_id: number
  policy_revision: number
  available_categories: AccountPersonaCategory[]
  governance_status: string
  runtime_send_eligible: boolean
  blocking_reasons: string[]
}

export interface AccountPersonaDetail {
  account_id: number
  account_type: 'promoter' | 'guardian_bot'
  operation_mode: 'growth' | 'ad_only' | null
  persona_applicable: boolean
  blocking_reason: string | null
  configured: boolean
  revision: number
  persona_hash: string | null
  persona: AccountPersona | null
  updated_at: string | null
  updated_by: AccountPersonaActor | null
  feature: AccountPersonaFeature
  limits: AccountPersonaLimits
  preview_targets: AccountPersonaPreviewTarget[]
}

export interface AccountPersonaSummary {
  account_id: number
  configured: boolean
  name: string | null
  revision: number
  applicable: boolean
  effective_enabled: boolean
}

export interface AccountPersonaReplaceInput {
  expected_revision: number
  persona: AccountPersona
}

export interface AccountPersonaResetInput {
  expected_revision: number
}

export interface AccountPersonaSampleContext {
  message_id: number
  user_name: string
  text: string
}

export interface AccountPersonaPreviewInput {
  owned_group_asset_id: number
  content_category: AccountPersonaCategory
  trigger_type: AccountPersonaTriggerType
  topic: string
  sample_context?: AccountPersonaSampleContext[]
  draft_persona?: AccountPersona
}

export interface AccountPersonaPromotionComposition {
  body_text: string
  cta_text: string
  destination_url: string
  final_text: string
}

export interface AccountPersonaEffectiveConstraints {
  language: AccountPersonaLanguageStyle
  max_chars: number
  allowed_topics: string[]
  forbidden_topic_count: number
  promotion_url_locked: boolean
}

export interface AccountPersonaGovernanceResult {
  allowed: boolean
  reason_code: string | null
}

export interface AccountPersonaPreviewUsage {
  provider: string
  model: string
  input_tokens: number
  output_tokens: number
}

export interface AccountPersonaPreviewResult {
  preview_id: string
  account_id: number
  owned_group_asset_id: number
  content_category: AccountPersonaCategory
  persona_source: AccountPersonaSource
  persona_revision: number | null
  base_account_revision: number
  persona_hash: string
  prompt_template_version: string
  prompt_hash: string
  governance_rules_hash: string
  sample_text: string
  promotion_composition: AccountPersonaPromotionComposition | null
  effective_constraints: AccountPersonaEffectiveConstraints
  governance: AccountPersonaGovernanceResult
  feature: Pick<AccountPersonaFeature, 'effective_enabled' | 'runtime_will_apply'>
  warnings: string[]
  usage: AccountPersonaPreviewUsage
}

export interface AccountPersonaAuditSnapshot {
  configured: boolean
  revision: number
  persona_hash: string | null
}

export interface AccountPersonaAuditEvent {
  id: number
  event_type: AccountPersonaAuditEventType
  account_id: number
  asset_id: number | null
  actor: AccountPersonaActor | null
  created_at: string
  before: AccountPersonaAuditSnapshot | null
  after: AccountPersonaAuditSnapshot | null
  changed_fields: string[]
  result: string
  reason_code: string | null
}

export interface AccountPersonaAuditPage {
  items: AccountPersonaAuditEvent[]
  next_cursor: string | null
}

export interface AccountPersonaAuditParams {
  cursor?: string
  limit?: number
  event_type?: AccountPersonaAuditEventType
}

export interface AccountPersonaEnvelope<T> {
  data: T
  correlation_id: string
}

export interface AccountPersonaApiError {
  code: string
  message: string
  details: Record<string, unknown>
  retryable: boolean
  correlation_id: string | null
  status: number | null
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)

const asRecord = (value: unknown): Record<string, unknown> => (isRecord(value) ? value : {})

const normalizeText = (value: string): string =>
  value
    .normalize('NFKC')
    .replace(/\r\n?/g, '\n')
    .replace(/[\u0000-\u0009\u000b\u000c\u000e-\u001f\u007f]/g, '')
    .replace(/[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]/g, '')
    .trim()

const normalizeList = (values: readonly string[]): string[] => {
  const seen = new Set<string>()
  const result: string[] = []
  for (const value of values) {
    const normalized = normalizeText(value)
    if (!normalized) continue
    const identity = normalized.toLocaleLowerCase()
    if (seen.has(identity)) continue
    seen.add(identity)
    result.push(normalized)
  }
  return result
}

export const createNeutralAccountPersona = (): AccountPersona => ({
  schema_version: 1,
  name: '中性群友',
  tone: '自然、克制、简短',
  interests: [],
  expertise: [],
  reply_length: 'short',
  preferred_topics: [],
  forbidden_topics: [],
  ad_style: 'neutral',
  catchphrases: [],
  language_style: 'auto',
  system_prompt: '',
})

export const cloneAccountPersona = (persona: AccountPersona): AccountPersona => ({
  ...persona,
  interests: [...persona.interests],
  expertise: [...persona.expertise],
  preferred_topics: [...persona.preferred_topics],
  forbidden_topics: [...persona.forbidden_topics],
  catchphrases: [...persona.catchphrases],
})

export const normalizeAccountPersona = (persona: AccountPersona): AccountPersona => ({
  schema_version: 1,
  name: normalizeText(persona.name),
  tone: normalizeText(persona.tone),
  interests: normalizeList(persona.interests),
  expertise: normalizeList(persona.expertise),
  reply_length: persona.reply_length,
  preferred_topics: normalizeList(persona.preferred_topics),
  forbidden_topics: normalizeList(persona.forbidden_topics),
  ad_style: persona.ad_style,
  catchphrases: normalizeList(persona.catchphrases),
  language_style: persona.language_style,
  system_prompt: normalizeText(persona.system_prompt),
})

const validateList = (
  errors: AccountPersonaFieldErrors,
  field: Extract<
    AccountPersonaField,
    'interests' | 'expertise' | 'preferred_topics' | 'forbidden_topics' | 'catchphrases'
  >,
  values: readonly string[],
  maxItems: number,
  maxChars: number,
) => {
  if (values.length > maxItems) {
    errors[field] = `最多 ${maxItems} 项`
    return
  }
  if (values.some((item) => item.length < 1 || item.length > maxChars)) {
    errors[field] = `每项需为 1–${maxChars} 个字符`
  }
}

export const validateAccountPersona = (
  value: AccountPersona,
  maxTotalBytes = 16384,
  maxSystemPromptChars = 1000,
): AccountPersonaFieldErrors => {
  const persona = normalizeAccountPersona(value)
  const errors: AccountPersonaFieldErrors = {}
  if (persona.name.length < 1 || persona.name.length > 50) errors.name = '名称需为 1–50 个字符'
  if (persona.tone.length < 1 || persona.tone.length > 120) errors.tone = '语气需为 1–120 个字符'
  validateList(errors, 'interests', persona.interests, 10, 40)
  validateList(errors, 'expertise', persona.expertise, 10, 40)
  validateList(errors, 'preferred_topics', persona.preferred_topics, 12, 60)
  validateList(errors, 'forbidden_topics', persona.forbidden_topics, 20, 60)
  validateList(errors, 'catchphrases', persona.catchphrases, 8, 60)
  if (persona.system_prompt.length > maxSystemPromptChars) {
    errors.system_prompt = `高级风格说明最多 ${maxSystemPromptChars} 个字符`
  }
  const preferred = new Set(persona.preferred_topics.map((item) => item.toLocaleLowerCase()))
  if (persona.forbidden_topics.some((item) => preferred.has(item.toLocaleLowerCase()))) {
    errors.forbidden_topics = '偏好话题与禁区不能重复'
  }
  if (new TextEncoder().encode(JSON.stringify(persona)).length > maxTotalBytes) {
    errors._form = `Persona 总大小不能超过 ${maxTotalBytes} bytes`
  }
  return errors
}

export const accountPersonaCacheKey = (accountId: number, revision: number): string =>
  `${accountId}:${revision}`

export const getAccountPersonaError = (
  error: unknown,
  fallback = 'Persona 操作失败',
): AccountPersonaApiError => {
  const outer = asRecord(error)
  const response = asRecord(outer.response)
  const payload = asRecord(response.data)
  const nested = isRecord(payload.error)
    ? payload.error
    : isRecord(payload.detail)
      ? payload.detail
      : payload
  return {
    code: typeof nested.code === 'string' ? nested.code : 'PERSONA_REQUEST_FAILED',
    message: typeof nested.message === 'string' ? nested.message : fallback,
    details: asRecord(nested.details),
    retryable: nested.retryable === true,
    correlation_id: typeof payload.correlation_id === 'string' ? payload.correlation_id : null,
    status: typeof response.status === 'number' ? response.status : null,
  }
}

const errorMessages: Readonly<Record<string, string>> = {
  ACCOUNT_NOT_FOUND: '账号不存在或已被删除',
  ADMIN_REQUIRED: '仅管理员可以查看或修改完整 Persona',
  AI_PROMPT_TOO_LARGE: '生成请求超过安全长度限制',
  AI_PROVIDER_SYSTEM_PROMPT_UNSUPPORTED: '当前 AI Provider 不支持 Persona 系统指令',
  AI_PROVIDER_UNSAFE: '当前 AI Provider 不满足 Persona 安全要求',
  AUTHENTICATION_REQUIRED: '登录已失效，请重新登录',
  GOVERNANCE_CONTEXT_UNAVAILABLE: '群治理上下文暂时不可用',
  OWNED_GROUP_ASSET_NOT_FOUND: '预览目标不存在或已被删除',
  PERSONA_ACCOUNT_MISMATCH: '预览目标与当前账号不匹配',
  PERSONA_ACCOUNT_MODE_UNSUPPORTED: '当前账号职责不支持 Persona',
  PERSONA_ACCOUNT_TYPE_UNSUPPORTED: '当前账号类型不支持 Persona',
  PERSONA_AUDIT_READER_REQUIRED: '当前角色无权读取 Persona 审计',
  PERSONA_CONFIG_INVALID: 'Persona 配置异常，请恢复中性默认',
  PERSONA_FEATURE_REVISION_CONFLICT: 'Persona 功能开关已被其他管理员修改',
  PERSONA_FEATURE_SETTING_NOT_INITIALIZED: 'Persona 运行时设置尚未初始化',
  PERSONA_OPERATION_CONFIG_MISSING: '账号缺少运营职责配置',
  PERSONA_POLICY_MODE_UNSUPPORTED: '所选群的当前类别不是 AI 模式',
  PERSONA_PREVIEW_RATE_LIMITED: '预览次数已达上限，请稍后再试',
  PERSONA_PREVIEW_RATE_LIMIT_UNAVAILABLE: '预览限流服务不可用，请稍后再试',
  PERSONA_PROMPT_INJECTION_REJECTED: '内容包含不允许的控制指令',
  PERSONA_REVISION_CONFLICT: 'Persona 已被其他管理员修改',
  PERSONA_TOPIC_FORBIDDEN: '主题与 Persona 禁区冲突',
  PERSONA_TOPIC_NOT_ALLOWED: '主题不在所选群策略允许范围',
  PERSONA_VALIDATION_FAILED: 'Persona 字段校验失败',
}

export const getAccountPersonaErrorMessage = (error: AccountPersonaApiError): string =>
  errorMessages[error.code] ?? 'Persona 操作失败，请稍后重试'

const readFieldName = (value: unknown): AccountPersonaField | null => {
  const supported: readonly AccountPersonaField[] = [
    'name',
    'tone',
    'interests',
    'expertise',
    'reply_length',
    'preferred_topics',
    'forbidden_topics',
    'ad_style',
    'catchphrases',
    'language_style',
    'system_prompt',
  ]
  if (typeof value === 'string') {
    const candidate = value
      .split(/[.\[\]]+/)
      .reverse()
      .find((item) => supported.includes(item as AccountPersonaField))
    if (candidate) return candidate as AccountPersonaField
  }
  if (Array.isArray(value)) {
    const candidate = [...value].reverse().find((item) => typeof item === 'string')
    if (typeof candidate === 'string' && supported.includes(candidate as AccountPersonaField)) {
      return candidate as AccountPersonaField
    }
  }
  return null
}

export const getAccountPersonaFieldErrors = (
  error: AccountPersonaApiError,
): AccountPersonaFieldErrors => {
  const result: AccountPersonaFieldErrors = {}
  const fieldErrors = error.details.field_errors
  if (!Array.isArray(fieldErrors)) return result
  for (const value of fieldErrors) {
    const row = asRecord(value)
    const field = readFieldName(row.field ?? row.loc)
    if (!field) continue
    const errorType = typeof row.error_type === 'string'
      ? row.error_type
      : typeof row.type === 'string'
        ? row.type
        : 'invalid'
    result[field] = `字段校验失败（${errorType}）`
  }
  return result
}

export const accountPersonasApi = {
  get: async (accountId: number): Promise<AccountPersonaDetail> => {
    const response = await apiClient.get<AccountPersonaEnvelope<AccountPersonaDetail>>(
      `/accounts/${accountId}/ai-persona`,
    )
    return response.data.data
  },

  replace: async (
    accountId: number,
    input: AccountPersonaReplaceInput,
  ): Promise<AccountPersonaDetail> => {
    const response = await apiClient.put<AccountPersonaEnvelope<AccountPersonaDetail>>(
      `/accounts/${accountId}/ai-persona`,
      input,
    )
    return response.data.data
  },

  reset: async (
    accountId: number,
    input: AccountPersonaResetInput,
  ): Promise<AccountPersonaDetail> => {
    const response = await apiClient.post<AccountPersonaEnvelope<AccountPersonaDetail>>(
      `/accounts/${accountId}/ai-persona/reset`,
      input,
    )
    return response.data.data
  },

  preview: async (
    accountId: number,
    input: AccountPersonaPreviewInput,
    requestId: string,
  ): Promise<AccountPersonaPreviewResult> => {
    const response = await apiClient.post<AccountPersonaEnvelope<AccountPersonaPreviewResult>>(
      `/accounts/${accountId}/ai-persona/preview`,
      input,
      { headers: { 'X-Request-ID': requestId } },
    )
    return response.data.data
  },

  listAuditEvents: async (
    accountId: number,
    params: AccountPersonaAuditParams = {},
  ): Promise<AccountPersonaAuditPage> => {
    const response = await apiClient.get<AccountPersonaEnvelope<AccountPersonaAuditPage>>(
      `/accounts/${accountId}/ai-persona/audit-events`,
      { params },
    )
    return response.data.data
  },
}
