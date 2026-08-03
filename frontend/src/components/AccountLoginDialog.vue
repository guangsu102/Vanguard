<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { ElMessage, ElSteps, ElStep, ElForm, ElFormItem, ElInput, ElButton, ElSelect, ElOption, ElUpload, ElAlert } from 'element-plus'
import type { FormInstance, FormRules, UploadRawFile } from 'element-plus'
import { proxiesApi, type Proxy } from '@/api/proxies'

interface Props {
  visible: boolean
}

interface Emits {
  (e: 'update:visible', value: boolean): void
  (e: 'success'): void
}

const props = defineProps<Props>()
const emit = defineEmits<Emits>()

const countryCodes = [
  'AD', 'AE', 'AF', 'AG', 'AI', 'AL', 'AM', 'AO', 'AQ', 'AR', 'AS', 'AT', 'AU', 'AW', 'AX',
  'AZ', 'BA', 'BB', 'BD', 'BE', 'BF', 'BG', 'BH', 'BI', 'BJ', 'BL', 'BM', 'BN', 'BO', 'BQ',
  'BR', 'BS', 'BT', 'BV', 'BW', 'BY', 'BZ', 'CA', 'CC', 'CD', 'CF', 'CG', 'CH', 'CI', 'CK',
  'CL', 'CM', 'CN', 'CO', 'CR', 'CU', 'CV', 'CW', 'CX', 'CY', 'CZ', 'DE', 'DJ', 'DK', 'DM',
  'DO', 'DZ', 'EC', 'EE', 'EG', 'EH', 'ER', 'ES', 'ET', 'FI', 'FJ', 'FK', 'FM', 'FO', 'FR',
  'GA', 'GB', 'GD', 'GE', 'GF', 'GG', 'GH', 'GI', 'GL', 'GM', 'GN', 'GP', 'GQ', 'GR', 'GS',
  'GT', 'GU', 'GW', 'GY', 'HK', 'HM', 'HN', 'HR', 'HT', 'HU', 'ID', 'IE', 'IL', 'IM', 'IN',
  'IO', 'IQ', 'IR', 'IS', 'IT', 'JE', 'JM', 'JO', 'JP', 'KE', 'KG', 'KH', 'KI', 'KM', 'KN',
  'KP', 'KR', 'KW', 'KY', 'KZ', 'LA', 'LB', 'LC', 'LI', 'LK', 'LR', 'LS', 'LT', 'LU', 'LV',
  'LY', 'MA', 'MC', 'MD', 'ME', 'MF', 'MG', 'MH', 'MK', 'ML', 'MM', 'MN', 'MO', 'MP', 'MQ',
  'MR', 'MS', 'MT', 'MU', 'MV', 'MW', 'MX', 'MY', 'MZ', 'NA', 'NC', 'NE', 'NF', 'NG', 'NI',
  'NL', 'NO', 'NP', 'NR', 'NU', 'NZ', 'OM', 'PA', 'PE', 'PF', 'PG', 'PH', 'PK', 'PL', 'PM',
  'PN', 'PR', 'PS', 'PT', 'PW', 'PY', 'QA', 'RE', 'RO', 'RS', 'RU', 'RW', 'SA', 'SB', 'SC',
  'SD', 'SE', 'SG', 'SH', 'SI', 'SJ', 'SK', 'SL', 'SM', 'SN', 'SO', 'SR', 'SS', 'ST', 'SV',
  'SX', 'SY', 'SZ', 'TC', 'TD', 'TF', 'TG', 'TH', 'TJ', 'TK', 'TL', 'TM', 'TN', 'TO', 'TR',
  'TT', 'TV', 'TW', 'TZ', 'UA', 'UG', 'UM', 'US', 'UY', 'UZ', 'VA', 'VC', 'VE', 'VG', 'VI',
  'VN', 'VU', 'WF', 'WS', 'YE', 'YT', 'ZA', 'ZM', 'ZW',
] as const

