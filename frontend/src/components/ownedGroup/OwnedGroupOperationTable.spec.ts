import { describe, expect, it } from "vitest";
import { shallowMount } from "@vue/test-utils";
import OwnedGroupOperationTable from "./OwnedGroupOperationTable.vue";

describe("OwnedGroupOperationTable", () => {
  it("shows unknown response states without offering unknown as an API filter", () => {
    const wrapper = shallowMount(OwnedGroupOperationTable, {
      props: { rows: [], total: 0, query: { offset: 0, limit: 20 } },
      global: {
        directives: { loading: {} },
        stubs: {
          "el-select": true, "el-option": true, "el-button": true, "el-table": true,
          "el-table-column": true, "el-pagination": true, "el-dialog": true,
          "el-descriptions": true, "el-descriptions-item": true, "el-tag": true,
        },
      },
    });
    expect((wrapper.vm as any).statusLabels.unknown).toBe("未知");
    expect((wrapper.vm as any).filterStatuses).not.toContain("unknown");
    wrapper.unmount();
  });
});
