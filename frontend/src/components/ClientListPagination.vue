<script setup lang="ts">
withDefaults(
  defineProps<{
    page: number
    pageSize: number
    total: number
    pageSizes?: number[]
  }>(),
  {
    pageSizes: () => [10, 20, 50, 100],
  },
)

const emit = defineEmits<{
  (event: 'update:page', value: number): void
  (event: 'update:pageSize', value: number): void
}>()
</script>

<template>
  <div class="client-list-pagination">
    <el-pagination
      :current-page="page"
      :page-size="pageSize"
      :page-sizes="pageSizes"
      :total="total"
      background
      layout="total, sizes, prev, pager, next, jumper"
      @update:current-page="emit('update:page', $event)"
      @update:page-size="emit('update:pageSize', $event)"
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
