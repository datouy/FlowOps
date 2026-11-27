<template>
  <div class="today-page">
    <header class="page-heading">
      <div class="page-heading__copy">
        <div class="title-line">
          <h1>今天</h1>
          <span class="page-kicker">运营驾驶舱</span>
        </div>
        <p>
          {{ dateLabel }}
          <template v-if="lastUpdatedAt"> · 页面刷新 {{ lastUpdatedAt }}</template>
        </p>
      </div>
      <div class="page-heading__actions">
        <el-button @click="load()" :loading="loading">刷新</el-button>
        <el-button type="primary" @click="router.push('/content?view=compose')">新建内容</el-button>
      </div>
    </header>

    <section class="kpi-grid" aria-label="运营关键指标">
      <button
        v-for="metric in cockpitMetrics"
        :key="metric.key"
        type="button"
        class="kpi-card"
        :class="'is-' + metric.tone"
        @click="router.push(metric.to)"
      >
        <span class="kpi-top">
          <span>{{ metric.label }}</span>
          <span class="kpi-dot" aria-hidden="true" />
        </span>
        <span class="kpi-number">
          {{ metric.value }}<small v-if="metric.unit">{{ metric.unit }}</small>
        </span>
        <span class="kpi-detail">{{ metric.detail }}</span>
        <span class="kpi-action">查看<el-icon><ArrowRight /></el-icon></span>
      </button>
    </section>

    <section class="cockpit-grid">
      <article class="panel cockpit-panel account-panel" aria-labelledby="account-matrix-title">
        <div class="panel-heading">
          <div>
            <h2 id="account-matrix-title">账号矩阵</h2>
            <p>{{ normalAccounts.length }} 个正常 · {{ accountAttention.length }} 个需处理</p>
          </div>
          <el-button text @click="router.push('/settings?view=accounts')">管理账号</el-button>
        </div>

        <div v-if="platformSummary.length" class="platform-strip" aria-label="平台账号分布">
          <span v-for="platform in platformSummary" :key="platform.name" class="platform-pill">
            <span>{{ platform.name }}</span>
            <strong>{{ platform.normal }}/{{ platform.total }}</strong>
          </span>
        </div>

        <div v-if="accountStore.accounts.length" class="account-grid">
          <button
            v-for="account in accountStore.accounts"
            :key="account.id"
            type="button"
            class="account-card"
            @click="router.push({ path: '/settings', query: { view: 'accounts', filter: account.name } })"
          >
            <span class="account-avatar" aria-hidden="true">{{ avatarText(account.name) }}</span>
            <span class="account-copy">
              <strong>{{ account.name }}</strong>
              <span>{{ account.platform }} · {{ account.identity || (account.identityBound ? '身份已绑定' : '身份待绑定') }}</span>
            </span>
            <span class="account-state" :class="'is-' + accountTone(account.status)">{{ account.status }}</span>
            <span v-if="accountIssue(account)" class="account-issue">{{ accountIssue(account) }}</span>
            <span class="account-time mono">{{ shortDateTime(account.lastCheckAt) || '尚未校验' }}</span>
          </button>
        </div>

        <div v-else class="panel-empty">
          <div>
            <strong>还没有媒体账号</strong>
            <p>登录账号后，发布、私信和数据同步才会开始工作。</p>
          </div>
          <el-button type="primary" @click="router.push('/settings?view=accounts')">添加账号</el-button>
        </div>
      </article>

      <div class="side-stack">
        <article class="panel cockpit-panel publish-panel" aria-labelledby="publish-overview-title">
          <div class="panel-heading">
            <div>
              <h2 id="publish-overview-title">发布流转</h2>
              <p>审核、排期和远端结果</p>
            </div>
            <el-button text @click="router.push('/insights?view=works')">发布记录</el-button>
          </div>

          <div class="publish-summary">
            <div class="rate-block">
              <strong>{{ publishSuccessRate }}<small>%</small></strong>
              <span>近 7 天成功率</span>
            </div>
            <div class="publish-counts">
              <span><strong>{{ publishSuccessCount }}</strong>成功</span>
              <span><strong>{{ publishFailedCount }}</strong>失败</span>
            </div>
          </div>
          <div class="progress-track" aria-hidden="true">
            <span :style="{ width: publishProgressWidth + '%' }" />
          </div>

          <div class="flow-list">
            <button type="button" @click="router.push('/content?view=review')">
              <span>待审核</span><strong class="mono">{{ pendingReview }}</strong>
            </button>
            <button type="button" @click="router.push('/content?view=calendar')">
              <span>今日待发布</span><strong class="mono">{{ scheduledToday }}</strong>
            </button>
            <button type="button" class="is-danger" @click="router.push('/insights?view=works')">
              <span>发布异常</span><strong class="mono">{{ failedJobs }}</strong>
            </button>
          </div>
        </article>

        <article class="panel cockpit-panel inbox-panel" aria-labelledby="inbox-overview-title">
          <div class="panel-heading">
            <div>
              <h2 id="inbox-overview-title">私信跟进</h2>
              <p>待回复、高意向和今日入站</p>
            </div>
            <el-button text @click="router.push('/inbox?view=messages')">打开收件箱</el-button>
          </div>
          <div class="compact-metrics">
            <div><strong class="mono">{{ dmPending }}</strong><span>待回复</span></div>
            <div><strong class="mono">{{ highIntent }}</strong><span>高意向</span></div>
            <div><strong class="mono">{{ todayInbound }}</strong><span>今日入站</span></div>
            <div><strong class="mono">{{ contactCount }}</strong><span>联系人</span></div>
          </div>
        </article>
      </div>
    </section>

    <section class="cockpit-grid cockpit-grid--lower">
      <article class="panel cockpit-panel trend-panel" aria-labelledby="trend-title">
        <div class="panel-heading">
          <div>
            <h2 id="trend-title">近期数据</h2>
            <p>近 7 天内容表现与播放快照</p>
          </div>
          <el-button text @click="router.push('/insights?view=overview')">查看洞察</el-button>
        </div>

        <div class="trend-summary">
          <div><span>近 7 日播放</span><strong class="mono">{{ compactNumber(weekPlays) }}</strong></div>
          <div><span>近 7 日点赞</span><strong class="mono">{{ compactNumber(weekLikes) }}</strong></div>
          <div><span>近 7 日评论</span><strong class="mono">{{ compactNumber(weekComments) }}</strong></div>
          <div><span>累计播放</span><strong class="mono">{{ compactNumber(cumulativePlays) }}</strong></div>
        </div>

        <div class="trend-chart" role="img" :aria-label="trendAriaLabel">
          <div v-for="day in recentSeries" :key="day.date" class="trend-day">
            <span class="bar-value mono">{{ day.plays ? compactNumber(day.plays) : '—' }}</span>
            <div class="bar-rail">
              <span
                class="bar-fill"
                :class="{ 'is-missing': !day.hasSnapshot }"
                :style="{ height: barHeight(day) + '%' }"
              />
            </div>
            <span class="bar-date">{{ shortDay(day.date) }}</span>
          </div>
        </div>
        <p class="data-note">
          7 天中 {{ coverageDays }} 天有数据快照（覆盖率 {{ coveragePercent }}%）。缺日显示为“—”，不会用累计值补造趋势。
        </p>
      </article>

      <article class="panel cockpit-panel sync-panel" aria-labelledby="sync-title">
        <div class="panel-heading">
          <div>
            <h2 id="sync-title">同步状态</h2>
            <p>{{ syncIssueCount }} 项异常 · {{ syncMissingCount }} 个平台未登录</p>
          </div>
          <el-button text @click="router.push('/settings?view=accounts')">账号设置</el-button>
        </div>
        <div v-if="syncRows.length" class="sync-list">
          <button
            v-for="platform in syncRows"
            :key="platform.key"
            type="button"
            class="sync-row"
            @click="router.push('/settings?view=accounts')"
          >
            <span class="sync-dot" :class="'is-' + syncTone(platform)" aria-hidden="true" />
            <span class="sync-copy">
              <strong>{{ platform.platform }}</strong>
              <span>{{ syncDetail(platform) }}</span>
            </span>
            <span class="sync-state" :class="'is-' + syncTone(platform)">{{ syncLabel(platform) }}</span>
            <span class="sync-next mono">{{ platform.nextRunAt ? shortDateTime(platform.nextRunAt).slice(6) : '—' }}</span>
          </button>
        </div>
        <div v-else class="panel-empty panel-empty--compact">
          <div><strong>暂无同步信息</strong><p>登录账号后会自动启动私信同步。</p></div>
        </div>
      </article>
    </section>

    <section class="work-panel panel" aria-labelledby="work-title">
      <div class="work-heading">
        <div>
          <h2 id="work-title">需要处理</h2>
          <p>按异常和时效排序</p>
        </div>
        <span class="mono">{{ activeTasks.length }}</span>
      </div>

      <div v-if="activeTasks.length" class="work-list">
        <button
          v-for="task in activeTasks"
          :key="task.key"
          class="work-row"
          type="button"
          @click="router.push(task.to)"
        >
          <span class="work-mark" :class="'is-' + task.tone" aria-hidden="true" />
          <span class="work-copy">
            <strong>{{ task.label }}</strong>
            <span>{{ task.detail }}</span>
          </span>
          <span class="work-count mono">{{ task.count }}</span>
          <el-icon><ArrowRight /></el-icon>
        </button>
      </div>

      <div v-else class="today-empty">
        <span class="empty-line" aria-hidden="true" />
        <div>
          <strong>暂时没有待处理项</strong>
          <p>可以准备下一条内容。</p>
        </div>
        <el-button type="primary" @click="router.push('/content?view=compose')">新建内容</el-button>
      </div>
    </section>

    <section v-if="priorityLeads.length" class="leads-panel" aria-labelledby="leads-title">
      <div class="section-heading">
        <h2 id="leads-title">优先跟进</h2>
        <el-button text @click="router.push('/inbox?view=messages&status=high')">查看全部</el-button>
      </div>
      <div class="lead-list">
        <button
          v-for="lead in priorityLeads"
          :key="lead.id"
          type="button"
          class="lead-row"
          @click="openLead(lead)"
        >
          <el-avatar :size="32">{{ avatarText(lead.nickname) }}</el-avatar>
          <span class="lead-copy">
            <strong>{{ lead.nickname || '匿名用户' }}</strong>
            <span>{{ lead.last_content || lead.recommended_action || '等待跟进' }}</span>
          </span>
          <span class="lead-state">{{ lead.priority_label || lead.stage_label || '待跟进' }}</span>
          <el-icon><ArrowRight /></el-icon>
        </button>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight } from '@element-plus/icons-vue'
