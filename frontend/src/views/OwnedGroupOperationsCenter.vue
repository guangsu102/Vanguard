<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { useRoute, useRouter } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { useOwnedGroupOperationsStore } from "@/stores/ownedGroupOperations";
import type { AuditQuery, MemberQuery, OperationQuery, OwnedGroupMember } from "@/api/ownedGroupOperations";
import { parseSafePositiveId, parseSafeTelegramId } from "@/utils/groupOpsAccess";
import OwnedGroupCapabilityCards from "@/components/ownedGroup/OwnedGroupCapabilityCards.vue";
import OwnedGroupMemberTable from "@/components/ownedGroup/OwnedGroupMemberTable.vue";
import OwnedGroupOperationTable from "@/components/ownedGroup/OwnedGroupOperationTable.vue";
import OwnedGroupAuditTable from "@/components/ownedGroup/OwnedGroupAuditTable.vue";

const route = useRoute();
const router = useRouter();
const authStore = useAuthStore();
const store = useOwnedGroupOperationsStore();
const tabs = ["overview", "members", "audit"] as const;
type TabName = (typeof tabs)[number];
const activeTab = ref<TabName>("overview");
const membersLoaded = ref(false);
const operationsLoaded = ref(false);
const auditLoaded = ref(false);
const sourceMember = ref<OwnedGroupMember | null>(null);

const assetId = computed(() => parseSafePositiveId(route.params.assetId));
const fatalStatus = computed(() => store.error.summary?.status ?? null);
const assetNotFound = computed(() => fatalStatus.value === 404 || !assetId.value);
const forbidden = computed(() => fatalStatus.value === 403);
const summary = computed(() => store.summary);

const normalizeTab = async (value: unknown) => {
  const raw = Array.isArray(value) ? value[0] : value;
  const next: TabName = typeof raw === "string" && tabs.includes(raw as TabName) ? raw as TabName : "overview";
  activeTab.value = next;
  if (raw !== next) await router.replace({ path: route.path, query: { ...route.query, tab: next } });
  return next;
};

const loadTab = async (tab = activeTab.value) => {
  const id = assetId.value;
  if (!id || forbidden.value || assetNotFound.value || !summary.value) return;
  if (tab === "members" && !membersLoaded.value) {
    try { const result = await store.fetchMembers(id); if (result && assetId.value === id) membersLoaded.value = true; } catch { /* channel-local error */ }
  }
  if (tab === "audit") {
    const requests: Promise<unknown>[] = [];
    if (!operationsLoaded.value) requests.push(store.fetchOperations(id).then((result) => { if (result && assetId.value === id) operationsLoaded.value = true; }).catch(() => undefined));
    if (!auditLoaded.value) requests.push(store.fetchAudit(id).then((result) => { if (result && assetId.value === id) auditLoaded.value = true; }).catch(() => undefined));
    await Promise.all(requests);
  }
};

const loadAsset = async () => {
  const id = assetId.value;
  membersLoaded.value = false; operationsLoaded.value = false; auditLoaded.value = false; sourceMember.value = null;
  if (!id) return;
  store.selectAsset(id);
  try {
    const result = await store.fetchSummary(id);
    if (result) await loadTab(activeTab.value);
  } catch { /* fatal state is rendered from the summary channel */ }
};

const setTab = async (tab: string | number) => {
  const next = tabs.includes(tab as TabName) ? tab as TabName : "overview";
  activeTab.value = next;
  await router.replace({ path: route.path, query: { ...route.query, tab: next } });
  await loadTab(next);
};

const retryOperations = async () => { const id = assetId.value; if (id) try { const result = await store.fetchOperations(id); if (result && assetId.value === id) operationsLoaded.value = true; } catch { /* local */ } };
const retryAudit = async () => { const id = assetId.value; if (id) try { const result = await store.fetchAudit(id); if (result && assetId.value === id) auditLoaded.value = true; } catch { /* local */ } };
const retryMembers = async () => { const id = assetId.value; if (id) try { const result = await store.fetchMembers(id); if (result && assetId.value === id) membersLoaded.value = true; } catch { /* local */ } };
const refresh = async () => {
  const id = assetId.value;
  if (!id) return;
  try { await store.fetchSummary(id); } catch { return; }
  if (activeTab.value === "members") await retryMembers();
  else if (activeTab.value === "audit") await Promise.all([retryOperations(), retryAudit()]);
};
const changeMemberQuery = async (query: MemberQuery) => { const id = assetId.value; if (id) try { const result = await store.fetchMembers(id, query); if (result && assetId.value === id) membersLoaded.value = true; } catch { /* local */ } };
const resetMembers = async () => { store.resetMemberFilters(); await retryMembers(); };
const changeOperationQuery = async (query: OperationQuery) => { const id = assetId.value; if (id) try { const result = await store.fetchOperations(id, query); if (result && assetId.value === id) operationsLoaded.value = true; } catch { /* local */ } };
const changeAuditQuery = async (query: AuditQuery) => { const id = assetId.value; if (id) try { const result = await store.fetchAudit(id, query); if (result && assetId.value === id) auditLoaded.value = true; } catch { /* local */ } };