const regionNameFormatter = new Intl.DisplayNames(['zh-CN'], { type: 'region' })

const getCountryName = (code: string) => {
  try {
    return regionNameFormatter.of(code) || code
  } catch {
    return code
  }
}

const countryOptions = countryCodes.map((code) => {
  const name = getCountryName(code)
  return {
    label: `${code} - ${name}`,
    value: code,
    name,
  }
})

// Dialog state
const currentStep = ref(0)
const loginMethod = ref<'code' | 'session'>('code')
const proxyOptions = ref<Proxy[]>([])

// Form data
const formData = reactive({
  phone: '',
  apiConfigName: 'default',
  countryCode: 'US',
  countryName: '美国',
  profileBio: '',
  code: '',
  password: '',
  sessionId: '',
  requires2FA: false,
  sessionString: '',
  proxyMode: 'dynamic' as 'dynamic' | 'static' | 'none',
  staticProxyId: undefined as number | undefined,
})

// Session import data
const sessionImportData = reactive({
  phone: '',
  apiConfigName: 'default',
  countryCode: 'US',
  countryName: '美国',
  profileBio: '',
  sessionFile: null as File | null,
  proxyMode: 'dynamic' as 'dynamic' | 'static' | 'none',
  staticProxyId: undefined as number | undefined,
})

const formRef = ref<FormInstance>()
const sessionFormRef = ref<FormInstance>()

const loading = ref(false)

const formatProxyOption = (proxy: Proxy) => {
  const bound = proxy.bindAccountCount || 0
  const suffix = bound > 0 ? ` (${bound}/3)` : ' (0/3)'
  return `${proxy.protocol}://${proxy.address}:${proxy.port}${suffix}`
}

const isProxyFull = (proxy: Proxy) => (proxy.remainingBindSlots ?? Math.max(3 - (proxy.bindAccountCount || 0), 0)) <= 0

// Form rules
const phoneRules: FormRules = {
  phone: [
    { required: true, message: '请输入手机号', trigger: 'blur' },
    { pattern: /^\+\d{10,15}$/, message: '请输入正确的国际手机号格式（如 +8613800138000）', trigger: 'blur' },
  ],
  apiConfigName: [{ required: true, message: '请选择API配置', trigger: 'change' }],
  countryCode: [{ required: true, message: '请选择国家代码', trigger: 'change' }],
  staticProxyId: [
    {
      validator: (_rule, value, callback) => {
        if (formData.proxyMode === 'static' && !value) {
          callback(new Error('请选择静态代理'))
          return
        }
        callback()
      },
      trigger: 'change',
    },
  ],
}

const codeRules: FormRules = {
  code: [
    { required: true, message: '请输入验证码', trigger: 'blur' },
    { pattern: /^\d{4,8}$/, message: '验证码为4-8位数字', trigger: 'blur' },
  ],
}

const passwordRules: FormRules = {
  password: [{ required: true, message: '请输入2FA密码', trigger: 'blur' }],
}

const sessionImportRules: FormRules = {
  phone: [
    { required: true, message: '请输入手机号', trigger: 'blur' },
    { pattern: /^\+\d{10,15}$/, message: '请输入正确的国际手机号格式', trigger: 'blur' },
  ],
  apiConfigName: [{ required: true, message: '请选择API配置', trigger: 'change' }],
  countryCode: [{ required: true, message: '请选择国家代码', trigger: 'change' }],
  staticProxyId: [
    {
      validator: (_rule, value, callback) => {
        if (sessionImportData.proxyMode === 'static' && !value) {
          callback(new Error('请选择静态代理'))
          return
        }
        callback()
      },
      trigger: 'change',
    },
  ],
}

// Computed
const dialogVisible = computed({
  get: () => props.visible,
  set: (val) => emit('update:visible', val),
})

const stepTitle = computed(() => {
  if (loginMethod.value === 'session') {
    return '导入Session文件'
  }

  switch (currentStep.value) {
    case 0:
      return '输入手机号'
    case 1:
      return formData.requires2FA ? '输入2FA密码' : '输入验证码'
    case 2:
      return '完成'
    default:
      return ''
  }
})

