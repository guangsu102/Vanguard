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
