<template>
  <div class="dashboard">
    <header class="page-heading">
      <div class="page-heading__copy">
        <h1>内容运营总览</h1>
        <p>跨平台账号、私信与发布任务集中在这里。先看需要处理的运行状态，再进入具体工作区。</p>
      </div>
      <div class="page-heading__actions">
        <el-button @click="navigateTo('/messages')">
          <el-icon><ChatDotRound /></el-icon>
          查看私信
        </el-button>
        <el-button type="primary" @click="navigateTo('/publish-center')">
          <el-icon><Upload /></el-icon>
          发布内容
        </el-button>
      </div>
    </header>

    <section class="overview-panel panel" aria-label="运营概览">
      <div class="account-total">
        <span class="section-label">账号总量</span>
        <div class="account-figure">
          <strong>{{ accountStats.total }}</strong>
          <span>个登录账号</span>
        </div>
        <div class="status-line">
          <span class="status-key is-good">{{ accountStats.normal }} 正常</span>
          <span class="status-key" :class="accountStats.abnormal ? 'is-danger' : ''">
            {{ accountStats.abnormal }} 异常
          </span>
        </div>
        <p>{{ platformStats.total }} 个平台已有账号记录</p>
      </div>

      <div class="overview-detail">
        <div class="overview-block platform-block">
          <div class="block-heading">
            <span>平台分布</span>
            <span class="mono">{{ accountStats.total }} 个账号</span>
          </div>
          <div class="platform-bars">
            <div v-for="platform in platformBars" :key="platform.name" class="platform-row">
              <span class="platform-name">{{ platform.name }}</span>
              <span class="bar-track" aria-hidden="true">
                <span
                  class="bar-fill"
                  :class="{ 'is-lead': platform.lead }"
                  :style="{ width: platform.width + '%' }"
                />
              </span>
              <span class="mono platform-count">{{ platform.count }}</span>
            </div>
          </div>
        </div>

        <div class="overview-block materials-block">
          <div class="block-heading">
            <span>素材库</span>
            <button class="quiet-link" @click="navigateTo('/material-management')">查看素材</button>
          </div>
          <div class="material-figure">
            <strong>{{ contentStats.total }}</strong>
            <span>份素材</span>
          </div>
          <div class="material-breakdown">
            <span>视频 <b class="mono">{{ contentStats.videos }}</b></span>
            <span>图片 <b class="mono">{{ contentStats.images }}</b></span>
            <span>其他 <b class="mono">{{ contentStats.others }}</b></span>
          </div>
        </div>
      </div>
    </section>

    <div class="operations-layout">
      <section class="ops-panel panel" aria-labelledby="ops-title">
        <div class="section-title-row">
          <div>
            <h2 id="ops-title">今日运营带</h2>
            <p>任务状态与本周排期，按时间顺序排开</p>
          </div>
          <el-button text @click="navigateTo('/publish-calendar')">打开发布日历</el-button>
        </div>

        <div class="metric-rail">
          <div class="rail-metric" @click="navigateTo('/todo')">
            <span>今日待办</span>
            <strong class="mono">{{ cockpit.todos?.length || 0 }}</strong>
            <small>待处理、待确认与进行中</small>
          </div>
          <div class="rail-metric">
            <span>任务成功率</span>
            <strong class="mono">{{ cockpit.successRate ?? '—' }}<template v-if="cockpit.successRate != null">%</template></strong>
            <small>按已记录任务计算</small>
          </div>
          <div class="rail-metric">
            <span>账号健康</span>
            <strong :class="cockpit.health?.ready ? 'is-good' : 'is-warning'">
              {{ cockpit.health?.ready ? '就绪' : '需检查' }}
            </strong>
            <small>{{ (cockpit.health?.blockers || []).join('、') || '无阻塞项' }}</small>
          </div>
          <div class="rail-metric">
            <span>7 日播放增量</span>
            <strong class="mono">{{ weekDeltaText }}</strong>
            <small>{{ cockpit.weekDelta?.snapshotAt ? `快照 ${cockpit.weekDelta.snapshotAt}` : '暂无快照' }}</small>
          </div>
        </div>

        <div class="schedule-heading">
          <span>本周排期</span>
          <span class="mono">{{ calendarItems.length }} 条</span>
        </div>

        <div v-if="calendarItems.length" class="schedule-list">
          <article v-for="item in calendarItems" :key="item.key" class="schedule-row">
            <div class="schedule-time">
              <strong class="mono">{{ item.time }}</strong>
              <span>{{ item.day }} · {{ item.weekday }}</span>
            </div>
            <span class="schedule-status-mark" :class="'is-' + item.tone" aria-hidden="true" />
            <div class="schedule-copy">
              <strong>{{ item.accountName }}</strong>
              <span>{{ item.statusLabel }}</span>
            </div>
            <span class="schedule-platform">{{ item.platform }}</span>
          </article>
        </div>
        <div v-else class="schedule-empty">
          <span class="schedule-status-mark" aria-hidden="true" />
          <div>
            <strong>本周暂无排期</strong>
            <p>发布任务安排后会在这里按时间显示。</p>
          </div>
          <el-button @click="navigateTo('/publish-calendar')">去排期</el-button>
        </div>
      </section>

      <SystemHealthCard class="health-rail" />
    </div>

    <section class="workspace-section" aria-labelledby="quick-title">
      <div class="section-title-row">
        <div>
          <h2 id="quick-title">工作区入口</h2>
          <p>常用操作保持在同一列，账号、素材和发布流程可以直接进入</p>
        </div>
      </div>
      <div class="quick-command-grid">
        <button
          v-for="action in quickActions"
          :key="action.path"
          class="command-row"
          @click="navigateTo(action.path)"
        >
          <span class="command-icon"><el-icon :size="18"><component :is="action.icon" /></el-icon></span>
          <span class="command-copy">
            <strong>{{ action.title }}</strong>
            <small>{{ action.description }}</small>
          </span>
          <el-icon class="command-arrow" :size="16"><ArrowRight /></el-icon>
        </button>
      </div>
    </section>

    <section class="recent-panel panel" aria-labelledby="recent-title">
      <div class="section-title-row">
        <div>
          <h2 id="recent-title">最近上传素材</h2>
          <p>最近 5 份文件</p>
        </div>
        <el-button text @click="navigateTo('/material-management')">查看全部</el-button>
      </div>

      <div v-loading="loading" class="table-wrap">
        <el-table :data="recentMaterials" class="dashboard-table">
          <el-table-column prop="filename" label="文件名" min-width="360" show-overflow-tooltip />
          <el-table-column prop="filesize" label="文件大小" width="130">
            <template #default="scope">{{ scope.row.filesize }} MB</template>
          </el-table-column>
          <el-table-column prop="upload_time" label="上传时间" width="190" />
          <el-table-column label="类型" width="100">
            <template #default="scope">
              <span class="file-kind">{{ getFileType(scope.row.filename) }}</span>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!loading && recentMaterials.length === 0" description="暂无素材" :image-size="72" />
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowRight,
  ChatDotRound,
  DataAnalysis,
  Picture,
  Upload,
  UserFilled,
} from '@element-plus/icons-vue'
import { materialApi } from '@/api/material'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { opsApi } from '@/api/ops'
import SystemHealthCard from '@/components/SystemHealthCard.vue'