import { http } from '@/utils/request'
import { useAccountStore } from '@/stores/account'
import { useLivePolling } from '@/composables/useLivePolling'

const router = useRouter()
const accountStore = useAccountStore()
const loading = ref(false)
const lastUpdatedAt = ref('')
const todo = ref({ date: '', counts: {}, items: [], priority_leads: [] })
const cockpit = ref({})
const analytics = ref({})
const dmHealth = ref({ platforms: [], problems: [] })
const dailyStats = ref({ coverage: {}, delta: {}, series: { series: [] } })

const dateLabel = computed(() => {
  const value = todo.value.date ? new Date(todo.value.date + 'T00:00:00') : new Date()
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'long',
    day: 'numeric',
    weekday: 'long'
  }).format(value)
})

const counts = computed(() => todo.value.counts || {})
const priorityLeads = computed(() => (todo.value.priority_leads || []).slice(0, 5))
const accountAttention = computed(() => accountStore.accounts.filter(account => account.status !== '正常'))
const normalAccounts = computed(() => accountStore.accounts.filter(account => account.status === '正常'))
const pendingReview = computed(() => Number(counts.value.pending_review || 0))
const failedJobs = computed(() => Number(counts.value.failed_jobs || 0))
const scheduledToday = computed(() => Number(counts.value.scheduled_today || 0))
const dmPending = computed(() => Number(analytics.value.pending || counts.value.messages || 0))
const highIntent = computed(() => Number(analytics.value.high_intent || counts.value.high_intent || 0))
const todayInbound = computed(() => Number(analytics.value.today_inbound || 0))
const contactCount = computed(() => Number(analytics.value.contacts || 0))

