<script setup lang="ts">
import { computed } from "vue";
import type { MemberQuery, OperationsPermissions, OwnedGroupMember, Coverage } from "@/api/ownedGroupOperations";

const props = defineProps<{
  rows: OwnedGroupMember[];
  total: number;
  query: MemberQuery;
  coverage: Coverage | null;
  permissions: OperationsPermissions;
  role?: string | null;
  loading?: boolean;
}>();
const emit = defineEmits<{
  queryChange: [query: MemberQuery]; reset: []; retry: [];
  viewAccount: [row: OwnedGroupMember]; editPersona: [row: OwnedGroupMember]; viewBot: [row: OwnedGroupMember];
  copyId: [row: OwnedGroupMember]; viewSources: [row: OwnedGroupMember];
}>();

const localQuery = computed({ get: () => props.query, set: (value) => emit("queryChange", value) });
const update = (patch: Partial<MemberQuery>) => emit("queryChange", { ...props.query, ...patch, offset: patch.offset ?? 0 });
const kindLabel = (row: OwnedGroupMember) => row.classification_status !== "resolved" || !row.member_kind
  ? "待修复" : ({ real_user: "真实用户", system_ad_account: "系统广告账号", system_bot: "系统机器人" }[row.member_kind]);
const modeLabel = (mode: string | null) => mode === "growth" ? "增长综合" : mode === "ad_only" ? "仅外部广告" : "-";
const roleLabel = (role: string) => ({ owner: "群主", administrator: "管理员", member: "成员", unknown: "未知" }[role] || role);
const presenceLabel = (row: OwnedGroupMember) => {
  if (row.presence_confidence === "observed" && row.presence_status === "present") return "最近观察为在群";
  if (row.presence_confidence === "observed" && row.presence_status === "left") return "最近观察为离群";
  if (row.presence_confidence === "verified" && row.presence_status === "present") return "已核验在群";
  return ({ present: "记录为在群", pending: "待加入", left: "已离群", failed: "加入失败", unknown: "未知" }[row.presence_status]);
};
const qualityLabel = (row: OwnedGroupMember) => row.data_quality === "reliable" ? "可靠" : row.data_quality === "conflict" ? "冲突" : "部分可信";
const canAdminAction = computed(() => props.role === "admin");
const canEditPersona = (row: OwnedGroupMember) => canAdminAction.value && props.permissions.manage_persona && row.member_kind === "system_ad_account" && row.classification_status === "resolved" && row.persona_configured !== null;
const unsafeTelegramId = (row: OwnedGroupMember) => row.telegram_user_id === null && Boolean(row.telegram_user_id_raw);
const copyIdDisabled = (row: OwnedGroupMember) => unsafeTelegramId(row)
  || !Number.isSafeInteger(row.telegram_user_id ?? row.account_id ?? row.guardian_bot_profile_id)
  || Number(row.telegram_user_id ?? row.account_id ?? row.guardian_bot_profile_id) <= 0;
const copyIdReason = (row: OwnedGroupMember) => unsafeTelegramId(row)
  ? "Telegram ID 超出浏览器安全整数范围，已禁止复制或请求"
  : "没有可安全复制的 ID";
const recentEvidence = (row: OwnedGroupMember) => row.last_observed_at || row.last_verified_at || row.joined_at || "—";
const asMember = (value: unknown) => value as OwnedGroupMember;
const riskLabel = (row: OwnedGroupMember) => row.member_kind === "system_bot"
  ? `健康 ${row.bot_health_status || "未知"} / 同步 ${row.bot_sync_status || "未知"}`
  : row.member_kind === "system_ad_account" ? `账号风险：${row.account_risk_level || "未知"}`
    : row.risk_scope === "user_global" ? `用户全局：${row.user_state || "未知"}${row.warning_count === null ? "" : `，警告 ${row.warning_count}`}` : "—";
const hasFilters = computed(() => Boolean(props.query.q || props.query.member_kind || props.query.presence_status || props.query.classification_status || props.query.telegram_role));
const emptyText = computed(() => {
  if (hasFilters.value) return "当前筛选无结果";
  if (props.coverage?.coverage_status === "not_started") return "尚未开始观察，暂无已观察真实用户";
  return "暂无系统编排成员或 Guardian 已观察成员";
});
</script>

