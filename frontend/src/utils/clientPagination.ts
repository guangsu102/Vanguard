import { computed, ref, watch, type Ref } from 'vue'

export function useClientPagination<T>(
  source: Readonly<Ref<readonly T[]>>,
  initialPageSize = 10,
) {
  const page = ref(1)
  const pageSize = ref(initialPageSize)
  const total = computed(() => source.value.length)
  const rows = computed(() => {
    const start = (page.value - 1) * pageSize.value
    return source.value.slice(start, start + pageSize.value)
  })

  const clampPage = () => {
    const lastPage = Math.max(1, Math.ceil(total.value / pageSize.value))
    if (page.value > lastPage) page.value = lastPage
  }

  watch(total, clampPage)
  watch(pageSize, () => {
    page.value = 1
  })

  return {
    page,
    pageSize,
    total,
    rows,
    reset: () => {
      page.value = 1
    },
  }
}