const platformSummary = computed(() => {
  const summary = new Map()
  for (const account of accountStore.accounts) {
    const name = account.platform || '未知平台'
    const row = summary.get(name) || { name, total: 0, normal: 0 }
    row.total += 1
    if (account.status === '正常') row.normal += 1
    summary.set(name, row)
  }
  return Array.from(summary.values()).sort((left, right) => right.total - left.total)
})

const publishGroups = computed(() => {
  const groups = new Map()
  for (const row of cockpit.value.byStatus || []) groups.set(row.status, Number(row.n || 0))
  return groups
})
const publishSuccessCount = computed(() => publishGroups.value.get('success') || 0)
const publishFailedCount = computed(() => publishGroups.value.get('failed') || failedJobs.value || 0)
const publishSuccessRate = computed(() => {
  const value = Number(cockpit.value.successRate)
  return Number.isFinite(value) ? value.toFixed(1) : '0.0'
})
const publishProgressWidth = computed(() => Math.max(0, Math.min(100, Number(publishSuccessRate.value) || 0)))

const syncRows = computed(() => dmHealth.value.platforms || [])
const syncIssueCount = computed(() => syncRows.value.reduce((total, platform) => {
  const platformIssue = platform.errorKind && platform.errorKind !== 'no_accounts' ? 1 : 0
  const accountIssue = (platform.accounts || []).filter(account => (
    account.errorKind && !['ok', ''].includes(account.errorKind)
  )).length
  return total + platformIssue + accountIssue
}, 0))
const syncMissingCount = computed(() => syncRows.value.filter(platform => platform.noAccounts).length)