// Methods
const handleClose = () => {
  resetForm()
  dialogVisible.value = false
}

const loadProxyOptions = async () => {
  try {
    const response = await proxiesApi.list({ page: 1, pageSize: 200, status: 'active' })
    proxyOptions.value = response.data.data.list || []
  } catch (error) {
    console.error('Load proxies error:', error)
  }
}

watch(dialogVisible, (visible) => {
  if (visible) {
    loadProxyOptions()
  }
})

const resetForm = () => {
  currentStep.value = 0
  loginMethod.value = 'code'
  Object.assign(formData, {
    phone: '',
    apiConfigName: 'default',
    countryCode: 'US',
    countryName: '美国',
    profileBio: '',
    code: '',
    password: '',
    sessionId: '',
    requires2FA: false,
    sessionString: '',
    proxyMode: 'dynamic',
    staticProxyId: undefined,
  })
  Object.assign(sessionImportData, {
    phone: '',
    apiConfigName: 'default',
    countryCode: 'US',
    countryName: '美国',
    profileBio: '',
    sessionFile: null,
    proxyMode: 'dynamic',
    staticProxyId: undefined,
  })
  formRef.value?.clearValidate()
  sessionFormRef.value?.clearValidate()
}

const handleCountryChange = (
  target: { countryCode: string; countryName: string },
  code: string
) => {
  const selected = countryOptions.find((item) => item.value === code)
  target.countryCode = code
  target.countryName = selected?.name || ''
}

const formatApiError = (result: any, fallback: string) => {
  if (result?.message) {
    return result.message
  }

  if (typeof result?.detail === 'string') {
    return result.detail
  }

  if (Array.isArray(result?.detail) && result.detail.length > 0) {
    return result.detail.map((item: any) => item?.msg || item?.message || String(item)).join('；')
  }

  return fallback
}

// Step 1: Send verification code
const handleSendCode = async () => {
  if (!formRef.value) return

  try {
    await formRef.value.validateField(['phone', 'apiConfigName', 'countryCode', 'staticProxyId'])
  } catch {
    return
  }

  loading.value = true
  try {
    const response = await fetch('/api/accounts/auth/send-code', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('token')}`,
      },
      body: JSON.stringify({
        phone: formData.phone,
        api_config_name: formData.apiConfigName,
        country_code: formData.countryCode,
        proxy_mode: formData.proxyMode,
        static_proxy_id: formData.proxyMode === 'static' ? formData.staticProxyId : undefined,
      }),
    })

    const result = await response.json()

    if (response.ok && result.code === 0) {
      formData.sessionId = result.data.session_id
      currentStep.value = 1
      ElMessage.success('验证码已发送，请查收短信或Telegram消息')
    } else {
      ElMessage.error(formatApiError(result, '发送验证码失败'))
    }
  } catch (error) {
    console.error('Send code error:', error)
    ElMessage.error('发送验证码失败')
  } finally {
    loading.value = false
  }
}

// Step 2: Verify code
const handleVerifyCode = async () => {
  if (!formRef.value) return

  try {
    await formRef.value.validateField(['code'])
  } catch {
    return
  }

  loading.value = true
  try {
    const response = await fetch('/api/accounts/auth/verify-code', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('token')}`,
      },
      body: JSON.stringify({
        session_id: formData.sessionId,
        code: formData.code,
      }),
    })

    const result = await response.json()

    if (response.ok && result.code === 0) {
      if (result.data.requires_2fa) {
        // Need 2FA
        formData.requires2FA = true
        ElMessage.warning('该账号开启了两步验证，请输入2FA密码')
      } else {
        // Login successful
        formData.sessionString = result.data.session_string
        await completeLogin()
      }
    } else {
      ElMessage.error(formatApiError(result, '验证码错误'))
    }
  } catch (error) {
    console.error('Verify code error:', error)
    ElMessage.error('验证失败')
  } finally {
    loading.value = false
  }
}

