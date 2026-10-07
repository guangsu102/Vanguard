<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import dayjs from "dayjs";
import { automationApi, type AdCreative } from "@/api/automation";
import {
  qqAutomationApi,
  type QQAutomationAccount,
  type QQAdCampaign,
  type QQAdBinding,
  type QQJoinTask,
  type QQAdSchedule,
  type QQAutomationLog,
} from "@/api/qqAutomation";
import { parseQQGroupNumbers, qqStatusLabels } from "@/utils/qqAutomation";

const loading = ref(false);
const saving = ref(false);
const activeTab = ref("campaigns");
const accounts = ref<QQAutomationAccount[]>([]);
const campaigns = ref<QQAdCampaign[]>([]);
const creatives = ref<AdCreative[]>([]);
const bindings = ref<QQAdBinding[]>([]);
const joinTasks = ref<QQJoinTask[]>([]);
const schedules = ref<QQAdSchedule[]>([]);
const logs = ref<QQAutomationLog[]>([]);
const joinPage = ref(1);
const schedulePage = ref(1);
const logPage = ref(1);
const totals = reactive({ joins: 0, schedules: 0, logs: 0 });
const pageSize = 50;
let refreshTimer: ReturnType<typeof setInterval> | undefined;

const accountName = (id: number) => {
  const account = accounts.value.find((a) => a.id === id);
  return account
    ? `${account.display_name || "QQ"} (${account.app_id})`
    : `账号 ${id}`;
};
const campaignName = (id?: number | null) =>
  campaigns.value.find((c) => c.id === id)?.name || "-";
const creativeName = (id?: number | null) =>
  creatives.value.find((c) => c.id === id)?.name || "-";
const formatTime = (value?: string | null) =>
  value ? dayjs(`${value}Z`).format("YYYY-MM-DD HH:mm:ss") : "-";
const statusLabel = (value: string) => qqStatusLabels[value] || value;
const hasJoinBridge = computed(() =>
  accounts.value.some((a) => a.join_configured),
);

async function loadRecords() {
  const [joinsRes, schedulesRes, logsRes] = await Promise.all([
    qqAutomationApi.joinTasks((joinPage.value - 1) * pageSize, pageSize),
    qqAutomationApi.schedules((schedulePage.value - 1) * pageSize, pageSize),
    qqAutomationApi.logs((logPage.value - 1) * pageSize, pageSize),
  ]);
  joinTasks.value = joinsRes.data.data;
  schedules.value = schedulesRes.data.data;
  logs.value = logsRes.data.data;
  totals.joins = joinsRes.data.total;
  totals.schedules = schedulesRes.data.total;
  totals.logs = logsRes.data.total;
}

async function loadData() {
  loading.value = true;
  try {
    const [accountsRes, campaignsRes, bindingsRes, creativesRes] =
      await Promise.all([
        qqAutomationApi.accounts(),
        qqAutomationApi.campaigns(),
        qqAutomationApi.bindings(),
        automationApi.getCreatives({ page: 1, page_size: 100 }),
      ]);
    accounts.value = accountsRes.data.data;
    campaigns.value = campaignsRes.data.data;
    bindings.value = bindingsRes.data.data;
    creatives.value = creativesRes.data.data;
    for (let page = 2; (page - 1) * 100 < creativesRes.data.total; page++) {
      const more = await automationApi.getCreatives({ page, page_size: 100 });
      creatives.value.push(...more.data.data);
    }
    await loadRecords();
  } finally {
    loading.value = false;
  }
}

const accountVisible = ref(false);
const editingAccount = ref<number>();
const accountForm = reactive({
  account_number: "",
  display_name: "",
  http_url: "",
  access_token: "",
  join_api_url: "",
  join_api_token: "",
  enabled: true,
  automation_enabled: false,
  join_interval_seconds: 600,
  send_interval_seconds: 60,
  max_joins_per_day: 10,
  max_sends_per_day: 30,
});

