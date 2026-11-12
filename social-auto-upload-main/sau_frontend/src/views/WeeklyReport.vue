<template>
  <div class="report-page">
    <el-alert
      v-if="hasNoAccounts"
      title="当前没有登录账号，无法生成运营周报"
      description="请先到「账号管理」添加并登录账号，发布数据同步后即可查看周报。"
      type="info"
      show-icon
      :closable="false"
      style="margin-bottom: 14px;"
    />
    <el-alert
      v-else-if="!deltaAvailable"
      title="本期增量暂不可用"
      description="缺少上一期的历史快照，累计值不能当作本期增量使用。下方「累计」数据仅为参考。"
      type="warning"
      show-icon
      :closable="false"
      style="margin-bottom: 14px;"
    />
    <el-alert
      v-else-if="!report.hasBaseline"
      title="本期增量按快照覆盖区间计算"
      :description="`缺少窗口外的基线快照，增量取快照覆盖区间 ${report.inWindow.from} 至 ${report.inWindow.to} 的差值，不足整 ${days} 天。`"
      type="warning"
      show-icon
      :closable="false"
      style="margin-bottom: 14px;"
    />
    <el-alert
      v-else-if="report.coverage && report.coverage.daysWithData < report.coverage.days"
      :title="`数据覆盖 ${report.coverage.daysWithData}/${report.coverage.days} 天`"
      :description="`缺少 ${report.coverage.missingDays.length} 天的快照，未回流的日子增量按 0 计，不是「没涨」。`"
      type="info"
      show-icon
      :closable="false"
      style="margin-bottom: 14px;"
    />
    <div class="page-head">
      <div>
        <h2>运营周报</h2>
        <p class="sub">
          主口径为「本期增量」（本期涨了多少）；累计值单独标注，仅作参考。
          <template v-if="report.snapshotAt">
            数据快照时间：{{ report.snapshotAt }}<template v-if="report.snapshotDate">（{{ report.snapshotDate }}）</template>
          </template>
          <template v-if="report.staleDays > 0">
            <span class="stale">· 距今 {{ report.staleDays }} 天未回流</span>
          </template>
        </p>
      </div>
      <div>
        <el-select v-model="days" style="width:120px" @change="load">
          <el-option label="近 7 天" :value="7" />
          <el-option label="近 14 天" :value="14" />
          <el-option label="近 30 天" :value="30" />
        </el-select>
        <el-button @click="load" :loading="loading">刷新</el-button>
      </div>
    </div>

    <div class="metric-grid">
      <div class="metric"><span>发布任务</span><strong>{{ report.jobs }}</strong><small>成功 {{ report.success }} / 失败 {{ report.failed }}</small></div>
      <div class="metric" :class="{ muted: !deltaAvailable }">
        <span>本期新增播放</span>
        <strong>{{ deltaAvailable ? formatNum(deltaPlays) : '—' }}</strong>
        <small>{{ deltaHint }}</small>
      </div>
      <div class="metric" :class="{ muted: !deltaAvailable }">
        <span>本期新增点赞</span>
        <strong>{{ deltaAvailable ? formatNum(deltaLikes) : '—' }}</strong>
        <small>互动 {{ interactionRate }}%</small>
      </div>
      <div class="metric"><span>累计播放（参考）</span><strong>{{ formatNum(report.totals.cumulative.plays) }}</strong><small>累计点赞 {{ formatNum(report.totals.cumulative.likes) }}</small></div>
    </div>

    <el-card shadow="never" style="margin-bottom:16px">
      <template #header>
        <div class="card-head">
          <span>每日播放趋势</span>
          <el-radio-group v-model="trendMetric" size="small">
            <el-radio-button value="plays">播放</el-radio-button>
            <el-radio-button value="likes">点赞</el-radio-button>
            <el-radio-button value="comments">评论</el-radio-button>
          </el-radio-group>
        </div>
      </template>
      <div v-if="!trendPoints.length" class="empty-hint">
        还没有每日快照。数据回流后会每天自动留一份快照，届时这里会显示趋势曲线。
      </div>
      <svg v-else class="trend" :viewBox="`0 0 ${trendWidth} ${trendHeight}`" preserveAspectRatio="none">
        <polyline :points="trendLine" fill="none" stroke="#409eff" stroke-width="2" />
        <circle
          v-for="(p, i) in trendPoints" :key="i"
          :cx="p.x" :cy="p.y" r="3" fill="#409eff"
        >
          <title>{{ p.label }}：{{ formatNum(p.value) }}{{ p.hasSnapshot ? '' : '（无快照）' }}</title>
        </circle>
      </svg>
      <div v-if="trendPoints.length" class="trend-axis">
        <span>{{ trendPoints[0].date }}</span>
        <span>{{ trendPoints[trendPoints.length - 1].date }}</span>
      </div>
    </el-card>

    <el-row :gutter="16">
      <el-col :xs="24" :lg="11">
        <el-card shadow="never">
          <template #header><span>平台表现</span></template>
          <el-table :data="report.platforms" size="small" stripe empty-text="还没有平台数据，同步平台数据后这里会显示各平台表现">
            <el-table-column prop="name" label="平台" width="90" />
            <el-table-column prop="contents" label="作品" width="70" />
            <el-table-column label="新增播放" min-width="90">
              <template #default="s">
                <template v-if="s.row.hasBaseline !== false">{{ formatNum(s.row.delta.plays) }}</template>
                <template v-else-if="s.row.inWindowHasBaseline">{{ formatNum(s.row.inWindowDelta.plays) }}</template>
                <template v-else>—</template>
              </template>
            </el-table-column>
            <el-table-column label="累计播放" min-width="90">
              <template #default="s">{{ formatNum(s.row.cumulative.plays) }}</template>
            </el-table-column>
          </el-table>
          <p v-if="report.inWindow && report.inWindow.hasBaseline" class="table-note">
            注：本期缺少窗口外的基线快照，「新增播放」按快照覆盖区间
            {{ report.inWindow.from }} 至 {{ report.inWindow.to }} 计算。
          </p>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="13">
        <el-card shadow="never">
          <template #header><span>表现最好的内容（累计）</span></template>
          <el-table :data="report.top_contents" size="small">
            <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
            <el-table-column prop="platform" label="平台" width="86" />
            <el-table-column label="播放" width="90"><template #default="s">{{ formatNum(s.row.plays) }}</template></el-table-column>
            <el-table-column label="点赞" width="80"><template #default="s">{{ formatNum(s.row.likes) }}</template></el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" style="margin-top:16px">
      <template #header>
        <div class="card-head"><span>周报文字（可直接复制）</span><div><el-button size="small" @click="copyText">复制</el-button><el-button size="small" type="primary" @click="exportCsv">导出 CSV</el-button></div></div>
      </template>
      <el-input v-model="report.text" type="textarea" :rows="12" />
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { http } from '@/utils/request'
import { useAccountStore } from '@/stores/account'

