<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { useRoute } from 'vue-router'
import dayjs from 'dayjs'

import { accountsApi, type Account } from '@/api/accounts'
import { automationApi, type AdCampaign, type AutoJoinVerificationLog } from '@/api/automation'
import ClientListPagination from '@/components/ClientListPagination.vue'
import { useClientPagination } from '@/utils/clientPagination'
const route = useRoute()
const queryNumber = (value: unknown) => {
  const item = Array.isArray(value) ? value[0] : value
  const parsed = Number(item)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined
}


const activeTab = ref(['join', 'verification', 'delivery'].includes(String(route.query.tab)) ? String(route.query.tab) : 'join')
const loading = ref(false)
const attempts = ref<any[]>([])
const verificationLogs = ref<AutoJoinVerificationLog[]>([])
const deliveryLogs = ref<any[]>([])
const deliveryPage = ref(1)
const deliveryPageSize = ref(20)
const deliveryTotal = ref(0)

const accounts = ref<Account[]>([])
const campaigns = ref<AdCampaign[]>([])
const deliveryAccountId = ref<number | undefined>(queryNumber(route.query.account_id))
const deliveryCampaignId = ref<number | undefined>(queryNumber(route.query.campaign_id))
const deliveryStatus = ref('')
const deliveryTimeRange = ref<string[]>([])
const attemptSource = computed(() => attempts.value)
const verificationSource = computed(() => verificationLogs.value)
const {
  page: attemptPage,
  pageSize: attemptPageSize,
  total: attemptTotal,
  rows: pagedAttempts,
} = useClientPagination(attemptSource, 20)
const {
  page: verificationPage,
  pageSize: verificationPageSize,
  total: verificationTotal,
  rows: pagedVerificationLogs,
} = useClientPagination(verificationSource, 20)

type TagType = 'success' | 'warning' | 'danger' | 'info'

function statusType(status?: string): TagType {
  if (['success', 'joined', 'sent', 'completed', 'active'].includes(status || '')) return 'success'
  if (['failed', 'error', 'banned', 'blocked'].includes(status || '')) return 'danger'
  if (['pending', 'scheduled', 'limited', 'retry'].includes(status || '')) return 'warning'
  return 'info'
}

function statusLabel(status?: string) {
  const labels: Record<string, string> = {
    success: '成功',
    joined: '已入群',
    sent: '已发送',
    completed: '已完成',
    failed: '失败',
    error: '异常',
    blocked: '阻塞',
    pending: '等待中',
    scheduled: '已排队',
    retry: '等待重试',
  }
  return labels[status || ''] || status || '-'
}

function formatTime(value?: string) {
  return value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-'
}

function accountLabel(accountId?: number) {
  const account = accounts.value.find((item) => item.id === accountId)
  return account?.display_name || account?.phone || account?.identifier || (accountId ? `#${accountId}` : '-')
}

function campaignLabel(campaignId?: number) {
  return campaigns.value.find((item) => item.id === campaignId)?.name || (campaignId ? `#${campaignId}` : '-')
}

const loadJoinLogs = async () => {
  const [attemptResponse, verificationResponse] = await Promise.all([
    automationApi.getAutoJoinAttempts({ limit: 500 }),
    automationApi.getAutoJoinVerificationLogs({ limit: 500 }),
  ])
  attempts.value = attemptResponse.data.data
  verificationLogs.value = verificationResponse.data.data
}

const loadDeliveryLogs = async () => {
  const response = await automationApi.getDeliveryLogs({
    account_id: deliveryAccountId.value,
    campaign_id: deliveryCampaignId.value,
    status: deliveryStatus.value || undefined,
    start_at: deliveryTimeRange.value?.[0],
    end_at: deliveryTimeRange.value?.[1],
    page: deliveryPage.value,
    page_size: deliveryPageSize.value,
  })
  deliveryLogs.value = response.data.data
  deliveryTotal.value = response.data.total || response.data.data.length
}

const loadFilterOptions = async () => {
  const [accountResponse, campaignResponse] = await Promise.all([
    accountsApi.list({ account_type: 'promoter', limit: 100 }),
    automationApi.getCampaigns({ page_size: 100 }),
  ])
  accounts.value = accountResponse.list
  campaigns.value = campaignResponse.data.data
}

const loadAll = async () => {
  loading.value = true
  try {
    await Promise.all([loadJoinLogs(), loadDeliveryLogs(), loadFilterOptions()])
  } finally {
    loading.value = false
  }
}

const searchDeliveryLogs = async () => {
  deliveryPage.value = 1
  await loadDeliveryLogs()
}

const resetDeliveryLogs = async () => {
  deliveryAccountId.value = undefined
  deliveryCampaignId.value = undefined
  deliveryStatus.value = ''
  deliveryTimeRange.value = []
  deliveryPage.value = 1
  await loadDeliveryLogs()
}

const handleDeliveryPageChange = async (value: number) => {
  deliveryPage.value = value
  await loadDeliveryLogs()
}

const handleDeliveryPageSizeChange = async (value: number) => {
  deliveryPageSize.value = value
  deliveryPage.value = 1
  await loadDeliveryLogs()
}

onMounted(loadAll)
</script>