function openAccount(account?: QQAutomationAccount) {
  editingAccount.value = account?.id;
  Object.assign(accountForm, {
    account_number: account?.app_id || "",
    display_name: account?.display_name || "",
    http_url: account?.http_url || "",
    access_token: "",
    join_api_url: account?.join_api_url || "",
    join_api_token: "",
    enabled: account?.enabled ?? true,
    automation_enabled: account?.automation_enabled ?? false,
    join_interval_seconds: account?.join_interval_seconds ?? 600,
    send_interval_seconds: account?.send_interval_seconds ?? 60,
    max_joins_per_day: account?.max_joins_per_day ?? 10,
    max_sends_per_day: account?.max_sends_per_day ?? 30,
  });
  accountVisible.value = true;
}

async function saveAccount() {
  saving.value = true;
  try {
    if (editingAccount.value) {
      await qqAutomationApi.updateAccount(editingAccount.value, {
        display_name: accountForm.display_name,
        http_url: accountForm.http_url,
        join_api_url: accountForm.join_api_url,
        enabled: accountForm.enabled,
        automation_enabled: accountForm.automation_enabled,
        join_interval_seconds: accountForm.join_interval_seconds,
        send_interval_seconds: accountForm.send_interval_seconds,
        max_joins_per_day: accountForm.max_joins_per_day,
        max_sends_per_day: accountForm.max_sends_per_day,
        ...(accountForm.access_token
          ? { access_token: accountForm.access_token }
          : {}),
        ...(accountForm.join_api_token
          ? { join_api_token: accountForm.join_api_token }
          : {}),
      });
    } else {
      await qqAutomationApi.createAccount({
        account_number: accountForm.account_number,
        display_name: accountForm.display_name,
        http_url: accountForm.http_url,
        access_token: accountForm.access_token,
      });
    }
    accountVisible.value = false;
    accountForm.access_token = accountForm.join_api_token = "";
    ElMessage.success("QQ 账号配置已保存");
    await loadData();
  } finally {
    saving.value = false;
  }
}

async function syncAccount(account: QQAutomationAccount) {
  const result = await qqAutomationApi.syncAccount(account.id);
  ElMessage.success(`已确认账号加入 ${result.data.data.total} 个群`);
  await loadData();
}

const campaignVisible = ref(false);
const editingCampaign = ref<QQAdCampaign>();
const campaignForm = reactive({
  name: "",
  enabled: false,
  auto_join_enabled: true,
  send_mode: "interval" as QQAdCampaign["send_mode"],
  min_wait_after_join_minutes: 60,
  interval_minutes: 1440,
  scheduled_times: "",
  timezone: "Asia/Shanghai",
  max_sends_per_group_per_day: 1,
  max_sends_per_account_per_day: 30,
  group_numbers: "",
  verify_message: "",
});

function openCampaign(campaign?: QQAdCampaign) {
  editingCampaign.value = campaign;
  Object.assign(campaignForm, {
    name: campaign?.name || "",
    enabled: campaign?.enabled ?? false,
    auto_join_enabled: campaign?.auto_join_enabled ?? true,
    send_mode: campaign?.send_mode || "interval",
    min_wait_after_join_minutes: campaign?.min_wait_after_join_minutes ?? 60,
    interval_minutes: campaign?.interval_minutes ?? 1440,
    scheduled_times: campaign?.scheduled_times.join(", ") || "",
    timezone: campaign?.timezone || "Asia/Shanghai",
    max_sends_per_group_per_day: campaign?.max_sends_per_group_per_day ?? 1,
    max_sends_per_account_per_day:
      campaign?.max_sends_per_account_per_day ?? 30,
    group_numbers:
      campaign?.targets.map((t) => t.group_number).join("\n") || "",
    verify_message: "",
  });
  campaignVisible.value = true;
}