const recentSeries = computed(() => dailyStats.value.series?.series || [])
const chartMax = computed(() => Math.max(1, ...recentSeries.value.map(day => Number(day.plays || 0))))
const weekWindow = computed(() => dailyStats.value.delta?.inWindow || {})
const weekPlays = computed(() => Number(weekWindow.value.plays || 0))
const weekLikes = computed(() => Number(weekWindow.value.likes || 0))
const weekComments = computed(() => Number(weekWindow.value.comments || 0))
const cumulativeTotals = computed(() => (dailyStats.value.delta?.platforms || []).reduce((total, platform) => {
  const cumulative = platform.cumulative || {}
  total.plays += Number(cumulative.plays || 0)
  total.likes += Number(cumulative.likes || 0)
  total.comments += Number(cumulative.comments || 0)
  return total
}, { plays: 0, likes: 0, comments: 0 }))
const cumulativePlays = computed(() => cumulativeTotals.value.plays)
const coverageDays = computed(() => Number(dailyStats.value.coverage?.daysWithData || 0))
const coveragePercent = computed(() => Number(dailyStats.value.coverage?.coverage || 0).toFixed(1))
const trendAriaLabel = computed(() => recentSeries.value.map(day => (
  shortDay(day.date) + ' ' + (day.hasSnapshot ? compactNumber(day.plays) : '无快照')
)).join('，'))

const cockpitMetrics = computed(() => [
  {
    key: 'accounts',
    label: '账号矩阵',
    value: normalAccounts.value.length + '/' + accountStore.accounts.length,
    unit: '正常',
    detail: accountAttention.value.length ? accountAttention.value.length + ' 个账号需处理' : '账号状态正常',
    tone: accountAttention.value.length ? 'danger' : 'good',
    to: '/settings?view=accounts'
  },
  {
    key: 'failed',
    label: '发布异常',
    value: failedJobs.value,
    unit: '项',
    detail: '成功率 ' + publishSuccessRate.value + '%',
    tone: failedJobs.value ? 'danger' : 'good',
    to: '/insights?view=works'
  },
  {
    key: 'scheduled',
    label: '今日待发布',
    value: scheduledToday.value,
    unit: '条',
    detail: pendingReview.value + ' 条等待审核',
    tone: scheduledToday.value ? 'accent' : 'neutral',
    to: '/content?view=calendar'
  },
  {
    key: 'messages',
    label: '待回复私信',
    value: dmPending.value,
    unit: '人',
    detail: highIntent.value + ' 个高意向',
    tone: dmPending.value ? 'warning' : 'neutral',
    to: '/inbox?view=messages'
  },
  {
    key: 'sync',
    label: '同步健康',
    value: syncIssueCount.value,
    unit: '项异常',
    detail: syncMissingCount.value + ' 个平台未登录',
    tone: syncIssueCount.value ? 'danger' : syncMissingCount.value ? 'warning' : 'good',
    to: '/settings?view=accounts'
  },
  {
    key: 'plays',
    label: '近 7 日播放',
    value: compactNumber(weekPlays.value),
    unit: '',
    detail: '累计 ' + compactNumber(cumulativePlays.value),
    tone: 'accent',
    to: '/insights?view=overview'
  }
])

