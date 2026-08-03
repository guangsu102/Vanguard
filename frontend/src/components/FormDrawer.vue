<script setup lang="ts">
import { ElDrawer, ElForm, ElFormItem, ElInput, ElSelect, ElOption, ElButton, ElIcon, ElInputNumber, ElSwitch, ElDatePicker } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { ref, watch } from 'vue'
import { Close } from '@element-plus/icons-vue'

interface FormField {
  prop: string
  label: string
  type: 'input' | 'textarea' | 'select' | 'number' | 'switch' | 'date' | 'datetime'
  placeholder?: string
  options?: Array<{ label: string; value: any; disabled?: boolean }>
  rules?: FormRules
  props?: Record<string, any>
}

interface Props {
  visible: boolean
  title: string
  fields: FormField[]
  modelValue: Record<string, any>
  rules?: FormRules
  loading?: boolean
  width?: string
  labelWidth?: string
  footer?: boolean
}

interface Emits {
  (e: 'update:visible', val: boolean): void
  (e: 'confirm', val: Record<string, any>): void
  (e: 'update:modelValue', val: Record<string, any>): void
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
  width: '500px',
  labelWidth: '120px',
  footer: true,
})

const emit = defineEmits<Emits>()

const formRef = ref<FormInstance>()
const formData = ref<Record<string, any>>({ ...props.modelValue })

watch(
  () => props.visible,
  (val) => {
    if (val) {
      formData.value = { ...props.modelValue }
      formRef.value?.clearValidate()
    }
  }
)

watch(
  () => props.modelValue,
  (val) => {
    formData.value = { ...val }
  },
  { deep: true }
)

const handleClose = () => {
  emit('update:visible', false)
}

const handleConfirm = async () => {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
    const nextValue = { ...formData.value }
    Object.assign(props.modelValue, nextValue)
    emit('update:modelValue', nextValue)
    emit('confirm', nextValue)
  } catch {
    // validation failed
  }
}

const updateField = (prop: string, value: any) => {
  formData.value[prop] = value
  props.modelValue[prop] = value
  emit('update:modelValue', { ...formData.value })
}
</script>

<template>
  <el-drawer
    :model-value="visible"
    :title="title"
    :width="width"
    :before-close="handleClose"
    :close-on-click-modal="false"
    :show-close="false"
    class="form-drawer"
  >
    <template #header>
      <div class="drawer-header">
        <span class="drawer-title">{{ title }}</span>
        <el-icon class="close-icon" @click="handleClose"><Close /></el-icon>
      </div>
    </template>

    <el-form
      ref="formRef"
      :model="formData"
      :rules="rules"
      :label-width="labelWidth"
      class="drawer-form"
    >
      <slot name="before" />

      <el-form-item
        v-for="field in fields"
        :key="field.prop"
        :label="field.label"
        :prop="field.prop"
        :rules="field.rules"
      >
        <el-input
          v-if="field.type === 'input'"
          :model-value="formData[field.prop]"
          :placeholder="field.placeholder"
          v-bind="field.props"
            @update:model-value="updateField(field.prop, $event)"
        />

        <el-input
          v-else-if="field.type === 'textarea'"
          :model-value="formData[field.prop]"
          type="textarea"
          :placeholder="field.placeholder"
          :rows="4"
          v-bind="field.props"
            @update:model-value="updateField(field.prop, $event)"
        />

        <el-select
          v-else-if="field.type === 'select'"
          :model-value="formData[field.prop]"
          :placeholder="field.placeholder || `请选择${field.label}`"
          v-bind="field.props"
            @update:model-value="updateField(field.prop, $event)"
        >
          <el-option
            v-for="option in field.options"
            :key="option.value"
            :label="option.label"
            :value="option.value"
            :disabled="option.disabled"
          />
        </el-select>

        <el-input-number
          v-else-if="field.type === 'number'"
          :model-value="formData[field.prop]"
          v-bind="field.props"
            @update:model-value="updateField(field.prop, $event)"
        />

        <el-switch
          v-else-if="field.type === 'switch'"
          :model-value="formData[field.prop]"
          v-bind="field.props"
            @update:model-value="updateField(field.prop, $event)"
        />

        <el-date-picker
          v-else-if="field.type === 'date'"
          :model-value="formData[field.prop]"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="选择日期"
          v-bind="field.props"
            @update:model-value="updateField(field.prop, $event)"
        />

        <el-date-picker
          v-else-if="field.type === 'datetime'"
          :model-value="formData[field.prop]"
          type="datetime"
          value-format="YYYY-MM-DD HH:mm:ss"
          placeholder="选择日期时间"
          v-bind="field.props"
            @update:model-value="updateField(field.prop, $event)"
        />
      </el-form-item>

      <slot />
    </el-form>

    <template v-if="footer" #footer>
      <div class="drawer-footer">
        <el-button @click="handleClose">取消</el-button>
        <el-button type="primary" :loading="loading" @click="handleConfirm">确定</el-button>
      </div>
    </template>
  </el-drawer>
</template>

<style scoped lang="scss">
.form-drawer {
  :deep(.el-drawer__header) {
    margin-bottom: 0;
    padding: 16px 20px;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }

  :deep(.el-drawer__body) {
    padding: 20px;
  }

  :deep(.el-drawer__close-btn) {
    display: none;
  }
}

.drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.drawer-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}

.close-icon {
  cursor: pointer;
  font-size: 18px;
  color: #909399;

  &:hover {
    color: #409eff;
  }
}

.drawer-form {
  :deep(.el-form-item) {
    margin-bottom: 20px;
  }
}

.drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  padding-top: 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}
</style>
