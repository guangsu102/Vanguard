export type AdBindingDeliveryPolicy = 'growth' | 'ad_only'
export type AdBindingAccountMode = 'growth' | 'ad_only'

type AccountWithId = {
  id: number
}

export const filterAccountsByDeliveryPolicy = <T extends AccountWithId>(
  accounts: T[],
  accountModes: ReadonlyMap<number, AdBindingAccountMode>,
  deliveryPolicy?: AdBindingDeliveryPolicy,
): T[] => {
  if (!deliveryPolicy) return []

  return accounts.filter(
    (account) => (accountModes.get(account.id) || 'growth') === deliveryPolicy,
  )
}