const accountStore = useAccountStore()
const hasNoAccounts = computed(() => !accountStore.accounts.length)
const days = ref(7)
const loading = ref(false)
const emptyTotals = {
  delta: { plays: 0, likes: 0, comments: 0, collects: 0, shares: 0, followers: 0 },
  cumulative: { plays: 0, likes: 0, comments: 0, collects: 0, shares: 0, followers: 0 },
  publishedScope: { contents: 0, plays: 0, likes: 0, comments: 0 },
}
const report = ref({
  jobs: 0, success: 0, failed: 0,
  totals: emptyTotals,
  scope: '', hasBaseline: false, snapshotAt: '',
  snapshotDate: '', staleDays: 0,
  inWindow: { from: '', to: '', hasBaseline: false, plays: 0, likes: 0, comments: 0 },
  coverage: { days: 0, daysWithData: 0, missingDays: [], coverage: 0 },
  platforms: [], top_contents: [], text: '',
})

const interactionRate = computed(() => {
  const p = Number(deltaPlays.value || 0)
  if (!p) return '0.0'
  const likes = Number(deltaLikes.value || 0)
  const comments = report.value.hasBaseline
    ? Number(report.value.totals?.delta?.comments || 0)
    : Number(report.value.inWindow?.comments || 0)
  return (((likes + comments) / p) * 100).toFixed(1)
})

// ---- 增量口径 ----
// 主口径是整窗口增量；主口径缺基线时，退到"窗口内首末快照差值"——
// 它覆盖的天数短一些，但仍是真实增量。两者都拿不到才显示"—"。
const deltaAvailable = computed(() => {
  if (report.value.hasBaseline) return true
  return !!report.value.inWindow?.hasBaseline
})

const deltaPlays = computed(() => {
  if (report.value.hasBaseline) return Number(report.value.totals?.delta?.plays || 0)
  return Number(report.value.inWindow?.plays || 0)
})

const deltaLikes = computed(() => {
  if (report.value.hasBaseline) return Number(report.value.totals?.delta?.likes || 0)
  return Number(report.value.inWindow?.likes || 0)
})

const deltaHint = computed(() => {
  if (report.value.hasBaseline) return `近 ${days.value} 天真实增量`
  if (report.value.inWindow?.hasBaseline) {
    return `快照覆盖 ${report.value.inWindow.from} 至 ${report.value.inWindow.to}`
  }
  return '缺少基线快照'
})

