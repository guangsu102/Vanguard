<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElTable, ElTableColumn, ElPagination, ElEmpty, ElCard } from 'element-plus'

interface Column<T = any> {
  prop?: string
  label: string
  width?: string | number
  minWidth?: string | number
  align?: string
  fixed?: string | boolean
  sortable?: boolean
  formatter?: (row: T, column: any, cellValue: any, $index: number) => string
  slot?: string
}

interface Props {
  columns: Column[]
  data: any[]
  total: number
  loading?: boolean
  page?: number
  pageSize?: number
  emptyText?: string
  stripe?: boolean
  border?: boolean
  selection?: boolean
  rowKey?: string
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
  page: 1,
  pageSize: 20,
  emptyText: '暂无数据',
  stripe: true,
  border: true,
  selection: false,
  rowKey: 'id',
})

const emit = defineEmits<{
  (e: 'page-change', page: number): void
  (e: 'page-size-change', pageSize: number): void
  (e: 'selection-change', rows: any[]): void
}>()

const currentPage = ref(props.page)
const currentPageSize = ref(props.pageSize)

watch(() => props.page, (val) => {
  currentPage.value = val
})

watch(() => props.pageSize, (val) => {
  currentPageSize.value = val
})

const handlePageChange = (page: number) => {
  emit('page-change', page)
}

const handlePageSizeChange = (pageSize: number) => {
  currentPageSize.value = pageSize
  emit('page-size-change', pageSize)
}

const handleSelectionChange = (selection: any[]) => {
  emit('selection-change', selection)
}

defineExpose({
  clearSelection: () => {},
})
</script>

<template>
  <el-card class="table-card" shadow="never" :body-style="{ padding: '0px' }">
    <el-table
      v-loading="loading"
      :data="data"
      :stripe="stripe"
      :border="border"
      :selection="selection"
      :row-key="rowKey"
      style="width: 100%"
      @selection-change="handleSelectionChange"
    >
      <el-table-column v-if="selection" type="selection" width="55" align="center" />

      <el-table-column
        v-for="column in columns"
        :key="column.prop || column.label"
        :prop="column.prop"
        :label="column.label"
        :width="column.width"
        :min-width="column.minWidth"
        :align="column.align || 'left'"
        :fixed="column.fixed"
        :sortable="column.sortable ? 'custom' : false"
        :formatter="column.formatter"
      >
        <template v-if="column.slot" #default="{ row }">
          <slot :name="column.slot" :row="row" />
        </template>
      </el-table-column>

      <template #empty>
        <el-empty :description="emptyText" />
      </template>
    </el-table>

    <div v-if="total > 0" class="pagination-wrapper">
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="currentPageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="total"
        layout="total, sizes, prev, pager, next, jumper"
        @current-change="handlePageChange"
        @size-change="handlePageSizeChange"
      />
    </div>
  </el-card>
</template>

<style scoped lang="scss">
.table-card {
  --el-card-padding: 0;

  :deep(.el-table) {
    .el-table__header th {
      background-color: #f5f7fa;
      color: #606266;
      font-weight: 600;
    }
  }
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  padding: 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}
</style>