<template>
  <section>
    <el-alert type="warning" :closable="false" show-icon title="成员数据仅包含系统编排成员和 Guardian 已观察成员，不代表 Telegram 全量实时成员名单。" />
    <div v-if="coverage" class="coverage-meta">覆盖状态：{{ coverage.coverage_status }}；最近观察：{{ coverage.last_observed_at || '尚未开始观察' }}</div>
    <el-form inline class="filters">
      <el-form-item label="搜索"><el-input :model-value="localQuery.q" clearable placeholder="名称、@用户名、Telegram ID、account:id" @update:model-value="update({ q: $event || undefined })" /></el-form-item>
      <el-form-item label="成员类型"><el-select :model-value="localQuery.member_kind" clearable @update:model-value="update({ member_kind: $event || undefined })"><el-option label="真实用户" value="real_user" /><el-option label="系统广告账号" value="system_ad_account" /><el-option label="系统机器人" value="system_bot" /></el-select></el-form-item>
      <el-form-item label="存在状态"><el-select :model-value="localQuery.presence_status" clearable @update:model-value="update({ presence_status: $event || undefined })"><el-option label="在群证据" value="present" /><el-option label="待处理" value="pending" /><el-option label="离群" value="left" /><el-option label="失败" value="failed" /><el-option label="未知" value="unknown" /></el-select></el-form-item>
      <el-form-item label="群内角色"><el-select :model-value="localQuery.telegram_role" clearable @update:model-value="update({ telegram_role: $event || undefined })"><el-option label="群主" value="owner" /><el-option label="管理员" value="administrator" /><el-option label="成员" value="member" /><el-option label="未知" value="unknown" /></el-select></el-form-item>
      <el-form-item label="分类质量"><el-select :model-value="localQuery.classification_status" clearable @update:model-value="update({ classification_status: $event || undefined })"><el-option label="已解析" value="resolved" /><el-option label="待修复" value="unresolved" /><el-option label="冲突" value="conflict" /></el-select></el-form-item>
      <el-form-item><el-button @click="emit('reset')">重置</el-button><el-button @click="emit('retry')">刷新</el-button></el-form-item>
    </el-form>
    <el-table v-loading="Boolean(loading)" :data="rows" border :empty-text="emptyText">
      <el-table-column label="成员" min-width="220"><template #default="{ row }"><strong>{{ row.display_name || '未命名成员' }}</strong><div v-if="row.username">@{{ row.username }}</div><small>{{ row.telegram_user_id ?? row.telegram_user_id_raw ?? '无 Telegram ID' }}</small></template></el-table-column>
      <el-table-column label="分类" width="140"><template #default="{ row }"><el-tag :type="row.classification_status === 'conflict' ? 'danger' : row.classification_status === 'unresolved' ? 'warning' : 'success'">{{ kindLabel(asMember(row)) }}</el-tag></template></el-table-column>
      <el-table-column label="账号模式" width="110"><template #default="{ row }">{{ modeLabel(row.operation_mode) }}</template></el-table-column>
      <el-table-column label="群内角色" width="100"><template #default="{ row }">{{ roleLabel(row.telegram_role) }}</template></el-table-column>
      <el-table-column label="存在状态" min-width="150"><template #default="{ row }">{{ presenceLabel(asMember(row)) }}<div class="muted">{{ row.presence_confidence }}</div></template></el-table-column>
      <el-table-column label="风控/健康" min-width="170"><template #default="{ row }">{{ riskLabel(asMember(row)) }}</template></el-table-column>
      <el-table-column label="最近证据" min-width="170"><template #default="{ row }">{{ recentEvidence(asMember(row)) }}</template></el-table-column>
      <el-table-column label="数据质量" width="130"><template #default="{ row }"><el-tooltip :content="row.quality_codes.length ? row.quality_codes.join('；') : row.classification_reason"><el-tag :type="row.data_quality === 'conflict' ? 'danger' : row.data_quality === 'partial' ? 'warning' : 'success'">{{ qualityLabel(asMember(row)) }}</el-tag></el-tooltip></template></el-table-column>
      <el-table-column label="操作" width="250" fixed="right"><template #default="{ row }">
        <template v-if="row.classification_status === 'resolved'">
          <el-button v-if="canAdminAction && row.member_kind === 'system_ad_account' && row.account_id" link type="primary" @click="emit('viewAccount', asMember(row))">查看账号</el-button>
          <el-button v-if="canEditPersona(asMember(row))" link type="primary" @click="emit('editPersona', asMember(row))">编辑 Persona</el-button>
          <el-button v-if="canAdminAction && row.member_kind === 'system_bot' && row.guardian_bot_profile_id" link type="primary" @click="emit('viewBot', asMember(row))">查看 Bot</el-button>
        </template>
        <el-tooltip :disabled="!copyIdDisabled(asMember(row))" :content="copyIdReason(asMember(row))">
          <span><el-button link :disabled="copyIdDisabled(asMember(row))" @click="emit('copyId', asMember(row))">复制 ID</el-button></span>
        </el-tooltip><el-button link @click="emit('viewSources', asMember(row))">查看来源</el-button>
      </template></el-table-column>
    </el-table>
    <el-pagination background layout="total, sizes, prev, pager, next" :total="total" :page-size="query.limit" :current-page="Math.floor(query.offset / query.limit) + 1" :page-sizes="[20, 50, 100]" @current-change="update({ offset: ($event - 1) * query.limit })" @size-change="update({ limit: $event, offset: 0 })" />
  </section>
</template>

<style scoped>
.coverage-meta { color: #909399; margin: 10px 0; font-size: 13px; }.filters { margin-top: 18px; }.muted { color: #909399; font-size: 12px; }.el-pagination { margin-top: 16px; justify-content: flex-end; }
</style>