async function saveCampaign() {
  let numbers: string[];
  try {
    numbers = parseQQGroupNumbers(campaignForm.group_numbers);
  } catch (error) {
    ElMessage.warning((error as Error).message);
    return;
  }
  if (!numbers.length) {
    ElMessage.warning("请输入目标 QQ 群号");
    return;
  }
  saving.value = true;
  try {
    const oldTargets = editingCampaign.value?.targets || [];
    await qqAutomationApi.saveCampaign(
      {
        name: campaignForm.name,
        enabled: campaignForm.enabled,
        auto_join_enabled: campaignForm.auto_join_enabled,
        send_mode: campaignForm.send_mode,
        min_wait_after_join_minutes: campaignForm.min_wait_after_join_minutes,
        interval_minutes: campaignForm.interval_minutes,
        timezone: campaignForm.timezone,
        scheduled_times: campaignForm.scheduled_times
          .split(/[\s,，]+/)
          .filter(Boolean),
        max_sends_per_group_per_day: campaignForm.max_sends_per_group_per_day,
        max_sends_per_account_per_day:
          campaignForm.max_sends_per_account_per_day,
        start_at: editingCampaign.value?.start_at,
        end_at: editingCampaign.value?.end_at,
        targets: numbers.map((number) => {
          const old = oldTargets.find((t) => t.group_number === number);
          return {
            group_number: number,
            local_name: old?.local_name,
            enabled: old?.enabled ?? true,
            verify_message:
              campaignForm.verify_message || old?.verify_message || "",
          };
        }),
      },
      editingCampaign.value?.id,
    );
    campaignVisible.value = false;
    ElMessage.success("QQ 广告计划已保存");
    await loadData();
  } finally {
    saving.value = false;
  }
}

async function toggleCampaign(campaign: QQAdCampaign) {
  const { id, ...data } = campaign;
  await qqAutomationApi.saveCampaign(
    { ...data, enabled: !campaign.enabled },
    id,
  );
  await loadData();
}

const bindingVisible = ref(false);
const bindingForm = reactive({
  connection_id: undefined as number | undefined,
  campaign_id: undefined as number | undefined,
  creative_ids: [] as number[],
  priority: 100,
});

async function saveBinding() {
  if (
    !bindingForm.connection_id ||
    !bindingForm.campaign_id ||
    !bindingForm.creative_ids.length
  ) {
    ElMessage.warning("请选择 QQ 账号、广告计划和素材");
    return;
  }
  saving.value = true;
  try {
    await qqAutomationApi.createBindings({
      ...bindingForm,
      connection_id: bindingForm.connection_id,
      campaign_id: bindingForm.campaign_id,
    });
    bindingVisible.value = false;
    bindingForm.creative_ids = [];
    ElMessage.success("素材已绑定到 QQ 账号和广告计划");
    await loadData();
  } finally {
    saving.value = false;
  }
}

async function removeBinding(binding: QQAdBinding) {
  await ElMessageBox.confirm("解除该 QQ 账号与此广告素材的绑定？", "解除绑定");
  await qqAutomationApi.deleteBinding(binding.id);
  await loadData();
}

const creativeVisible = ref(false);
const creativeForm = reactive({
  name: "",
  content: "",
  creative_type: "text" as AdCreative["creative_type"],
  media_url: "",
  link_url: "",
  weight: 100,
  enabled: true,
});
async function saveCreative() {
  saving.value = true;
  try {
    await automationApi.createCreative({ ...creativeForm });
    creativeVisible.value = false;
    Object.assign(creativeForm, {
      name: "",
      content: "",
      media_url: "",
      link_url: "",
    });
    ElMessage.success("素材已加入共用素材库");
    await loadData();
  } finally {
    saving.value = false;
  }
}

async function retryJoin(task: QQJoinTask) {
  await qqAutomationApi.retryJoin(task.id);
  await loadRecords();
}
async function cancelJoin(task: QQJoinTask) {
  await qqAutomationApi.cancelJoin(task.id);
  await loadRecords();
}
async function resumeSchedule(schedule: QQAdSchedule) {
  await ElMessageBox.confirm(
    "请先查看 QQ 群记录。确认上一条广告没有发送，才恢复任务，避免重复投放。",
    "恢复投放",
    { confirmButtonText: "已核对未发送，恢复", type: "warning" },
  );
  await qqAutomationApi.resumeSchedule(schedule.id, true);
  await loadRecords();
}

onMounted(async () => {
  await loadData();
  refreshTimer = setInterval(() => {
    void loadData().catch(() => {});
  }, 30000);
});
onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer);
});
</script>

