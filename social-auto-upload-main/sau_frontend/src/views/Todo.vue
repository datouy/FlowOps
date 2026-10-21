<template>
  <div class="todo-page">
    <div class="page-head">
      <div>
        <h2>今日运营待办</h2>
        <p class="sub">{{ todo.date }} · 一屏看完今天需要处理的事情</p>
      </div>
      <el-button @click="load" :loading="loading">刷新</el-button>
    </div>

    <div class="metric-grid">
      <div class="metric" @click="go('/messages')">
        <span>待回复私信/留言</span><strong>{{ todo.counts.messages || 0 }}</strong>
        <small>高意向 {{ todo.counts.high_intent || 0 }} · 建议优先跟进 {{ todo.counts.priority_leads || 0 }}</small>
      </div>
      <div class="metric" @click="go('/publish-records')">
        <span>失败发布任务</span><strong class="danger">{{ todo.counts.failed_jobs || 0 }}</strong>
        <small>到发布记录查看原因</small>
      </div>
      <div class="metric" @click="go('/review')">
        <span>待审核内容</span><strong>{{ todo.counts.pending_review || 0 }}</strong>
        <small>审核通过后自动入队</small>
      </div>
      <div class="metric" @click="go('/publish-calendar')">
        <span>今日定时发布</span><strong>{{ todo.counts.scheduled_today || 0 }}</strong>
        <small>查看发布日历</small>
      </div>
    </div>

    <el-card shadow="never" class="priority-card">
      <template #header>
        <div class="card-title">
          <span>今日优先跟进</span>
          <span class="card-sub">按意向、未回复、留资完整度和最近咨询时间排序</span>
        </div>
      </template>
      <el-table
        v-if="todo.priority_leads && todo.priority_leads.length"
        :data="todo.priority_leads"
        class="priority-table"
        @row-click="openLead"
      >
        <el-table-column label="优先级" width="100">
          <template #default="scope">
            <el-tag :type="priorityTag(scope.row.priority)" effect="dark" size="small">
              {{ scope.row.priority_label }}
            </el-tag>
            <span class="score">{{ scope.row.lead_score }}</span>
          </template>
        </el-table-column>
        <el-table-column label="客户" width="150">
          <template #default="scope">
            <div class="lead-name">{{ scope.row.nickname || '匿名用户' }}</div>
            <div class="muted">{{ scope.row.platform }} · {{ scope.row.account || '未填写账号' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="需求信息" min-width="210">
          <template #default="scope">
            <div>{{ leadInfo(scope.row) }}</div>
            <div class="muted last-message">{{ scope.row.last_content || '暂无最近消息' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="推荐动作" min-width="250">
          <template #default="scope">
            <div>{{ scope.row.recommended_action }}</div>
            <div class="reason-row">
              <el-tag v-for="reason in scope.row.priority_reasons" :key="reason" size="small" effect="plain">
                {{ reason }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="" width="94">
          <template #default="scope">
            <el-button type="primary" link @click.stop="openLead(scope.row)">去跟进</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂时没有需要优先跟进的线索" :image-size="70" />
    </el-card>

    <el-card shadow="never">
      <template #header><span>待办清单</span></template>
      <div v-if="todo.items && todo.items.length" class="todo-list">
        <div v-for="(item, i) in todo.items" :key="i" class="todo-item" :class="item.level">
          <el-icon><Bell /></el-icon>
          <span>{{ item.text }}</span>
        </div>
      </div>
      <el-empty v-else description="今天没有紧急待办，可以准备新内容了" :image-size="80" />
    </el-card>

    <el-card shadow="never" style="margin-top:16px">
      <template #header><span>快捷入口</span></template>
      <div class="quick-links">
        <el-button @click="go('/ai-content')">生成文案</el-button>
        <el-button @click="go('/content-pack')">生成多平台内容包</el-button>
        <el-button @click="go('/publish-center')">去发布</el-button>
        <el-button @click="go('/weekly-report')">查看周报</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Bell } from '@element-plus/icons-vue'
import { http } from '@/utils/request'

const router = useRouter()
const loading = ref(false)
const todo = ref({ date: '', counts: {}, items: [] })

async function load() {
  loading.value = true
  try {
    const r = await http.get('/todo/summary')
    todo.value = r.data
  } catch (e) { console.error(e) }
  finally { loading.value = false }
}

function go(path) { router.push(path) }
function priorityTag(priority) {
  return { urgent: 'danger', high: 'warning', normal: 'info' }[priority] || 'info'
}
function leadInfo(lead) {
  const parts = []
  if (lead.travel_date) parts.push(lead.travel_date)
  if (lead.traveler_count) parts.push(`${lead.traveler_count} 人`)
  if (lead.budget) parts.push(lead.budget)
  if (lead.origin) parts.push(`从${lead.origin}出发`)
  if (lead.stage_label) parts.push(lead.stage_label)
  return parts.join(' · ') || '出行需求待确认'
}
function openLead(lead) {
  router.push({ path: '/messages', query: { contact: lead.id, platform: lead.platform || '' } })
}
onMounted(load)
</script>

<style scoped>
.todo-page { padding: 8px 4px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.page-head h2 { margin: 0 0 6px; }
.sub { color: #909399; font-size: 13px; margin: 0; }
.metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
.metric { background: #fff; border-radius: 8px; padding: 16px; cursor: pointer; display: flex; flex-direction: column; gap: 4px; }
.metric span { color: #606266; font-size: 13px; }
.metric strong { font-size: 26px; }
.metric strong.danger { color: #f56c6c; }
.metric small { color: #909399; font-size: 12px; }
.priority-card { margin-bottom: 16px; }
.card-title { display: flex; align-items: baseline; gap: 12px; }
.card-sub { color: #909399; font-size: 12px; }
.priority-table :deep(.el-table__row) { cursor: pointer; }
.score { color: #909399; font-size: 11px; margin-left: 6px; }
.lead-name { font-weight: 600; color: #303133; }
.muted { color: #909399; font-size: 12px; margin-top: 3px; }
.last-message { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.reason-row { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 6px; }
.todo-list { display: flex; flex-direction: column; gap: 8px; }
.todo-item { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: 6px; background: #f5f7fa; }
.todo-item.high { background: #fef0f0; color: #f56c6c; }
.quick-links { display: flex; gap: 10px; flex-wrap: wrap; }
@media (max-width: 900px) { .metric-grid { grid-template-columns: repeat(2, 1fr); } }
</style>
