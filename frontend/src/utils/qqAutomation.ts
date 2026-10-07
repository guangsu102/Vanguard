export function parseQQGroupNumbers(text: string): string[] {
  const values = text
    .trim()
    .split(/[\s,，;；]+/)
    .filter(Boolean);
  const invalid = values.find((value) => !/^\d{5,20}$/.test(value));
  if (invalid) throw new Error(`群号格式错误：${invalid}`);
  const result = [...new Set(values)];
  if (result.length > 500) throw new Error("每个计划最多支持 500 个目标群");
  return result;
}

export const qqStatusLabels: Record<string, string> = {
  queued: "等待申请",
  requesting: "申请中",
  pending_approval: "等待入群确认",
  joined: "已入群",
  rejected: "被拒绝",
  unsupported: "执行端不支持",
  action_required: "需要人工处理",
  unknown: "结果未知，需核对",
  failed: "失败",
  cancelled: "已取消",
  sending: "发送中",
  succeeded: "成功",
  confirmed_not_sent: "已确认未发送",
  active: "执行中",
  paused: "已暂停",
  completed: "已完成",
};
