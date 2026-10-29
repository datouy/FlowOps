<template>
  <div class="account-management">
    <div class="page-header">
      <h1>账号管理</h1>
    </div>
    
    <div class="account-tabs">
      <el-tabs v-model="activeTab" class="account-tabs-nav">
        <el-tab-pane
          v-for="tab in accountTabs"
          :key="tab.name"
          :label="tab.label"
          :name="tab.name"
        >
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="输入名称或账号搜索"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">添加账号</el-button>
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">刷新中</span>
                </el-button>
              </div>
            </div>

            <div v-if="activeAccounts.length > 0" class="account-list">
              <el-table :data="activeAccounts" style="width: 100%">
                <el-table-column label="头像" width="80">
                  <template #default="scope">
                    <el-avatar :src="getDefaultAvatar(scope.row.name)" :size="40" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="状态">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '验证中' ? 'is-loading' : ''" v-if="scope.row.status === '验证中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <el-button size="small" @click="handleEdit(scope.row)">编辑</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下载Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上传Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>

            <div v-else class="empty-data">
              <el-empty :description="tabEmptyText(tab)" />
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>
    
    <!-- 添加/编辑账号对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="reLoginMode ? '重新登录账号' : (dialogType === 'add' ? '添加账号' : '编辑账号')"
      width="500px"
      :close-on-click-modal="false"
      :close-on-press-escape="!sseConnecting"
      :show-close="!sseConnecting"
    >
      <el-form :model="accountForm" label-width="80px" :rules="rules" ref="accountFormRef">
        <el-form-item label="平台" prop="platform">
          <el-select 
            v-model="accountForm.platform" 
            placeholder="请选择平台" 
            style="width: 100%"
            :disabled="dialogType === 'edit' || sseConnecting"
            @change="handleLoginPlatformChange"
          >
            <el-option
              v-for="platform in platformOptions"
              :key="platform"
              :label="platform"
              :value="platform"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="账号名称备注" prop="name">
          <el-input 
            v-model="accountForm.name" 
            placeholder="选填，不填将自动生成名称"
            :disabled="sseConnecting"
          />
        </el-form-item>
        <el-form-item
          v-if="accountForm.platform === '抖音' && (dialogType === 'add' || reLoginMode)"
          label="登录方式"
        >
          <el-radio-group v-model="loginMethod" :disabled="sseConnecting">
            <el-radio-button label="qr">扫码登录</el-radio-button>
            <el-radio-button label="sms">验证码登录</el-radio-button>
            <el-radio-button label="password">密码登录</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <template
          v-if="accountForm.platform === '抖音' && (dialogType === 'add' || reLoginMode) && loginMethod !== 'qr'"
        >
          <el-form-item label="手机号">
            <el-input
              v-model="loginPhone"
              maxlength="11"
              placeholder="请输入抖音绑定手机号"
              :disabled="sseConnecting"
            />
          </el-form-item>
          <el-form-item v-if="loginMethod === 'password'" label="登录密码">
            <el-input
              v-model="loginPassword"
              type="password"
              show-password
              autocomplete="current-password"
              placeholder="密码仅用于本次登录，不会保存"
              :disabled="sseConnecting"
              @keyup.enter="submitAccountForm"
            />
          </el-form-item>
          <p v-if="loginMethod === 'sms' && !sseConnecting" class="direct-login-hint">
            点击下方“确认”后，将通过抖音发送短信验证码，并打开登录浏览器；如出现安全验证，请在该浏览器中完成。
          </p>
        </template>
        
        <!-- 二维码显示区域 -->
        <div v-if="sseConnecting || loginStatus" class="qrcode-container">
          <div v-if="needSecondVerify && !loginStatus" class="verify-wrapper">
            <p class="qrcode-tip">
              {{ directSmsFlow ? '短信验证码已请求发送，请输入验证码完成登录' : '抖音要求短信二次验证，请按提示完成' }}
            </p>
            <el-input
              v-if="!directSmsFlow"
              v-model="verifyPhone"
              placeholder="请输入手机号"
              :disabled="verifyStage !== 'phone' || verifySubmitting"
            />
            <el-input
              v-model="verifyCode"
              placeholder="请输入短信验证码"
              :disabled="verifyStage !== 'code' || verifySubmitting"
            />
            <el-button
              v-if="verifyStage === 'phone'"
              type="primary"
              :loading="verifySubmitting"
              :disabled="verifySubmitting || verifyCooldown > 0"
              @click="submitSecondVerify('phone')"
            >
              {{ verifyCooldown > 0 ? `${verifyCooldown} 秒后可重新发送` : '发送验证码' }}
            </el-button>
            <el-button
              v-else
              type="primary"
              :loading="verifySubmitting"
              :disabled="verifySubmitting"
              @click="submitSecondVerify('code')"
            >
              {{ directSmsFlow ? '登录' : '提交验证' }}
            </el-button>
            <p class="verify-hint">
              {{ directSmsFlow && loginStatus !== '500'
                ? '若登录浏览器出现安全验证，请先在该浏览器中完成，完成后这里会自动确认登录。'
                : '若长时间收不到短信，请确认该手机号已绑定当前抖音账号。' }}
            </p>
          </div>
          <div v-else-if="qrCodeData && !loginStatus" class="qrcode-wrapper">
            <p class="qrcode-tip">二维码已生成，请在 120 秒内使用对应平台 APP 扫码</p>
            <img :src="qrCodeData" alt="登录二维码" class="qrcode-image" />
            <el-button
              class="refresh-qrcode-button"
              type="info"
              text
              bg
              @click="refreshLogin"
            >
              <el-icon><Refresh /></el-icon>
              二维码过期？刷新二维码
            </el-button>
          </div>
          <div v-else-if="!qrCodeData && !loginStatus" class="loading-wrapper">
            <el-icon class="is-loading"><Refresh /></el-icon>
            <span>{{ loginMethod === 'qr' ? '请求中...' : '正在登录抖音...' }}</span>
          </div>
          <div v-else-if="loginStatus === '200'" class="success-wrapper">
            <el-icon><CircleCheckFilled /></el-icon>
            <span>添加成功</span>
          </div>
          <div v-else-if="loginStatus === '500'" class="error-wrapper">
            <el-icon><CircleCloseFilled /></el-icon>
            <span>{{ loginErrorMessage || (loginMethod === 'qr' ? '登录失败或扫码超时，请重新发起登录' : '登录失败，请检查手机号、密码或验证码') }}</span>
          </div>
        </div>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="cancelLogin">取消</el-button>
          <el-button 
            type="primary" 
            @click="submitAccountForm" 
            :loading="sseConnecting && !qrCodeData"
            :disabled="sseConnecting"
          >
            {{
              sseConnecting
                ? (loginMethod === 'qr' ? (qrCodeData ? '等待扫码' : '获取二维码') : '登录中')
                : (dialogType === 'edit' && !reLoginMode
                  ? '保存'
                  : loginMethod === 'sms'
                    ? '发送验证码并登录'
                    : loginMethod === 'password'
                      ? '登录'
                      : '确认')
            }}
          </el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { Refresh, CircleCheckFilled, CircleCloseFilled, Download, Upload, Loading } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { accountApi } from '@/api/account'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { http } from '@/utils/request'
