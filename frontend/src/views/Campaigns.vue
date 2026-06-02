<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElButton, ElDialog, ElFormItem, ElIcon, ElInput, ElMessage, ElMessageBox, ElOption, ElSelect, ElTabPane, ElTabs, ElTag } from 'element-plus'
import { Edit, Plus, Refresh, Delete } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import TableCard from '@/components/TableCard.vue'
import SearchBar from '@/components/SearchBar.vue'
import FormDrawer from '@/components/FormDrawer.vue'
import { useCampaignStore } from '@/stores/campaign'
import type {
  Campaign,
  CampaignDistributionMode,
  CampaignScope,
  CampaignTriggerTiming,
  CampaignType,
  ManagedGroupTriggerEvent,
  ManagedGroupTriggerTiming,
} from '@/api/campaigns'
import { guardianApi, type GuardianBot, type ManagedGroupBinding } from '@/api/guardian'

const route = useRoute()
const campaignStore = useCampaignStore()

const loading = ref(false)
const activeTab = ref<CampaignScope>('global')
const drawerVisible = ref(false)
const detailVisible = ref(false)
const editingId = ref<number | null>(null)
const managedGroups = ref<ManagedGroupBinding[]>([])
const guardianBots = ref<GuardianBot[]>([])

const managedGroupTriggerEventOptions: Array<{ label: string; value: ManagedGroupTriggerEvent }> = [
  { label: '用户入群', value: 'user_joined' },
  { label: '验证通过', value: 'verification_passed' },
  { label: '新人延迟触发', value: 'new_member_delay' },
  { label: '定时活动', value: 'scheduled' },
  { label: '手动广播', value: 'manual_broadcast' },
  { label: '周期活动', value: 'periodic' },
]

const managedGroupTriggerEventLabelMap = managedGroupTriggerEventOptions.reduce<Record<string, string>>((map, item) => {
  map[item.value] = item.label
  return map
}, {})

const campaignTriggerTimingOptions: Array<{ label: string; value: CampaignTriggerTiming }> = [
  { label: '注册后触发', value: 'after_register' },
  { label: '即时触发', value: 'immediate' },
  { label: '延时触发', value: 'delayed' },
  { label: '定时触发', value: 'scheduled' },
  { label: '手动触发', value: 'manual' },
  { label: '周期触发', value: 'periodic' },
]

const campaignTriggerTimingLabelMap = campaignTriggerTimingOptions.reduce<Record<CampaignTriggerTiming, string>>((map, item) => {
  map[item.value] = item.label
  return map
}, {} as Record<CampaignTriggerTiming, string>)

const campaignDistributionModeOptions: Array<{ label: string; value: CampaignDistributionMode }> = [
  { label: '入群欢迎', value: 'welcome' },
  { label: '延迟分发', value: 'delayed' },
  { label: '定时分发', value: 'scheduled' },
  { label: '手动分发', value: 'manual' },
  { label: '周期分发', value: 'periodic' },
]

const campaignDistributionModeLabelMap = campaignDistributionModeOptions.reduce<Record<CampaignDistributionMode, string>>((map, item) => {
  map[item.value] = item.label
  return map
}, {} as Record<CampaignDistributionMode, string>)

const managedGroupTriggerTimingLabelMap: Record<ManagedGroupTriggerTiming, string> = {
  immediate: '即时触发',
  delayed: '延时触发',
  scheduled: '定时触发',
  manual: '手动触发',
  periodic: '周期触发',
}

const managedGroupTriggerTimingByEvent: Record<ManagedGroupTriggerEvent, ManagedGroupTriggerTiming> = {
  user_joined: 'immediate',
  verification_passed: 'immediate',
  new_member_delay: 'delayed',
  scheduled: 'scheduled',
  manual_broadcast: 'manual',
  periodic: 'periodic',
}

const managedGroupDistributionModeByEvent: Record<ManagedGroupTriggerEvent, CampaignDistributionMode> = {
  user_joined: 'welcome',
  verification_passed: 'welcome',
  new_member_delay: 'delayed',
  scheduled: 'scheduled',
  manual_broadcast: 'manual',
  periodic: 'periodic',
}