const router = useRouter()
const accountStore = useAccountStore()
const appStore = useAppStore()
const loading = ref(false)
const cockpit = ref({
  todos: [],
  byStatus: [],
  byPlatform: [],
  weekDelta: {},
  successRate: null,
  health: {},
  calendar: [],
})

const weekDeltaText = computed(() => {
  const platforms = cockpit.value.weekDelta?.platforms || []
  let plays = 0
  for (const platform of platforms) plays += Number(platform.plays || 0)
  return plays ? `+${plays.toLocaleString('zh-CN')}` : '暂无增量'
})

const accountStats = computed(() => {
  const accounts = accountStore.accounts
  const normal = accounts.filter(account => account.status === '正常').length
  const abnormal = accounts.filter(account => account.status !== '正常' && account.status !== '验证中').length
  return { total: accounts.length, normal, abnormal }
})

const platformStats = computed(() => {
  const accounts = accountStore.accounts
  const kuaishou = accounts.filter(account => account.platform === '快手').length
  const douyin = accounts.filter(account => account.platform === '抖音').length
  const channels = accounts.filter(account => account.platform === '视频号').length
  const xiaohongshu = accounts.filter(account => account.platform === '小红书').length
  const total = [kuaishou, douyin, channels, xiaohongshu].filter(count => count > 0).length
  return { total, kuaishou, douyin, channels, xiaohongshu }
})

