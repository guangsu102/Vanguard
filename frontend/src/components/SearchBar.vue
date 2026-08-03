<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElInput, ElSelect, ElOption, ElDatePicker, ElButton, ElCard } from 'element-plus'
import type { FormInstance } from 'element-plus'

interface FilterItem {
  type: 'input' | 'select' | 'date-range'
  key: string
  label: string
  placeholder?: string
  options?: Array<{ label: string; value: any }>
  width?: string | number
  clearable?: boolean
  defaultValue?: any
}

interface Props {
  filters: FilterItem[]
  loading?: boolean
  searchText?: string
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
  searchText: '搜索',
})

const emit = defineEmits<{
  (e: 'search', values: Record<string, any>): void
  (e: 'reset'): void
}>()

const formRef = ref<FormInstance>()
const formData = ref<Record<string, any>>({})

watch(
  () => props.filters,
  (newFilters) => {
    newFilters.forEach((filter) => {
      if (filter.type === 'date-range' && filter.defaultValue) {
        formData.value[filter.key] = filter.defaultValue
      }
    })
  },
  { immediate: true }
)

const handleSearch = () => {
  const values: Record<string, any> = {}
  Object.keys(formData.value).forEach((key) => {
    const value = formData.value[key]
    if (value !== undefined && value !== '' && value !== null) {
      values[key] = value
    }
  })
  emit('search', values)
}

const handleReset = () => {
  formData.value = {}
  formRef.value?.resetFields()
  emit('reset')
}
</script>

<template>
  <el-card class="search-bar" shadow="never" :body-style="{ padding: '16px' }">
    <el-form ref="formRef" :model="formData" :inline="true" class="search-form">
      <el-form-item
        v-for="filter in filters"
        :key="filter.key"
        :label="filter.label"
        :prop="filter.key"
        class="search-item"
      >
        <el-input
          v-if="filter.type === 'input'"
          v-model="formData[filter.key]"
          :placeholder="filter.placeholder || `请输入${filter.label}`"
          :style="{ width: filter.width || '180px' }"
          :clearable="filter.clearable !== false"
          @keyup.enter="handleSearch"
        />

        <el-select
          v-else-if="filter.type === 'select'"
          v-model="formData[filter.key]"
          :placeholder="filter.placeholder || `请选择${filter.label}`"
          :style="{ width: filter.width || '150px' }"
          :clearable="filter.clearable !== false"
        >
          <el-option
            v-for="option in filter.options"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>

        <el-date-picker
          v-else-if="filter.type === 'date-range'"
          v-model="formData[filter.key]"
          type="datetimerange"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          :style="{ width: filter.width || '320px' }"
          value-format="YYYY-MM-DD HH:mm:ss"
        />
      </el-form-item>

      <el-form-item class="search-actions">
        <el-button type="primary" :loading="loading" @click="handleSearch">
          <el-icon v-if="!loading"><Search /></el-icon>
          {{ searchText }}
        </el-button>
        <el-button @click="handleReset">重置</el-button>
      </el-form-item>
    </el-form>
  </el-card>
</template>

<script lang="ts">
import { Search } from '@element-plus/icons-vue'
export default {
  components: { Search },
}
</script>

<style scoped lang="scss">
.search-bar {
  margin-bottom: 16px;
}

.search-form {
  :deep(.el-form-item) {
    margin-bottom: 0;
    margin-right: 16px;
  }

  :deep(.el-form-item__label) {
    font-weight: 500;
  }
}

.search-actions {
  :deep(.el-form-item__content) {
    justify-content: flex-end;
  }
}
</style>
