<template>
  <div class="publish-calendar">
    <div class="cal-header">
      <h2>发布日历</h2>
      <div class="cal-actions">
        <el-button @click="shiftWeek(-1)">上一周</el-button>
        <el-button @click="shiftWeek(0)">本周</el-button>
        <el-button @click="shiftWeek(1)">下一周</el-button>
        <el-button @click="loadAll" :loading="loading">刷新</el-button>
      </div>
    </div>
    <el-alert type="info" :closable="false" style="margin-bottom:14px"
      title="显示已排期的定时发布任务（在发布中心开启「定时发布」后自动出现在这里）。当天格子显示计划时间与内容标题。" />
    <el-card v-if="suggest.platforms.length" shadow="never" style="margin-bottom:14px">
      <template #header><span>智能排期建议</span></template>
      <div class="suggest-grid">
        <div v-for="s in suggest.platforms" :key="s.platform" class="suggest-item">
          <strong>{{ s.name }}</strong>
          <span>{{ s.times.join(' / ') }}</span>
          <small>{{ s.source === 'history' ? '基于历史表现' : '默认推荐时段' }}</small>
        </div>
      </div>
      <el-alert v-if="suggest.conflicts.length" type="warning" :closable="false" style="margin-top:10px"
        :title="'检测到 ' + suggest.conflicts.length + ' 个时间点存在多条任务撞车，请调整排期'" />
    </el-card>
    <div class="week-grid">
      <div v-for="day in weekDays" :key="day.date" class="day-cell" :class="{ today: day.isToday }">
        <div class="day-title">
          <span class="day-name">{{ day.name }}</span>
          <span class="day-date">{{ day.date.slice(5) }}</span>
        </div>
        <div class="day-body">
          <div v-if="byDate[day.date] && byDate[day.date].length" class="event-list">
            <div v-for="ev in byDate[day.date]" :key="ev.job_id + ev.scheduled_at" class="event"
                 :class="{ done: ev.status === 'success', failed: ev.status === 'failed' }">
              <div class="ev-time">{{ ev.scheduled_at.slice(11) }}</div>
              <div class="ev-title">{{ ev.title }}</div>
              <div v-if="ev.account" class="ev-account">{{ accountShort(ev.account) }}</div>
              <div class="ev-meta">
                <el-tag size="small" :type="platformTag(ev.type)">{{ platformName(ev.type) }}</el-tag>
                <span class="ev-status">{{ statusText(ev.status) }}</span>
              </div>
            </div>
          </div>
          <div v-else class="empty-day">—</div>
        </div>
      </div>
    </div>
    <div v-if="scheduledItems.length === 0" class="hint">
      <el-empty description="当前没有排期的定时发布任务。去发布中心上传素材并开启「定时发布」即可。" />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { http } from '@/utils/request'

const loading = ref(false)
const scheduledItems = ref([])
const suggest = ref({ platforms: [], conflicts: [] })
const weekOffset = ref(0)
let timer = null

const platformNames = {1:'小红书',2:'视频号',3:'抖音',4:'快手',5:'微博',6:'百家号'}
const platformTags = {1:'info',2:'warning',3:'danger',4:'success',5:'danger',6:'primary'}
const platformName = t => platformNames[t] || ('类型' + t)
const platformTag = t => platformTags[t] || 'info'

function statusText(s) {
  return { pending: '已排期', running: '发布中', success: '已完成', failed: '失败' }[s] || s
}

function accountShort(acc) {
  const base = String(acc).replace(/\.json$/i, '')
  return base.length > 12 ? base.slice(0, 8) + '…' : base
}

const weekDays = computed(() => {
  const names = ['周一','周二','周三','周四','周五','周六','周日']
  const now = new Date()
  const monday = new Date(now)
  const dow = (now.getDay() + 6) % 7
  monday.setDate(now.getDate() - dow + weekOffset.value * 7)
  const days = []
  for (let i = 0; i < 7; i++) {
    const d = new Date(monday)
    d.setDate(monday.getDate() + i)
    const iso = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    days.push({ date: iso, name: names[i], isToday: iso === todayIso() })
  }
  return days
})

function todayIso() {
  const n = new Date()
  return n.getFullYear() + '-' + String(n.getMonth()+1).padStart(2,'0') + '-' + String(n.getDate()).padStart(2,'0')
}

function shiftWeek(n) {
  weekOffset.value = n === 0 ? 0 : weekOffset.value + n
}

const byDate = computed(() => {
  const map = {}
  for (const ev of scheduledItems.value) {
    if (!map[ev.date]) map[ev.date] = []
    map[ev.date].push(ev)
  }
  return map
})

async function loadAll() {
  loading.value = true
  try {
    const [r, s] = await Promise.all([http.get('/schedule/week'), http.get('/schedule/suggest')])
    scheduledItems.value = r.data
    suggest.value = s.data
  } catch(e) { console.error(e) }
  finally { loading.value = false }
}

onMounted(() => {
  loadAll()
  timer = setInterval(loadAll, 30000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.publish-calendar { padding: 8px 4px; }
.cal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
.cal-actions { display: flex; gap: 8px; }
.week-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; }
.suggest-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.suggest-item { background: #f5f7fa; border-radius: 6px; padding: 10px; display: flex; flex-direction: column; gap: 4px; }
.suggest-item small { color: #909399; }
.day-cell { background: #fff; border-radius: 8px; min-height: 220px; display: flex; flex-direction: column; overflow: hidden; }
.day-cell.today { outline: 2px solid #409eff; }
.day-title { background: #f5f7fa; padding: 8px 10px; display: flex; justify-content: space-between; font-size: 13px; }
.day-cell.today .day-title { background: #ecf5ff; color: #409eff; font-weight: 600; }
.day-body { padding: 6px; flex: 1; overflow-y: auto; }
.event { background: #f4f8ff; border-left: 3px solid #409eff; border-radius: 4px; padding: 6px; margin-bottom: 6px; font-size: 12px; }
.event.done { border-left-color: #67c23a; background: #f0f9eb; }
.event.failed { border-left-color: #f56c6c; background: #fef0f0; }
.ev-time { font-weight: 700; color: #409eff; }
.ev-title { margin: 4px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ev-meta { display: flex; align-items: center; gap: 6px; }
.ev-status { color: #909399; }
.ev-account { color: #67c23a; font-size: 11px; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty-day { color: #c0c4cc; text-align: center; padding-top: 30px; }
.hint { margin-top: 16px; }
</style>
