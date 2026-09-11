import { describe, expect, it } from "vitest";
import { shallowMount } from "@vue/test-utils";
import OwnedGroupMemberTable from "./OwnedGroupMemberTable.vue";

const base = { rows: [], total: 0, permissions: { manage_orchestration: false, manage_governance: false, manage_messaging: false, manage_persona: false, manage_activities: false, view_members: true, view_audit: true }, role: "auditor" };
const mountTable = (query: Record<string, unknown>, status = "not_started") => shallowMount(OwnedGroupMemberTable, { props: { ...base, query: { offset: 0, limit: 50, ...query }, coverage: { data_scope: "managed_and_observed", is_complete: false, full_roster_supported: false, coverage_status: status, observation_started_at: null, last_observed_at: null, blocking_reasons: [] } as any }, global: { directives: { loading: {} }, stubs: { "el-alert": true, "el-form": true, "el-form-item": true, "el-input": true, "el-select": true, "el-option": true, "el-button": true, "el-table": true, "el-table-column": true, "el-pagination": true, "el-tooltip": true, "el-tag": true } } });

describe("OwnedGroupMemberTable empty states", () => {
  it("distinguishes observation not started from a filtered empty page", () => {
    const empty = mountTable({});
    expect((empty.vm as any).emptyText).toContain("尚未开始观察");
    empty.unmount();
    const filtered = mountTable({ q: "nobody" });
    expect((filtered.vm as any).emptyText).toBe("当前筛选无结果");
    filtered.unmount();
  });

  it("disables copy when an unsafe Telegram ID has a safe internal account ID", () => {
    const wrapper = mountTable({});
    const row = { telegram_user_id: null, telegram_user_id_raw: "9007199254740993", account_id: 21, guardian_bot_profile_id: null };
    expect((wrapper.vm as any).copyIdDisabled(row)).toBe(true);
    expect((wrapper.vm as any).copyIdReason(row)).toContain("Telegram ID 超出");
    wrapper.unmount();
  });
});