const activeTasks = computed(() => {
  const tasks = []
  if (pendingReview.value) {
    tasks.push({
      key: 'review',
      label: '等待审核',
      detail: '通过后进入发布队列',
      count: pendingReview.value,
      tone: 'attention',
      to: '/content?view=review'
    })
  }
  if (failedJobs.value) {
    tasks.push({
      key: 'failed',
      label: '发布异常',
      detail: '查看失败原因并重试',
      count: failedJobs.value,
      tone: 'danger',
      to: '/insights?view=works'
    })
  }
  if (dmPending.value) {
    tasks.push({
      key: 'messages',
      label: '等待回复',
      detail: '高意向 ' + highIntent.value,
      count: dmPending.value,
      tone: 'attention',
      to: '/inbox?view=messages'
    })
  }
  if (scheduledToday.value) {
    tasks.push({
      key: 'scheduled',
      label: '今天定时发布',
      detail: '查看时间安排',
      count: scheduledToday.value,
      tone: 'neutral',
      to: '/content?view=calendar'
    })
  }
  if (accountAttention.value.length) {
    tasks.push({
      key: 'accounts',
      label: '账号需处理',
      detail: accountAttention.value.map(item => item.name).slice(0, 2).join('、'),
      count: accountAttention.value.length,
      tone: 'danger',
      to: '/settings?view=accounts&filter=attention'
    })
  }
  if (syncIssueCount.value) {
    tasks.push({
      key: 'sync',
      label: '私信同步异常',
      detail: syncRows.value.filter(platform => syncTone(platform) === 'danger').map(item => item.platform).join('、'),
      count: syncIssueCount.value,
      tone: 'danger',
      to: '/settings?view=accounts'
    })
  }
  if (syncMissingCount.value) {
    tasks.push({
      key: 'missing-account',
      label: '平台尚未登录',
      detail: syncRows.value.filter(platform => platform.noAccounts).map(item => item.platform).join('、'),
      count: syncMissingCount.value,
      tone: 'attention',
      to: '/settings?view=accounts'
    })
  }
  return tasks
})

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const results = await Promise.allSettled([
      http.get('/todo/summary', null, { silent: true }),
      // 账号列表变化很慢：静默轮询不再每轮都拉一遍 /api/v2/accounts。
      // 以前每分钟整表重建一次，页面挂久了就是这个在持续制造主线程工作。
      silent ? Promise.resolve(null) : accountStore.loadAccounts(true),
      http.get('/api/v2/dashboard/cockpit', null, { silent: true }),
      http.get('/dm/analytics', null, { silent: true }),
      http.get('/dm/health', null, { silent: true }),
      http.get('/api/v2/stats/daily', { days: 7 }, { silent: true })
    ])
    if (results[0].status === 'fulfilled') todo.value = results[0].value.data || todo.value
    if (results[2].status === 'fulfilled') cockpit.value = results[2].value.data || cockpit.value
    if (results[3].status === 'fulfilled') analytics.value = results[3].value.data || analytics.value
    if (results[4].status === 'fulfilled') dmHealth.value = results[4].value.data || dmHealth.value
    if (results[5].status === 'fulfilled') dailyStats.value = results[5].value.data || dailyStats.value
    lastUpdatedAt.value = new Intl.DateTimeFormat('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    }).format(new Date())
  } finally {
    if (!silent) loading.value = false
  }
}

function accountTone(status) {
  if (status === '正常') return 'good'
  if (status === '验证中') return 'warning'
  return 'danger'
}

function accountIssue(account) {
  const labels = { login: '登录', publish: '发布', dm: '私信', comments: '评论', verification: '回查' }
  const issues = Object.entries(account.capabilities || {})
    .filter(([key, value]) => key !== 'identity' && value && value.ok === false)
    .map(([key]) => labels[key] || key)
  return issues.length ? issues.join('、') + '受限' : ''
}

function syncTone(platform) {
  if (platform.running) return 'warning'
  if (platform.noAccounts) return 'muted'
  const accountError = (platform.accounts || []).some(account => (
    account.errorKind && !['ok', ''].includes(account.errorKind)
  ))
  if (platform.errorKind || accountError) return 'danger'
  if (platform.lastSuccessAt) return 'good'
  return platform.enabled ? 'warning' : 'muted'
}

function syncLabel(platform) {
  const tone = syncTone(platform)
  return ({ good: '正常', warning: platform.running ? '同步中' : '等待同步', danger: '异常', muted: '未登录' })[tone]
}

function syncDetail(platform) {
  const accountError = (platform.accounts || []).find(account => (
    account.errorKind && !['ok', ''].includes(account.errorKind)
  ))
  if (platform.noAccounts) return '添加账号后可开始同步'
  if (platform.running) return '正在读取最新消息'
  if (accountError?.lastError) return accountError.lastError
  if (platform.lastError) return platform.lastError
  if (platform.lastSuccessAt) return '最近成功 ' + shortDateTime(platform.lastSuccessAt)
  return platform.enabled ? '已开启，等待首次同步' : '同步已暂停'
}

function compactNumber(value) {
  const number = Number(value || 0)
  if (Math.abs(number) >= 100000000) return (number / 100000000).toFixed(number >= 1000000000 ? 0 : 1) + '亿'
  if (Math.abs(number) >= 10000) return (number / 10000).toFixed(number >= 100000 ? 0 : 1) + '万'
  return new Intl.NumberFormat('zh-CN').format(number)
}

