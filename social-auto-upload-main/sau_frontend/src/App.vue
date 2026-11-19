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
              <span>内容运营台</span>
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

        <el-menu
          :default-active="route.path"
          class="sidebar-menu"
          :router="true"
          :collapse="isCollapse && !isMobile"
        >
          <el-menu-item-group
            v-for="group in navigationGroups"
            :key="group.label"
            :title="group.label"
          >
            <el-menu-item
              v-for="item in group.items"
              :key="item.path"
              :index="item.path"
              @click="mobileNavOpen = false"
            >
              <el-icon><component :is="item.icon" /></el-icon>
              <template #title>{{ item.label }}</template>
            </el-menu-item>
          </el-menu-item-group>
        </el-menu>

        <div v-show="!isCollapse || isMobile" class="sidebar-foot">
          <span class="status-key is-good">工作台在线</span>
          <span class="mono">FlowOps</span>
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
        <p>输入管理员提供的访问令牌，或直接打开邀请链接。</p>
      </div>
    </div>
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
    <template #footer>
      <el-button type="primary" :loading="checking" @click="submitToken">进入工作台</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getAccessToken, setAccessToken } from '@/config'
import { http } from '@/utils/request'
import {
  Bell,
  Calendar,
  ChatDotRound,
  ChatLineSquare,
  Checked,
  DataAnalysis,
  DataLine,
  EditPen,
  Expand,
  Files,
  Fold,
  HomeFilled,
  InfoFilled,
  List,
  Menu as MenuIcon,
  Picture,
  Upload,
  User,
} from '@element-plus/icons-vue'

const route = useRoute()
const isCollapse = ref(false)
const mobileNavOpen = ref(false)
const isMobile = ref(typeof window !== 'undefined' && window.matchMedia('(max-width: 900px)').matches)
const accessReady = ref(false)
const accessDialog = ref(false)
const tokenInput = ref(getAccessToken())
const checking = ref(false)
let mediaQuery = null
let syncViewport = null

const navigationGroups = [
  {
    label: '运营',
    items: [
      { path: '/', label: '首页', icon: HomeFilled },
      { path: '/todo', label: '今日待办', icon: Bell },
      { path: '/messages', label: '私信中心', icon: ChatDotRound },
      { path: '/comments', label: '评论运营', icon: ChatLineSquare },
    ],
  },
  {
    label: '内容',
    items: [
      { path: '/account-management', label: '账号管理', icon: User },
      { path: '/material-management', label: '素材管理', icon: Picture },
      { path: '/publish-center', label: '发布中心', icon: Upload },
      { path: '/ai-content', label: 'AI 文案', icon: EditPen },
      { path: '/content-pack', label: '内容包', icon: Files },
      { path: '/review', label: '审核中心', icon: Checked },
    ],
  },
  {
    label: '数据',
    items: [
      { path: '/weekly-report', label: '运营周报', icon: DataLine },
      { path: '/publish-records', label: '发布记录', icon: List },
      { path: '/publish-calendar', label: '发布日历', icon: Calendar },
      { path: '/roi', label: '线索 ROI', icon: DataLine },
    ],
  },
  {
    label: '系统',
    items: [
      { path: '/ops', label: '运维台面', icon: DataAnalysis },
      { path: '/about', label: '关于', icon: InfoFilled },
    ],
  },
]

const pageTitles = {
  '/': '运营总览',
  '/todo': '今日待办',
  '/messages': '私信中心',
  '/comments': '评论运营',
  '/account-management': '账号管理',
  '/material-management': '素材管理',
  '/publish-center': '发布中心',
  '/ai-content': 'AI 文案',
  '/content-pack': '内容包',
  '/review': '审核中心',
  '/weekly-report': '运营周报',
  '/publish-records': '发布记录',
  '/publish-calendar': '发布日历',
  '/roi': '线索 ROI',
  '/ops': '运维台面',
  '/about': '关于',
}

const pageTitle = computed(() => pageTitles[route.path] || '云策工作台')

async function verifyAccess() {
  try {
    await http.get('/auth/check', null, { silent: true })
    accessReady.value = true
    accessDialog.value = false
    return true
  } catch {
    accessReady.value = false
    accessDialog.value = true
    return false
  }
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

const toggleSidebar = () => {
  if (isMobile.value) {
    mobileNavOpen.value = !mobileNavOpen.value
    return
  }
  isCollapse.value = !isCollapse.value
}

watch(() => route.fullPath, () => {
  mobileNavOpen.value = false
})

onMounted(() => {
  verifyAccess()
  mediaQuery = window.matchMedia('(max-width: 900px)')
  syncViewport = (event) => {
    isMobile.value = event.matches
    if (!event.matches) mobileNavOpen.value = false
  }
  mediaQuery.addEventListener('change', syncViewport)
})

onBeforeUnmount(() => {
  mediaQuery?.removeEventListener('change', syncViewport)
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
.mobile-menu-button {
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
.mobile-menu-button:hover {
  color: #f2f3ef;
  border-color: rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.05);
}

.sidebar-menu {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px 16px;
  border-right: 0;
  background: transparent;
}

.sidebar-menu:not(.el-menu--collapse) {
  width: 100%;
}

.sidebar :deep(.el-menu-item-group__title) {
  padding: 24px 10px 8px;
  color: #747c75;
  font-size: 11px;
  line-height: 1;
  letter-spacing: 0;
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

.sidebar-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.07);
}

.sidebar-foot .status-key {
  color: #9ba39b;
}

.sidebar-foot .mono {
  color: #6f776f;
  font-size: 11px;
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

  .mobile-menu-button:hover {
    color: $text-primary;
    border-color: $border-light;
    background: $bg-color-raised;
  }

  .mobile-context {
    display: flex;
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