// Step 3: Verify 2FA (if needed)
const handleVerify2FA = async () => {
  if (!formRef.value) return

  try {
    await formRef.value.validateField(['password'])
  } catch {
    return
  }

  loading.value = true
  try {
    const response = await fetch('/api/accounts/auth/verify-2fa', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('token')}`,
      },
      body: JSON.stringify({
        session_id: formData.sessionId,
        password: formData.password,
      }),
    })

    const result = await response.json()

    if (response.ok && result.code === 0) {
      formData.sessionString = result.data.session_string
      await completeLogin()
    } else {
      ElMessage.error(formatApiError(result, '2FA密码错误'))
    }
  } catch (error) {
    console.error('Verify 2FA error:', error)
    ElMessage.error('验证失败')
  } finally {
    loading.value = false
  }
}

// Complete login and create account
const completeLogin = async () => {
  loading.value = true
  try {
    const response = await fetch('/api/accounts/auth/complete-login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('token')}`,
      },
      body: JSON.stringify({
        phone: formData.phone,
        api_config_name: formData.apiConfigName,
        country_code: formData.countryCode,
        country_name: formData.countryName,
        profile_bio: formData.profileBio.trim() || undefined,
        session_string: formData.sessionString,
        proxy_mode: formData.proxyMode,
        static_proxy_id: formData.proxyMode === 'static' ? formData.staticProxyId : undefined,
      }),
    })

    const result = await response.json()

    if (response.ok) {
      ElMessage.success('账号添加成功')
      emit('success')
      handleClose()
    } else {
      ElMessage.error(formatApiError(result, '添加账号失败'))
    }
  } catch (error) {
    console.error('Complete login error:', error)
    ElMessage.error('添加账号失败')
  } finally {
    loading.value = false
  }
}

// Import session file
const handleSessionFileChange = (file: UploadRawFile) => {
  sessionImportData.sessionFile = file
  return false // Prevent auto upload
}