<template>
  <div class="growth-logs-page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2>增长日志</h2>
        <p>加群、入群验证和广告发送记录的统一流水。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadAll">刷新</el-button>
    </div>

    <el-tabs v-model="activeTab" class="log-tabs">
      <el-tab-pane label="加群记录" name="join">
        <el-table :data="pagedAttempts" border size="small" empty-text="暂无加群记录">
          <el-table-column prop="id" label="ID" width="80" />
          <el-table-column label="账号" min-width="150" show-overflow-tooltip>
            <template #default="{ row }">{{ accountLabel(row.account_id) }}</template>
          </el-table-column>
          <el-table-column label="目标群" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">{{ row.group_title || row.group_username || row.keyword || '-' }}</template>
          </el-table-column>
          <el-table-column prop="keyword" label="来源关键词" min-width="150" show-overflow-tooltip />
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" effect="plain">{{ statusLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="原因" min-width="240" show-overflow-tooltip />
          <el-table-column label="时间" width="180">
            <template #default="{ row }">{{ formatTime(row.attempted_at || row.created_at) }}</template>
          </el-table-column>
        </el-table>
        <ClientListPagination
          v-model:page="attemptPage"
          v-model:page-size="attemptPageSize"
          :total="attemptTotal"
        />
      </el-tab-pane>

      <el-tab-pane label="入群验证" name="verification">
        <el-table :data="pagedVerificationLogs" border size="small" empty-text="暂无入群验证记录">
          <el-table-column label="账号" min-width="150" show-overflow-tooltip>
            <template #default="{ row }">{{ accountLabel(row.account_id) }}</template>
          </el-table-column>
          <el-table-column prop="group_title" label="群" min-width="220" show-overflow-tooltip />
          <el-table-column prop="challenge_type" label="验证类型" min-width="140" />
          <el-table-column prop="action" label="动作" width="120" />
          <el-table-column label="结果" width="110">
            <template #default="{ row }">
              <el-tag :type="row.success === false ? 'danger' : 'success'" effect="plain">
                {{ row.success === false ? '失败' : '成功' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="原因" min-width="240" show-overflow-tooltip />
          <el-table-column label="时间" width="180">
            <template #default="{ row }">{{ formatTime(row.updated_at || row.created_at) }}</template>
          </el-table-column>
        </el-table>
        <ClientListPagination
          v-model:page="verificationPage"
          v-model:page-size="verificationPageSize"
          :total="verificationTotal"
        />
      </el-tab-pane>

      <el-tab-pane label="广告发送记录" name="delivery">
        <el-table :data="deliveryLogs" border size="small" empty-text="暂无广告发送记录">
          <el-table-column prop="id" label="ID" width="80" />
          <el-table-column label="账号" min-width="150" show-overflow-tooltip>
            <template #default="{ row }">{{ accountLabel(row.account_id) }}</template>
          </el-table-column>
          <el-table-column label="计划" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.campaign_name || campaignLabel(row.campaign_id || row.ad_campaign_id) }}</template>
          </el-table-column>
          <el-table-column prop="group_title" label="群" min-width="200" show-overflow-tooltip />
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" effect="plain">{{ statusLabel(row.status) }}</el-tag>
        <div class="delivery-filters">
          <el-select v-model="deliveryAccountId" clearable filterable placeholder="全部账号" class="filter-control">
            <el-option
              v-for="account in accounts"
              :key="account.id"
              :label="accountLabel(account.id)"
              :value="account.id"
            />
          </el-select>
          <el-select v-model="deliveryCampaignId" clearable filterable placeholder="全部计划" class="filter-control">
            <el-option v-for="campaign in campaigns" :key="campaign.id" :label="campaign.name" :value="campaign.id" />
          </el-select>
          <el-select v-model="deliveryStatus" clearable placeholder="全部状态" class="filter-control">
            <el-option label="已发送" value="sent" />
            <el-option label="成功" value="success" />
            <el-option label="失败" value="failed" />
            <el-option label="阻塞" value="blocked" />
            <el-option label="等待中" value="pending" />
          </el-select>
          <el-date-picker
            v-model="deliveryTimeRange"
            type="datetimerange"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            value-format="YYYY-MM-DDTHH:mm:ss"
            class="time-range-control"
          />
          <el-button type="primary" :loading="loading" @click="searchDeliveryLogs">查询</el-button>
          <el-button @click="resetDeliveryLogs">重置</el-button>
        </div>
            </template>
          </el-table-column>
          <el-table-column prop="survival_status" label="存活" width="110" />
          <el-table-column prop="error" label="错误" min-width="240" show-overflow-tooltip />
          <el-table-column label="时间" width="180">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
        </el-table>
        <div class="server-pagination">
          <el-pagination
            :current-page="deliveryPage"
            :page-size="deliveryPageSize"
            :page-sizes="[10, 20, 50, 100]"
            :total="deliveryTotal"
            background
            layout="total, sizes, prev, pager, next, jumper"
            @update:current-page="handleDeliveryPageChange"
            @update:page-size="handleDeliveryPageSizeChange"
          />
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.growth-logs-page {
  min-width: 0;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0;
  color: #303133;
  font-size: 20px;
}

.page-header p {
  margin: 6px 0 0;
  color: #606266;
}

.log-tabs {
  min-width: 0;
}

.server-pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
  overflow-x: auto;
}

@media (max-width: 768px) {
  .page-header {
    align-items: stretch;
    flex-direction: column;

.delivery-filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 14px;
}

.filter-control {
  width: 180px;
}

.time-range-control {
  width: 360px;
}
  }

  .filter-control,
  .time-range-control {
    width: 100%;
  }

  .server-pagination {
    justify-content: flex-start;
  }
}
</style>
