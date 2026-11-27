<template>
  <div v-if="accessReady" class="main-layout">
    <button
      v-if="isMobile && mobileNavOpen"
      class="sidebar-scrim"
      aria-label="关闭导航"
      @click="mobileNavOpen = false"
    />
    <el-container class="app-shell">
      <el-aside
        :width="isMobile ? '260px' : isCollapse ? '72px' : '248px'"
        class="sidebar"
        :class="{ 'is-collapsed': isCollapse && !isMobile, 'is-open': mobileNavOpen }"
      >
        <div class="brand-row">
          <div class="brand">
            <div class="brand-mark" aria-hidden="true">云</div>
            <div v-show="!isCollapse || isMobile" class="brand-copy">
              <strong>云策</strong>
              <span>{{ identityLabel }}</span>
            </div>
          </div>
          <button
            class="sidebar-toggle"
            :aria-label="isMobile ? '关闭导航' : isCollapse ? '展开导航' : '收起导航'"
            @click="toggleSidebar"
          >
            <el-icon :size="18"><Expand v-if="isCollapse || isMobile" /><Fold v-else /></el-icon>
          </button>
        </div>

        <button
          v-if="userStore.can('content.edit')"
          v-show="!isCollapse || isMobile"
          class="new-content-action"
          type="button"
          @click="openComposer"
        >
          <el-icon><Plus /></el-icon>
          <span>新建内容</span>
        </button>

        <button class="command-trigger" type="button" aria-label="搜索工作台" @click="paletteOpen = true">
          <el-icon><Search /></el-icon>
          <span v-show="!isCollapse || isMobile">搜索</span>
          <kbd v-show="!isCollapse || isMobile">Ctrl K</kbd>
        </button>

        <el-menu
          :default-active="activeMenuIndex"
          class="sidebar-menu"
          :router="true"
          :collapse="isCollapse && !isMobile"
        >
          <el-menu-item
            v-for="item in navigationItems"
            :key="item.path"
            :index="item.path"
            @click="mobileNavOpen = false"
          >
            <el-icon><component :is="item.icon" /></el-icon>
            <template #title>{{ item.label }}</template>
          </el-menu-item>
        </el-menu>

        <el-menu
          :default-active="activeMenuIndex"
          class="sidebar-menu sidebar-menu--secondary"
          :router="true"
          :collapse="isCollapse && !isMobile"
        >
          <el-menu-item
            v-if="userStore.can('account.manage')"
            index="/settings?view=accounts"
            @click="mobileNavOpen = false"
          >
            <el-icon><User /></el-icon>
            <template #title>账号登录</template>
          </el-menu-item>
          <el-menu-item index="/settings" @click="mobileNavOpen = false">
            <el-icon><Setting /></el-icon>
            <template #title>设置</template>
          </el-menu-item>
        </el-menu>

        <div v-if="!userStore.session.localMode" class="sidebar-session">
          <button type="button" :title="isCollapse ? '退出登录' : ''" @click="logout">
            <el-icon><SwitchButton /></el-icon>
            <span v-show="!isCollapse || isMobile">退出登录</span>
          </button>
        </div>
      </el-aside>

      <el-container class="content-shell">
        <header v-if="isMobile" class="mobile-bar">
          <button class="mobile-menu-button" aria-label="打开导航" @click="mobileNavOpen = true">
            <el-icon :size="20"><MenuIcon /></el-icon>
          </button>
          <div class="mobile-context">
            <span>云策</span>
            <strong>{{ pageTitle }}</strong>
          </div>
          <button class="mobile-search-button" aria-label="搜索工作台" @click="paletteOpen = true">
            <el-icon :size="18"><Search /></el-icon>
          </button>
        </header>
        <el-main class="main-content">
          <div class="content-frame">
            <router-view v-slot="{ Component, route: viewRoute }">
              <KeepAlive>
                <component :is="Component" :key="viewRoute.name || viewRoute.path" />
              </KeepAlive>
            </router-view>
          </div>
        </el-main>
      </el-container>
    </el-container>
  </div>

  <CommandPalette v-model="paletteOpen" />

  <el-dialog
    v-model="accessDialog"
    title="访问工作台"
    width="min(460px, calc(100vw - 32px))"
    :show-close="false"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    class="access-dialog"
  >
    <div class="access-copy">
      <div class="access-mark" aria-hidden="true">云</div>
      <div>
        <strong>云策内容运营台</strong>
        <p v-if="mfaRequired">请输入认证器中的 6 位动态验证码。</p>
        <p v-else-if="passwordGateMode">输入访问密码后进入工作台。</p>
        <p v-else-if="loginMode === 'password'">使用管理员分配的个人账号登录。</p>
        <p v-else>输入管理员提供的访问令牌，或直接打开邀请链接。</p>
      </div>
    </div>
    <template v-if="mfaRequired">
      <div class="mfa-account">{{ mfaUserLabel }}</div>
      <label class="access-label" for="mfa-code">动态验证码</label>
      <el-input
        id="mfa-code"
        v-model="mfaCode"
        inputmode="numeric"
        maxlength="6"
        autocomplete="one-time-code"
        placeholder="6 位数字"
        @keyup.enter="submitMfaLogin"
      />
    </template>
    <template v-else-if="passwordGateMode">
      <label class="access-label" for="gate-password">访问密码</label>
      <el-input
        id="gate-password"
        v-model="passwordInput"
        type="password"
        show-password
        autocomplete="current-password"
        placeholder="请输入访问密码"
        @keyup.enter="submitPasswordGate"
      />
    </template>
    <template v-else-if="loginMode === 'password'">
      <label class="access-label" for="login-username">用户名</label>
      <el-input
        id="login-username"
        v-model="usernameInput"
        autocomplete="username"
        clearable
        placeholder="用户名或员工账号"
      />
      <label class="access-label" for="login-password">密码</label>
      <el-input
        id="login-password"
        v-model="passwordInput"
        type="password"
        show-password
        autocomplete="current-password"
        placeholder="个人密码"
        @keyup.enter="submitPasswordLogin"
      />
    </template>
    <template v-else>
      <label class="access-label" for="access-token">访问令牌</label>
      <el-input
        id="access-token"
        v-model="tokenInput"
        type="password"
        show-password
        clearable
        placeholder="访问令牌"
        @keyup.enter="submitToken"
      />
    </template>
    <template #footer>
      <div class="access-actions">
        <el-button
          v-if="mfaRequired"
          text
          @click="backToPasswordLogin"
        >
          返回账号登录
        </el-button>
        <el-button
          v-else-if="!passwordGateMode && authConfig.personalAuthEnabled && authConfig.tokenFallbackEnabled"
          text
          @click="toggleLoginMode"
        >
          {{ loginMode === 'password' ? '使用访问令牌' : '使用个人账号' }}
        </el-button>
        <el-button
          type="primary"
          :loading="checking"
          @click="mfaRequired ? submitMfaLogin() : passwordGateMode ? submitPasswordGate() : loginMode === 'password' ? submitPasswordLogin() : submitToken()"
        >
          {{ mfaRequired ? '验证并进入' : '进入工作台' }}
        </el-button>
      </div>
    </template>
  </el-dialog>

  <el-dialog
    v-model="passwordDialog"
    title="设置新密码"
    width="min(440px, calc(100vw - 32px))"
    :show-close="false"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
  >
    <p class="password-help">首次登录或管理员重置密码后，需要先设置新密码。修改后所有会话都会失效。</p>
    <label class="access-label" for="current-password">当前密码</label>
    <el-input
      id="current-password"
      v-model="passwordInput"
      type="password"
      show-password
      autocomplete="current-password"
    />
    <label class="access-label" for="new-password">新密码</label>
    <el-input
      id="new-password"
      v-model="newPasswordInput"
      type="password"
      show-password
      autocomplete="new-password"
      placeholder="至少 12 个字符"
    />
    <label class="access-label" for="confirm-password">确认新密码</label>
    <el-input
      id="confirm-password"
      v-model="confirmPasswordInput"
      type="password"
      show-password
      autocomplete="new-password"
      @keyup.enter="submitPasswordChange"
    />
    <template #footer>
      <el-button type="primary" :loading="checking" @click="submitPasswordChange">保存并重新登录</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getAccessToken, setAccessToken } from '@/config'