const capabilityAction = async (key: string) => {
  if (!summary.value || !assetId.value) return;
  const asset = summary.value.asset;
  if (key === "orchestration") return router.push({ path: "/owned-groups", query: { assetId: String(assetId.value) } });
  if (key === "members" || key === "audit") return setTab(key);
  if (key === "messaging") return router.push(`/owned-groups/${assetId.value}/messaging`);
  if (key === "governance") {
    const groupId = parseSafeTelegramId(asset.telegram_chat_id);
    if (!groupId) return ElMessage.warning("Telegram Chat ID 不安全，已禁止打开治理深链");
    const botId = parseSafePositiveId(asset.guardian_bot_account_id);
    return router.push({ path: "/guardian/policies", query: { groupId: String(groupId), title: asset.title, botId: botId ? String(botId) : undefined, assetId: String(assetId.value) } });
  }
  if (key === "activities") return router.push({ path: "/guardian/groups", query: { assetId: String(assetId.value), action: "pinned-message" } });
  if (key === "campaign") {
    const groupId = parseSafeTelegramId(asset.telegram_chat_id);
    if (!groupId) return ElMessage.warning("Telegram Chat ID 不安全，已禁止打开活动深链");
    const botId = parseSafePositiveId(asset.guardian_bot_account_id);
    return router.push({ path: "/campaigns", query: { scope: "managed_group", groupId: String(groupId), title: asset.title, botId: botId ? String(botId) : undefined, assetId: String(assetId.value) } });
  }
};

const viewAccount = (row: OwnedGroupMember) => {
  const id = parseSafePositiveId(row.account_id); if (!id || !assetId.value) return;
  router.push({ path: "/accounts", query: { tab: "list", account_id: String(id), assetId: String(assetId.value) } });
};
const editPersona = (row: OwnedGroupMember) => {
  const id = parseSafePositiveId(row.account_id); if (!id || !assetId.value) return;
  router.push({ path: "/accounts", query: { tab: "list", persona_account_id: String(id), assetId: String(assetId.value) } });
};
const viewBot = (row: OwnedGroupMember) => {
  const id = parseSafePositiveId(row.guardian_bot_profile_id); if (!id || !assetId.value) return;
  router.push({ path: "/guardian/bots", query: { profileId: String(id), assetId: String(assetId.value) } });
};
const copyId = async (row: OwnedGroupMember) => {
  if (row.telegram_user_id === null && row.telegram_user_id_raw) {
    return ElMessage.warning("Telegram ID 超出浏览器安全整数范围，已禁止复制或请求");
  }
  const value = row.telegram_user_id ?? row.account_id ?? row.guardian_bot_profile_id;
  if (!Number.isSafeInteger(value) || Number(value) <= 0) return ElMessage.warning("ID 超出浏览器安全整数范围，已禁止复制或请求");
  try { await navigator.clipboard.writeText(String(value)); ElMessage.success("ID 已复制"); } catch { ElMessage.warning("浏览器未允许复制，请手动复制"); }
};

watch(() => route.query.tab, async (value) => { const tab = await normalizeTab(value); await loadTab(tab); }, { immediate: true });
watch(() => route.params.assetId, loadAsset, { immediate: true });
onBeforeUnmount(() => store.dispose());
</script>

