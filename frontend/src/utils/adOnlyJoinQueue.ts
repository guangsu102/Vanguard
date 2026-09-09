export interface ParsedPacedJoinLinks {
  links: string[]
  totalCount: number
  duplicateCount: number
}

export const parsePacedJoinLinks = (value: string): ParsedPacedJoinLinks => {
  const values = value
    .split(/[\r\n,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  const links = [...new Set(values)]
  return {
    links,
    totalCount: values.length,
    duplicateCount: values.length - links.length,
  }
}

export const estimatePacedJoinMinutes = (
  linkCount: number,
  minimumMinutes: number,
  maximumMinutes: number,
) =>
  Math.max(
    0,
    Math.ceil(
      (Math.max(0, linkCount) - 1) *
        (Math.max(1, minimumMinutes) + Math.max(1, maximumMinutes)) /
        2,
    ),
  )
