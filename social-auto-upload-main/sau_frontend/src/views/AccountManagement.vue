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
      :width="interactiveLoginActive ? 'min(1080px, 94vw)' : '500px'"
      :close-on-click-modal="false"
      :close-on-press-escape="!sseConnecting && !interactiveLoginActive"
      :show-close="!sseConnecting && !interactiveLoginActive"
      @closed="closeInteractiveLogin"
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
          v-if="interactiveLoginPlatform && (dialogType === 'add' || reLoginMode)"
          label="登录方式"
        >
          <div class="official-login-note">
            <strong>{{ interactiveLoginPlatform }}官方登录窗口</strong>
            <span>扫码、短信验证码、密码和滑块验证都在该窗口内完成，与直接打开{{ interactiveLoginPlatform }}官方页面一致。</span>
          </div>
        </el-form-item>

        <div v-if="interactiveLoginActive || interactiveLoginStatus" class="interactive-login-panel">
          <div class="interactive-login-status" :class="interactiveLoginStatus">
            <span>{{ interactiveLoginMessage }}</span>
            <el-button
              v-if="interactiveLoginStatus === 'failed' || interactiveLoginStatus === 'expired'"
              type="primary"
              size="small"
              @click="retryInteractiveLogin"
            >重新打开</el-button>
          </div>
          <div
            v-if="interactiveLoginStatus === 'running' || interactiveLoginStatus === 'starting'"
            class="remote-browser-stage"
            :class="{ dragging: remotePointerDown }"
            :style="remoteStageStyle"
          >
            <img
              ref="interactiveImageRef"
              v-if="interactiveFrameUrl"
              :src="interactiveFrameUrl"
              :alt="`${interactiveLoginPlatform || '官方'}登录窗口`"
              draggable="false"
              @pointerdown.prevent="startRemotePointer"
              @pointermove.prevent="moveRemotePointer"
              @pointerup.prevent="endRemotePointer"
              @pointercancel="endRemotePointer"
              @wheel.prevent="scrollRemoteBrowser"
              :style="remoteImageStyle"
            />
            <div v-else class="remote-browser-loading">
              <el-icon class="is-loading"><Refresh /></el-icon>
              <span>正在载入{{ interactiveLoginPlatform || '官方' }}登录页...</span>
            </div>
          </div>
          <div
            v-if="interactiveLoginStatus === 'running' || interactiveLoginStatus === 'starting'"
            class="remote-browser-controls"
          >
            <el-input
              v-model="remoteInputText"
              maxlength="128"
              placeholder="先点击上方手机号或密码框，再在这里输入内容"
              @keyup.enter="sendRemoteText"
            >
              <template #append>
                <el-button @click="sendRemoteText">输入到登录框</el-button>
              </template>
            </el-input>
            <div class="remote-key-row">
              <el-button size="small" @click="sendRemoteKey('Tab')">Tab</el-button>
              <el-button size="small" @click="sendRemoteKey('Backspace')">退格</el-button>
              <el-button size="small" @click="sendRemoteKey('Enter')">Enter</el-button>
              <el-button size="small" @click="reloadRemoteBrowser">刷新页面</el-button>
              <span>滑块验证可直接在上方画面中拖动。</span>
            </div>
          </div>
          <div
            v-if="interactiveLoginStatus === 'running' || interactiveLoginStatus === 'starting'"
            class="remote-zoom-row"
          >
            <span class="remote-zoom-label">画面缩放</span>
            <el-button size="small" @click="zoomRemoteOut">缩小</el-button>
            <span class="remote-zoom-value">{{ Math.round(remoteZoom * 100) }}%</span>
            <el-button size="small" @click="zoomRemoteIn">放大</el-button>
            <el-button size="small" text @click="resetRemoteZoom">重置</el-button>
            <span class="remote-zoom-hint">放大后拖动画面下方的滚动条查看，二维码更清晰</span>
          </div>
        </div>
        
        <!-- 二维码显示区域 -->
        <div v-if="!interactiveLoginActive && (sseConnecting || loginStatus)" class="qrcode-container">
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
              {{ verifySubmitted
                ? '验证码已提交，正在等待抖音确认登录。正常需要几秒钟；若平台弹出安全验证，请完成后再等待结果。'
                : directSmsFlow && loginStatus !== '500'
                  ? '若登录浏览器出现安全验证，请先在该浏览器中完成，完成后这里会自动确认登录。'
                  : '若长时间收不到短信，请确认该手机号已绑定当前抖音账号。' }}
            </p>
          </div>
          <div v-else-if="qrCodeData && !loginStatus" class="qrcode-wrapper">
            <p class="qrcode-tip">二维码已生成，请在 120 秒内使用对应平台 APP 扫码</p>
            <img :src="qrCodeData" alt="登录二维码" class="qrcode-image" :style="qrImageStyle" />
            <div class="qr-zoom-row">
              <el-button size="small" @click="zoomQrOut">缩小</el-button>
              <span class="qr-zoom-value">{{ Math.round(qrZoom * 100) }}%</span>
              <el-button size="small" @click="zoomQrIn">放大</el-button>
              <el-button size="small" text @click="resetQrZoom">重置</el-button>
            </div>
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
            <p v-if="loginProgressText" class="qrcode-progress">
              <el-icon class="is-loading"><Refresh /></el-icon>
              <span>{{ loginProgressText }}</span>
            </p>
          </div>
          <div v-else-if="!qrCodeData && !loginStatus" class="loading-wrapper">
            <el-icon v-if="!loginProgressText" class="is-loading"><Refresh /></el-icon>
            <span>{{ loginProgressText || (loginMethod === 'qr' ? '请求中...' : '正在登录抖音...') }}</span>
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
            :loading="interactiveLoginStarting || (sseConnecting && !qrCodeData)"
            :disabled="sseConnecting || interactiveLoginActive"
          >
            {{
              interactiveLoginActive
                ? '请在官方窗口完成登录'
                : interactiveLoginStarting
                  ? '正在打开官方窗口'
                  : sseConnecting
                ? (loginMethod === 'qr' ? (qrCodeData ? '等待扫码' : '获取二维码') : '登录中')
                : (dialogType === 'edit' && !reLoginMode
                  ? '保存'
                  : INTERACTIVE_PLATFORMS[accountForm.platform]
                    ? `打开${accountForm.platform}官方登录窗口`
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
import { accountApi } from '@/api/account'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { http } from '@/utils/request'
import { API_BASE, withToken, authHeaders } from '@/config'

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
// 走"官方登录窗口"的平台：把平台真实登录页拉到对话框里完成登录。
// 视频号也走这里——它的登录页是微信扫码界面，直接把真实页面搬到对话框，
// 用户看到的就是官方页面，二维码清晰可扫、扫码状态一目了然。
const INTERACTIVE_PLATFORMS = {
  '抖音': 'douyin',
  '小红书': 'xiaohongshu',
  '视频号': 'tencent'
}
// 平台键 -> 中文名，用于官方登录窗口的提示文案。
const INTERACTIVE_PLATFORM_LABELS = {
  douyin: '抖音',
  xiaohongshu: '小红书',
  tencent: '视频号'
}
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
const verifySubmitted = ref(false)
const verifyCooldown = ref(0)
const loginMethod = ref('qr')
const loginPhone = ref('')
const loginPassword = ref('')
const loginErrorMessage = ref('')
const loginProgressText = ref('')
const directSmsFlow = ref(false)
const currentLoginId = ref('')
const currentPlatform = ref('')
const currentAccountName = ref('')
const currentAccountId = ref(null)
// 当前走"官方登录窗口"的平台 key（douyin / xiaohongshu）
const currentInteractivePlatform = ref('douyin')
let verifyCooldownTimer = null
const interactiveLoginActive = ref(false)
const interactiveLoginStarting = ref(false)
const interactiveLoginStatus = ref('')
const interactiveLoginMessage = ref('')
const interactiveLoginId = ref('')
const interactiveLoginPlatform = ref('')
const interactiveFrameUrl = ref('')
const interactiveImageRef = ref(null)
const remoteInputText = ref('')
const remotePointerDown = ref(false)
let interactiveFrameTimer = null
let interactiveStatusTimer = null
let interactiveFrameLoading = false
let interactiveFrameObjectUrl = ''
let remoteLastMoveAt = 0
const remoteZoom = ref(1)
const qrZoom = ref(1)
const REMOTE_ZOOM_MIN = 0.5
const REMOTE_ZOOM_MAX = 3
const QR_ZOOM_MIN = 1
const QR_ZOOM_MAX = 4

const clampZoom = (value, min, max) => Math.min(max, Math.max(min, Math.round(value * 100) / 100))

const remoteStageStyle = computed(() =>
  remoteZoom.value > 1 ? { overflow: 'auto' } : {}
)

const remoteImageStyle = computed(() => {
  if (remoteZoom.value <= 1) return {}
  return {
    width: `${remoteZoom.value * 100}%`,
    maxHeight: 'none',
  }
})

const qrImageStyle = computed(() => ({
  width: `${200 * qrZoom.value}px`,
  maxWidth: 'none',
  maxHeight: 'none',
}))

const zoomRemoteIn = () => { remoteZoom.value = clampZoom(remoteZoom.value + 0.25, REMOTE_ZOOM_MIN, REMOTE_ZOOM_MAX) }
const zoomRemoteOut = () => { remoteZoom.value = clampZoom(remoteZoom.value - 0.25, REMOTE_ZOOM_MIN, REMOTE_ZOOM_MAX) }
const resetRemoteZoom = () => { remoteZoom.value = 1 }
const zoomQrIn = () => { qrZoom.value = clampZoom(qrZoom.value + 0.5, QR_ZOOM_MIN, QR_ZOOM_MAX) }
const zoomQrOut = () => { qrZoom.value = clampZoom(qrZoom.value - 0.5, QR_ZOOM_MIN, QR_ZOOM_MAX) }
const resetQrZoom = () => { qrZoom.value = 1 }

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
  resetInteractiveLogin()
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
  verifySubmitted.value = false
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
  resetInteractiveLogin()
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
  verifySubmitted.value = false
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
  closeInteractiveLogin()
  closeSSEConnection()
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  needSecondVerify.value = false
  verifyStage.value = 'phone'
  verifyPhone.value = ''
  verifyCode.value = ''
  verifySubmitting.value = false
  verifySubmitted.value = false
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

const clearInteractiveTimers = () => {
  if (interactiveFrameTimer) {
    clearInterval(interactiveFrameTimer)
    interactiveFrameTimer = null
  }
  if (interactiveStatusTimer) {
    clearInterval(interactiveStatusTimer)
    interactiveStatusTimer = null
  }
}

const releaseInteractiveFrame = () => {
  if (interactiveFrameObjectUrl) {
    URL.revokeObjectURL(interactiveFrameObjectUrl)
    interactiveFrameObjectUrl = ''
  }
  interactiveFrameUrl.value = ''
}

const resetInteractiveLogin = () => {
  clearInteractiveTimers()
  releaseInteractiveFrame()
  interactiveLoginActive.value = false
  interactiveLoginStarting.value = false
  interactiveLoginStatus.value = ''
  interactiveLoginMessage.value = ''
  interactiveLoginId.value = ''
  interactiveLoginPlatform.value = ''
  remoteInputText.value = ''
  remotePointerDown.value = false
  interactiveFrameLoading = false
}

const closeInteractiveLogin = (notifyServer = true) => {
  const sessionId = interactiveLoginId.value
  clearInteractiveTimers()
  releaseInteractiveFrame()
  if (notifyServer && sessionId && interactiveLoginStatus.value !== 'success') {
    http.post('/interactiveLogin/stop', { id: sessionId }, { silent: true }).catch(() => {})
  }
  resetInteractiveLogin()
}

const refreshInteractiveFrame = async () => {
  if (!interactiveLoginId.value || interactiveFrameLoading) return
  const status = interactiveLoginStatus.value
  if (status !== 'running' && status !== 'starting') return
  interactiveFrameLoading = true
  try {
    const response = await fetch(
      `${API_BASE}/interactiveLogin/frame?id=${encodeURIComponent(interactiveLoginId.value)}&t=${Date.now()}`,
      { headers: authHeaders(), cache: 'no-store' }
    )
    if (response.ok && response.status === 200) {
      const blob = await response.blob()
      if (blob.size > 0) {
        const nextUrl = URL.createObjectURL(blob)
        if (interactiveFrameObjectUrl) URL.revokeObjectURL(interactiveFrameObjectUrl)
        interactiveFrameObjectUrl = nextUrl
        interactiveFrameUrl.value = nextUrl
      }
    }
  } catch (error) {
    // A later poll will recover from a transient frame timeout.
  } finally {
    interactiveFrameLoading = false
  }
}

const completeInteractiveLogin = () => {
  if (interactiveLoginStatus.value === 'success') return
  interactiveLoginStatus.value = 'success'
  interactiveLoginMessage.value = '登录成功，账号和 Cookie 已保存'
  clearInteractiveTimers()
  setTimeout(async () => {
    dialogVisible.value = false
    interactiveLoginActive.value = false
    reLoginMode.value = false
    await fetchAccounts()
    ElMessage.success(dialogType.value === 'edit' ? '重新登录成功' : '账号添加成功')
  }, 900)
}

const pollInteractiveLogin = async () => {
  if (!interactiveLoginId.value) return
  try {
    const response = await http.get(
      '/interactiveLogin/status',
      { id: interactiveLoginId.value },
      { silent: true }
    )
    const data = response.data || {}
    interactiveLoginMessage.value = data.message || interactiveLoginMessage.value
    if (data.status === 'success') {
      completeInteractiveLogin()
    } else if (data.status === 'failed' || data.status === 'expired') {
      clearInteractiveTimers()
      interactiveLoginMessage.value = data.message || '官方登录窗口异常退出'
      ElMessage.error(interactiveLoginMessage.value)
      interactiveLoginStatus.value = data.status
    } else {
      interactiveLoginStatus.value = data.status || interactiveLoginStatus.value
    }
  } catch (error) {
    if (error?.response?.status === 404) {
      clearInteractiveTimers()
      interactiveLoginStatus.value = 'failed'
      interactiveLoginMessage.value = '登录窗口已关闭，请重新打开'
    }
  }
}

const startInteractiveLogin = async (name, accountId = null, platform = 'douyin') => {
  closeInteractiveLogin()
  remoteZoom.value = 1
  const platformLabel = INTERACTIVE_PLATFORM_LABELS[platform] || '抖音'
  interactiveLoginActive.value = true
  interactiveLoginStarting.value = true
  interactiveLoginStatus.value = 'starting'
  interactiveLoginMessage.value = `正在打开${platformLabel}官方登录窗口`
  interactiveLoginPlatform.value = platformLabel
  currentAccountName.value = name
  currentAccountId.value = accountId
  try {
    const response = await http.post('/interactiveLogin/start', {
      id: name,
      account_id: accountId,
      platform
    })
    const data = response.data || {}
    interactiveLoginId.value = data.session || ''
    interactiveLoginStatus.value = data.status || 'running'
    interactiveLoginMessage.value = data.message || `请在${platformLabel}官方窗口完成登录`
    interactiveFrameTimer = setInterval(refreshInteractiveFrame, 220)
    interactiveStatusTimer = setInterval(pollInteractiveLogin, 450)
    refreshInteractiveFrame()
  } catch (error) {
    interactiveLoginStatus.value = 'failed'
    interactiveLoginMessage.value = error?.message || `打开${platformLabel}官方登录窗口失败`
  } finally {
    interactiveLoginStarting.value = false
  }
}

const retryInteractiveLogin = () => {
  startInteractiveLogin(
    currentAccountName.value || accountForm.name,
    currentAccountId.value || (reLoginMode.value ? accountForm.id : null),
    currentInteractivePlatform.value
  )
}

const sendRemoteInput = (kind, payload = {}) => {
  if (!interactiveLoginId.value || interactiveLoginStatus.value !== 'running') return
  http.post(
    '/interactiveLogin/input',
    { id: interactiveLoginId.value, kind, payload },
    { silent: true }
  ).catch(() => {})
  // 点击后立即拉一帧，让画面尽快反映操作结果
  setTimeout(() => refreshInteractiveFrame(), 40)
}

const remotePoint = (event) => {
  const image = interactiveImageRef.value
  if (!image) return { x: 0, y: 0 }
  const rect = image.getBoundingClientRect()
  const naturalWidth = image.naturalWidth || 1280
  const naturalHeight = image.naturalHeight || 900
  const scale = Math.min(rect.width / naturalWidth, rect.height / naturalHeight)
  const contentWidth = naturalWidth * scale
  const contentHeight = naturalHeight * scale
  const offsetX = (rect.width - contentWidth) / 2
  const offsetY = (rect.height - contentHeight) / 2
  return {
    x: Math.max(0, Math.min(1279, ((event.clientX - rect.left - offsetX) / contentWidth) * 1280)),
    y: Math.max(0, Math.min(899, ((event.clientY - rect.top - offsetY) / contentHeight) * 900))
  }
}

const startRemotePointer = (event) => {
  remotePointerDown.value = true
  event.currentTarget?.setPointerCapture?.(event.pointerId)
  sendRemoteInput('mouse_down', remotePoint(event))
}

const moveRemotePointer = (event) => {
  if (!remotePointerDown.value) return
  const now = Date.now()
  if (now - remoteLastMoveAt < 45) return
  remoteLastMoveAt = now
  sendRemoteInput('mouse_move', remotePoint(event))
}

const endRemotePointer = (event) => {
  if (!remotePointerDown.value) return
  remotePointerDown.value = false
  sendRemoteInput('mouse_move', remotePoint(event))
  sendRemoteInput('mouse_up', remotePoint(event))
}

const scrollRemoteBrowser = (event) => {
  if (event.ctrlKey) {
    if (event.deltaY < 0) zoomRemoteIn()
    else zoomRemoteOut()
    return
  }
  sendRemoteInput('scroll', { deltaY: event.deltaY })
}

const sendRemoteText = () => {
  const text = remoteInputText.value
  if (!text) return
  sendRemoteInput('text', { text })
  remoteInputText.value = ''
}

const sendRemoteKey = (key) => {
  sendRemoteInput('key', { key })
}

const reloadRemoteBrowser = () => {
  sendRemoteInput('reload')
}

// 把后端推送的登录中间态转成用户看得懂的提示。
// 关键点：手机端提示"登录成功"后，界面必须立刻给出反馈，
// 否则用户完全无法判断自己是不是还在等。
const applyLoginStatusEvent = (eventName) => {
  const map = {
    scanned: '已扫码！请在手机上确认登录',
    confirmed: '手机端已确认，正在保存登录状态...',
    qrcode_expired: '二维码已过期，正在自动刷新...',
    no_qrcode: '未能自动读取二维码，请直接扫描浏览器窗口中的二维码'
  }
  const text = map[eventName]
  if (text) {
    loginProgressText.value = text
  }
}

// 建立SSE连接
const connectSSE = async (platform, name, accountId = null, options = {}) => {
  // 关闭可能存在的连接
  closeSSEConnection()
  qrZoom.value = 1

  const authMethod = platform === '抖音' ? (options.authMethod || 'qr') : 'qr'
  loginMethod.value = authMethod

  // 设置连接状态
  sseConnecting.value = true
  qrCodeData.value = ''
  loginStatus.value = ''
  loginErrorMessage.value = ''
  loginProgressText.value = ''
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

    // 登录进度中间态：让用户明确知道"扫码了没有 / 到哪一步了"，
    // 而不是只能对着二维码干等。
    if (data.startsWith('LOGIN_STATUS:')) {
      const event_name = data.slice('LOGIN_STATUS:'.length).trim()
      applyLoginStatusEvent(event_name)
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
    } else {
      verifySubmitted.value = true
      ElMessage.success('验证码已提交，正在等待抖音确认')
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
        const accountId = reLoginMode.value ? accountForm.id : null
        // 抖音和小红书都走"官方登录窗口"：把平台真实登录页拉到对话框里，
        // 用户直接在上面扫码/输验证码，和官方页面完全一致。
        const interactiveKey = INTERACTIVE_PLATFORMS[accountForm.platform]
        if (interactiveKey) {
          currentInteractivePlatform.value = interactiveKey
          await startInteractiveLogin(accountForm.name, accountId, interactiveKey)
        } else {
          // 其他平台继续使用现有二维码 SSE 登录流程。
          await connectSSE(accountForm.platform, accountForm.name, accountId, { authMethod: 'qr' })
        }
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
  closeInteractiveLogin()
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

  .official-login-note {
    display: flex;
    flex-direction: column;
    gap: 4px;
    line-height: 1.55;
    color: #606266;
    font-size: 12px;

    strong {
      color: #303133;
      font-size: 13px;
    }
  }

  .interactive-login-panel {
    margin: 8px 0 18px;
    border: 1px solid #dcdfe6;
    border-radius: 10px;
    overflow: hidden;
    background: #f5f7fa;

    .interactive-login-status {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 38px;
      padding: 7px 12px;
      color: #606266;
      font-size: 13px;
      background: #fff;
      border-bottom: 1px solid #ebeef5;

      &.success { color: #529b2e; }
      &.failed, &.expired { color: #c45656; }
    }

    .remote-browser-stage {
      position: relative;
      width: 100%;
      min-height: 360px;
      max-height: 66vh;
      background: #111827;
      user-select: none;
      touch-action: none;
      cursor: default;

      &.dragging { cursor: grabbing; }

      img {
        display: block;
        width: 100%;
        height: auto;
        max-height: 66vh;
        margin: 0 auto;
        object-fit: contain;
        pointer-events: auto;
      }
    }

    .remote-zoom-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      background: #fff;
      border-top: 1px solid #ebeef5;
      color: #606266;
      font-size: 12px;

      .remote-zoom-value {
        min-width: 44px;
        text-align: center;
      }

      .remote-zoom-hint {
        color: #909399;
      }
    }

    .remote-browser-loading {
      height: 420px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      color: #dcdfe6;

      .el-icon { animation: rotate 1s linear infinite; }
    }

    .remote-browser-controls {
      padding: 10px 12px 12px;
      background: #fff;
      border-top: 1px solid #ebeef5;

      .remote-key-row {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 8px;
        color: #909399;
        font-size: 12px;

        .el-button + .el-button { margin-left: 0; }
      }
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
        width: 200px;
        height: auto;
        border: 1px solid #ebeef5;
        background-color: black;
      }

      .qr-zoom-row {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        margin-top: 8px;
        color: #606266;
        font-size: 12px;

        .qr-zoom-value {
          min-width: 44px;
          text-align: center;
        }
      }

      .refresh-qrcode-button {
        margin-top: 10px;
      }

      // 登录进度提示：扫码/确认/刷新等中间态，让用户知道系统在等什么
      .qrcode-progress {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        margin: 12px 0 0;
        padding: 6px 14px;
        border-radius: 16px;
        font-size: 13px;
        color: #67c23a;
        background-color: #f0f9eb;
        border: 1px solid #e1f3d8;

        .el-icon {
          font-size: 14px;
        }
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
