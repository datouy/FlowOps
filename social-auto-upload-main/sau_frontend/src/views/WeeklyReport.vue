<template>
  <div class="report-page">
    <div class="page-head">
      <div>
        <h2>运营周报</h2>
        <p class="sub">自动汇总发布任务、内容数据和表现最好的作品，可复制或导出 CSV。</p>
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
      <div class="metric"><span>总播放</span><strong>{{ formatNum(report.totals.plays) }}</strong><small>{{ days }} 天累计</small></div>
      <div class="metric"><span>总点赞</span><strong>{{ formatNum(report.totals.likes) }}</strong><small>互动 {{ interactionRate }}%</small></div>
      <div class="metric"><span>总评论</span><strong>{{ formatNum(report.totals.comments) }}</strong><small>内容 {{ report.top_contents.length }} 条</small></div>
    </div>

    <el-row :gutter="16">
      <el-col :xs="24" :lg="11">
        <el-card shadow="never">
          <template #header><span>平台表现</span></template>
          <el-table :data="report.platforms" size="small" stripe>
            <el-table-column prop="name" label="平台" width="90" />
            <el-table-column prop="contents" label="作品" width="70" />
            <el-table-column label="播放" min-width="90"><template #default="s">{{ formatNum(s.row.plays) }}</template></el-table-column>
            <el-table-column label="点赞" min-width="80"><template #default="s">{{ formatNum(s.row.likes) }}</template></el-table-column>
          </el-table>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="13">
        <el-card shadow="never">
          <template #header><span>表现最好的内容</span></template>
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
import { ElMessage } from 'element-plus'
import { http } from '@/utils/request'

const days = ref(7)
const loading = ref(false)
const report = ref({ jobs: 0, success: 0, failed: 0, totals: { plays: 0, likes: 0, comments: 0 }, platforms: [], top_contents: [], text: '' })

const interactionRate = computed(() => {
  const p = Number(report.value.totals.plays || 0)
  if (!p) return '0.0'
  return (((Number(report.value.totals.likes) + Number(report.value.totals.comments)) / p) * 100).toFixed(1)
})

function formatNum(n) {
  const v = Number(n || 0)
  if (v >= 10000) return (v / 10000).toFixed(1) + '万'
  return v.toLocaleString('zh-CN')
}

async function load() {
  loading.value = true
  try {
    const r = await http.get('/report/weekly', { days: days.value })
    report.value = r.data
  } catch (e) { ElMessage.error('周报加载失败') }
  finally { loading.value = false }
}

async function copyText() {
  await navigator.clipboard.writeText(report.value.text)
  ElMessage.success('周报已复制')
}

function exportCsv() {
  const rows = [['指标', '数值'], ['发布任务', report.value.jobs], ['成功', report.value.success], ['失败', report.value.failed],
    ['播放', report.value.totals.plays], ['点赞', report.value.totals.likes], ['评论', report.value.totals.comments]]
  const csv = '\ufeff' + rows.map(r => r.join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = '运营周报_' + new Date().toISOString().slice(0, 10) + '.csv'
  a.click()
}

onMounted(load)
</script>

<style scoped>
.report-page { padding: 8px 4px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.page-head h2 { margin: 0 0 6px; }
.sub { color: #909399; font-size: 13px; margin: 0; }
.metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
.metric { background: #fff; border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 4px; }
.metric span { color: #606266; font-size: 13px; }
.metric strong { font-size: 26px; }
.metric small { color: #909399; font-size: 12px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
@media (max-width: 900px) { .metric-grid { grid-template-columns: repeat(2, 1fr); } }
</style>