<template>
  <div class="qq-automation" v-loading="loading">
    <header class="page-header">
      <div>
        <h1>QQ 自动化</h1>
        <p>绑定广告素材，配置目标群和投放计划。</p>
      </div>
      <el-button @click="loadData">刷新</el-button>
    </header>
    <el-alert
      v-if="!hasJoinBridge"
      title="当前 NapCat 标准接口不支持主动申请入群。已加入的目标群可自动投放；未加入的群会显示执行端不支持。"
      type="info"
      :closable="false"
      show-icon
    />
    <el-tabs v-model="activeTab">
      <el-tab-pane label="广告计划" name="campaigns">
        <div class="toolbar">
          <el-button type="primary" @click="openCampaign()"
            >新建广告计划</el-button
          ><span
            >计划启用、账号启用自动化且已绑定素材后，调度器每分钟检查一次。</span
          >
        </div>
        <el-table :data="campaigns">
          <el-table-column prop="name" label="计划" min-width="160" />
          <el-table-column label="状态" width="100"
            ><template #default="{ row }">{{
              row.enabled ? "已启用" : "已停用"
            }}</template></el-table-column
          >
          <el-table-column label="模式" width="130"
            ><template #default="{ row }">{{
              {
                after_join: "入群后一次",
                interval: "间隔投放",
                scheduled: "每日定时",
              }[row.send_mode as QQAdCampaign["send_mode"]]
            }}</template></el-table-column
          >
          <el-table-column label="目标群" width="90"
            ><template #default="{ row }">{{
              row.targets.length
            }}</template></el-table-column
          >
          <el-table-column label="频率" min-width="170"
            ><template #default="{ row }"
              >单群每天 {{ row.max_sends_per_group_per_day }} 次；入群等待
              {{ row.min_wait_after_join_minutes }} 分钟</template
            ></el-table-column
          >
          <el-table-column label="操作" width="160"
            ><template #default="{ row }"
              ><el-button
                link
                type="primary"
                @click="openCampaign(row as QQAdCampaign)"
                >编辑</el-button
              ><el-button link @click="toggleCampaign(row as QQAdCampaign)">{{
                row.enabled ? "停用" : "启用"
              }}</el-button></template
            ></el-table-column
          >
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="QQ 账号" name="accounts">
        <div class="toolbar">
          <el-button type="primary" @click="openAccount()">接入账号</el-button
          ><span>每个账号对应独立的 NapCat 登录会话。</span>
        </div>
        <el-table :data="accounts">
          <el-table-column label="账号" min-width="180"
            ><template #default="{ row }">{{
              accountName(row.id)
            }}</template></el-table-column
          >
          <el-table-column prop="status" label="连接状态" width="110" />
          <el-table-column label="自动化" width="100"
            ><template #default="{ row }">{{
              row.automation_enabled && row.enabled ? "已启用" : "已停用"
            }}</template></el-table-column
          >
          <el-table-column label="主动加群" min-width="140"
            ><template #default="{ row }">{{
              row.join_configured ? "执行接口已配置" : "执行端不支持"
            }}</template></el-table-column
          >
          <el-table-column
            prop="max_sends_per_day"
            label="每日广告上限"
            width="130"
          />
          <el-table-column
            prop="last_error"
            label="最近异常"
            min-width="180"
            show-overflow-tooltip
          />
          <el-table-column label="操作" width="160"
            ><template #default="{ row }"
              ><el-button
                link
                type="primary"
                @click="openAccount(row as QQAutomationAccount)"
                >配置</el-button
              ><el-button link @click="syncAccount(row as QQAutomationAccount)"
                >同步已入群</el-button
              ></template
            ></el-table-column
          >
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="素材绑定" name="bindings">
        <div class="toolbar">
          <el-button type="primary" @click="bindingVisible = true"
            >绑定素材</el-button
          ><el-button @click="creativeVisible = true">新增素材</el-button
          ><span>与 Telegram 共用素材库；QQ 图片使用 HTTP/HTTPS 地址。</span>
        </div>
        <el-table :data="bindings">
          <el-table-column label="QQ 账号" min-width="170"
            ><template #default="{ row }">{{
              accountName(row.connection_id)
            }}</template></el-table-column
          >
          <el-table-column label="广告计划" min-width="150"
            ><template #default="{ row }">{{
              campaignName(row.campaign_id)
            }}</template></el-table-column
          >
          <el-table-column label="素材" min-width="150"
            ><template #default="{ row }">{{
              creativeName(row.creative_id)
            }}</template></el-table-column
          >
          <el-table-column prop="priority" label="投放权重" width="100" />
          <el-table-column label="操作" width="100"
            ><template #default="{ row }"
              ><el-button
                link
                type="danger"
                @click="removeBinding(row as QQAdBinding)"
                >解除绑定</el-button
              ></template
            ></el-table-column
          >
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="加群任务" name="joins">
        <el-table :data="joinTasks">
          <el-table-column label="QQ 账号" min-width="160"
            ><template #default="{ row }">{{
              accountName(row.connection_id)
            }}</template></el-table-column
          >
          <el-table-column prop="group_number" label="目标群号" width="150" />
          <el-table-column label="状态" min-width="160"
            ><template #default="{ row }">{{
              statusLabel(row.status)
            }}</template></el-table-column
          >
          <el-table-column
            prop="error_message"
            label="原因"
            min-width="240"
            show-overflow-tooltip
          />
          <el-table-column label="操作" width="140"
            ><template #default="{ row }"
              ><template
                v-if="
                  [
                    'failed',
                    'rejected',
                    'unsupported',
                    'action_required',
                    'cancelled',
                  ].includes(row.status)
                "
                ><el-button link @click="retryJoin(row as QQJoinTask)"
                  >重新排队</el-button
                ></template
              ><el-button
                v-if="
                  [
                    'queued',
                    'failed',
                    'unsupported',
                    'rejected',
                    'action_required',
                  ].includes(row.status)
                "
                link
                @click="cancelJoin(row as QQJoinTask)"
                >取消</el-button
              ></template
            ></el-table-column
          >
        </el-table>
        <el-pagination
          v-model:current-page="joinPage"
          :total="totals.joins"
          :page-size="pageSize"
          layout="total, prev, pager, next"
          @current-change="loadRecords"
        />
      </el-tab-pane>
      <el-tab-pane label="投放记录" name="logs">
        <h3>群投放状态</h3>
        <el-table :data="schedules">
          <el-table-column label="账号 / 计划" min-width="200"
            ><template #default="{ row }"
              >{{ accountName(row.connection_id) }} /
              {{ campaignName(row.campaign_id) }}</template
            ></el-table-column
          >
          <el-table-column prop="group_number" label="群号" width="140" />
          <el-table-column label="状态" width="110"
            ><template #default="{ row }">{{
              statusLabel(row.status)
            }}</template></el-table-column
          >
          <el-table-column label="下次投放" width="180"
            ><template #default="{ row }">{{
              formatTime(row.next_due_at)
            }}</template></el-table-column
          >
          <el-table-column
            prop="error_message"
            label="原因"
            min-width="180"
            show-overflow-tooltip
          />
          <el-table-column label="操作" width="100"
            ><template #default="{ row }"
              ><el-button
                v-if="row.status === 'paused'"
                link
                @click="resumeSchedule(row as QQAdSchedule)"
                >核对并恢复</el-button
              ></template
            ></el-table-column
          >
        </el-table>
        <el-pagination
          v-model:current-page="schedulePage"
          :total="totals.schedules"
          :page-size="pageSize"
          layout="total, prev, pager, next"
          @current-change="loadRecords"
        />
        <h3>执行记录</h3>
        <el-table :data="logs">
          <el-table-column label="账号" min-width="150"
            ><template #default="{ row }">{{
              accountName(row.connection_id)
            }}</template></el-table-column
          >
          <el-table-column prop="group_number" label="群号" width="140" />
          <el-table-column label="操作" width="90"
            ><template #default="{ row }">{{
              row.operation_type === "ad" ? "广告" : "加群"
            }}</template></el-table-column
          >
          <el-table-column label="素材" min-width="120"
            ><template #default="{ row }">{{
              creativeName(row.creative_id)
            }}</template></el-table-column
          >
          <el-table-column label="结果" min-width="160"
            ><template #default="{ row }">{{
              statusLabel(row.status)
            }}</template></el-table-column
          >
          <el-table-column
            prop="provider_message_id"
            label="消息回执"
            width="130"
          />
          <el-table-column
            prop="error_message"
            label="异常"
            min-width="180"
            show-overflow-tooltip
          />
          <el-table-column label="时间" width="180"
            ><template #default="{ row }">{{
              formatTime(row.created_at)
            }}</template></el-table-column
          >
        </el-table>
        <el-pagination
          v-model:current-page="logPage"
          :total="totals.logs"
          :page-size="pageSize"
          layout="total, prev, pager, next"
          @current-change="loadRecords"
        />
      </el-tab-pane>
    </el-tabs>

    <el-dialog
      v-model="accountVisible"
      :title="editingAccount ? 'QQ 账号配置' : '接入 QQ 账号'"
      width="650px"
    >
      <el-form label-width="150px">
        <el-form-item label="QQ 号"
          ><el-input
            v-model="accountForm.account_number"
            :disabled="!!editingAccount"
        /></el-form-item>
        <el-form-item label="显示名称"
          ><el-input v-model="accountForm.display_name"
        /></el-form-item>
        <el-form-item label="OneBot 接口地址"
          ><el-input
            v-model="accountForm.http_url"
            placeholder="http://napcat:3000"
        /></el-form-item>
        <el-form-item label="接口凭证"
          ><el-input
            v-model="accountForm.access_token"
            type="password"
            show-password
            :placeholder="
              editingAccount ? '留空保留现有凭证' : '至少 32 个字符'
            "
        /></el-form-item>
        <template v-if="editingAccount">
          <el-form-item label="启用账号"
            ><el-switch v-model="accountForm.enabled"
          /></el-form-item>
          <el-form-item label="启用自动化"
            ><el-switch v-model="accountForm.automation_enabled"
          /></el-form-item>
          <el-form-item label="广告最小间隔 / 秒"
            ><el-input-number
              v-model="accountForm.send_interval_seconds"
              :min="1"
              :max="86400"
          /></el-form-item>
          <el-form-item label="每日广告上限"
            ><el-input-number
              v-model="accountForm.max_sends_per_day"
              :min="0"
              :max="10000"
          /></el-form-item>
          <el-divider>主动申请加群执行端</el-divider>
          <p class="form-note">
            NapCat
            标准接口无法主动申请入群。只有接入支持此能力的执行接口，下面的配置才会生效。
          </p>
          <el-form-item label="主动加群接口地址"
            ><el-input
              v-model="accountForm.join_api_url"
              placeholder="支持主动申请入群的执行接口地址"
          /></el-form-item>
          <el-form-item label="加群接口凭证"
            ><el-input
              v-model="accountForm.join_api_token"
              type="password"
              show-password
              placeholder="留空保留现有凭证"
          /></el-form-item>
          <el-form-item label="加群最小间隔 / 秒"
            ><el-input-number
              v-model="accountForm.join_interval_seconds"
              :min="1"
              :max="86400"
          /></el-form-item>
          <el-form-item label="每日申请上限"
            ><el-input-number
              v-model="accountForm.max_joins_per_day"
              :min="0"
              :max="10000"
          /></el-form-item>
        </template>
      </el-form>
      <template #footer
        ><el-button @click="accountVisible = false">取消</el-button
        ><el-button type="primary" :loading="saving" @click="saveAccount"
          >保存</el-button
        ></template
      >
    </el-dialog>
    <el-dialog v-model="campaignVisible" title="QQ 广告计划" width="700px">
      <el-form label-width="160px">
        <el-form-item label="计划名称"
          ><el-input v-model="campaignForm.name" maxlength="120"
        /></el-form-item>
        <el-form-item label="启用计划"
          ><el-switch v-model="campaignForm.enabled"
        /></el-form-item>
        <el-form-item label="自动申请入群"
          ><el-switch v-model="campaignForm.auto_join_enabled" /><span
            class="form-note"
            >需要执行端支持主动加群</span
          ></el-form-item
        >
        <el-form-item label="投放模式"
          ><el-select v-model="campaignForm.send_mode"
            ><el-option label="入群后一次" value="after_join" /><el-option
              label="间隔投放"
              value="interval" /><el-option
              label="每日定时"
              value="scheduled" /></el-select
        ></el-form-item>
        <el-form-item label="入群等待 / 分钟"
          ><el-input-number
            v-model="campaignForm.min_wait_after_join_minutes"
            :min="0"
            :max="10080"
        /></el-form-item>
        <el-form-item
          v-if="campaignForm.send_mode === 'interval'"
          label="投放间隔 / 分钟"
          ><el-input-number
            v-model="campaignForm.interval_minutes"
            :min="1"
            :max="525600"
        /></el-form-item>
        <el-form-item
          v-if="campaignForm.send_mode === 'scheduled'"
          label="每日定时时间"
          ><el-input
            v-model="campaignForm.scheduled_times"
            placeholder="09:00, 18:00"
        /></el-form-item>
        <el-form-item label="时区"
          ><el-input v-model="campaignForm.timezone"
        /></el-form-item>
        <el-form-item label="单群每日上限"
          ><el-input-number
            v-model="campaignForm.max_sends_per_group_per_day"
            :min="0"
            :max="10000"
        /></el-form-item>
        <el-form-item label="单账号每日上限"
          ><el-input-number
            v-model="campaignForm.max_sends_per_account_per_day"
            :min="0"
            :max="10000"
        /></el-form-item>
        <el-form-item label="目标 QQ 群号"
          ><el-input
            v-model="campaignForm.group_numbers"
            type="textarea"
            :rows="6"
            placeholder="每行一个群号，也可使用逗号分隔"
        /></el-form-item>
        <el-form-item label="加群验证消息"
          ><el-input
            v-model="campaignForm.verify_message"
            maxlength="500"
            placeholder="统一验证消息；留空保留已有群的验证消息"
        /></el-form-item>
      </el-form>
      <template #footer
        ><el-button @click="campaignVisible = false">取消</el-button
        ><el-button type="primary" :loading="saving" @click="saveCampaign"
          >保存计划</el-button
        ></template
      >
    </el-dialog>
    <el-dialog
      v-model="bindingVisible"
      title="QQ 账号绑定广告素材"
      width="600px"
    >
      <el-form label-width="100px">
        <el-form-item label="QQ 账号"
          ><el-select v-model="bindingForm.connection_id" filterable
            ><el-option
              v-for="account in accounts"
              :key="account.id"
              :value="account.id"
              :label="accountName(account.id)" /></el-select
        ></el-form-item>
        <el-form-item label="广告计划"
          ><el-select v-model="bindingForm.campaign_id" filterable
            ><el-option
              v-for="campaign in campaigns"
              :key="campaign.id"
              :value="campaign.id"
              :label="campaign.name" /></el-select
        ></el-form-item>
        <el-form-item label="广告素材"
          ><el-select v-model="bindingForm.creative_ids" multiple filterable
            ><el-option
              v-for="creative in creatives.filter((c) => c.enabled)"
              :key="creative.id"
              :value="creative.id"
              :label="creative.name" /></el-select
        ></el-form-item>
        <el-form-item label="投放权重"
          ><el-input-number
            v-model="bindingForm.priority"
            :min="1"
            :max="10000"
        /></el-form-item>
      </el-form>
      <template #footer
        ><el-button @click="bindingVisible = false">取消</el-button
        ><el-button type="primary" :loading="saving" @click="saveBinding"
          >绑定素材</el-button
        ></template
      >
    </el-dialog>
    <el-dialog v-model="creativeVisible" title="新增广告素材" width="650px">
      <el-form label-width="100px">
        <el-form-item label="素材名称"
          ><el-input v-model="creativeForm.name"
        /></el-form-item>
        <el-form-item label="素材类型"
          ><el-select v-model="creativeForm.creative_type"
            ><el-option label="文字" value="text" /><el-option
              label="图片"
              value="image" /><el-option
              label="图文"
              value="mixed" /></el-select
        ></el-form-item>
        <el-form-item label="广告正文"
          ><el-input v-model="creativeForm.content" type="textarea" :rows="6"
        /></el-form-item>
        <el-form-item
          v-if="creativeForm.creative_type !== 'text'"
          label="图片地址"
          ><el-input v-model="creativeForm.media_url" placeholder="https://..."
        /></el-form-item>
        <el-form-item label="落地页链接"
          ><el-input v-model="creativeForm.link_url"
        /></el-form-item>
      </el-form>
      <template #footer
        ><el-button @click="creativeVisible = false">取消</el-button
        ><el-button type="primary" :loading="saving" @click="saveCreative"
          >保存素材</el-button
        ></template
      >
    </el-dialog>
  </div>
</template>

<style scoped>
.qq-automation {
  display: grid;
  gap: 18px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
h1 {
  margin: 0;
  font-size: 22px;
}
p,
.form-note,
.toolbar span {
  color: #606266;
  font-size: 13px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.form-note {
  margin-left: 12px;
}
.el-select {
  width: 100%;
}
.el-pagination {
  margin: 16px 0;
  justify-content: flex-end;
}
</style>