import { API_BASE, withToken } from '@/config'

// 获取账号状态管理
const accountStore = useAccountStore()
// 获取应用状态管理
const appStore = useAppStore()

// 平台元数据：标签页、添加账号下拉框和扫码登录共用同一份配置
const platformMeta = {
  '快手': { key: 'kuaishou', type: 4 },
  '抖音': { key: 'douyin', type: 3 },
  '视频号': { key: 'channels', type: 2 },
  '小红书': { key: 'xiaohongshu', type: 1 },
  '微博': { key: 'weibo', type: 5 },
  '百家号': { key: 'baijiahao', type: 6 }
}
const platformOptions = Object.keys(platformMeta)
const accountTabs = [
  { name: 'all', label: '全部' },
  ...platformOptions.map(label => ({ name: platformMeta[label].key, label }))
]

// 当前激活的标签页
const activeTab = ref('all')

// 搜索关键词
const searchKeyword = ref('')

// 获取账号数据（快速，不验证）
const fetchAccountsQuick = async () => {
  try {
    const res = await accountApi.getAccounts()
    if (res.code === 200 && res.data) {
      // 将所有账号的状态暂时设为"验证中"
      const accountsWithPendingStatus = res.data.map(account => {
        const updatedAccount = [...account];
        updatedAccount[4] = -1; // -1 表示验证中的临时状态
        return updatedAccount;
      });
      accountStore.setAccounts(accountsWithPendingStatus);
    }
  } catch (error) {
    console.error('快速获取账号数据失败:', error)
  }
}