const formData = reactive({
  name: '',
  campaign_type: 'discount' as CampaignType,
  campaign_scope: 'global' as CampaignScope,
  trigger_timing: 'after_register' as CampaignTriggerTiming,
  trigger_event: '',
  delay_minutes: 10,
  schedule_times_text: '',
  periodic_interval_minutes: 60,
  validity_hours: 168,
  target_group_ids: [] as number[],
  bot_account_id: undefined as number | undefined,
  distribution_mode: undefined as CampaignDistributionMode | undefined,
  reward_policy_json_text: '',
  broadcast_policy_json_text: '',
  eligibility_policy_json_text: '',
  enabled: false,
})

const formRules = {
  name: [{ required: true, message: '请输入活动名称', trigger: 'blur' }],
}

const currentManagedGroupTriggerTiming = computed<ManagedGroupTriggerTiming | ''>(() => {
  if (formData.campaign_scope !== 'managed_group') return ''
  return managedGroupTriggerTimingByEvent[formData.trigger_event as ManagedGroupTriggerEvent] || ''
})

const currentManagedGroupDistributionMode = computed<CampaignDistributionMode | ''>(() => {
  if (formData.campaign_scope !== 'managed_group') return ''
  return managedGroupDistributionModeByEvent[formData.trigger_event as ManagedGroupTriggerEvent] || ''
})

const formatManagedGroupLabel = (group: ManagedGroupBinding) => {
  const name = group.title || group.username || String(group.telegram_group_id)
  const username = group.username && group.username !== name ? ` @${group.username}` : ''
  return `${name}${username} (${group.telegram_group_id})`
}

const managedGroupOptions = computed(() => {
  const selectedBotId = formData.bot_account_id
  return managedGroups.value
    .filter((item) => !selectedBotId || item.bot_account_id === selectedBotId)
    .map((item) => ({
      label: formatManagedGroupLabel(item),
      value: item.telegram_group_id,
    }))
})

const selectedManagedGroupTitle = computed(() => {
  const routeTitle = route.query.title
  if (typeof routeTitle === 'string' && routeTitle) return routeTitle
  if (activeTab.value !== 'managed_group') return ''
  const groupId = Number(route.query.groupId)
  const matched = managedGroups.value.find((item) => item.telegram_group_id === groupId)
  return matched ? matched.title || matched.username || String(matched.telegram_group_id) : ''
})

const searchFilters = computed(() => [
  {
    type: 'input' as const,
    key: 'search',
    label: '关键词',
    placeholder: '活动名称 / 触发时机 / 触发事件',
    width: '220px',
  },
  {
    type: 'select' as const,
    key: 'enabled',
    label: '状态',
    placeholder: '全部状态',
    width: '120px',
    options: [
      { label: '全部', value: '' },
      { label: '启用', value: true },
      { label: '停用', value: false },
    ],
  },
])

const columns = [
  { prop: 'name', label: '活动名称', minWidth: '180' },
  { prop: 'campaign_type', label: '活动类型', width: '110', slot: 'campaign_type' },
  { prop: 'trigger_timing', label: '触发时机', width: '130', slot: 'trigger_timing' },
  { prop: 'trigger_event', label: '事件', width: '130', slot: 'trigger_event' },
  { prop: 'validity_hours', label: '有效期(小时)', width: '120' },
  { prop: 'enabled', label: '状态', width: '100', slot: 'enabled' },
  { prop: 'created_at', label: '创建时间', width: '170', slot: 'created_at' },
  { prop: 'actions', label: '操作', width: '190', fixed: 'right', slot: 'actions' },
]

