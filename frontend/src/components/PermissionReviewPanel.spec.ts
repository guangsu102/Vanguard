import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ElementPlus from 'element-plus'
import PermissionReviewPanel from './PermissionReviewPanel.vue'

describe('membership permission review', () => {
  it('shows actual permission, verification and manual pause without legacy approval gates', async () => {
    const wrapper = mount(PermissionReviewPanel, {
      props: { rows: [
        { id: 1, membership_id: 1, account_id: 7, group_id: 2, group_title: '测试群',
          telegram_group_id: -1000000000123, status: 'joined', review_status: 'approved',
          can_send_text: true, verification_status: 'complete', ad_status: 'active', reason: 'text_allowed' },
        { id: 2, membership_id: 2, account_id: 8, group_id: 3, group_title: '暂停群',
          telegram_group_id: -1000000000124, status: 'joined', review_status: 'rejected',
          can_send_text: null, verification_status: 'pending', ad_status: 'paused', reason: 'manual_pause' },
      ] },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('可投放')
    expect(wrapper.text()).toContain('已完成')
    expect(wrapper.text()).toContain('manual_pause')
    expect(wrapper.text()).not.toMatch(/预算池|先例|成熟|AI.*审核/)
  })
})