import { http, rememberRefreshToken, setRefreshToken } from '@/utils/request'
import { userApi } from '@/api/user'
import { useUserStore } from '@/stores/user'
import {
  ChatDotRound,
  DataLine,
  Expand,
  Files,
  Fold,
  HomeFilled,
  Menu as MenuIcon,
  Monitor,
  Plus,
  Search,
  Setting,
  SwitchButton,
  User,
} from '@element-plus/icons-vue'
import CommandPalette from '@/components/CommandPalette.vue'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const isCollapse = ref(false)
const mobileNavOpen = ref(false)
const paletteOpen = ref(false)
const isMobile = ref(typeof window !== 'undefined' && window.matchMedia('(max-width: 900px)').matches)
const accessReady = ref(false)
const accessDialog = ref(false)
const tokenInput = ref(getAccessToken())
const usernameInput = ref('')
const passwordInput = ref('')
const mfaRequired = ref(false)
const mfaChallengeToken = ref('')
const mfaCode = ref('')
const mfaUserLabel = ref('')
const newPasswordInput = ref('')
const confirmPasswordInput = ref('')
const passwordDialog = ref(false)
const loginMode = ref('password')
const authConfig = ref({
  personalAuthEnabled: false,
  tokenFallbackEnabled: true,
  passwordGateEnabled: false,
  passwordMinLength: 12
})
// 单密码门禁：只显示一个密码框（密码在 .env 的 YUNCE_ACCESS_PASSWORD 里自定义）
const passwordGateMode = computed(() => Boolean(authConfig.value.passwordGateEnabled))
const checking = ref(false)
let mediaQuery = null
let syncViewport = null

