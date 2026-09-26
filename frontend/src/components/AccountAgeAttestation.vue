<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { accountsApi, type Account, type AgeAttestation } from '@/api/accounts'
import { useAuthStore } from '@/stores/auth'
import { ElMessage } from 'element-plus'
const props = defineProps<{ account: Account }>()
const auth = useAuthStore()
const proof = ref<AgeAttestation | undefined>(props.account.age_attestation)
const days = ref(180)
const busy = ref(false)
const pending = ref<{ key: string; data: { expected_version: number; minimum_age_days: number; revoke: boolean } } | null>(null)
watch(() => props.account, value => { proof.value = value.age_attestation; pending.value = null })
const label = computed(() => proof.value?.revoked_at ? '号龄声明已撤销' : proof.value?.confirmed_at ? `确认至少 ${proof.value.minimum_age_days} 天` : '尚无有效人工号龄声明')
async function save(revoke: boolean) {
  if (!pending.value) pending.value = { key: crypto.randomUUID(), data: { expected_version: proof.value?.version || 0, minimum_age_days: days.value, revoke } }
  busy.value = true
  try {
    proof.value = (await accountsApi.setAgeAttestation(props.account.id, pending.value.data, pending.value.key)).data.data
    pending.value = null
    ElMessage.success('号龄声明已更新')
  } catch { ElMessage.error('保存未确认，请重试原请求；若提示版本冲突请重新打开账号详情。') }
  finally { busy.value = false }
}
</script>
<template>
  <el-card shadow="never" style="margin-bottom: 16px">
    <strong>{{ label }}</strong>
    <p>这是号龄下限声明。准确注册时间单独保存，账号限制仍按实时状态执行。</p>
    <div v-if="proof?.confirmed_at">确认时间：{{ proof.confirmed_at }}；操作员 #{{ proof.confirmed_by }}；版本 {{ proof.version }}</div>
    <el-alert v-if="account.verified_age?.conflict" title="号龄证据冲突，推广已阻止，请核对注册日期与声明。" type="error" :closable="false" />
    <div v-if="auth.userInfo?.role === 'admin'" style="margin-top: 10px; display: flex; gap: 8px">
      <el-input-number v-model="days" :min="0" :max="36500" :disabled="busy || !!pending" />
      <el-button type="primary" :loading="busy" @click="save(false)">{{ pending ? '重试原请求' : '确认号龄下限' }}</el-button>
      <el-button :disabled="busy || !!pending || !proof?.confirmed_at || !!proof?.revoked_at" @click="save(true)">撤销声明</el-button>
    </div>
  </el-card>
</template>
