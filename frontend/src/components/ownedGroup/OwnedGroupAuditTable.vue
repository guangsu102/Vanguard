<script setup lang="ts">
import { ref } from "vue";
import { ElMessage } from "element-plus";
import type { AuditEventItem, AuditQuery } from "@/api/ownedGroupOperations";
const props = defineProps<{ rows: AuditEventItem[]; total: number; query: AuditQuery; loading?: boolean }>();
const emit = defineEmits<{ queryChange: [query: AuditQuery]; retry: [] }>();
const detail = ref<AuditEventItem | null>(null);
const range = ref<[string, string] | null>(null);
const change = (patch: Partial<AuditQuery>) => emit("queryChange", { ...props.query, ...patch, offset: patch.offset ?? 0 });
const applyRange = (value: [string, string] | null) => { range.value = value; change({ created_after: value?.[0], created_before: value?.[1] }); };
const renderState = (value: string | null) => { if (value === null) return "无"; if (value === "") return "—"; try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; } };
const resource = (row: AuditEventItem) => row.resource_type && row.resource_id !== null ? `${row.resource_type}#${row.resource_id}` : row.resource_type || (row.resource_id === null ? "非资源事件" : String(row.resource_id));
const asAudit = (value: unknown) => value as AuditEventItem;
const copy = async (value: string) => {
  try { await navigator.clipboard.writeText(value); ElMessage.success("链路 ID 已复制"); }
  catch { ElMessage.warning("浏览器未允许复制，请手动复制"); }
};
</script>
<template>
  <section>
    <el-form inline class="filters"><el-form-item label="事件"><el-input :model-value="query.event_type" clearable @update:model-value="change({ event_type: $event || undefined })" /></el-form-item><el-form-item label="结果"><el-input :model-value="query.result" clearable @update:model-value="change({ result: $event || undefined })" /></el-form-item><el-form-item label="资源类型"><el-input :model-value="query.resource_type" clearable @update:model-value="change({ resource_type: $event || undefined })" /></el-form-item><el-form-item label="时间"><el-date-picker :model-value="range" type="datetimerange" value-format="YYYY-MM-DDTHH:mm:ss[Z]" @update:model-value="applyRange" /></el-form-item><el-form-item><el-button @click="emit('retry')">刷新</el-button></el-form-item></el-form>
    <el-table v-loading="Boolean(loading)" :data="rows" border><el-table-column prop="created_at" label="时间" min-width="170" /><el-table-column prop="event_type" label="事件" min-width="150" /><el-table-column label="结果" width="120"><template #default="{ row }"><el-tag type="info">{{ row.result }}</el-tag></template></el-table-column><el-table-column label="对象" min-width="150"><template #default="{ row }">{{ resource(asAudit(row)) }}</template></el-table-column><el-table-column label="操作者" width="100"><template #default="{ row }">{{ row.actor_id ?? '系统' }}</template></el-table-column><el-table-column label="原因" min-width="140"><template #default="{ row }">{{ row.reason_code || '—' }}</template></el-table-column><el-table-column label="关联任务" min-width="140"><template #default="{ row }">{{ row.operation_id ? `#${row.operation_id}` : '—' }}{{ row.operation_item_id ? ` / item #${row.operation_item_id}` : '' }}</template></el-table-column><el-table-column label="链路" min-width="190"><template #default="{ row }">{{ row.correlation_id || '—' }}<el-button v-if="row.correlation_id" link @click="copy(row.correlation_id)">复制</el-button></template></el-table-column><el-table-column label="详情" width="90"><template #default="{ row }"><el-button link @click="detail = asAudit(row)">查看</el-button></template></el-table-column></el-table>
    <el-pagination background layout="total, sizes, prev, pager, next" :total="total" :page-size="query.limit" :current-page="Math.floor(query.offset / query.limit) + 1" :page-sizes="[20, 50, 100]" @current-change="change({ offset: ($event - 1) * query.limit })" @size-change="change({ limit: $event, offset: 0 })" />
    <el-dialog :model-value="Boolean(detail)" title="审计详情" width="720px" @close="detail = null"><template v-if="detail"><h4>before_state</h4><pre>{{ renderState(detail.before_state) }}</pre><h4>after_state</h4><pre>{{ renderState(detail.after_state) }}</pre></template></el-dialog>
  </section>
</template>
<style scoped>.filters{margin-bottom:8px}.el-pagination{margin-top:16px;justify-content:flex-end}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:12px;border-radius:6px;max-height:260px;overflow:auto}</style>