const allBusinessPermissions = [
  'content.edit', 'material.write', 'review.decide', 'publish.execute',
  'message.operate', 'account.manage', 'team.manage', 'audit.read'
]
const navigationItems = computed(() => [
  { path: '/today', label: '今天', icon: HomeFilled, visible: userStore.canAny(...allBusinessPermissions) },
  { path: '/content', label: '内容', icon: Files, visible: userStore.canAny('content.edit', 'review.decide', 'publish.execute') },
  { path: '/inbox', label: '收件箱', icon: ChatDotRound, visible: userStore.can('message.operate') },
  { path: '/console', label: '官方私信', icon: Monitor, visible: userStore.can('message.operate') },
  { path: '/insights', label: '洞察', icon: DataLine, visible: userStore.can('publish.execute') },
].filter(item => item.visible))

const pageTitles = {
  '/today': '今天',
  '/content': '内容',
  '/inbox': '收件箱',
  '/console': '官方私信',
  '/insights': '洞察',
  '/settings': '设置',
}

const pageTitle = computed(() => pageTitles[route.path] || '云策工作台')
const activeMenuIndex = computed(() => (
  route.path === '/settings' && route.query.view === 'accounts'
    ? '/settings?view=accounts'
    : route.path
))
const roleNames = {
  admin: '管理员', operator: '运营', cutter: '剪辑', reviewer: '审核', viewer: '只读'
}
const identityLabel = computed(() => {
  const role = userStore.session.role
  const roleName = roleNames[role] || role || '未登录'
  const user = userStore.session.displayName || userStore.session.user
  if (userStore.session.localMode) return `${roleName} · 本机模式`
  return user ? `${user} · ${roleName}` : roleName
})

function openComposer() {
  mobileNavOpen.value = false
  router.push('/content?view=compose')
}

async function verifyAccess() {
  try {
    await http.get('/auth/check', null, { silent: true })
    const response = await http.get('/api/v2/session', null, { silent: true })
    const identity = response.data || {}
    userStore.setSession(identity)
    if (identity.mustChangePassword) {
      accessReady.value = false
      accessDialog.value = false
      passwordDialog.value = true
      return true
    }
    accessReady.value = true
    accessDialog.value = false
    passwordDialog.value = false
    return true
  } catch {
    userStore.reset()
    accessReady.value = false
    accessDialog.value = true
    mfaRequired.value = false
    mfaChallengeToken.value = ''
    mfaCode.value = ''
    return false
  }
}

async function loadAuthConfig() {
  try {
    const response = await userApi.getAuthConfig()
    authConfig.value = { ...authConfig.value, ...(response.data || {}) }
  } catch {
    authConfig.value = {
      ...authConfig.value,
      personalAuthEnabled: false,
      tokenFallbackEnabled: true,
      passwordGateEnabled: false
    }
  }
  loginMode.value = authConfig.value.personalAuthEnabled ? 'password' : 'token'
}

function toggleLoginMode() {
  loginMode.value = loginMode.value === 'password' ? 'token' : 'password'
}

