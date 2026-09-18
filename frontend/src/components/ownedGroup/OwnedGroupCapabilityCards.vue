<script setup lang="ts">
import type { OperationsCenterSummary, OperationsCenterSection } from "@/api/ownedGroupOperations";

type CapabilityKey = "orchestration" | "governance" | "members" | "messaging" | "activities" | "audit";
type CapabilityAction = CapabilityKey | "campaign";
const props = defineProps<{ summary: OperationsCenterSummary }>();
const emit = defineEmits<{ action: [key: CapabilityAction] }>();

const cards: Array<{ key: CapabilityKey; title: string; description: string; action: string }> = [
  { key: "orchestration", title: "自建群编排", description: "管理群资产和系统编排成员", action: "返回编排" },
  { key: "governance", title: "Guardian 治理", description: "验证、反垃圾、处罚和治理策略", action: "配置策略" },
  { key: "members", title: "成员与角色", description: "系统成员和 Guardian 已观察成员", action: "查看成员" },
  { key: "messaging", title: "群内 AI/模板消息", description: "群内策略、审核和消息执行", action: "进入消息工作区" },
  { key: "activities", title: "公告与活动", description: "群公告、置顶消息和群内活动", action: "配置公告与活动" },
  { key: "audit", title: "任务与审计", description: "编排任务和操作审计记录", action: "查看任务与审计" },
];

const stateLabels: Record<string, string> = {
  ready: "可用", not_configured: "未配置", pending: "处理中", degraded: "已降级",
  disabled: "已禁用", stopped: "已停止", unavailable: "不可用",
};
const stateTypes: Record<string, "success" | "warning" | "danger" | "info"> = {
  ready: "success", pending: "warning", degraded: "warning", unavailable: "danger",
  disabled: "info", stopped: "warning", not_configured: "info",
};
const reasonLabels: Record<string, string> = {
  member_data_scope_partial: "成员数据仅覆盖系统编排成员和 Guardian 已观察成员",
  telegram_full_roster_not_loaded: "Telegram 全量成员名单不可用",
  guardian_not_managed: "尚未接入有效 Guardian 治理",
  governance_binding_missing: "Guardian 治理绑定已失效",
  asset_archived: "群资产已归档，仅可查看历史数据",
  messaging_runtime_disabled: "消息运行开关已关闭",
  messaging_disabled: "群内消息能力已关闭",
  messaging_dry_run: "消息能力处于演练模式",
  target_mapping_invalid: "群资产与治理目标映射不一致",
  telegram_chat_id_unsafe: "Telegram Chat ID 超出浏览器安全整数范围",
  governance_stop: "Guardian 治理已停止",
  governance_stop_enabled: "Guardian 治理运行停止门禁已开启",
  member_observation_paused: "成员观察已暂停，历史数据仍可查看",
  asset_not_ready: "群资产尚未就绪",
  core_group_mapping_missing: "尚未接入 Guardian 治理，群映射未建立",
  asset_needs_attention: "群资产需要人工处理",
  owned_group_execution_disabled: "自建群执行总开关已关闭",
  governance_gate_backend_unavailable: "Guardian 治理门禁服务不可用",
  guardian_degraded: "Guardian 运行状态已降级",
  governance_feature_disabled: "Guardian 治理功能已关闭",
  messaging_source_unavailable: "群内消息来源暂不可用",
  messaging_feature_disabled: "群内消息功能已关闭",
};

const section = (key: CapabilityKey): OperationsCenterSection => props.summary.sections[key];
const reasons = (key: CapabilityKey) => section(key).blocking_reasons.map((reason) => reasonLabels[reason] || `未知阻断原因：${reason}`);
const actionReasons = (key: CapabilityKey) => {
  const result = [...reasons(key)];
  if (["governance", "activities"].includes(key) && (!section(key).can_manage || !section(key).can_execute)) {
    result.push("当前角色或业务门禁仅允许查看，不能执行配置操作");
  }
  return [...new Set(result)];
};
const disabled = (key: CapabilityKey) => {
  const value = section(key);
  if (!value.can_view) return true;
  if (key === "governance") {
    return !value.can_manage || !value.can_execute || ["unavailable", "stopped"].includes(value.state) || !Number.isSafeInteger(props.summary.asset.telegram_chat_id);
  }
  if (key === "messaging") return value.state === "unavailable";
  if (key === "activities") return !value.can_manage || !value.can_execute || !props.summary.asset.managed_binding_id || ["unavailable", "stopped", "not_configured"].includes(value.state);
  return false;
};
</script>

<template>
  <div class="capability-grid">
    <el-card v-for="card in cards" :key="card.key" shadow="hover" class="capability-card">
      <template #header>
        <div class="card-header">
          <strong>{{ card.title }}</strong>
          <el-tag :type="stateTypes[section(card.key).state] || 'info'" size="small">
            {{ stateLabels[section(card.key).state] || section(card.key).state }}
          </el-tag>
        </div>
      </template>
      <p>{{ card.description }}</p>
      <div v-if="reasons(card.key).length" class="reasons">
        <div v-for="item in reasons(card.key)" :key="item">{{ item }}</div>
      </div>
      <div class="card-actions">
        <el-tooltip :disabled="!disabled(card.key) || !actionReasons(card.key).length" :content="actionReasons(card.key).join('；')">
          <span><el-button type="primary" plain :disabled="disabled(card.key)" @click="emit('action', card.key)">{{ card.action }}</el-button></span>
        </el-tooltip>
        <el-tooltip v-if="card.key === 'activities'" :disabled="!disabled(card.key) || !actionReasons(card.key).length" :content="actionReasons(card.key).join('；')">
          <span><el-button plain :disabled="disabled(card.key)" @click="emit('action', 'campaign')">配置活动</el-button></span>
        </el-tooltip>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.capability-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }
.card-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.capability-card p { color: #606266; min-height: 40px; margin: 0 0 12px; }
.reasons { color: #b88230; font-size: 12px; min-height: 34px; margin-bottom: 12px; }
.card-actions { display: flex; flex-wrap: wrap; gap: 8px; }
</style>
