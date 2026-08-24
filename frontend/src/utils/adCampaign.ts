const TARGET_GROUP_LEVEL_LABELS: Record<string, string> = {
  A: 'A级',
  B: 'B级',
  C: 'C级',
  unrated: '未评级',
}

const TARGET_GROUP_LEVEL_ORDER = ['A', 'B', 'C', 'unrated']

export function formatAdTargetGroupLevels(levels: unknown): string {
  if (!Array.isArray(levels)) return '未设置'

  const normalized = Array.from(
    new Set(levels.map((level) => String(level).trim()).filter(Boolean)),
  )

  normalized.sort((left, right) => {
    const leftRank = TARGET_GROUP_LEVEL_ORDER.indexOf(left)
    const rightRank = TARGET_GROUP_LEVEL_ORDER.indexOf(right)
    if (leftRank === -1 && rightRank === -1) return left.localeCompare(right)
    if (leftRank === -1) return 1
    if (rightRank === -1) return -1
    return leftRank - rightRank
  })

  return normalized.length
    ? normalized.map((level) => TARGET_GROUP_LEVEL_LABELS[level] || level).join(' / ')
    : '未设置'
}