async function submitPasswordGate() {
  const password = String(passwordInput.value || '')
  if (!password || checking.value) return
  checking.value = true
  try {
    const response = await userApi.unlock(password)
    setAccessToken(response.data?.token || '')
    rememberRefreshToken(response.data)
    passwordInput.value = ''
    const ok = await verifyAccess()
    if (!ok) {
      ElMessage.error('密码不正确或登录状态校验失败')
    }
  } catch (error) {
    passwordInput.value = ''
    // 后端已返回具体原因（密码错误 / 尝试过于频繁），这里不重复弹第二遍
    if (!error?.response) ElMessage.error('登录失败，请稍后重试')
  } finally {
    checking.value = false
  }
}

async function submitPasswordLogin() {
  const username = String(usernameInput.value || '').trim()
  const password = String(passwordInput.value || '')
  if (!username || !password || checking.value) return
  checking.value = true
  try {
    const response = await userApi.login({ username, password })
    if (response.data?.mfaRequired) {
      mfaRequired.value = true
      mfaChallengeToken.value = response.data.challengeToken || ''
      mfaUserLabel.value = response.data.user || username
      mfaCode.value = ''
      passwordInput.value = ''
      return
    }
    setAccessToken(response.data?.token || '')
    rememberRefreshToken(response.data)
    tokenInput.value = response.data?.token || ''
    const ok = await verifyAccess()
    if (!ok) ElMessage.error('登录状态校验失败')
  } catch {
    setAccessToken('')
  } finally {
    checking.value = false
  }
}

async function submitMfaLogin() {
  const code = String(mfaCode.value || '').replace(/\s+/g, '')
  if (!/^\d{6}$/.test(code) || checking.value) {
    if (!checking.value) ElMessage.error('请输入 6 位动态验证码')
    return
  }
  checking.value = true
  try {
    const response = await userApi.verifyMfa({
      challengeToken: mfaChallengeToken.value,
      code
    })
    setAccessToken(response.data?.token || '')
    rememberRefreshToken(response.data)
    mfaRequired.value = false
    mfaChallengeToken.value = ''
    mfaCode.value = ''
    const ok = await verifyAccess()
    if (!ok) ElMessage.error('双因素登录状态校验失败')
  } catch {
    mfaCode.value = ''
  } finally {
    checking.value = false
  }
}

function backToPasswordLogin() {
  mfaRequired.value = false
  mfaChallengeToken.value = ''
  mfaCode.value = ''
}

async function submitToken() {
  const token = String(tokenInput.value || '').trim()
  if (!token || checking.value) return
  checking.value = true
  setAccessToken(token)
  try {
    const ok = await verifyAccess()
    if (!ok) ElMessage.error('访问令牌不正确')
  } finally {
    checking.value = false
  }
}

async function submitPasswordChange() {
  const newPassword = String(newPasswordInput.value || '')
  const minimum = Number(authConfig.value.passwordMinLength || 12)
  if (newPassword.length < minimum) {
    ElMessage.error(`新密码至少需要 ${minimum} 个字符`)
    return
  }
  if (newPassword !== String(confirmPasswordInput.value || '')) {
    ElMessage.error('两次输入的新密码不一致')
    return
  }
  checking.value = true
  try {
    await userApi.changePassword({
      currentPassword: String(passwordInput.value || ''),
      newPassword
    })
    setAccessToken('')
    tokenInput.value = ''
    passwordInput.value = ''
    newPasswordInput.value = ''
    confirmPasswordInput.value = ''
    userStore.reset()
    passwordDialog.value = false
    accessReady.value = false
    accessDialog.value = true
    loginMode.value = 'password'
    ElMessage.success('密码已修改，请重新登录')
  } finally {
    checking.value = false
  }
}

async function logout() {
  try {
    await userApi.logout()
  } catch {
    // Local/expired sessions can already be invalid; clear the client either way.
  }
  setAccessToken('')
  setRefreshToken('')
  tokenInput.value = ''
  passwordInput.value = ''
  userStore.reset()
  accessReady.value = false
  accessDialog.value = true
  mfaRequired.value = false
  mfaChallengeToken.value = ''
  mfaCode.value = ''
  loginMode.value = authConfig.value.personalAuthEnabled ? 'password' : 'token'
}

function handleAuthExpired() {
  setAccessToken('')
  setRefreshToken('')
  userStore.reset()
  accessReady.value = false
  accessDialog.value = true
  mfaRequired.value = false
  mfaChallengeToken.value = ''
  mfaCode.value = ''
}

const toggleSidebar = () => {
  if (isMobile.value) {
    mobileNavOpen.value = !mobileNavOpen.value
    return
  }
  isCollapse.value = !isCollapse.value
}

