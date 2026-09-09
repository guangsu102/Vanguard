<script setup lang="ts">
import { computed } from 'vue'
import { normalizePageSize, normalizePageSizeOptions, PAGE_SIZE_OPTIONS } from '@/utils/pagination'

const props = withDefaults(
  defineProps<{
    page: number
    pageSize: number
    total: number
    pageSizes?: number[]
  }>(),
  {
    pageSizes: () => [...PAGE_SIZE_OPTIONS],
  },
)

const emit = defineEmits<{
  (event: 'update:page', value: number): void
  (event: 'update:pageSize', value: number): void
}>()

const normalizedPageSizes = computed(() => normalizePageSizeOptions(props.pageSizes))
</script>

<template>
  <div class="client-list-pagination">
    <el-pagination
      :current-page="page"
      :page-size="pageSize"
      :page-sizes="normalizedPageSizes"
      :total="total"
      background
      layout="total, sizes, prev, pager, next, jumper"
      @update:current-page="emit('update:page', $event)"
      @update:page-size="emit('update:pageSize', normalizePageSize($event))"
    />
  </div>
</template>

<style scoped>
.client-list-pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
  overflow-x: auto;
}

@media (max-width: 768px) {
  .client-list-pagination {
    justify-content: flex-start;
  }
}
</style>
