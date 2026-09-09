export const DEFAULT_PAGE_SIZE = 20
export const MAX_PAGE_SIZE = 100
export const PAGE_SIZE_OPTIONS: number[] = [10, 20, 50, 100]

export function normalizePageSize(value: unknown, fallback = DEFAULT_PAGE_SIZE): number {
  const fallbackValue = Number.isFinite(fallback)
    ? Math.min(MAX_PAGE_SIZE, Math.max(1, Math.trunc(fallback)))
    : DEFAULT_PAGE_SIZE
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallbackValue
  return Math.min(MAX_PAGE_SIZE, Math.max(1, Math.trunc(parsed)))
}

export function normalizePageSizeOptions(values: readonly number[]): number[] {
  const options = [...new Set(
    values
      .filter(Number.isFinite)
      .map((value) => Math.trunc(value))
      .filter((value) => value >= 1 && value <= MAX_PAGE_SIZE),
  )].sort((left, right) => left - right)

  return options.length > 0 ? options : [...PAGE_SIZE_OPTIONS]
}

export interface ListPayload<T> {
  list: T[]
  total: number
}

export function normalizeListPayload<T>(responseData: any): ListPayload<T> {
  const payload = responseData?.data

  if (Array.isArray(payload)) {
    return {
      list: payload,
      total: Number(responseData?.total ?? payload.length),
    }
  }

  if (payload && Array.isArray(payload.list)) {
    return {
      list: payload.list,
      total: Number(payload.total ?? responseData?.total ?? payload.list.length),
    }
  }

  return {
    list: [],
    total: Number(responseData?.total ?? 0),
  }
}