watch(() => route.fullPath, () => {
  mobileNavOpen.value = false
  paletteOpen.value = false
})

function handleGlobalKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === 'k') {
    event.preventDefault()
    paletteOpen.value = true
  }
}

onMounted(() => {
  loadAuthConfig().finally(() => verifyAccess())
  window.addEventListener('keydown', handleGlobalKeydown)
  window.addEventListener('yunce:auth-expired', handleAuthExpired)
  mediaQuery = window.matchMedia('(max-width: 900px)')
  syncViewport = (event) => {
    isMobile.value = event.matches
    if (!event.matches) mobileNavOpen.value = false
  }
  mediaQuery.addEventListener('change', syncViewport)
})

onBeforeUnmount(() => {
  mediaQuery?.removeEventListener('change', syncViewport)
  window.removeEventListener('keydown', handleGlobalKeydown)
  window.removeEventListener('yunce:auth-expired', handleAuthExpired)
})
</script>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html,
body,
#app {
  height: 100%;
}
</style>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;

.main-layout,
.app-shell {
  height: 100dvh;
  min-height: 100%;
}

.app-shell {
  background: $bg-color-page;
}

.sidebar {
  position: relative;
  z-index: 20;
  display: flex;
  flex-direction: column;
  flex: none;
  overflow: hidden;
  color: #d9ddd7;
  background: $ink-950;
  border-right: 1px solid rgba(255, 255, 255, 0.06);
}

.brand-row {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 72px;
  padding: 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
}

.brand {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 10px;
}

.brand-mark,
.access-mark {
  display: grid;
  place-items: center;
  flex: none;
  width: 34px;
  height: 34px;
  color: #fff9f3;
  background: $primary-color;
  border-radius: 8px;
  font-size: 16px;
  font-weight: 500;
}

.brand-copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
  line-height: 1.15;
}

.brand-copy strong {
  color: #f2f3ef;
  font-size: 17px;
  font-weight: 500;
  white-space: nowrap;
}

.brand-copy span {
  margin-top: 3px;
  color: #8f968f;
  font-size: 11px;
  white-space: nowrap;
}

.sidebar-toggle,
.mobile-menu-button,
.mobile-search-button {
  display: grid;
  place-items: center;
  flex: none;
  width: 32px;
  height: 32px;
  color: #9ca49d;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
  transition: color $duration-base $ease-out, background-color $duration-base $ease-out,
    border-color $duration-base $ease-out;
}

.sidebar-toggle:hover,
.mobile-menu-button:hover,
.mobile-search-button:hover {
  color: #f2f3ef;
  border-color: rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.05);
}

.sidebar-menu {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px;
  border-right: 0;
  background: transparent;
}

.sidebar-menu--secondary {
  flex: none;
  padding: 8px 12px 16px;
  border-top: 1px solid rgba(255, 255, 255, 0.07);
}

.sidebar-session {
  flex: none;
  padding: 0 12px 14px;
}

.sidebar-session button {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 34px;
  padding: 0 12px;
  color: #8f968f;
  border: 0;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
}

.sidebar-session button:hover {
  color: #eef0ec;
  background: rgba(255, 255, 255, 0.05);
}

.sidebar.is-collapsed .sidebar-session {
  padding-inline: 8px;
}

.sidebar.is-collapsed .sidebar-session button {
  justify-content: center;
  padding: 0;
}

.new-content-action {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 12px 4px;
  min-height: 36px;
  padding: 0 12px;
  color: #fffaf5;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.07);
  cursor: pointer;
  transition: background-color $duration-base $ease-out;
}

.new-content-action:hover {
  background: rgba(255, 255, 255, 0.11);
}

.new-content-action span {
  font-size: 13px;
}

.command-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 4px 12px 6px;
  min-height: 34px;
  padding: 0 12px;
  color: #aeb5af;
  border: 0;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
}

.command-trigger:hover {
  color: #eef0ec;
  background: rgba(255, 255, 255, 0.05);
}

.command-trigger .el-icon {
  flex: none;
  font-size: 16px;
}

.command-trigger span {
  flex: 1;
  font-size: 12px;
  text-align: left;
}

.command-trigger kbd {
  padding: 2px 5px;
  color: #7f8780;
  border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: 4px;
  font: inherit;
  font-size: 9px;
}

.sidebar.is-collapsed .command-trigger {
  justify-content: center;
  margin-inline: 8px;
  padding: 0;
}

