import { describe, expect, it } from "vitest";
import { parseQQGroupNumbers } from "./qqAutomation";

describe("QQ target group import", () => {
  it("accepts pasted lists and deduplicates group numbers", () => {
    expect(
      parseQQGroupNumbers("123456789\n987654321，123456789;55555"),
    ).toEqual(["123456789", "987654321", "55555"]);
  });

  it("rejects malformed entries instead of silently skipping target groups", () => {
    expect(() => parseQQGroupNumbers("123456789\nhttps://example.com")).toThrow(
      "群号格式错误",
    );
    expect(() => parseQQGroupNumbers("1234")).toThrow("群号格式错误");
  });

  it("rejects target batches exceeding the server limit", () => {
    const groups = Array.from({ length: 501 }, (_, index) =>
      String(100000 + index),
    );
    expect(() => parseQQGroupNumbers(groups.join("\n"))).toThrow("500");
  });
});