const platformBars = computed(() => {
  const rows = [
    { name: '抖音', count: platformStats.value.douyin },
    { name: '视频号', count: platformStats.value.channels },
    { name: '小红书', count: platformStats.value.xiaohongshu },
    { name: '快手', count: platformStats.value.kuaishou },
  ]
  const max = Math.max(1, ...rows.map(row => row.count))
  const lead = rows.reduce((best, row) => row.count > best.count ? row : best, rows[0])
  return rows.map(row => ({
    ...row,
    width: row.count ? Math.max(18, (row.count / max) * 100) : 0,
    lead: row.count > 0 && row.name === lead.name,
  }))
})

const videoExtensions = ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv']
const imageExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']

const contentStats = computed(() => {
  const materials = appStore.materials
  const videos = materials.filter(material => videoExtensions.some(ext => material.filename.toLowerCase().endsWith(ext))).length
  const images = materials.filter(material => imageExtensions.some(ext => material.filename.toLowerCase().endsWith(ext))).length
  return { total: materials.length, videos, images, others: materials.length - videos - images }
})

const recentMaterials = computed(() => (
  [...appStore.materials]
    .sort((a, b) => new Date(b.upload_time) - new Date(a.upload_time))
    .slice(0, 5)
))

const quickActions = [
  { path: '/account-management', title: '账号管理', description: '登录会话、健康状态与发布环境', icon: UserFilled },
  { path: '/material-management', title: '素材管理', description: '上传视频与图片，整理发布素材', icon: Picture },
  { path: '/publish-center', title: '发布中心', description: '选择账号和平台，安排发布任务', icon: Upload },
  { path: '/ops', title: '运维台面', description: '检查后台任务、接口与运行记录', icon: DataAnalysis },
]

const calendarItems = computed(() => (
  (cockpit.value.calendar || []).slice(0, 8).map((item, index) => {
    const raw = String(item.scheduled_at || '').replace('T', ' ')
    const [datePart = '', timePart = ''] = raw.split(' ')
    const [year, month, day] = datePart.split('-').map(Number)
    const validDate = Boolean(year && month && day)
    const weekday = validDate
      ? ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][new Date(year, month - 1, day).getDay()]
      : '时间待定'
    return {
      ...item,
      key: item.id || `${item.scheduled_at || 'schedule'}-${index}`,
      time: timePart ? timePart.slice(0, 5) : '待定',
      day: validDate ? `${month}月${day}日` : '未定日期',
      weekday,
      accountName: calendarAccount(item.account),
      platform: item.platform || item.account_platform || '跨平台',
      statusLabel: calendarStatus(item.status),
      tone: calendarTone(item.status),
    }
  })
))

const calendarAccount = (account) => {
  if (typeof account === 'string' && account) return account
  return account?.name || account?.account || '未指定账号'
}

const calendarStatus = (status) => {
  const normalized = String(status || '').toLowerCase()
  const labels = {
    scheduled: '已排期',
    pending: '待处理',
    queued: '等待发布',
    running: '发布中',
    processing: '处理中',
    success: '已发布',
    published: '已发布',
    failed: '发布失败',
    error: '发布失败',
    cancelled: '已取消',
  }
  return labels[normalized] || (status || '状态未记录')
}

const calendarTone = (status) => {
  const normalized = String(status || '').toLowerCase()
  if (/success|published|完成|已发布/.test(normalized)) return 'good'
  if (/failed|error|失败|异常/.test(normalized)) return 'danger'
  if (/pending|scheduled|queued|running|processing|待|排期|发布中/.test(normalized)) return 'warning'
  return 'neutral'
}

const getFileType = (filename = '') => {
  if (videoExtensions.some(ext => filename.toLowerCase().endsWith(ext))) return '视频'
  if (imageExtensions.some(ext => filename.toLowerCase().endsWith(ext))) return '图片'
  return '其他'
}

const navigateTo = (path) => router.push(path)

const fetchDashboardData = async () => {
  loading.value = true
  try {
    const [accountRes, materialRes, cockpitRes] = await Promise.allSettled([
      accountStore.loadAccounts(true),
      materialApi.getAllMaterials(),
      opsApi.cockpit(),
    ])

    if (accountRes.status === 'rejected') console.error('获取账号数据失败:', accountRes.reason)
    if (materialRes.status === 'fulfilled' && materialRes.value.code === 200) {
      appStore.setMaterials(materialRes.value.data)
    }
    if (cockpitRes.status === 'fulfilled' && cockpitRes.value?.code === 200) {
      cockpit.value = { ...cockpit.value, ...cockpitRes.value.data }
    }
  } catch (error) {
    console.error('获取仪表盘数据失败:', error)
  } finally {
    loading.value = false
  }
}