const drawerFields = computed(() => {
  const baseFields = [
    { prop: 'name', label: '活动名称', type: 'input' as const, placeholder: '例如：新人优惠券 / 验证通过优惠券' },
    { prop: 'validity_hours', label: '有效期(小时)', type: 'number' as const, props: { min: 1 } },
    {
      prop: 'distribution_mode',
      label: '分发模式',
      type: 'select' as const,
      options: campaignDistributionModeOptions,
      placeholder: '请选择分发模式',
      props: {
        clearable: formData.campaign_scope !== 'managed_group',
        disabled: formData.campaign_scope === 'managed_group',
      },
    },
    { prop: 'enabled', label: '启用', type: 'switch' as const },
  ]

  if (formData.campaign_scope === 'managed_group') {
    baseFields.splice(2, 0, {
      prop: 'trigger_event',
      label: '群内事件',
      type: 'select' as const,
      options: managedGroupTriggerEventOptions,
      placeholder: '请选择群内事件',
    })
    baseFields.splice(3, 0, {
      prop: 'bot_account_id',
      label: 'Bot账号',
      type: 'select' as const,
      options: guardianBots.value.map((item) => ({
        label: item.display_name || item.identifier,
        value: item.account_id,
      })),
      props: { filterable: true },
    })
    baseFields.splice(4, 0, {
      prop: 'target_group_ids',
      label: '目标群组',
      type: 'select' as const,
      options: managedGroupOptions.value,
      placeholder: '按群名称搜索选择',
      props: {
        multiple: true,
        filterable: true,
        clearable: true,
        collapseTags: true,
        collapseTagsTooltip: true,
      },
    })
  } else {
    baseFields.splice(2, 0, {
      prop: 'trigger_timing',
      label: '触发时机',
      type: 'select' as const,
      options: campaignTriggerTimingOptions,
      placeholder: '请选择触发时机',
    })
  }

  return baseFields
})

const detailRows = computed(() => {
  const campaign = campaignStore.currentCampaign
  if (!campaign) return []
  return [
    ['活动名称', campaign.name],
    ['活动范围', campaign.campaign_scope === 'managed_group' ? '群内活动' : '转化活动'],
    ['活动类型', '优惠券活动'],
    ['触发时机', formatTriggerTiming(campaign)],
    ['触发事件', campaign.trigger_event ? managedGroupTriggerEventLabelMap[campaign.trigger_event] || campaign.trigger_event : '-'],
    ['有效期', `${campaign.validity_hours} 小时`],
    ['Bot账号', campaign.bot_account_id || '-'],
    ['目标群组', formatTargetGroups(campaign.target_group_ids)],
    ['分发模式', formatDistributionMode(campaign.distribution_mode)],
  ]
})

const parseJsonText = (text: string) => {
  const trimmed = text.trim()
  if (!trimmed) return undefined
  return JSON.parse(trimmed)
}

