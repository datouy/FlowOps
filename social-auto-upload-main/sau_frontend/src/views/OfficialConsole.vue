<template>
  <div class="official-console">
    <header class="console-bar">
      <div class="console-title">
        <strong>官方私信窗口</strong>
        <span>在项目里直接看、直接点抖音创作者中心的私信页（真实浏览器，不是 iframe）</span>
      </div>
      <div class="console-actions">
        <el-select
          v-model="accountId"
          placeholder="选择抖音账号"
          size="small"
          class="account-select"
          :disabled="Boolean(sessionId)"
        >
          <el-option
            v-for="account in douyinAccounts"
            :key="account.id"
            :label="`${account.name}（${account.status}）`"
            :value="account.id"
          />
        </el-select>
        <el-button
          v-if="!sessionId"
          type="primary"
          size="small"
          :loading="opening"
          :disabled="!accountId"
          @click="openSession"
        >打开窗口</el-button>
        <template v-else>
          <el-button size="small" :loading="reloading" @click="reloadPage">刷新页面</el-button>
          <el-button size="small" type="danger" plain @click="closeSession">关闭窗口</el-button>
        </template>
      </div>
    </header>

    <div class="console-status">
      <span class="dot" :class="statusTone" />
      <span>{{ statusText }}</span>
      <span v-if="sessionId" class="mono">帧 #{{ frameVersion }}</span>
      <span v-if="lastError" class="error">{{ lastError }}</span>
    </div>

    <div ref="stageRef" class="console-stage">
      <img
        v-if="frameSrc"
        :src="frameSrc"
        class="console-frame"
        alt="官方私信窗口画面"
        @click="onClick"
        @wheel.prevent="onWheel"
        @load="onFrameLoad"
      />
      <div v-else class="console-empty">
        <p>选择账号后点「打开窗口」</p>
        <p class="hint">首次打开要等抖音创作者中心启动，实测 60~90 秒，画面会逐步刷出来。</p>
      </div>
    </div>

    <footer class="console-input">
      <el-input
        v-model="text"
        size="small"
        placeholder="在这里输入文字，回车发送到页面输入框"
        :disabled="!sessionId"
        @keyup.enter="sendText"
      />
      <el-button size="small" :disabled="!sessionId" @click="sendText">填入</el-button>
      <el-button size="small" :disabled="!sessionId" @click="pressKey('Enter')">回车</el-button>
      <el-button size="small" :disabled="!sessionId" @click="pressKey('Backspace')">退格</el-button>
      <el-button size="small" :disabled="!sessionId" @click="pressKey('Escape')">Esc</el-button>
    </footer>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { API_BASE, authHeaders } from '@/config'
import { http } from '@/utils/request'
import { useAccountStore } from '@/stores/account'

const accountStore = useAccountStore()
const accountId = ref(null)
const sessionId = ref('')
const frameVersion = ref(0)
const viewport = ref({ width: 1280, height: 860 })
const status = ref('idle')
const message = ref('')
const lastError = ref('')
const opening = ref(false)
const reloading = ref(false)
const text = ref('')
const stageRef = ref(null)
const frameSrc = ref('')
let tick = 0
let timer = null
let frameObjectUrl = ''

const douyinAccounts = computed(() =>
  accountStore.accounts.filter(account => Number(account.type) === 3)
)

const statusText = computed(() => {
  if (!sessionId.value) return '未打开'
  if (status.value === 'starting') return '正在打开页面…'
  if (status.value === 'failed') return message.value || '打开失败'
  return message.value || '页面已打开'
})

const statusTone = computed(() => {
  if (!sessionId.value) return 'is-idle'
  if (status.value === 'failed') return 'is-error'
  if (status.value === 'starting') return 'is-pending'
  return 'is-ready'
})

async function loadAccounts() {
  try {
    await accountStore.loadAccounts()
  } catch (error) {
    console.warn('加载账号失败:', error)
  }
}

async function openSession() {
  if (!accountId.value || opening.value) return
  opening.value = true
  lastError.value = ''
  try {
    const response = await http.post('/api/v2/console/open', { accountId: accountId.value })
    const data = response?.data || {}
    sessionId.value = data.id || ''
    viewport.value = data.viewport || viewport.value
    if (data.accountId) accountId.value = data.accountId
    startPolling()
    await refreshStatus()
    ElMessage.success('窗口已打开，首屏可能需要 60~90 秒')
  } catch (error) {
    lastError.value = error?.message || '打开失败'
  } finally {
    opening.value = false
  }
}

function startPolling() {
  stopPolling()
  timer = window.setInterval(async () => {
    if (document.hidden) return
    tick += 1
    frameVersion.value += 1
    await Promise.all([refreshStatus(), refreshFrame()])
  }, 900)
}