onMounted(fetchDashboardData)
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.dashboard {
  padding-bottom: 32px;
}

.overview-panel {
  display: grid;
  grid-template-columns: minmax(220px, 0.72fr) minmax(0, 2fr);
  overflow: hidden;
  margin-bottom: 24px;
}

.account-total {
  padding: 24px;
  border-right: 1px solid $border-lighter;
  background: #fbfaf6;
}

.section-label,
.block-heading > span:first-child,
.schedule-heading > span:first-child {
  color: $text-secondary;
  font-size: 12px;
  font-weight: 500;
}

.account-figure {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  margin-top: 16px;
}

.account-figure strong {
  color: $text-primary;
  font-size: clamp(44px, 5vw, 64px);
  font-weight: 500;
  line-height: 0.95;
  letter-spacing: -0.04em;
  font-variant-numeric: tabular-nums;
}

.account-figure span {
  padding-bottom: 6px;
  color: $text-secondary;
  font-size: 13px;
}

.status-line {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 20px;
}

.account-total > p {
  margin: 14px 0 0;
  color: $text-secondary;
  font-size: 12px;
}

.overview-detail {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(220px, 0.8fr);
}

.overview-block {
  min-width: 0;
  padding: 24px;
}

.overview-block + .overview-block {
  border-left: 1px solid $border-lighter;
}

.block-heading,
.schedule-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.block-heading .mono,
.schedule-heading .mono {
  color: $text-secondary;
  font-size: 11px;
}

.quiet-link {
  padding: 0;
  color: $text-regular;
  border: 0;
  background: transparent;
  font-size: 12px;
  cursor: pointer;
}

.quiet-link:hover {
  color: $primary-color;
}

.platform-bars {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 18px;
}

.platform-row {
  display: grid;
  grid-template-columns: 48px minmax(60px, 1fr) 24px;
  align-items: center;
  gap: 10px;
}

.platform-name {
  color: $text-regular;
  font-size: 12px;
}

.bar-track {
  height: 5px;
  overflow: hidden;
  border-radius: 1px;
  background: $ink-100;
}

.bar-fill {
  display: block;
  height: 100%;
  border-radius: 1px;
  background: $ink-300;
}

.bar-fill.is-lead {
  background: $primary-color;
}

.platform-count {
  color: $text-secondary;
  font-size: 11px;
  text-align: right;
}

.material-figure {
  display: flex;
  align-items: flex-end;
  gap: 7px;
  margin-top: 18px;
}

.material-figure strong {
  color: $text-primary;
  font-size: 38px;
  font-weight: 500;
  line-height: 1;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}

.material-figure span {
  padding-bottom: 4px;
  color: $text-secondary;
  font-size: 12px;
}

.material-breakdown {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 16px;
  color: $text-secondary;
  font-size: 12px;
}

.material-breakdown b {
  margin-left: 3px;
  color: $text-regular;
  font-weight: 500;
}

.operations-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(300px, 0.75fr);
  align-items: start;
  gap: 24px;
  margin-bottom: 32px;
}

.ops-panel {
  min-width: 0;
  padding: 24px;
}

.section-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}

.section-title-row h2 {
  margin: 0;
  color: $text-primary;
  font-size: 20px;
  font-weight: 500;
  line-height: 1.25;
  letter-spacing: -0.02em;
}

.section-title-row p {
  margin: 6px 0 0;
  color: $text-secondary;
  font-size: 12px;
  line-height: 1.5;
}

.metric-rail {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-top: 24px;
  border-block: 1px solid $border-lighter;
}

.rail-metric {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
  padding: 16px;
  cursor: default;
}

.rail-metric:first-child {
  padding-left: 0;
  cursor: pointer;
}

.rail-metric + .rail-metric {
  border-left: 1px solid $border-lighter;
}

.rail-metric > span {
  color: $text-secondary;
  font-size: 12px;
}