// 获取账号数据（带验证）
const fetchAccounts = async () => {
  if (appStore.isAccountRefreshing) return

  appStore.setAccountRefreshing(true)

  try {
    const res = await accountApi.getValidAccounts()
    if (res.code === 200 && res.data) {
      accountStore.setAccounts(res.data)
      ElMessage.success('账号数据获取成功')
      // 标记为已访问
      if (appStore.isFirstTimeAccountManagement) {
        appStore.setAccountManagementVisited()
      }
    } else {
      ElMessage.error('获取账号数据失败')
    }
  } catch (error) {
    console.error('获取账号数据失败:', error)
    ElMessage.error('获取账号数据失败')
  } finally {
    appStore.setAccountRefreshing(false)
  }
}

// 后台验证所有账号（优化版本，使用setTimeout避免阻塞UI）
const validateAllAccountsInBackground = async () => {
  // 使用setTimeout将验证过程放在下一个事件循环，避免阻塞UI
  setTimeout(async () => {
    try {
      const res = await accountApi.getValidAccounts()
      if (res.code === 200 && res.data) {
        accountStore.setAccounts(res.data)
      }
    } catch (error) {
      console.error('后台验证账号失败:', error)
    }
  }, 0)
}

// 页面加载时获取账号数据
onMounted(() => {
  // 快速获取账号列表（不验证），立即显示
  fetchAccountsQuick()

  // 在后台验证所有账号
  setTimeout(() => {
    validateAllAccountsInBackground()
  }, 100) // 稍微延迟一下，让用户看到快速加载的效果
})

// 获取平台标签类型
const getPlatformTagType = (platform) => {
  const typeMap = {
    '快手': 'success',
    '抖音': 'danger',
    '视频号': 'warning',
    '小红书': 'info',
    '微博': 'danger',
    '百家号': 'primary'
  }
  return typeMap[platform] || 'info'
}

// 判断状态是否可点击（异常状态可点击）
const isStatusClickable = (status) => {
  return status === '异常'; // 只有异常状态可点击，验证中不可点击
}

// 获取状态标签类型
const getStatusTagType = (status) => {
  if (status === '验证中') {
    return 'info'; // 验证中使用灰色
  } else if (status === '正常') {
    return 'success'; // 正常使用绿色
  } else {
    return 'danger'; // 无效使用红色
  }
}

// 处理状态点击事件
const handleStatusClick = (row) => {
  if (isStatusClickable(row.status)) {
    // 触发重新登录流程
    handleReLogin(row)
  }
}

// 过滤后的账号列表
const filteredAccounts = computed(() => {
  if (!searchKeyword.value) return accountStore.accounts
  return accountStore.accounts.filter(account =>
    account.name.includes(searchKeyword.value)
  )
})

// 当前标签页对应的账号列表
const activeAccounts = computed(() => {
  const tab = accountTabs.find(item => item.name === activeTab.value)
  if (!tab || tab.name === 'all') return filteredAccounts.value
  return filteredAccounts.value.filter(account => account.platform === tab.label)
})

const tabEmptyText = (tab) => {
  return tab.name === 'all' ? '暂无账号数据' : `暂无${tab.label}账号数据`
}

// 搜索处理
const handleSearch = () => {
  // 搜索逻辑已通过计算属性实现
}

// 对话框相关
const dialogVisible = ref(false)
const dialogType = ref('add') // 'add' 或 'edit'
const reLoginMode = ref(false)
const accountFormRef = ref(null)

// 账号表单
const accountForm = reactive({
  id: null,
  name: '',
  platform: '',
  status: '正常'
})

// 表单验证规则
const rules = {
  platform: [{ required: true, message: '请选择平台', trigger: 'change' }],
  name: []
}

