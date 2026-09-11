import { beforeEach, describe, expect, it, vi } from "vitest";
const get = vi.hoisted(() => vi.fn());
vi.mock("./client", () => ({ default: { get } }));
import { guardianApi } from "./guardian";

describe("guardian API", () => {
  beforeEach(() => get.mockReset());
  it("returns the unwrapped GuardianBot detail DTO", async () => {
    const dto = { id: 9, account_id: 21, identifier: "guardian", account_type: "guardian_bot", status: "online", is_active: true, health_status: "healthy", sync_status: "synced", enabled: true, created_at: "x", updated_at: "y" };
    get.mockResolvedValue({ data: dto });
    await expect(guardianApi.getBot(9)).resolves.toEqual(dto);
    expect(get).toHaveBeenCalledWith("/guardian-bots/9");
  });
});