// ---- 每日趋势（内联 SVG，不引第三方图表库）----
const trendMetric = ref('plays')
const trendWidth = 720
const trendHeight = 160
const series = ref([])

const trendPoints = computed(() => {
  const rows = series.value || []
  if (!rows.length) return []
  const values = rows.map(r => Number(r[trendMetric.value] || 0))
  const max = Math.max(...values, 1)
  const step = rows.length > 1 ? trendWidth / (rows.length - 1) : 0
  return rows.map((r, i) => ({
    x: Number((i * step).toFixed(2)),
    y: Number((trendHeight - (values[i] / max) * (trendHeight - 20) - 10).toFixed(2)),
    value: values[i],
    date: r.date,
    label: r[trendMetric.value],
    hasSnapshot: r.hasSnapshot !== false,
  }))
})

const trendLine = computed(() => trendPoints.value.map(p => `${p.x},${p.y}`).join(' '))

function formatNum(n) {
  const v = Number(n || 0)
  if (v >= 10000) return (v / 10000).toFixed(1) + '万'
  return v.toLocaleString('zh-CN')
}

async function load() {
  if (hasNoAccounts.value) return
  loading.value = true
  try {
    const r = await http.get('/report/weekly', { days: days.value })
    const data = r.data || {}
    // 老结构兜底，避免接口未升级时前端崩掉。
    data.totals = data.totals?.delta ? data.totals : { ...emptyTotals, delta: data.totals || emptyTotals.delta }
    report.value = { ...report.value, ...data }
  } catch (e) { ElMessage.error('周报加载失败') }
  finally { loading.value = false }
  // 趋势单独拉：它来自 daily_metrics，和周报口径无关，失败不影响主流程。
  try {
    const s = await http.get('/api/v2/stats/daily', { days: days.value })
    series.value = s.data?.series?.series || []
  } catch (e) {
    series.value = []
  }
}

async function copyText() {
  await navigator.clipboard.writeText(report.value.text)
  ElMessage.success('周报已复制')
}

function exportCsv() {
  const t = report.value.totals
  const rows = [
    ['指标', '数值', '口径'],
    ['发布任务', report.value.jobs, '窗口内'],
    ['成功', report.value.success, '窗口内'],
    ['失败', report.value.failed, '窗口内'],
    ['新增播放', deltaAvailable.value ? deltaPlays.value : '', '本期增量'],
    ['新增点赞', deltaAvailable.value ? deltaLikes.value : '', '本期增量'],
    ['新增评论', deltaAvailable.value ? (report.value.hasBaseline ? t.delta.comments : report.value.inWindow.comments) : '', '本期增量'],
    ['累计播放', t.cumulative.plays, '终身累计（参考）'],
    ['累计点赞', t.cumulative.likes, '终身累计（参考）'],
    ['本期发布内容数', t.publishedScope.contents, '本期发布口径'],
    ['本期发布内容累计播放', t.publishedScope.plays, '本期发布口径'],
    ['快照覆盖天数', report.value.coverage.daysWithData, `共 ${report.value.coverage.days} 天`],
    ['快照日期', report.value.snapshotDate, ''],
    ['快照时间', report.value.snapshotAt, ''],
  ]
  const csv = '\ufeff' + rows.map(r => r.join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = '运营周报_' + new Date().toISOString().slice(0, 10) + '.csv'
  a.click()
}

onMounted(async () => {
  try {
    await accountStore.loadAccounts()
  } catch (error) {
    console.error('加载账号列表失败:', error)
  }
  load()
})
</script>

<style scoped>
.report-page { padding: 8px 4px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.page-head h2 { margin: 0 0 6px; }
.sub { color: #909399; font-size: 13px; margin: 0; }
.stale { color: #e6a23c; margin-left: 6px; }
.metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
.metric { background: #fff; border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 4px; }
.metric.muted { opacity: 0.6; }
.metric span { color: #606266; font-size: 13px; }
.metric strong { font-size: 26px; }
.metric small { color: #909399; font-size: 12px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.trend { width: 100%; height: 160px; }
.trend-axis { display: flex; justify-content: space-between; color: #909399; font-size: 12px; margin-top: 4px; }
.empty-hint { color: #909399; font-size: 13px; padding: 24px 0; text-align: center; }
.table-note { color: #909399; font-size: 12px; margin: 8px 0 0; line-height: 1.6; }
@media (max-width: 900px) { .metric-grid { grid-template-columns: repeat(2, 1fr); } }
</style>