// SSE连接状态
const sseConnecting = ref(false)
const qrCodeData = ref('')
const loginStatus = ref('')
const needSecondVerify = ref(false)
const verifyStage = ref('phone')
const verifyPhone = ref('')
const verifyCode = ref('')
const verifySubmitting = ref(false)
const verifyCooldown = ref(0)
const loginMethod = ref('qr')
const loginPhone = ref('')
const loginPassword = ref('')
const loginErrorMessage = ref('')
const directSmsFlow = ref(false)
const currentLoginId = ref('')
const currentPlatform = ref('')
const currentAccountName = ref('')
const currentAccountId = ref(null)
let verifyCooldownTimer = null

const clearVerifyCooldown = () => {
  if (verifyCooldownTimer) {
    clearInterval(verifyCooldownTimer)
    verifyCooldownTimer = null
  }
  verifyCooldown.value = 0
}

const startVerifyCooldown = (seconds = 60) => {
  clearVerifyCooldown()
  verifyCooldown.value = seconds
  verifyCooldownTimer = setInterval(() => {
    verifyCooldown.value = Math.max(0, verifyCooldown.value - 1)
    if (verifyCooldown.value === 0) {
      clearVerifyCooldown()
    }
  }, 1000)
}

const handleLoginPlatformChange = (platform) => {
  if (platform !== '抖音') {
    loginMethod.value = 'qr'
    loginPhone.value = ''
    loginPassword.value = ''
  }
}

// 添加账号
const handleAddAccount = () => {
  const currentTab = accountTabs.find(item => item.name === activeTab.value)
  const defaultPlatform = currentTab && currentTab.name !== 'all' ? currentTab.label : ''
  dialogType.value = 'add'
  reLoginMode.value = false
  Object.assign(accountForm, {
    id: null,
    name: '',
    platform: defaultPlatform,
    status: '正常'
  })
  // 重置SSE状态
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  needSecondVerify.value = false
  verifyStage.value = 'phone'
  verifyPhone.value = ''
  verifyCode.value = ''
  verifySubmitting.value = false
  clearVerifyCooldown()
  loginMethod.value = 'qr'
  loginPhone.value = ''
  loginPassword.value = ''
  loginErrorMessage.value = ''
  directSmsFlow.value = false
  currentLoginId.value = ''
  dialogVisible.value = true
}

// 编辑账号
const handleEdit = (row) => {
  dialogType.value = 'edit'
  reLoginMode.value = false
  Object.assign(accountForm, {
    id: row.id,
    name: row.name,
    platform: row.platform,
    status: row.status
  })
  dialogVisible.value = true
}

// 删除账号
const handleDelete = (row) => {
  ElMessageBox.confirm(
    `确定要删除账号 ${row.name} 吗？`,
    '警告',
    {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    }
  )
    .then(async () => {
      try {
        // 调用API删除账号
        const response = await accountApi.deleteAccount(row.id)

        if (response.code === 200) {
          // 从状态管理中删除账号
          accountStore.deleteAccount(row.id)
          ElMessage({
            type: 'success',
            message: '删除成功',
          })
        } else {
          ElMessage.error(response.msg || '删除失败')
        }
      } catch (error) {
        console.error('删除账号失败:', error)
        ElMessage.error('删除账号失败')
      }
    })
    .catch(() => {
      // 取消删除
    })
}

