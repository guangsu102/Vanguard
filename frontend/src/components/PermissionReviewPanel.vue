<script setup lang="ts">
import type { MembershipPermission } from '@/api/groupReviews'
defineProps<{ rows: MembershipPermission[] }>()
const labels: Record<string, string> = { pending: '待检查', approved: '可投放', rejected: '不可投放' }
function time(value?: string | null) {
  if (!value) return '—'
  const date = new Date(/Z$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN')
}
</script>

<template>
  <section>
    <p>发送成功即计一次成功，广告不再进行存活复核。连续群发送失败达到阈值后退群。</p>
    <el-table :data="rows" row-key="membership_id" empty-text="暂无成员权限记录">
      <el-table-column prop="account_id" label="账号" width="80" />
      <el-table-column prop="group_title" label="群组" min-width="160" />
      <el-table-column label="投放资格" width="110">
        <template #default="{ row }">{{ labels[row.review_status] || '待检查' }}</template>
      </el-table-column>
      <el-table-column label="验证" width="100">
        <template #default="{ row }">{{ row.verification_status === 'complete' ? '已完成' : '待确认' }}</template>
      </el-table-column>
      <el-table-column label="文字权限" width="100">
        <template #default="{ row }">{{ row.can_send_text === true ? '允许' : row.can_send_text === false ? '禁止' : '未知' }}</template>
      </el-table-column>
      <el-table-column label="最后发送" min-width="140"><template #default="{ row }">{{ ({ success: '成功', unknown: '结果未知', sending: '发送中', permission_changed: '群权限失败', failed: '失败', rate_limited: '等待限流解除', content_invalid: '素材错误', not_member: '已离群' } as Record<string, string>)[row.last_send_outcome] || '暂无' }}</template></el-table-column>
      <el-table-column prop="ad_failure_streak" label="连续群失败" width="110" />
      <el-table-column label="退群状态" min-width="140"><template #default="{ row }">{{ ({ left: '已离群', needs_attention: '退群待处理', pending: '待退群', none: '无' } as Record<string, string>)[row.leave_state] || '无' }} {{ row.leave_state !== 'none' ? (row.leave_reason || '') : '' }}</template></el-table-column>
      <el-table-column prop="last_send_error" label="发送原因" min-width="160" />
      <el-table-column prop="reason" label="当前原因" min-width="180" />
      <el-table-column label="检查时间" min-width="170"><template #default="{ row }">{{ time(row.checked_at) }}</template></el-table-column>
      <el-table-column label="下次检查" min-width="170"><template #default="{ row }">{{ time(row.next_review_at) }}</template></el-table-column>
      <el-table-column label="下次可投放" min-width="170"><template #default="{ row }">{{ time(row.next_ad_allowed_at) }}</template></el-table-column>
    </el-table>
  </section>
</template>
