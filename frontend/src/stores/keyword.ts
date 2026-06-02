import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  keywordsApi,
  type Keyword,
  type KeywordListParams,
  type KeywordFormData,
  type ModerationItem,
  type KeywordType,
  type MatchMode,
} from '@/api/keywords'
import { normalizeListPayload } from '@/utils/pagination'

const keywordTypeMap: Record<string, KeywordType> = {
  demand: 'demand',
  inquiry: 'inquiry',
  price: 'price',
  competitor: 'competitor',
  whitelist: 'demand',
  blacklist: 'competitor',
}

const matchModeMap: Record<string, MatchMode> = {
  fuzzy: 'contains',
  contains: 'contains',
  exact: 'exact',
  regex: 'regex',
}

const normalizeKeyword = (item: any): Keyword => {
  const rawType = item.type ?? 'demand'
  const rawMatchMode = item.matchMode ?? item.match_mode ?? 'fuzzy'
  return {
    ...item,
    word: item.word ?? item.text ?? '',
    text: item.text ?? item.word ?? '',
    type: keywordTypeMap[rawType] ?? 'demand',
    rawType,
    matchMode: matchModeMap[rawMatchMode] ?? 'contains',
    match_mode: rawMatchMode,
    hitCount: Number(item.hitCount ?? item.trigger_count ?? 0),
    trigger_count: Number(item.trigger_count ?? item.hitCount ?? 0),
    createdBy: item.createdBy ?? 'system',
    createdAt: item.createdAt ?? item.created_at ?? '',
    created_at: item.created_at ?? item.createdAt ?? '',
    updatedAt: item.updatedAt ?? item.updated_at ?? item.created_at ?? item.createdAt ?? '',
    updated_at: item.updated_at ?? item.updatedAt ?? item.created_at ?? item.createdAt ?? '',
    status: item.status ?? 'pending',
  }
}

const unwrapKeyword = (responseData: any): Keyword => {
  return normalizeKeyword(responseData?.data ?? responseData)
}

export const useKeywordStore = defineStore('keyword', () => {
  const list = ref<Keyword[]>([])
  const total = ref(0)
  const loading = ref(false)
  const moderationQueue = ref<ModerationItem[]>([])
  const moderationTotal = ref(0)

  const page = ref(1)
  const pageSize = ref(20)
  const params = ref<KeywordListParams>({})

  const fetchList = async (newParams?: KeywordListParams) => {
    loading.value = true
    try {
      if (newParams) {
        params.value = newParams
      }
      const res = await keywordsApi.list({
        page: page.value,
        pageSize: pageSize.value,
        ...params.value,
      })
      const payload = normalizeListPayload<Keyword>(res.data)
      list.value = payload.list.map(normalizeKeyword)
      total.value = payload.total
      return list.value
    } finally {
      loading.value = false
    }
  }

  const create = async (data: KeywordFormData) => {
    loading.value = true
    try {
      const res = await keywordsApi.create(data)
      await fetchList()
      return unwrapKeyword(res.data)
    } finally {
      loading.value = false
    }
  }

  const batchCreate = async (data: { words: string[]; type: KeywordType; matchMode: MatchMode }) => {
    loading.value = true
    try {
      const res = await keywordsApi.batchCreate(data)
      await fetchList()
      return res.data.data
    } finally {
      loading.value = false
    }
  }

  const update = async (id: number, data: Partial<KeywordFormData>) => {
    loading.value = true
    try {
      const res = await keywordsApi.update(id, data)
      const updatedKeyword = unwrapKeyword(res.data)
      const index = list.value.findIndex((item) => item.id === id)
      if (index !== -1) {
        list.value[index] = updatedKeyword
      }
      return updatedKeyword
    } finally {
      loading.value = false
    }
  }

  const remove = async (id: number) => {
    loading.value = true
    try {
      await keywordsApi.delete(id)
      list.value = list.value.filter((item) => item.id !== id)
      total.value--
    } finally {
      loading.value = false
    }
  }

  const fetchModerationQueue = async (params?: { page?: number; pageSize?: number }) => {
    loading.value = true
    try {
      const res = await keywordsApi.getModerationQueue(params)
      const payload = normalizeListPayload<any>(res.data)
      moderationQueue.value = payload.list.map(normalizeKeyword)
      moderationTotal.value = payload.total
      return moderationQueue.value
    } finally {
      loading.value = false
    }
  }

  const approveKeyword = async (id: number) => {
    loading.value = true
    try {
      const res = await keywordsApi.approveKeyword(id)
      const approvedKeyword = unwrapKeyword(res.data)
      moderationQueue.value = moderationQueue.value.filter((item) => item.id !== id)
      moderationTotal.value--
      return approvedKeyword
    } finally {
      loading.value = false
    }
  }

  const rejectKeyword = async (id: number) => {
    loading.value = true
    try {
      await keywordsApi.rejectKeyword(id)
      moderationQueue.value = moderationQueue.value.filter((item) => item.id !== id)
      moderationTotal.value--
    } finally {
      loading.value = false
    }
  }

  const generateByAI = async (data: { category: KeywordType; count: number }) => {
    loading.value = true
    try {
      const res = await keywordsApi.generateByAI(data)
      return res.data.data.words ?? res.data.data.keywords?.map((item) => item.text) ?? []
    } finally {
      loading.value = false
    }
  }

  const setPage = (newPage: number) => {
    page.value = newPage
  }

  const setPageSize = (newPageSize: number) => {
    pageSize.value = newPageSize
    page.value = 1
  }

  return {
    list,
    total,
    loading,
    moderationQueue,
    moderationTotal,
    page,
    pageSize,
    fetchList,
    create,
    batchCreate,
    update,
    remove,
    fetchModerationQueue,
    approveKeyword,
    rejectKeyword,
    generateByAI,
    setPage,
    setPageSize,
  }
})