<template>
  <div class="operations-center">
    <el-result v-if="forbidden" icon="warning" title="无权查看群运营中心" sub-title="当前角色不在 admin/operator/auditor 白名单中"><template #extra><el-button @click="router.push('/dashboard')">返回仪表盘</el-button></template></el-result>
    <el-result v-else-if="assetNotFound" icon="warning" title="群资产不存在" sub-title="请返回群资产总览重新选择"><template #extra><el-button type="primary" @click="router.push('/owned-groups')">返回群资产总览</el-button></template></el-result>
    <el-skeleton v-else-if="store.loading.summary && !summary" :rows="8" animated />
    <el-result v-else-if="store.error.summary && !summary" icon="error" title="运营详情读取失败" :sub-title="store.error.summary.message"><template #extra><el-button type="primary" @click="loadAsset">重试</el-button><el-button @click="router.push('/owned-groups')">返回群资产总览</el-button></template></el-result>
    <template v-else-if="summary">
      <el-breadcrumb separator="/"><el-breadcrumb-item>群运营中心</el-breadcrumb-item><el-breadcrumb-item :to="{ path: '/owned-groups', query: { assetId: String(summary.asset.asset_id) } }">群资产总览</el-breadcrumb-item><el-breadcrumb-item>{{ summary.asset.title }}</el-breadcrumb-item></el-breadcrumb>
      <div class="page-header"><div><h2>{{ summary.asset.title }}</h2><div class="tags"><el-tag>{{ summary.asset.asset_status }}</el-tag><el-tag type="info">Guardian: {{ summary.governance.status }}</el-tag></div></div><div><el-button :loading="store.loading.summary" @click="refresh">刷新</el-button><el-button @click="router.push({ path: '/owned-groups', query: { assetId: String(summary.asset.asset_id) } })">返回编排</el-button></div></div>
      <el-alert class="scope-alert" type="warning" :closable="false" show-icon title="成员数据仅包含系统编排成员和 Guardian 已观察成员，不代表 Telegram 全量实时成员名单。" />
      <div class="summary-grid"><el-card><span>已核验编排成员</span><strong>{{ summary.member_summary.managed_resource_count }}</strong></el-card><el-card><span>已观察真实用户</span><strong>{{ summary.member_summary.observed_real_user_count }}</strong></el-card><el-card><span>待修复身份</span><strong>{{ summary.member_summary.unresolved_count }}</strong></el-card><el-card><span>分类冲突</span><strong>{{ summary.member_summary.conflict_count }}</strong></el-card></div>
      <el-tabs :model-value="activeTab" @update:model-value="setTab">
        <el-tab-pane label="概览" name="overview"><OwnedGroupCapabilityCards :summary="summary" @action="capabilityAction" /></el-tab-pane>
        <el-tab-pane label="成员与角色" name="members"><el-result v-if="store.error.members" icon="error" title="成员读取失败" :sub-title="store.error.members.message"><template #extra><el-button type="primary" @click="retryMembers">局部重试</el-button></template></el-result><OwnedGroupMemberTable v-else :rows="store.members" :total="store.memberTotal" :query="store.memberQuery" :coverage="store.coverage || summary.member_summary.coverage" :permissions="summary.permissions" :role="authStore.userInfo?.role" :loading="store.loading.members" @query-change="changeMemberQuery" @reset="resetMembers" @retry="retryMembers" @view-account="viewAccount" @edit-persona="editPersona" @view-bot="viewBot" @copy-id="copyId" @view-sources="sourceMember = $event" /></el-tab-pane>
        <el-tab-pane label="任务与审计" name="audit"><el-card><template #header><strong>成员编排任务</strong></template><el-result v-if="store.error.operations" icon="error" title="任务读取失败" :sub-title="store.error.operations.message"><template #extra><el-button @click="retryOperations">局部重试</el-button></template></el-result><OwnedGroupOperationTable v-else :rows="store.operations" :total="store.operationTotal" :query="store.operationQuery" :loading="store.loading.operations" @query-change="changeOperationQuery" @retry="retryOperations" /></el-card><el-card class="audit-card"><template #header><strong>审计事件</strong></template><el-result v-if="store.error.audit" icon="error" title="审计读取失败" :sub-title="store.error.audit.message"><template #extra><el-button @click="retryAudit">局部重试</el-button></template></el-result><OwnedGroupAuditTable v-else :rows="store.auditEvents" :total="store.auditTotal" :query="store.auditQuery" :loading="store.loading.audit" @query-change="changeAuditQuery" @retry="retryAudit" /></el-card></el-tab-pane>
      </el-tabs>
      <el-drawer :model-value="Boolean(sourceMember)" title="成员来源" size="520px" @close="sourceMember = null"><template v-if="sourceMember"><el-descriptions :column="1" border><el-descriptions-item label="成员键">{{ sourceMember.member_key }}</el-descriptions-item><el-descriptions-item label="分类原因">{{ sourceMember.classification_reason }}</el-descriptions-item><el-descriptions-item label="质量码">{{ sourceMember.quality_codes.join('；') || '无' }}</el-descriptions-item></el-descriptions><el-table :data="sourceMember.sources" border><el-table-column prop="source" label="来源" /><el-table-column prop="record_id" label="记录 ID" /><el-table-column prop="observed_at" label="证据时间" /></el-table></template></el-drawer>
    </template>
  </div>
</template>

<style scoped>
.operations-center{display:flex;flex-direction:column;gap:18px}.page-header{display:flex;align-items:center;justify-content:space-between;gap:16px}.page-header h2{margin:0 0 8px}.tags{display:flex;gap:8px}.scope-alert{margin:0}.summary-grid{display:grid;grid-template-columns:repeat(4,minmax(160px,1fr));gap:14px}.summary-grid :deep(.el-card__body){display:flex;flex-direction:column;gap:10px}.summary-grid span{color:#606266}.summary-grid strong{font-size:28px}.audit-card{margin-top:18px}@media(max-width:900px){.summary-grid{grid-template-columns:repeat(2,1fr)}.page-header{align-items:flex-start;flex-direction:column}}
</style>
