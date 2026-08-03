<script setup lang="ts">
import { ElMessageBox } from 'element-plus'

interface Props {
  title?: string
  message: string
  confirmText?: string
  cancelText?: string
  confirmType?: 'primary' | 'danger' | 'warning'
}

interface Emits {
  (e: 'confirm'): void
  (e: 'cancel'): void
}

const props = withDefaults(defineProps<Props>(), {
  title: '提示',
  confirmText: '确定',
  cancelText: '取消',
  confirmType: 'primary',
})

const emit = defineEmits<Emits>()

const confirm = async () => {
  try {
    await ElMessageBox.confirm(props.message, props.title, {
      confirmButtonText: props.confirmText,
      cancelButtonText: props.cancelText,
      type: props.confirmType === 'danger' ? 'error' : props.confirmType === 'warning' ? 'warning' : 'info',
      confirmButtonClass: `el-button--${props.confirmType}`,
    })
    emit('confirm')
  } catch {
    emit('cancel')
  }
}

defineExpose({ confirm })
</script>

<template>
  <slot />
</template>