function stopPolling() {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

async function refreshFrame() {
  if (!sessionId.value) return
  try {
    const response = await fetch(
      `${API_BASE}/api/v2/console/frame?id=${encodeURIComponent(sessionId.value)}&t=${Date.now()}`,
      { headers: authHeaders(), cache: 'no-store' }
    )
    if (response.status !== 200) return
    const blob = await response.blob()
    if (!blob.size) return
    const nextUrl = URL.createObjectURL(blob)
    if (frameObjectUrl) URL.revokeObjectURL(frameObjectUrl)
    frameObjectUrl = nextUrl
    frameSrc.value = nextUrl
  } catch (error) {
    console.warn('读取官方窗口画面失败:', error)
  }
}

function clearFrame() {
  frameSrc.value = ''
  if (frameObjectUrl) URL.revokeObjectURL(frameObjectUrl)
  frameObjectUrl = ''
}

async function refreshStatus() {
  if (!sessionId.value) return
  try {
    const response = await http.get('/api/v2/console/status', { id: sessionId.value }, { silent: true })
    const data = response?.data || {}
    status.value = data.status || status.value
    message.value = data.message || ''
    if (data.viewport) viewport.value = data.viewport
    if (data.status === 'failed') {
      lastError.value = data.message || '内嵌窗口异常'
    }
  } catch (error) {
    // 会话被回收时后端返回 404：给出明确提示并复位
    stopPolling()
    sessionId.value = ''
    status.value = 'idle'
    lastError.value = '窗口已关闭（可能空闲超时）'
  }
}

async function sendInput(kind, payload = {}) {
  if (!sessionId.value) return
  try {
    await http.post('/api/v2/console/input', { id: sessionId.value, kind, payload }, { silent: true })
  } catch (error) {
    lastError.value = error?.message || '操作失败'
  }
}

function imagePoint(event) {
  const img = event.currentTarget
  const rect = img.getBoundingClientRect()
  const natural = img.naturalWidth || viewport.value.width
  const naturalHeight = img.naturalHeight || viewport.value.height
  const x = ((event.clientX - rect.left) / rect.width) * natural
  const y = ((event.clientY - rect.top) / rect.height) * naturalHeight
  return { x: Math.round(x), y: Math.round(y) }
}

function onClick(event) {
  const point = imagePoint(event)
  sendInput('click', point)
}

let wheelAt = 0
function onWheel(event) {
  const now = Date.now()
  if (now - wheelAt < 120) return
  wheelAt = now
  sendInput('scroll', { deltaY: event.deltaY })
}

function sendText() {
  const value = String(text.value || '')
  if (!value) return
  sendInput('text', { text: value })
  text.value = ''
}

function pressKey(key) {
  sendInput('key', { key })
}

async function reloadPage() {
  reloading.value = true
  try {
    await sendInput('reload')
    frameVersion.value += 1
    await refreshFrame()
    ElMessage.success('已通知页面刷新')
  } finally {
    reloading.value = false
  }
}

function onFrameLoad() {
  // 画面尺寸拿到后用真实像素做坐标换算
  const img = stageRef.value?.querySelector('img')
  if (img?.naturalWidth && Math.abs(img.naturalWidth - viewport.value.width) > 2) {
    viewport.value = { width: img.naturalWidth, height: img.naturalHeight }
  }
}

async function closeSession() {
  const id = sessionId.value
  stopPolling()
  clearFrame()
  sessionId.value = ''
  status.value = 'idle'
  message.value = ''
  if (id) {
    try {
      await http.post('/api/v2/console/close', { id }, { silent: true })
    } catch (error) {
      console.warn('关闭内嵌窗口失败:', error)
    }
  }
}

onMounted(async () => {
  await loadAccounts()
  if (!accountId.value && douyinAccounts.value.length) {
    accountId.value = douyinAccounts.value[0].id
  }
})

onBeforeUnmount(() => {
  stopPolling()
  clearFrame()
  // 页面离开必须释放：内嵌窗口占着账号的浏览器目录和账号租约
  if (sessionId.value) {
    http.post('/api/v2/console/close', { id: sessionId.value }, { silent: true }).catch(() => {})
  }
})
</script>

<style lang="scss" scoped>
.official-console {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  gap: 10px;
}

.console-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.console-title {
  display: flex;
  flex-direction: column;
  gap: 2px;

  strong {
    font-size: 16px;
    color: var(--el-text-color-primary);
  }

  span {
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }
}

.console-actions {
  display: flex;
  align-items: center;
  gap: 8px;

  .account-select {
    width: 220px;
  }
}

.console-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);

  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #c0c4cc;

    &.is-ready { background: #2f9e44; }
    &.is-pending { background: #d98324; }
    &.is-error { background: #d64545; }
  }

  .error {
    color: #d64545;
  }
}

.console-stage {
  flex: 1;
  min-height: 320px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 10px;
  background: #f5f6f8;
  overflow: auto;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 8px;
}

.console-frame {
  width: 100%;
  max-width: 1280px;
  cursor: crosshair;
  border-radius: 6px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.08);
  background: #fff;
}

.console-empty {
  margin: auto;
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;

  .hint {
    margin-top: 6px;
    font-size: 12px;
    color: var(--el-text-color-placeholder);
  }
}

.console-input {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
