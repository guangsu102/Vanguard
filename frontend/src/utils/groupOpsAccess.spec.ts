import { describe, expect, it } from "vitest";
import { GROUP_OPS_READ_ROLES, canReadGroupOps, parseSafePositiveId, parseSafeTelegramId } from "./groupOpsAccess";

describe("groupOpsAccess", () => {
  it("uses one exact read-role whitelist and denies unknown roles", () => {
    expect(GROUP_OPS_READ_ROLES).toEqual(["admin", "operator", "auditor"]);
    expect(GROUP_OPS_READ_ROLES.every(canReadGroupOps)).toBe(true);
    expect(canReadGroupOps("viewer")).toBe(false);
    expect(canReadGroupOps(null)).toBe(false);
  });

  it("rejects unsafe and ambiguous local identifiers", () => {
    expect(parseSafePositiveId("18")).toBe(18);
    expect(parseSafePositiveId("01")).toBeNull();
    expect(parseSafePositiveId("-18")).toBeNull();
    expect(parseSafePositiveId("9007199254740993")).toBeNull();
  });

  it("accepts signed safe Telegram chat identifiers only", () => {
    expect(parseSafeTelegramId("-1001234567890")).toBe(-1001234567890);
    expect(parseSafeTelegramId("123456")).toBe(123456);
    expect(parseSafeTelegramId("0")).toBeNull();
    expect(parseSafeTelegramId("9007199254740993")).toBeNull();
  });
});
