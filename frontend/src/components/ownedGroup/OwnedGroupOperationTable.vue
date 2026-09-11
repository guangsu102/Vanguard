<script setup lang="ts">
import { ref } from "vue";
import { ElMessage } from "element-plus";
import type { OperationListItem, OperationQuery } from "@/api/ownedGroupOperations";
const props = defineProps<{ rows: OperationListItem[]; total: number; query: OperationQuery; loading?: boolean }>();
const emit = defineEmits<{ queryChange: [query: OperationQuery]; retry: [] }>();
const detail = ref<OperationListItem | null>(null);
const statusLabels: Record<string, string> = { draft: "草稿", queued: "排队中", running: "执行中", paused: "已暂停", stopping: "停止中", stopped: "已停止", completed: "已完成", partial_completed: "部分完成", failed: "失败", unknown: "未知" };
const filterStatuses = ["draft", "queued", "running", "paused", "stopping", "stopped", "completed", "partial_completed", "failed"] as const;
const change = (patch: Partial<OperationQuery>) => emit("queryChange", { ...props.query, ...patch, offset: patch.offset ?? 0 });
const count = (value: number | null) => value === null ? "—" : String(value);
const asOperation = (value: unknown) => value as OperationListItem;
const copy = async (value: string) => {
  if (!value) return;
  try { await navigator.clipboard.writeText(value); ElMessage.success("已复制"); }
  catch { ElMessage.warning("浏览器未允许复制，请手动复制"); }
};
</script>
<template>
  <section>
    <div class="toolbar"><el-select :model-value="query.status" clearable placeholder="全部状态" @update:model-value="change({ status: $event || undefined })"><el-option v-for="value in filterStatuses" :key="value" :label="statusLabels[value]" :value="value" /></el-select><el-button @click="emit('retry')">刷新</el-button></div>
    <el-table v-loading="Boolean(loading)" :data="rows" border>
      <el-table-column label="任务 ID" width="100"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
      <el-table-column label="状态" width="130"><template #default="{ row }"><el-tag :type="row.status === 'failed' ? 'danger' : row.status === 'completed' ? 'success' : 'info'">{{ row.status === 'unknown' ? row.raw_status : statusLabels[row.status] }}</el-tag></template></el-table-column>
      <el-table-column label="进度" min-width="180"><template #default="{ row }"><strong>{{ count(row.completed_count) }}/{{ count(row.planned_count) }}</strong><div class="muted">跳过 {{ count(row.skipped_count) }} · 失败 {{ count(row.failed_count) }}</div></template></el-table-column>
      <el-table-column label="计划时间" min-width="170"><template #default="{ row }">{{ row.schedule_at || '立即执行' }}</template></el-table-column>
      <el-table-column prop="created_at" label="创建时间" min-width="170" /><el-table-column prop="updated_at" label="更新时间" min-width="170" />
      <el-table-column label="技术信息" width="100"><template #default="{ row }"><el-button link @click="detail = asOperation(row)">查看</el-button></template></el-table-column>
    </el-table>
    <el-pagination background layout="total, sizes, prev, pager, next" :total="total" :page-size="query.limit" :current-page="Math.floor(query.offset / query.limit) + 1" :page-sizes="[20, 50, 100]" @current-change="change({ offset: ($event - 1) * query.limit })" @size-change="change({ limit: $event, offset: 0 })" />
    <el-dialog :model-value="Boolean(detail)" title="任务技术信息" width="620px" @close="detail = null"><el-descriptions v-if="detail" :column="1" border><el-descriptions-item label="selection hash"><code>{{ detail.selection_snapshot_hash || '—' }}</code><el-button v-if="detail.selection_snapshot_hash" link @click="copy(detail.selection_snapshot_hash)">复制</el-button></el-descriptions-item><el-descriptions-item label="config hash"><code>{{ detail.config_snapshot_hash || '—' }}</code><el-button v-if="detail.config_snapshot_hash" link @click="copy(detail.config_snapshot_hash)">复制</el-button></el-descriptions-item><el-descriptions-item label="idempotency key"><code>{{ detail.idempotency_key || '—' }}</code><el-button v-if="detail.idempotency_key" link @click="copy(detail.idempotency_key)">复制</el-button></el-descriptions-item></el-descriptions></el-dialog>
  </section>
</template>
<style scoped>.toolbar{display:flex;gap:12px;margin-bottom:12px}.muted{font-size:12px;color:#909399}.el-pagination{margin-top:16px;justify-content:flex-end}code{overflow-wrap:anywhere}</style>