const handleImportSession = async () => {
  if (!sessionFormRef.value) return

  await sessionFormRef.value.validate(async (valid) => {
    if (!valid) return

    if (!sessionImportData.sessionFile) {
      ElMessage.error('请选择session文件')
      return
    }

    loading.value = true
    try {
      const formDataToSend = new FormData()
      formDataToSend.append('phone', sessionImportData.phone)
      formDataToSend.append('api_config_name', sessionImportData.apiConfigName)
      formDataToSend.append('country_code', sessionImportData.countryCode)
      if (sessionImportData.countryName) {
        formDataToSend.append('country_name', sessionImportData.countryName)
      }
      if (sessionImportData.profileBio.trim()) {
        formDataToSend.append('profile_bio', sessionImportData.profileBio.trim())
      }
      formDataToSend.append('proxy_mode', sessionImportData.proxyMode)
      if (sessionImportData.proxyMode === 'static' && sessionImportData.staticProxyId) {
        formDataToSend.append('static_proxy_id', String(sessionImportData.staticProxyId))
      }
      formDataToSend.append('session_file', sessionImportData.sessionFile)

      const response = await fetch('/api/accounts/auth/import-session', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${localStorage.getItem('token')}`,
        },
        body: formDataToSend,
      })

      const result = await response.json()

      if (response.ok && result.code === 0) {
        ElMessage.success('Session导入成功')
        emit('success')
        handleClose()
      } else {
        ElMessage.error(formatApiError(result, '导入失败'))
      }
    } catch (error) {
      console.error('Import session error:', error)
      ElMessage.error('导入失败')
    } finally {
      loading.value = false
    }
  })
}

const handleNext = () => {
  if (currentStep.value === 0) {
    handleSendCode()
  } else if (currentStep.value === 1) {
    if (formData.requires2FA) {
      handleVerify2FA()
    } else {
      handleVerifyCode()
    }
  }
}
</script>

<template>
  <el-dialog
    v-model="dialogVisible"
    :title="stepTitle"
    width="600px"
    :close-on-click-modal="false"
    @close="handleClose"
  >
    <!-- Login method selection -->
    <div v-if="currentStep === 0" class="method-selection">
      <el-button
        :type="loginMethod === 'code' ? 'primary' : 'default'"
        @click="loginMethod = 'code'"
      >
        验证码登录
      </el-button>
      <el-button
        :type="loginMethod === 'session' ? 'primary' : 'default'"
        @click="loginMethod = 'session'"
      >
        导入Session文件
      </el-button>
    </div>

    <!-- Code login flow -->
    <div v-if="loginMethod === 'code'">
      <el-steps :active="currentStep" align-center style="margin-bottom: 30px">
        <el-step title="输入手机号" />
        <el-step :title="formData.requires2FA ? '输入2FA密码' : '输入验证码'" />
        <el-step title="完成" />
      </el-steps>

      <el-form ref="formRef" :model="formData" label-width="100px">
        <!-- Step 0: Phone number -->
        <div v-show="currentStep === 0">
          <el-form-item label="手机号" prop="phone" :rules="phoneRules.phone">
            <el-input
              v-model="formData.phone"
              placeholder="+8613800138000"
              clearable
            />
          </el-form-item>
          <el-form-item label="API配置" prop="apiConfigName" :rules="phoneRules.apiConfigName">
            <el-select v-model="formData.apiConfigName" placeholder="请选择" style="width: 100%">
              <el-option label="默认配置" value="default" />
            </el-select>
          </el-form-item>
          <el-form-item label="国家代码" prop="countryCode" :rules="phoneRules.countryCode">
            <el-select
              v-model="formData.countryCode"
              filterable
              placeholder="请选择国家代码"
              style="width: 100%"
              @change="handleCountryChange(formData, $event)"
            >
              <el-option
                v-for="country in countryOptions"
                :key="country.value"
                :label="country.label"
                :value="country.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="国家名称" prop="countryName">
            <el-input v-model="formData.countryName" placeholder="自动填充，可手动调整" />
          </el-form-item>
          <el-form-item label="账号简介" prop="profileBio">
            <el-input
              v-model="formData.profileBio"
              type="textarea"
              :rows="3"
              maxlength="70"
              show-word-limit
              placeholder="用户点开账号资料时看到的简介"
            />
          </el-form-item>
          <el-form-item label="代理模式" prop="proxyMode">
            <el-select v-model="formData.proxyMode" style="width: 100%">
              <el-option label="动态住宅代理" value="dynamic" />
              <el-option label="静态绑定代理" value="static" />
              <el-option label="不使用代理" value="none" />
            </el-select>
          </el-form-item>
          <el-form-item
            v-if="formData.proxyMode === 'static'"
            label="静态代理"
            prop="staticProxyId"
            :rules="phoneRules.staticProxyId"
          >
            <el-alert
              v-if="proxyOptions.length === 0"
              type="warning"
              show-icon
              :closable="false"
              title="暂无可用静态代理IP，请先到增长中心的静态代理IP页面添加。"
              class="proxy-empty-alert"
            />
            <el-select
              v-model="formData.staticProxyId"
              filterable
              placeholder="请选择静态代理"
              style="width: 100%"
            >
              <el-option
                v-for="proxy in proxyOptions"
                :key="proxy.id"
                :label="formatProxyOption(proxy)"
                :value="proxy.id"
                :disabled="isProxyFull(proxy)"
              />
            </el-select>
          </el-form-item>
        </div>

        <!-- Step 1: Verification code or 2FA -->
        <div v-show="currentStep === 1">
          <el-form-item
            v-if="!formData.requires2FA"
            label="验证码"
            prop="code"
            :rules="codeRules.code"
          >
            <el-input
              v-model="formData.code"
              placeholder="请输入4-8位验证码"
              maxlength="8"
              clearable
            />
          </el-form-item>
          <el-form-item
            v-else
            label="2FA密码"
            prop="password"
            :rules="passwordRules.password"
          >
            <el-input
              v-model="formData.password"
              type="password"
              placeholder="请输入两步验证密码"
              show-password
              clearable
            />
          </el-form-item>
        </div>
      </el-form>
    </div>

    <!-- Session import flow -->
    <div v-else>
      <el-form ref="sessionFormRef" :model="sessionImportData" label-width="100px">
        <el-form-item label="手机号" prop="phone" :rules="sessionImportRules.phone">
          <el-input
            v-model="sessionImportData.phone"
            placeholder="+8613800138000"
            clearable
          />
        </el-form-item>
        <el-form-item label="API配置" prop="apiConfigName" :rules="sessionImportRules.apiConfigName">
          <el-select v-model="sessionImportData.apiConfigName" placeholder="请选择" style="width: 100%">
            <el-option label="默认配置" value="default" />
          </el-select>
        </el-form-item>
        <el-form-item label="国家代码" prop="countryCode" :rules="sessionImportRules.countryCode">
          <el-select
            v-model="sessionImportData.countryCode"
            filterable
            placeholder="请选择国家代码"
            style="width: 100%"
            @change="handleCountryChange(sessionImportData, $event)"
          >
            <el-option
              v-for="country in countryOptions"
              :key="country.value"
              :label="country.label"
              :value="country.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="国家名称" prop="countryName">
          <el-input v-model="sessionImportData.countryName" placeholder="自动填充，可手动调整" />
        </el-form-item>
        <el-form-item label="账号简介" prop="profileBio">
          <el-input
            v-model="sessionImportData.profileBio"
            type="textarea"
            :rows="3"
            maxlength="70"
            show-word-limit
            placeholder="用户点开账号资料时看到的简介"
          />
        </el-form-item>
        <el-form-item label="代理模式" prop="proxyMode">
          <el-select v-model="sessionImportData.proxyMode" style="width: 100%">
            <el-option label="动态住宅代理" value="dynamic" />
            <el-option label="静态绑定代理" value="static" />
            <el-option label="不使用代理" value="none" />
          </el-select>
        </el-form-item>
        <el-form-item
          v-if="sessionImportData.proxyMode === 'static'"
          label="静态代理"
          prop="staticProxyId"
          :rules="sessionImportRules.staticProxyId"
        >
          <el-alert
            v-if="proxyOptions.length === 0"
            type="warning"
            show-icon
            :closable="false"
            title="暂无可用静态代理IP，请先到增长中心的静态代理IP页面添加。"
            class="proxy-empty-alert"
          />
          <el-select
            v-model="sessionImportData.staticProxyId"
            filterable
            placeholder="请选择静态代理"
            style="width: 100%"
          >
            <el-option
              v-for="proxy in proxyOptions"
              :key="proxy.id"
              :label="formatProxyOption(proxy)"
              :value="proxy.id"
              :disabled="isProxyFull(proxy)"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="Session文件" required>
          <el-upload
            :before-upload="handleSessionFileChange"
            :show-file-list="true"
            :limit="1"
            accept=".session"
            :auto-upload="false"
          >
            <el-button>选择.session文件</el-button>
          </el-upload>
        </el-form-item>
      </el-form>
    </div>

    <template #footer>
      <div class="dialog-footer">
        <el-button @click="handleClose">取消</el-button>
        <el-button
          v-if="loginMethod === 'code' && currentStep > 0"
          @click="currentStep--"
        >
          上一步
        </el-button>
        <el-button
          v-if="loginMethod === 'code'"
          type="primary"
          :loading="loading"
          @click="handleNext"
        >
          {{ currentStep === 1 ? '完成' : '下一步' }}
        </el-button>
        <el-button
          v-else
          type="primary"
          :loading="loading"
          @click="handleImportSession"
        >
          导入
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.method-selection {
  display: flex;
  gap: 12px;
  justify-content: center;
  margin-bottom: 30px;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