function shortDateTime(value) {
  const text = String(value || '').replace('T', ' ')
  return text ? text.slice(5, 16) : ''
}

function shortDay(value) {
  return String(value || '').slice(5).replace('-', '/')
}

function barHeight(day) {
  if (!day.hasSnapshot) return 4
  return Math.max(4, Math.round((Number(day.plays || 0) / chartMax.value) * 100))
}

function openLead(lead) {
  router.push({ path: '/inbox', query: { view: 'messages', contact: lead.id, platform: lead.platform || '' } })
}

function avatarText(name) {
  return String(name || '访').trim().slice(0, 1).toUpperCase()
}

useLivePolling(() => load(true), { interval: 180000 })
onMounted(load)
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;

.today-page { padding-bottom: 32px; }
.title-line { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.page-kicker {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 0 9px;
  color: $primary-color-dark;
  border: 1px solid rgba($primary-color, 0.22);
  border-radius: 999px;
  background: $primary-color-soft;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.04em;
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 18px;
}
.kpi-card {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 15px 16px 13px;
  color: $text-primary;
  border: 1px solid $border-lighter;
  border-radius: 14px;
  background: $paper;
  text-align: left;
  cursor: pointer;
  transition: border-color $duration-base $ease-out, transform $duration-base $ease-out,
    box-shadow $duration-base $ease-out;
}
.kpi-card:hover {
  border-color: $border-base;
  box-shadow: $box-shadow-dark;
  transform: translateY(-1px);
}
.kpi-top { display: flex; align-items: center; justify-content: space-between; color: $text-secondary; font-size: 11px; }
.kpi-dot { width: 7px; height: 7px; border-radius: 50%; background: $ink-300; }
.kpi-card.is-good .kpi-dot { background: $success-color; }
.kpi-card.is-danger .kpi-dot { background: $danger-color; }
.kpi-card.is-warning .kpi-dot { background: $warning-color; }
.kpi-card.is-accent .kpi-dot { background: $primary-color; }
.kpi-number { overflow: hidden; color: $text-primary; font-family: ui-monospace, 'SFMono-Regular', Consolas, monospace; font-size: 27px; line-height: 1.05; text-overflow: ellipsis; white-space: nowrap; }
.kpi-number small { margin-left: 4px; color: $text-secondary; font-family: inherit; font-size: 10px; font-weight: 400; }
.kpi-detail { overflow: hidden; color: $text-secondary; font-size: 11px; line-height: 1.45; text-overflow: ellipsis; white-space: nowrap; }
.kpi-action { display: inline-flex; align-items: center; gap: 2px; margin-top: auto; color: $text-placeholder; font-size: 10px; }

.cockpit-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(320px, 0.75fr); gap: 16px; margin-bottom: 16px; }
.cockpit-grid--lower { grid-template-columns: minmax(0, 1.35fr) minmax(360px, 0.65fr); }
.cockpit-panel { min-width: 0; overflow: hidden; }
.panel-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 18px 20px 14px; }
.panel-heading h2 { color: $text-primary; font-size: 15px; font-weight: 600; }
.panel-heading p { margin-top: 4px; color: $text-secondary; font-size: 11px; line-height: 1.5; }
.panel-heading :deep(.el-button) { margin-top: -5px; font-size: 12px; }

