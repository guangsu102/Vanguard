<script setup lang="ts">
import { computed } from 'vue'

interface Props {
  status: string
  type?: 'account' | 'proxy' | 'group' | 'user' | 'campaign' | 'rule' | 'keyword' | 'custom'
  size?: 'small' | 'default' | 'large'
}

const props = withDefaults(defineProps<Props>(), {
  type: 'custom',
  size: 'default',
})

const statusConfig: Record<string, { label: string; type: string }> = {
  // Account status
  online: { label: '在线', type: 'success' },
  offline: { label: '离线', type: 'info' },
  working: { label: '工作中', type: 'success' },
  idle: { label: '空闲', type: 'warning' },
  banned: { label: '封禁', type: 'danger' },
  account_banned: { label: '封禁', type: 'danger' },
  suspended: { label: '暂停', type: 'warning' },

  // Proxy status
  active: { label: '正常', type: 'success' },
  proxy_inactive: { label: '停用', type: 'info' },
  error: { label: '异常', type: 'danger' },

  // Group status
  pending_join: { label: '待入群', type: 'warning' },
  join_failed: { label: '入群失败', type: 'danger' },
  cooling_down: { label: '冷却中', type: 'warning' },
  left: { label: '已离开', type: 'warning' },
  rejected: { label: '已拒绝', type: 'danger' },
  unrated: { label: '未评级', type: 'info' },

  // User status
  muted: { label: '已禁言', type: 'warning' },

  // Campaign status
  pending: { label: '未开始', type: 'info' },
  paused: { label: '已暂停', type: 'warning' },
  ended: { label: '已结束', type: 'info' },
  cancelled: { label: '已取消', type: 'danger' },

  // Rule status
  inactive: { label: '停用', type: 'info' },

  // Keyword type
  whitelist: { label: '白名单', type: 'success' },
  blacklisted: { label: '黑名单', type: 'danger' },
  approved: { label: '已启用', type: 'success' },
  discarded: { label: '已废弃', type: 'info' },

  // General
  success: { label: '成功', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  processing: { label: '处理中', type: 'warning' },
}

const config = computed(() => {
  if (props.type === 'group' && props.status === 'pending') {
    return { label: '等待审核', type: 'warning' }
  }
  if (props.type === 'keyword' && props.status === 'pending') {
    return { label: '待审核', type: 'warning' }
  }
  return statusConfig[props.status] || { label: props.status, type: 'info' }
})
</script>

<template>
  <el-tag :type="config.type as any" :size="size" effect="light">
    {{ config.label }}
  </el-tag>
</template>