const parseScheduleTimes = (text: string) => {
  const trimmed = text.trim()
  if (!trimmed) return undefined
  return trimmed
    .split(/[,\n，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

const formatTriggerEvent = (value?: string) => {
  if (!value) return '-'
  return managedGroupTriggerEventLabelMap[value] || value
}

const formatTriggerTiming = (campaign: Campaign) => {
  if (campaign.campaign_scope === 'managed_group' && campaign.trigger_event) {
    const mapped = managedGroupTriggerTimingByEvent[campaign.trigger_event as ManagedGroupTriggerEvent]
    if (mapped) return managedGroupTriggerTimingLabelMap[mapped]
  }
  return campaignTriggerTimingLabelMap[campaign.trigger_timing] || campaign.trigger_timing || '-'
}

const formatDistributionMode = (value?: CampaignDistributionMode) => {
  if (!value) return '-'
  return campaignDistributionModeLabelMap[value] || value
}

const formatTargetGroups = (groupIds?: number[]) => {
  if (!groupIds?.length) return '-'
  return groupIds
    .map((groupId) => {
      const matched = managedGroups.value.find((item) => item.telegram_group_id === groupId)
      return matched ? formatManagedGroupLabel(matched) : String(groupId)
    })
    .join(', ')
}

const buildManagedGroupBroadcastPolicy = () => {
  const broadcastPolicy = parseJsonText(formData.broadcast_policy_json_text) || {}
  const timing = currentManagedGroupTriggerTiming.value

  if (timing === 'delayed') {
    broadcastPolicy.delay_minutes = formData.delay_minutes
    delete broadcastPolicy.schedule_times
    delete broadcastPolicy.interval_minutes
  } else if (timing === 'scheduled') {
    broadcastPolicy.schedule_times = parseScheduleTimes(formData.schedule_times_text)
    delete broadcastPolicy.delay_minutes
    delete broadcastPolicy.interval_minutes
  } else if (timing === 'periodic') {
    broadcastPolicy.interval_minutes = formData.periodic_interval_minutes
    delete broadcastPolicy.delay_minutes
    delete broadcastPolicy.schedule_times
  } else {
    delete broadcastPolicy.delay_minutes
    delete broadcastPolicy.schedule_times
    delete broadcastPolicy.interval_minutes
  }

  return broadcastPolicy
}

const applyRouteContext = () => {
  const scope = route.query.scope
  if (scope === 'managed_group' || scope === 'global') {
    activeTab.value = scope as CampaignScope
  }

  const groupId = Number(route.query.groupId)
  const botId = Number(route.query.botId)

  if (activeTab.value === 'managed_group' && editingId.value === null) {
    if (Number.isFinite(groupId) && groupId > 0) {
      formData.target_group_ids = [groupId]
    }
    if (Number.isFinite(botId) && botId > 0) {
      formData.bot_account_id = botId
    }
  }
}

const loadManagedGroupContext = async () => {
  const [groupsRes, botsRes] = await Promise.all([
    guardianApi.listManagedGroups({ limit: 200 }),
    guardianApi.listBots({ enabled: true, limit: 200 }),
  ])
  managedGroups.value = groupsRes.data.data
  guardianBots.value = botsRes.data.data
}

const fillForm = (campaign?: Campaign) => {
  if (!campaign) {
    const defaultTriggerEvent: ManagedGroupTriggerEvent = 'user_joined'
    const defaultTriggerTiming = managedGroupTriggerTimingByEvent[defaultTriggerEvent]
    Object.assign(formData, {
      name: '',
      campaign_type: 'discount',
      campaign_scope: activeTab.value,
      trigger_timing: activeTab.value === 'global' ? 'after_register' : defaultTriggerTiming,
      trigger_event: activeTab.value === 'managed_group' ? defaultTriggerEvent : '',
      delay_minutes: 10,
      schedule_times_text: '',
      periodic_interval_minutes: 60,
      validity_hours: 168,
      target_group_ids: [],
      bot_account_id: undefined,
      distribution_mode: activeTab.value === 'managed_group'
        ? managedGroupDistributionModeByEvent[defaultTriggerEvent]
        : undefined,
      reward_policy_json_text: '',
      broadcast_policy_json_text: '',
      eligibility_policy_json_text: '',
      enabled: false,
    })
    applyRouteContext()
    return
  }

  const broadcastPolicy = campaign.broadcast_policy_json || {}
  const scheduleTimes = Array.isArray(broadcastPolicy.schedule_times) ? broadcastPolicy.schedule_times.join(', ') : ''

  Object.assign(formData, {
    name: campaign.name,
    campaign_type: campaign.campaign_type,
    campaign_scope: campaign.campaign_scope,
    trigger_timing: campaign.trigger_timing,
    trigger_event: campaign.trigger_event || '',
    delay_minutes: typeof broadcastPolicy.delay_minutes === 'number' ? broadcastPolicy.delay_minutes : 10,
    schedule_times_text: scheduleTimes,
    periodic_interval_minutes: typeof broadcastPolicy.interval_minutes === 'number' ? broadcastPolicy.interval_minutes : 60,
    validity_hours: campaign.validity_hours,
    target_group_ids: campaign.target_group_ids || [],
    bot_account_id: campaign.bot_account_id,
    distribution_mode: campaign.distribution_mode,
    reward_policy_json_text: campaign.reward_policy_json ? JSON.stringify(campaign.reward_policy_json, null, 2) : '',
    broadcast_policy_json_text: campaign.broadcast_policy_json ? JSON.stringify(campaign.broadcast_policy_json, null, 2) : '',
    eligibility_policy_json_text: campaign.eligibility_policy_json ? JSON.stringify(campaign.eligibility_policy_json, null, 2) : '',
    enabled: campaign.enabled,
  })
}

const normalizeManagedGroupFormByEvent = (event?: string) => {
  if (formData.campaign_scope !== 'managed_group') return
  const safeEvent = (event || formData.trigger_event || 'user_joined') as ManagedGroupTriggerEvent
  const timing = managedGroupTriggerTimingByEvent[safeEvent]
  if (timing) {
    formData.trigger_timing = timing
  }

  const distributionMode = managedGroupDistributionModeByEvent[safeEvent]
  if (distributionMode) {
    formData.distribution_mode = distributionMode
  }
}

const fetchData = async (params?: Record<string, any>) => {
  loading.value = true
  try {
    await campaignStore.fetchList({
      campaign_scope: activeTab.value,
      ...params,
    })
  } finally {
    loading.value = false
  }
}

const handleSearch = (values: Record<string, any>) => {
  campaignStore.setPage(1)
  fetchData(values)
}

const handleReset = () => {
  campaignStore.setPage(1)
  fetchData()
}

const handlePageChange = (page: number) => {
  campaignStore.setPage(page)
  fetchData()
}

const handlePageSizeChange = (pageSize: number) => {
  campaignStore.setPageSize(pageSize)
  fetchData()
}

const handleTabChange = (tab: string) => {
  activeTab.value = tab as CampaignScope
  campaignStore.setPage(1)
  fetchData()
}

const handleDrawerScopeChange = (tab: string) => {
  if (editingId.value !== null) return
  const scope = tab as CampaignScope
  formData.campaign_scope = scope
  activeTab.value = scope

  if (scope === 'global') {
    formData.trigger_timing = 'after_register'
    formData.trigger_event = ''
    formData.target_group_ids = []
    formData.bot_account_id = undefined
    formData.distribution_mode = undefined
    return
  }

  if (!formData.trigger_event) {
    formData.trigger_event = 'user_joined'
  }
  normalizeManagedGroupFormByEvent()
}

const openAddDrawer = () => {
  editingId.value = null
  fillForm()
  drawerVisible.value = true
}

const openEditDrawer = (row: Campaign) => {
  editingId.value = row.id
  fillForm(row)
  drawerVisible.value = true
}

const buildPayload = () => ({
  name: formData.name.trim(),
  campaign_type: 'discount' as CampaignType,
  campaign_scope: formData.campaign_scope,
  trigger_timing: formData.campaign_scope === 'managed_group'
    ? currentManagedGroupTriggerTiming.value || 'immediate'
    : formData.trigger_timing,
  trigger_event: formData.campaign_scope === 'managed_group' ? formData.trigger_event.trim() || undefined : undefined,
  validity_hours: formData.validity_hours,
  target_group_ids: formData.campaign_scope === 'managed_group' ? formData.target_group_ids : undefined,
  bot_account_id: formData.campaign_scope === 'managed_group' ? formData.bot_account_id || undefined : undefined,
  distribution_mode: formData.campaign_scope === 'managed_group'
    ? currentManagedGroupDistributionMode.value || undefined
    : formData.distribution_mode,
  reward_policy_json: parseJsonText(formData.reward_policy_json_text),
  broadcast_policy_json: formData.campaign_scope === 'managed_group'
    ? buildManagedGroupBroadcastPolicy()
    : parseJsonText(formData.broadcast_policy_json_text),
  eligibility_policy_json: parseJsonText(formData.eligibility_policy_json_text),
  enabled: formData.enabled,
})

const handleSubmit = async () => {
  try {
    const payload = buildPayload()
    if (editingId.value) {
      await campaignStore.update(editingId.value, payload)
      ElMessage.success('活动已更新')
    } else {
      await campaignStore.create(payload)
      ElMessage.success('活动已创建')
    }
    drawerVisible.value = false
    fetchData()
  } catch (error) {
    console.error('Failed to save campaign:', error)
  }
}

const handleDelete = async (row: Campaign) => {
  try {
    await ElMessageBox.confirm(`确定删除活动 "${row.name}" 吗？`, '提示', { type: 'warning' })
    await campaignStore.remove(row.id)
    ElMessage.success('删除成功')
  } catch {
    // cancelled
  }
}

const handleToggle = async (row: Campaign) => {
  await campaignStore.toggle(row.id)
  ElMessage.success(row.enabled ? '活动已启用' : '活动已停用')
}

const handleViewDetail = async (row: Campaign) => {
  await campaignStore.getById(row.id)
  await campaignStore.fetchStats(row.id)
  detailVisible.value = true
}

const formatDate = (date?: string) => (date ? dayjs(date).format('YYYY-MM-DD HH:mm') : '-')

onMounted(() => {
  loadManagedGroupContext()
  applyRouteContext()
  fetchData()
  normalizeManagedGroupFormByEvent()
})

watch(
  () => route.query,
  () => {
    if (editingId.value === null) {
      applyRouteContext()
    }
    const scope = route.query.scope
    if (scope === 'managed_group' || scope === 'global') {
      activeTab.value = scope as CampaignScope
      fetchData()
    }
  },
)

watch(
  () => formData.trigger_event,
  (value) => {
    normalizeManagedGroupFormByEvent(value)
  },
)

watch(
  () => formData.campaign_scope,
  (value) => {
    if (value === 'managed_group') {
      if (!formData.trigger_event) {
        formData.trigger_event = 'user_joined'
      }
      normalizeManagedGroupFormByEvent()
    }
  },
)

watch(
  () => formData.bot_account_id,
  () => {
    if (formData.campaign_scope !== 'managed_group') return
    const allowedGroupIds = new Set(managedGroupOptions.value.map((item) => item.value))
    formData.target_group_ids = formData.target_group_ids.filter((groupId) => allowedGroupIds.has(groupId))
  },
)
</script>

<template>
  <div class="campaigns-page">
    <div class="page-header">
      <div>
        <h2 class="page-title">活动管理</h2>
        <p class="page-desc">支持转化活动和群内活动两类配置，群内活动面向 Bot 管理群运营。</p>
        <p v-if="selectedManagedGroupTitle" class="page-subtitle">当前管理群：{{ selectedManagedGroupTitle }}</p>
      </div>
      <div class="header-actions">
        <el-button @click="fetchData()">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button type="primary" @click="openAddDrawer">
          <el-icon><Plus /></el-icon>
          新增活动
        </el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" @tab-change="handleTabChange">
      <el-tab-pane label="转化活动" name="global" />
      <el-tab-pane label="群内活动" name="managed_group" />
    </el-tabs>

    <SearchBar
      :filters="searchFilters"
      :loading="loading"
      @search="handleSearch"
      @reset="handleReset"
    />

    <TableCard
      :columns="columns"
      :data="campaignStore.list"
      :total="campaignStore.total"
      :loading="loading"
      :page="campaignStore.page"
      :page-size="campaignStore.pageSize"
      row-key="id"
      @page-change="handlePageChange"
      @page-size-change="handlePageSizeChange"
    >
      <template #campaign_type="{ row }">
        <el-tag effect="plain">优惠券活动</el-tag>
      </template>

      <template #trigger_timing="{ row }">
        <span>{{ formatTriggerTiming(row) }}</span>
      </template>

      <template #trigger_event="{ row }">
        <span>{{ formatTriggerEvent(row.trigger_event) }}</span>
      </template>

      <template #enabled="{ row }">
        <el-tag :type="row.enabled ? 'success' : 'info'" effect="plain">
          {{ row.enabled ? '启用' : '停用' }}
        </el-tag>
      </template>

      <template #created_at="{ row }">
        {{ formatDate(row.created_at) }}
      </template>

      <template #actions="{ row }">
        <el-button type="primary" link size="small" @click="openEditDrawer(row)">
          <el-icon><Edit /></el-icon>
          编辑
        </el-button>
        <el-button type="info" link size="small" @click="handleViewDetail(row)">
          详情
        </el-button>
        <el-button type="warning" link size="small" @click="handleToggle(row)">
          {{ row.enabled ? '停用' : '启用' }}
        </el-button>
        <el-button type="danger" link size="small" @click="handleDelete(row)">
          <el-icon><Delete /></el-icon>
          删除
        </el-button>
      </template>
    </TableCard>

    <FormDrawer
      v-model:visible="drawerVisible"
      :title="editingId ? '编辑活动' : '新增活动'"
      :fields="drawerFields"
      :model-value="formData"
      :rules="formRules"
      width="600px"
      @confirm="handleSubmit"
    >
      <template #before>
        <el-tabs
          v-model="formData.campaign_scope"
          class="drawer-scope-tabs"
          @tab-change="handleDrawerScopeChange"
        >
          <el-tab-pane label="转化活动" name="global" :disabled="editingId !== null" />
          <el-tab-pane label="群内活动" name="managed_group" :disabled="editingId !== null" />
        </el-tabs>
      </template>

      <template #default>
        <div class="json-section">
          <el-form-item v-if="formData.campaign_scope === 'managed_group'" label="触发方式">
            <el-input :model-value="currentManagedGroupTriggerTiming ? managedGroupTriggerTimingLabelMap[currentManagedGroupTriggerTiming] : '-'" disabled />
          </el-form-item>
          <el-form-item v-if="currentManagedGroupTriggerTiming === 'delayed'" label="延时分钟">
            <el-input
              v-model.number="formData.delay_minutes"
              type="number"
              min="1"
              max="10080"
              placeholder="例如 10"
            />
          </el-form-item>
          <el-form-item v-if="currentManagedGroupTriggerTiming === 'scheduled'" label="定时时间">
            <el-input
              v-model="formData.schedule_times_text"
              type="textarea"
              :rows="2"
              placeholder="例如 09:00,14:30,21:00"
            />
          </el-form-item>
          <el-form-item v-if="currentManagedGroupTriggerTiming === 'periodic'" label="间隔分钟">
            <el-input
              v-model.number="formData.periodic_interval_minutes"
              type="number"
              min="5"
              max="10080"
              placeholder="例如 60"
            />
          </el-form-item>
          <el-form-item label="奖励策略JSON">
            <el-input
              v-model="formData.reward_policy_json_text"
              type="textarea"
              :rows="4"
              placeholder='例如 {"coupon_type":"discount","limit_per_user":1}'
            />
          </el-form-item>
          <el-form-item label="广播策略JSON">
            <el-input
              v-model="formData.broadcast_policy_json_text"
              type="textarea"
              :rows="4"
              placeholder='例如 {"delay_minutes":10,"template":"welcome"}'
            />
          </el-form-item>
          <el-form-item label="资格策略JSON">
            <el-input
              v-model="formData.eligibility_policy_json_text"
              type="textarea"
              :rows="4"
              placeholder='例如 {"verified_only":true,"once_per_user":true}'
            />
          </el-form-item>
        </div>
      </template>
    </FormDrawer>

    <el-dialog v-model="detailVisible" title="活动详情" width="760px">
      <template v-if="campaignStore.currentCampaign">
        <div class="detail-grid">
          <div v-for="[label, value] in detailRows" :key="label" class="detail-row">
            <div class="detail-label">{{ label }}</div>
            <div class="detail-value">{{ value }}</div>
          </div>
        </div>

        <div v-if="campaignStore.currentStats" class="stats-panel">
          <div class="stats-title">活动统计</div>
          <div class="stats-grid">
            <div class="stats-card">
              <div class="stats-label">总跟踪</div>
              <div class="stats-value">{{ campaignStore.currentStats.total_tracked }}</div>
            </div>
            <div class="stats-card">
              <div class="stats-label">已注册</div>
              <div class="stats-value">{{ campaignStore.currentStats.registered }}</div>
            </div>
            <div class="stats-card">
              <div class="stats-label">已转化</div>
              <div class="stats-value">{{ campaignStore.currentStats.converted }}</div>
            </div>
            <div class="stats-card">
              <div class="stats-label">转化率</div>
              <div class="stats-value">{{ campaignStore.currentStats.conversion_rate }}%</div>
            </div>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.campaigns-page {
  padding: 0;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 16px;
}

.page-title {
  margin: 0;
  color: #303133;
  font-size: 20px;
  font-weight: 600;
}

.page-desc {
  margin: 6px 0 0;
  color: #606266;
}

.page-subtitle {
  margin: 6px 0 0;
  color: #909399;
}

.header-actions {
  display: flex;
  gap: 12px;
}

.drawer-scope-tabs {
  margin-bottom: 16px;
}

.detail-grid {
  display: grid;
  gap: 12px;
}

.detail-row {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid #f0f2f5;
}

.detail-label {
  color: #909399;
}

.detail-value {
  color: #303133;
}

.stats-panel {
  margin-top: 20px;
}

.stats-title {
  margin-bottom: 12px;
  color: #303133;
  font-weight: 600;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(120px, 1fr));
  gap: 12px;
}

.stats-card {
  padding: 16px;
  background: #f8fafc;
  border-radius: 8px;
}

.stats-label {
  color: #909399;
  font-size: 13px;
}

.stats-value {
  margin-top: 8px;
  color: #303133;
  font-size: 22px;
  font-weight: 700;
}
</style>
