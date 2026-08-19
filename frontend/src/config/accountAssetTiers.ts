import type { AccountAssetTier } from '@/api/accounts'

export const accountAssetTierOptions: Array<{ label: string; value: AccountAssetTier }> = [
  { label: '未标注', value: 'unknown' },
  { label: '新号（1个月内）', value: 'month_1' },
  { label: '3-6个月号', value: 'month_3_6' },
  { label: '1年号', value: 'year_1' },
  { label: '2年老号', value: 'year_2' },
  { label: '3年老号+', value: 'year_3_plus' },
]