.platform-strip { display: flex; gap: 7px; padding: 0 20px 14px; overflow-x: auto; }
.platform-pill { display: inline-flex; align-items: center; gap: 7px; flex: none; min-height: 28px; padding: 0 9px; border: 1px solid $border-lighter; border-radius: 7px; background: $bg-color-raised; color: $text-secondary; font-size: 11px; }
.platform-pill strong { color: $text-primary; font-family: ui-monospace, 'SFMono-Regular', Consolas, monospace; font-size: 11px; }
.account-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border-top: 1px solid $border-lighter; }
.account-card {
  min-width: 0;
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 14px 18px;
  color: inherit;
  border: 0;
  border-right: 1px solid $border-lighter;
  border-bottom: 1px solid $border-lighter;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.account-card:nth-child(2n) { border-right: 0; }
.account-card:hover { background: $bg-color-raised; }
.account-avatar { display: grid; place-items: center; width: 36px; height: 36px; color: #fff; border-radius: 10px; background: $ink-700; font-size: 14px; font-weight: 600; }
.account-copy { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.account-copy strong, .account-copy span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.account-copy strong { color: $text-primary; font-size: 13px; font-weight: 500; }
.account-copy span { color: $text-secondary; font-size: 10px; }
.account-state, .sync-state { display: inline-flex; align-items: center; min-height: 22px; padding: 0 7px; border-radius: 999px; font-size: 10px; white-space: nowrap; }
.account-state.is-good, .sync-state.is-good { color: $success-color; background: $success-color-soft; }
.account-state.is-warning, .sync-state.is-warning { color: $warning-color; background: $warning-color-soft; }
.account-state.is-danger, .sync-state.is-danger { color: $danger-color; background: $danger-color-soft; }
.sync-state.is-muted { color: $info-color; background: $ink-100; }
.account-issue { grid-column: 2 / -1; margin-top: -2px; color: $danger-color; font-size: 10px; }
.account-time { grid-column: 2 / -1; color: $text-placeholder; font-size: 9px; }
.panel-empty { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 140px; padding: 24px; border-top: 1px solid $border-lighter; }
.panel-empty strong { font-size: 13px; font-weight: 500; }
.panel-empty p { margin-top: 5px; color: $text-secondary; font-size: 11px; line-height: 1.6; }
.panel-empty--compact { min-height: 110px; }

.side-stack { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.side-stack .cockpit-panel { flex: 1; }
.publish-summary { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; padding: 2px 20px 12px; }
.rate-block { display: flex; flex-direction: column; gap: 3px; }
.rate-block strong { color: $text-primary; font-family: ui-monospace, 'SFMono-Regular', Consolas, monospace; font-size: 34px; line-height: 1; }
.rate-block strong small { margin-left: 2px; color: $text-secondary; font-size: 12px; }
.rate-block span, .publish-counts span { color: $text-secondary; font-size: 10px; }
.publish-counts { display: flex; gap: 16px; }
.publish-counts span { display: flex; flex-direction: column; gap: 3px; text-align: right; }
.publish-counts strong { color: $text-primary; font-family: ui-monospace, 'SFMono-Regular', Consolas, monospace; font-size: 17px; }
.progress-track { height: 5px; margin: 0 20px 14px; overflow: hidden; border-radius: 999px; background: $ink-100; }
.progress-track span { display: block; height: 100%; border-radius: inherit; background: $success-color; }
.flow-list { border-top: 1px solid $border-lighter; }
.flow-list button { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 38px; padding: 0 20px; color: $text-secondary; border: 0; border-bottom: 1px solid $border-lighter; background: transparent; font-size: 11px; text-align: left; cursor: pointer; }
.flow-list button:last-child { border-bottom: 0; }
.flow-list button:hover { background: $bg-color-raised; }
.flow-list strong { color: $text-primary; font-size: 13px; }
.flow-list button.is-danger strong { color: $danger-color; }

.compact-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-top: 1px solid $border-lighter; }
.compact-metrics div { display: flex; flex-direction: column; gap: 4px; padding: 13px 12px 15px; border-right: 1px solid $border-lighter; }
.compact-metrics div:last-child { border-right: 0; }
.compact-metrics strong { color: $text-primary; font-size: 20px; line-height: 1; }
.compact-metrics span { color: $text-secondary; font-size: 9px; white-space: nowrap; }

.trend-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin: 0 20px 14px; overflow: hidden; border: 1px solid $border-lighter; border-radius: 10px; background: $border-lighter; }
.trend-summary div { display: flex; flex-direction: column; gap: 5px; padding: 11px 12px; background: $bg-color-raised; }
.trend-summary span { color: $text-secondary; font-size: 9px; }
.trend-summary strong { color: $text-primary; font-size: 17px; }
.trend-chart { height: 176px; display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); align-items: end; gap: 8px; margin: 0 20px; padding: 8px 8px 0; border-bottom: 1px solid $border-lighter; }
.trend-day { height: 100%; min-width: 0; display: grid; grid-template-rows: 20px minmax(0, 1fr) 22px; align-items: end; text-align: center; }
.bar-value { overflow: hidden; color: $text-placeholder; font-size: 8px; text-overflow: ellipsis; white-space: nowrap; }
.bar-rail { height: 100%; display: flex; align-items: flex-end; justify-content: center; }
.bar-fill { width: min(24px, 70%); min-height: 3px; border-radius: 5px 5px 2px 2px; background: linear-gradient(180deg, rgba($primary-color, 0.78), $primary-color); transition: height $duration-base $ease-out; }
.bar-fill.is-missing { height: 4px !important; background: $ink-200; }
.bar-date { align-self: center; color: $text-secondary; font-size: 9px; }
.data-note { margin: 11px 20px 16px; color: $text-placeholder; font-size: 9px; line-height: 1.55; }

.sync-list { border-top: 1px solid $border-lighter; }
.sync-row { width: 100%; display: grid; grid-template-columns: 8px minmax(0, 1fr) auto auto; align-items: center; gap: 10px; min-height: 55px; padding: 9px 18px; color: inherit; border: 0; border-bottom: 1px solid $border-lighter; background: transparent; text-align: left; cursor: pointer; }
.sync-row:last-child { border-bottom: 0; }
.sync-row:hover { background: $bg-color-raised; }
.sync-dot { width: 7px; height: 7px; border-radius: 50%; background: $ink-300; }
.sync-dot.is-good { background: $success-color; }
.sync-dot.is-warning { background: $warning-color; }
.sync-dot.is-danger { background: $danger-color; }
.sync-dot.is-muted { background: $ink-300; }
.sync-copy { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.sync-copy strong, .sync-copy span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sync-copy strong { color: $text-primary; font-size: 12px; font-weight: 500; }
.sync-copy span { color: $text-secondary; font-size: 9px; }
.sync-next { align-self: end; color: $text-placeholder; font-size: 8px; }

.work-panel { overflow: hidden; margin-top: 16px; }
.work-heading, .section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.work-heading { padding: 18px 20px 12px; }
.work-heading h2, .section-heading h2 { color: $text-primary; font-size: 15px; font-weight: 600; }
.work-heading p { margin-top: 4px; color: $text-secondary; font-size: 10px; }
.work-heading > span { color: $text-secondary; font-size: 13px; }
.work-list { border-top: 1px solid $border-lighter; }
.work-row, .lead-row { width: 100%; display: flex; align-items: center; gap: 14px; padding: 14px 20px; color: inherit; border: 0; border-bottom: 1px solid $border-lighter; background: transparent; text-align: left; cursor: pointer; transition: background-color $duration-base $ease-out; }
.work-row:last-child, .lead-row:last-child { border-bottom: 0; }
.work-row:hover, .lead-row:hover { background: $bg-color-raised; }
.work-mark { width: 6px; height: 28px; border-radius: 2px; background: $ink-300; }
.work-mark.is-danger { background: $danger-color; }
.work-mark.is-attention { background: $warning-color; }
.work-copy, .lead-copy { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 4px; }
.work-copy strong, .lead-copy strong { color: $text-primary; font-size: 13px; font-weight: 500; }
.work-copy span, .lead-copy span { overflow: hidden; color: $text-secondary; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.work-count { min-width: 28px; color: $text-primary; font-size: 17px; text-align: right; }
.work-row .el-icon, .lead-row .el-icon { color: $text-placeholder; }
.today-empty { display: flex; align-items: center; gap: 16px; padding: 24px 20px 28px; border-top: 1px solid $border-lighter; }
.empty-line { width: 44px; height: 1px; background: $primary-color; }
.today-empty div { flex: 1; }
.today-empty strong { font-size: 13px; font-weight: 500; }
.today-empty p { margin-top: 4px; color: $text-secondary; font-size: 11px; }
.leads-panel { margin-top: 24px; }
.section-heading { padding: 0 4px 10px; }
.lead-list { overflow: hidden; border: 1px solid $border-lighter; border-radius: 14px; background: $paper; }
.lead-state { color: $text-secondary; font-size: 11px; white-space: nowrap; }

@media (max-width: 1280px) {
  .kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .cockpit-grid, .cockpit-grid--lower { grid-template-columns: 1fr; }
  .side-stack { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 820px) {
  .kpi-grid, .side-stack { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .account-grid { grid-template-columns: 1fr; }
  .account-card { border-right: 0; }
  .trend-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 620px) {
  .title-line { align-items: flex-start; flex-direction: column; gap: 8px; }
  .side-stack { grid-template-columns: 1fr; }
  .compact-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .compact-metrics div:nth-child(2) { border-right: 0; }
  .compact-metrics div:nth-child(-n+2) { border-bottom: 1px solid $border-lighter; }
  .work-row, .lead-row { padding: 13px 16px; }
  .work-heading { padding-inline: 16px; }
  .lead-state { display: none; }
  .today-empty, .panel-empty { align-items: flex-start; flex-wrap: wrap; }
  .sync-row { grid-template-columns: 8px minmax(0, 1fr) auto; }
  .sync-next { display: none; }
}
</style>
