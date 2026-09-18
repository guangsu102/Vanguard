import { describe, expect, it } from "vitest";
import { shallowMount } from "@vue/test-utils";
import OwnedGroupCapabilityCards from "./OwnedGroupCapabilityCards.vue";

const summary = { asset: { telegram_chat_id: -1001, managed_binding_id: 2 }, sections: Object.fromEntries(["orchestration", "governance", "members", "messaging", "activities", "audit"].map((key) => [key, { state: "ready", can_view: true, can_manage: key !== "governance" && key !== "activities", can_execute: key !== "governance" && key !== "activities", blocking_reasons: [] }])) } as any;

describe("OwnedGroupCapabilityCards", () => {
  it("disables write cards for a read-only role and explains why", () => {
    const wrapper = shallowMount(OwnedGroupCapabilityCards, { props: { summary }, global: { stubs: { "el-card": { template: "<section><slot name='header'/><slot /></section>" }, "el-tag": true, "el-tooltip": true, "el-button": true } } });
    expect((wrapper.vm as any).disabled("governance")).toBe(true);
    expect((wrapper.vm as any).disabled("activities")).toBe(true);
    expect((wrapper.vm as any).actionReasons("governance")).toContain("当前角色或业务门禁仅允许查看，不能执行配置操作");
    wrapper.unmount();
  });

  it("renders known backend blockers as Chinese explanations", () => {
    const blockers = [
      "asset_not_ready", "core_group_mapping_missing", "asset_needs_attention", "owned_group_execution_disabled",
      "governance_gate_backend_unavailable", "guardian_degraded", "governance_feature_disabled",
      "messaging_source_unavailable", "messaging_feature_disabled",
    ];
    const value = structuredClone(summary);
    value.sections.governance.blocking_reasons = blockers;
    const wrapper = shallowMount(OwnedGroupCapabilityCards, { props: { summary: value }, global: { stubs: { "el-card": { template: "<section><slot name='header'/><slot /></section>" }, "el-tag": true, "el-tooltip": true, "el-button": true } } });
    const explanations = (wrapper.vm as any).reasons("governance");
    expect(explanations).toHaveLength(blockers.length);
    expect(explanations.every((reason: string) => !reason.startsWith("未知阻断原因"))).toBe(true);
    wrapper.unmount();
  });
});