.rail-metric strong {
  overflow: hidden;
  color: $text-primary;
  font-size: 24px;
  font-weight: 500;
  line-height: 1.15;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.rail-metric strong.is-good {
  color: $success-color;
}

.rail-metric strong.is-warning {
  color: $warning-color;
}

.rail-metric small {
  overflow: hidden;
  color: $text-secondary;
  font-size: 11px;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.schedule-heading {
  margin-top: 24px;
  margin-bottom: 8px;
}

.schedule-list {
  display: flex;
  flex-direction: column;
}

.schedule-row {
  display: grid;
  grid-template-columns: 100px 8px minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  min-height: 64px;
  border-bottom: 1px solid $border-lighter;
}

.schedule-row:last-child {
  border-bottom: 0;
}

.schedule-time {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.schedule-time strong {
  color: $text-primary;
  font-size: 13px;
  font-weight: 500;
}

.schedule-time span,
.schedule-copy span {
  color: $text-secondary;
  font-size: 11px;
}

.schedule-status-mark {
  width: 6px;
  height: 6px;
  border-radius: 1px;
  background: $info-color;
}

.schedule-status-mark.is-good {
  background: $success-color;
}

.schedule-status-mark.is-warning {
  background: $warning-color;
}

.schedule-status-mark.is-danger {
  background: $danger-color;
}

.schedule-copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 3px;
}

.schedule-copy strong {
  overflow: hidden;
  color: $text-primary;
  font-size: 13px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.schedule-platform {
  color: $text-secondary;
  font-size: 12px;
}

.schedule-empty {
  display: flex;
  align-items: center;
  gap: 14px;
  min-height: 96px;
  padding-top: 12px;
}

.schedule-empty > div {
  flex: 1;
}

.schedule-empty strong {
  color: $text-primary;
  font-size: 14px;
  font-weight: 500;
}

.schedule-empty p {
  margin: 5px 0 0;
  color: $text-secondary;
  font-size: 12px;
}

.health-rail {
  position: sticky;
  top: 0;
}

.workspace-section {
  margin-bottom: 32px;
}

.quick-command-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-top: 16px;
}

.command-row {
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr) 16px;
  align-items: center;
  gap: 12px;
  min-height: 88px;
  padding: 18px;
  text-align: left;
  color: $text-primary;
  border: 1px solid $border-lighter;
  border-radius: 12px;
  background: $paper;
  cursor: pointer;
  transition: background-color $duration-base $ease-out, border-color $duration-base $ease-out,
    transform $duration-fast $ease-out;
}

.command-row:hover {
  border-color: $ink-200;
  background: #fbfaf6;
}

.command-row:active {
  transform: scale(0.99);
}

.command-icon {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  color: $text-regular;
  border: 1px solid $border-lighter;
  border-radius: 8px;
  background: $bg-color-raised;
}

.command-copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 4px;
}

.command-copy strong {
  color: $text-primary;
  font-size: 14px;
  font-weight: 500;
}

.command-copy small {
  overflow: hidden;
  color: $text-secondary;
  font-size: 11px;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.command-arrow {
  color: $ink-400;
}

.recent-panel {
  padding: 24px;
}

.table-wrap {
  overflow-x: auto;
  margin-top: 16px;
}

.dashboard-table {
  min-width: 780px;
}

.file-kind {
  color: $text-secondary;
  font-size: 12px;
}

@media (max-width: 1240px) {
  .overview-panel {
    grid-template-columns: minmax(200px, 0.65fr) minmax(0, 1.8fr);
  }

  .operations-layout {
    grid-template-columns: minmax(0, 1.35fr) minmax(280px, 0.7fr);
  }

  .quick-command-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 1040px) {
  .overview-panel,
  .operations-layout {
    grid-template-columns: 1fr;
  }

  .account-total {
    border-right: 0;
    border-bottom: 1px solid $border-lighter;
  }

  .health-rail {
    position: static;
  }
}

@media (max-width: 720px) {
  .overview-detail {
    grid-template-columns: 1fr;
  }

  .overview-block + .overview-block {
    border-top: 1px solid $border-lighter;
    border-left: 0;
  }

  .metric-rail {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .rail-metric:nth-child(3) {
    border-top: 1px solid $border-lighter;
    border-left: 0;
  }

  .rail-metric:nth-child(4) {
    border-top: 1px solid $border-lighter;
  }

  .rail-metric:nth-child(odd) {
    padding-left: 0;
  }

  .quick-command-grid {
    grid-template-columns: 1fr;
  }

  .schedule-row {
    grid-template-columns: 88px 8px minmax(0, 1fr);
    padding-block: 12px;
  }

  .schedule-platform {
    grid-column: 3;
  }

  .section-title-row {
    flex-direction: column;
    gap: 10px;
  }

  .account-total,
  .overview-block,
  .ops-panel,
  .recent-panel {
    padding: 20px;
  }
}
</style>
