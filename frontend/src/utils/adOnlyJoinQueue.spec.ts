import { describe, expect, it } from 'vitest'

import { estimatePacedJoinMinutes, parsePacedJoinLinks } from './adOnlyJoinQueue'

describe('paced Ad-only join queue helpers', () => {
  it('parses multiline links and reports duplicates without reordering', () => {
    const parsed = parsePacedJoinLinks(`
      https://t.me/group_one
      https://t.me/+PrivateTwo
      https://t.me/group_one
    `)

    expect(parsed.links).toEqual([
      'https://t.me/group_one',
      'https://t.me/+PrivateTwo',
    ])
    expect(parsed.totalCount).toBe(3)
    expect(parsed.duplicateCount).toBe(1)
  })

  it('estimates completion from the average configured interval', () => {
    expect(estimatePacedJoinMinutes(1, 1, 30)).toBe(0)
    expect(estimatePacedJoinMinutes(100, 1, 30)).toBe(1535)
    expect(estimatePacedJoinMinutes(3, 4, 10)).toBe(14)
  })
})