// 下载Cookie文件
const handleDownloadCookie = (row) => {
  // 从后端获取Cookie文件
  const baseUrl = API_BASE
  const downloadUrl = withToken(`${baseUrl}/downloadCookie?filePath=${encodeURIComponent(row.filePath)}`)

  // 创建一个隐藏的链接来触发下载
  const link = document.createElement('a')
  link.href = downloadUrl
  link.download = `${row.name}_cookie.json`
  link.target = '_blank'
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

// 上传Cookie文件
const handleUploadCookie = (row) => {
  // 创建一个隐藏的文件输入框
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.json'
  input.style.display = 'none'
  document.body.appendChild(input)

  input.onchange = async (event) => {
    const file = event.target.files[0]
    if (!file) return

    // 检查文件类型
    if (!file.name.endsWith('.json')) {
      ElMessage.error('请选择JSON格式的Cookie文件')
      document.body.removeChild(input)
      return
    }

    try {
      // 创建FormData对象
      const formData = new FormData()
      formData.append('file', file)
      formData.append('id', row.id)
      formData.append('platform', row.platform)

      // 使用统一的http封装发送上传请求
      const result = await http.upload('/uploadCookie', formData)

      ElMessage.success('Cookie文件上传成功')
      // 刷新账号列表以显示更新
      fetchAccounts()
    } catch (error) {
      ElMessage.error('Cookie文件上传失败')
    } finally {
      document.body.removeChild(input)
    }
  }

  input.click()
}

// 重新登录账号
const handleReLogin = (row) => {
  // 设置表单信息
  dialogType.value = 'edit'
  reLoginMode.value = true
  Object.assign(accountForm, {
    id: row.id,
    name: row.name,
    platform: row.platform,
    status: row.status
  })

  // 重置SSE状态
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  needSecondVerify.value = false
  verifyStage.value = 'phone'
  verifyPhone.value = ''
  verifyCode.value = ''
  verifySubmitting.value = false
  clearVerifyCooldown()
  loginMethod.value = 'qr'
  loginPhone.value = ''
  loginPassword.value = ''
  loginErrorMessage.value = ''
  directSmsFlow.value = false
  currentLoginId.value = ''

  // 显示对话框，由用户选择扫码、验证码或密码登录
  dialogVisible.value = true
}

// 获取默认头像
const getDefaultAvatar = (name) => {
  // 使用简单的默认头像，可以基于用户名生成不同的颜色
  return `https://ui-avatars.com/api/?name=${encodeURIComponent(name)}&background=random`
}

// SSE事件源对象
let eventSource = null

// 关闭SSE连接
const closeSSEConnection = () => {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
}

const cancelLogin = () => {
  closeSSEConnection()
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  needSecondVerify.value = false
  verifyStage.value = 'phone'
  verifyPhone.value = ''
  verifyCode.value = ''
  verifySubmitting.value = false
  clearVerifyCooldown()
  loginMethod.value = 'qr'
  loginPhone.value = ''
  loginPassword.value = ''
  loginErrorMessage.value = ''
  directSmsFlow.value = false
  currentLoginId.value = ''
  reLoginMode.value = false
  dialogVisible.value = false
}

// 建立SSE连接
const connectSSE = async (platform, name, accountId = null, options = {}) => {
  // 关闭可能存在的连接
  closeSSEConnection()

  const authMethod = platform === '抖音' ? (options.authMethod || 'qr') : 'qr'
  loginMethod.value = authMethod

  // 设置连接状态
  sseConnecting.value = true
  qrCodeData.value = ''
  loginStatus.value = ''
  loginErrorMessage.value = ''
  needSecondVerify.value = false
  verifyStage.value = 'phone'
  verifyPhone.value = authMethod === 'sms' ? loginPhone.value.trim() : ''
  verifyCode.value = ''
  verifySubmitting.value = false
  directSmsFlow.value = authMethod === 'sms'
  clearVerifyCooldown()
  currentPlatform.value = platform
  currentAccountName.value = name
  currentAccountId.value = accountId
  currentLoginId.value = `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`

  // 获取平台类型编号
  const type = platformMeta[platform]?.type || 1

  if (authMethod !== 'qr') {
    try {
      await http.post('/loginPrepare', {
        session: currentLoginId.value,
        type,
        id: name,
        account_id: accountId,
        auth_method: authMethod,
        phone: loginPhone.value.trim(),
        password: loginPassword.value
      })
      loginPassword.value = ''
    } catch (error) {
      sseConnecting.value = false
      directSmsFlow.value = false
      return
    }
  }

  // 创建SSE连接
  const baseUrl = API_BASE
  const accountParam = accountId ? `&account_id=${encodeURIComponent(accountId)}` : ''
  const sessionParam = `&session=${encodeURIComponent(currentLoginId.value)}`
  const url = withToken(`${baseUrl}/login?type=${type}&id=${encodeURIComponent(name)}${accountParam}${sessionParam}`)

  eventSource = new EventSource(url)

  // 监听消息
  eventSource.onmessage = (event) => {
    const data = event.data

    if (data.startsWith('LOGIN_ERROR:')) {
      loginErrorMessage.value = data.slice('LOGIN_ERROR:'.length).trim() || '抖音登录失败'
      return
    }

    if (data === 'AUTH_SMS_CODE') {
      directSmsFlow.value = true
      needSecondVerify.value = true
      verifyStage.value = 'code'
      verifySubmitting.value = false
      return
    }

    // 抖音二次验证：先收集手机号，再收集短信验证码
    if (data === 'SECOND_VERIFY_PHONE' || data === 'SECOND_VERIFY_CODE') {
      needSecondVerify.value = true
      verifyStage.value = data === 'SECOND_VERIFY_PHONE' ? 'phone' : 'code'
      verifySubmitting.value = false
      return
    }

    // 二维码可能是 data URI、普通 URL 或纯 base64
    if (data && data !== '200' && data !== '500') {
      try {
        if (data.startsWith('data:image') || data.startsWith('http://') || data.startsWith('https://')) {
          qrCodeData.value = data
        } else {
          qrCodeData.value = `data:image/png;base64,${data}`
        }
      } catch (error) {
        // 处理二维码数据出错
      }
    }
    // 如果收到状态码
    else if (data === '200' || data === '500') {
      loginStatus.value = data
      closeSSEConnection()

      // 如果登录成功
      if (data === '200') {
        setTimeout(() => {
          // 1秒后关闭对话框并开始刷新
          setTimeout(() => {
            dialogVisible.value = false
            sseConnecting.value = false
            loginPassword.value = ''
            directSmsFlow.value = false
            reLoginMode.value = false

            // 根据是否是重新登录显示不同提示
            ElMessage.success(dialogType.value === 'edit' ? '重新登录成功' : '账号添加成功')

            // 显示更新账号信息提示
            ElMessage({
              type: 'info',
              message: '正在同步账号信息...',
              duration: 0
            })

            // 触发刷新操作
            fetchAccounts().then(() => {
              // 刷新完成后关闭提示
              ElMessage.closeAll()
              ElMessage.success('账号信息已更新')
            })
          }, 1000)
        }, 1000)
      } else {
        // 保留失败原因，直到用户重新发起登录，避免错误提示一闪而过。
        if (loginErrorMessage.value) {
          ElMessage.error(loginErrorMessage.value)
        }
        sseConnecting.value = false
        verifySubmitting.value = false
        loginPassword.value = ''
      }
    }
  }

  // 监听错误
  eventSource.onerror = (error) => {
    if (!eventSource || eventSource.readyState === EventSource.CLOSED) {
      return
    }
    console.error('SSE连接错误:', error)
    ElMessage.error('连接服务器失败，请稍后再试')
    closeSSEConnection()
    sseConnecting.value = false
  }
}

// 手动刷新二维码：重新开一个独立登录会话
const refreshLogin = () => {
  if (!currentPlatform.value || !currentAccountName.value) {
    return
  }
  ElMessage.info('正在刷新二维码')
  connectSSE(currentPlatform.value, currentAccountName.value, currentAccountId.value, { authMethod: 'qr' })
}

// 提交抖音短信二次验证
const submitSecondVerify = async (stage) => {
  if (verifySubmitting.value) return
  if (needSecondVerify.value && stage !== verifyStage.value) return
  if (!currentLoginId.value) {
    ElMessage.error('登录会话已失效，请重新发起登录')
    return
  }
  if (stage === 'phone' && !verifyPhone.value.trim()) {
    ElMessage.warning('请先输入手机号')
    return
  }
  if (stage === 'code' && !verifyCode.value.trim()) {
    ElMessage.warning('请先输入短信验证码')
    return
  }

  verifySubmitting.value = true
  try {
    await http.post('/loginVerify', {
      id: currentLoginId.value,
      phone: verifyPhone.value.trim(),
      code: verifyCode.value.trim(),
      stage
    })
    if (stage === 'phone') {
      ElMessage.info('手机号已提交，正在请求抖音发送验证码')
      startVerifyCooldown(60)
    }
  } catch (error) {
    // 请求工具已经统一弹出错误提示
  } finally {
    verifySubmitting.value = false
  }
}

// 提交账号表单
const submitAccountForm = () => {
  accountFormRef.value.validate(async (valid) => {
    if (valid) {
      if (dialogType.value === 'add' || reLoginMode.value) {
        // 名称选填：留空时自动生成默认名称（如“抖音账号1”）
        const name = accountForm.name.trim()
        if (!name) {
          const samePlatformCount = accountStore.accounts.filter(
            acc => acc.platform === accountForm.platform
          ).length
          accountForm.name = `${accountForm.platform}账号${samePlatformCount + 1}`
        }
        const authMethod = accountForm.platform === '抖音' ? loginMethod.value : 'qr'
        const normalizedPhone = loginPhone.value.replace(/[\s-]/g, '').replace(/^\+?86/, '')
        if (authMethod === 'sms' || authMethod === 'password') {
          if (!/^1\d{10}$/.test(normalizedPhone)) {
            ElMessage.warning('请输入正确的 11 位手机号')
            return
          }
          if (authMethod === 'password' && !loginPassword.value) {
            ElMessage.warning('请输入登录密码')
            return
          }
          loginPhone.value = normalizedPhone
        }
        // 建立SSE连接；重新登录时沿用已有账号 id
        await connectSSE(
          accountForm.platform,
          accountForm.name,
          reLoginMode.value ? accountForm.id : null,
          { authMethod }
        )
      } else {
        // 编辑账号逻辑
        try {
          // 将平台名称转换为类型数字
          const type = platformMeta[accountForm.platform]?.type || 1;

          const res = await accountApi.updateAccount({
            id: accountForm.id,
            type: type,
            userName: accountForm.name
          })
          if (res.code === 200) {
            // 更新状态管理中的账号
            const updatedAccount = {
              id: accountForm.id,
              name: accountForm.name,
              platform: accountForm.platform,
              status: accountForm.status // Keep the existing status
            };
            accountStore.updateAccount(accountForm.id, updatedAccount)
            ElMessage.success('更新成功')
            dialogVisible.value = false
            // 刷新账号列表
            fetchAccounts()
          } else {
            ElMessage.error(res.msg || '更新账号失败')
          }
        } catch (error) {
          console.error('更新账号失败:', error)
          ElMessage.error('更新账号失败')
        }
      }
    } else {
      return false
    }
  })
}

// 组件卸载前关闭SSE连接
onBeforeUnmount(() => {
  clearVerifyCooldown()
  closeSSEConnection()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

@keyframes rotate {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.account-management {
  .direct-login-hint {
    margin: -8px 0 14px 80px;
    color: #909399;
    font-size: 12px;
    line-height: 1.6;
  }

  .page-header {
    margin-bottom: 20px;
    
    h1 {
      font-size: 24px;
      color: $text-primary;
      margin: 0;
    }
  }
  
  .account-tabs {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: $box-shadow-light;
    
    .account-tabs-nav {
      padding: 20px;
    }
  }
  
  .account-list-container {
    .account-search {
      display: flex;
      justify-content: space-between;
      margin-bottom: 20px;
      
      .el-input {
        width: 300px;
      }
      
      .action-buttons {
        display: flex;
        gap: 10px;
        
        .el-icon.is-loading {
          animation: rotate 1s linear infinite;
        }
      }
    }
    
    .account-list {
      margin-bottom: 20px;
    }
    
    .empty-data {
      padding: 40px 0;
    }
  }
  
  // 二维码容器样式
  .clickable-status {
    cursor: pointer;
    transition: all 0.3s;

    &:hover {
      transform: scale(1.05);
      box-shadow: 0 0 8px rgba(0, 0, 0, 0.15);
    }
  }

  .qrcode-container {
    margin-top: 20px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 250px;
    
    .qrcode-wrapper {
      text-align: center;
      
      .qrcode-tip {
        margin-bottom: 15px;
        color: #606266;
      }
      
      .qrcode-image {
        max-width: 200px;
        max-height: 200px;
        border: 1px solid #ebeef5;
        background-color: black;
      }

      .refresh-qrcode-button {
        margin-top: 10px;
      }
    }

    .verify-wrapper {
      width: 100%;
      display: flex;
      flex-direction: column;
      align-items: stretch;
      gap: 12px;
      padding: 0 20px;

      .qrcode-tip {
        text-align: center;
        color: #606266;
      }

      .verify-hint {
        margin: 0;
        color: #909399;
        font-size: 12px;
        line-height: 1.6;
        text-align: center;
      }
    }
    
    .loading-wrapper, .success-wrapper, .error-wrapper {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 10px;
      
      .el-icon {
        font-size: 48px;
        
        &.is-loading {
          animation: rotate 1s linear infinite;
        }
      }
      
      span {
        font-size: 16px;
      }
    }
    
    .success-wrapper .el-icon {
      color: #67c23a;
    }
    
    .error-wrapper .el-icon {
      color: #f56c6c;
    }
  }
}
</style>