.sidebar-menu:not(.el-menu--collapse) {
  width: 100%;
}

.sidebar :deep(.el-menu-item) {
  position: relative;
  height: 36px;
  margin: 2px 0;
  padding: 0 12px !important;
  color: #b8beb8;
  border-radius: 8px;
  line-height: 36px;
}

.sidebar :deep(.el-menu-item .el-icon) {
  width: 18px;
  margin-right: 10px;
  color: #858d86;
  font-size: 18px;
}

.sidebar :deep(.el-menu-item:hover) {
  color: #eef0ec;
  background: rgba(255, 255, 255, 0.05);
}

.sidebar :deep(.el-menu-item.is-active) {
  color: #ffffff;
  background: $ink-850;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08);
}

.sidebar :deep(.el-menu-item.is-active::before) {
  position: absolute;
  top: 12px;
  left: 4px;
  width: 2px;
  height: 12px;
  border-radius: 1px;
  background: $primary-color;
  content: '';
}

.sidebar :deep(.el-menu-item.is-active .el-icon) {
  color: #eef0ec;
}

.sidebar.is-collapsed .brand-row {
  justify-content: center;
  padding-inline: 0;
}

.sidebar.is-collapsed .sidebar-toggle {
  position: absolute;
  right: 7px;
  bottom: 14px;
}

.sidebar.is-collapsed .sidebar-menu {
  padding-inline: 8px;
}

.sidebar.is-collapsed :deep(.el-menu-item) {
  justify-content: center;
  padding: 0 !important;
}

.sidebar.is-collapsed :deep(.el-menu-item .el-icon) {
  margin-right: 0;
}


.content-shell {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.main-content {
  min-width: 0;
  padding: 32px;
  overflow-y: auto;
  background: $bg-color-page;
}

.content-frame {
  width: min(100%, 1536px);
  min-height: 100%;
  margin: 0 auto;
}

.mobile-bar {
  display: none;
}

.sidebar-scrim {
  position: fixed;
  inset: 0;
  z-index: 19;
  border: 0;
  background: rgba(23, 26, 24, 0.45);
}

.access-copy {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 4px 0 20px;
}

.access-copy strong {
  color: $text-primary;
  font-size: 15px;
  font-weight: 500;
}

.access-copy p {
  margin: 6px 0 0;
  color: $text-secondary;
  font-size: 13px;
  line-height: 1.6;
}

.access-label {
  display: block;
  margin-bottom: 8px;
  color: $text-regular;
  font-size: 13px;
  font-weight: 500;
}

.access-label:not(:first-of-type) {
  margin-top: 16px;
}

.access-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.mfa-account {
  margin-bottom: 14px;
  padding: 10px 12px;
  color: $text-regular;
  border-radius: 8px;
  background: $bg-color-raised;
  font-size: 13px;
}

.password-help {
  margin: 0 0 18px;
  color: $text-secondary;
  font-size: 13px;
  line-height: 1.7;
}

@media (max-width: 1100px) {
  .main-content {
    padding: 24px;
  }
}

@media (max-width: 900px) {
  .sidebar {
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    width: 260px !important;
    transform: translateX(-100%);
    box-shadow: 16px 0 48px rgba(23, 26, 24, 0.2);
    transition: transform 220ms $ease-out;
  }

  .sidebar.is-open {
    transform: translateX(0);
  }

  .content-shell {
    width: 100%;
  }

  .mobile-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 56px;
    padding: 8px 16px;
    border-bottom: 1px solid $border-lighter;
    background: rgba(255, 254, 251, 0.94);
  }

  .mobile-menu-button {
    color: $text-regular;
    border-color: $border-lighter;
    background: $paper;
  }

  .mobile-menu-button:hover,
  .mobile-search-button:hover {
    color: $text-primary;
    border-color: $border-light;
    background: $bg-color-raised;
  }

  .mobile-search-button {
    margin-left: auto;
    color: $text-regular;
    border-color: $border-lighter;
    background: $paper;
  }

  .mobile-context {
    display: flex;
    flex: 1;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
  }

  .mobile-context span {
    color: $text-secondary;
    font-size: 12px;
  }

  .mobile-context strong {
    overflow: hidden;
    color: $text-primary;
    font-size: 14px;
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .main-content {
    height: calc(100dvh - 56px);
    padding: 20px 16px 24px;
  }
}

@media (max-width: 560px) {
  .main-content {
    padding: 16px 12px 24px;
  }
}
</style>
