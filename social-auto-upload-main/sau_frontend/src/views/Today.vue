<template>
  <div class="today-page">
    <header class="page-heading">
      <div class="page-heading__copy">
        <h1>今天</h1>
        <p>{{ dateLabel }}</p>
      </div>
      <div class="page-heading__actions">
        <el-button @click="load" :loading="loading">刷新</el-button>
        <el-button type="primary" @click="router.push('/content?view=compose')">新建内容</el-button>
      </div>
    </header>

    <section class="work-panel panel" aria-labelledby="work-title">
      <div class="work-heading">
        <h2 id="work-title">需要处理</h2>
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
          <span class="work-mark" :class="`is-${task.tone}`" aria-hidden="true" />
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
          <strong>今天没有待处理</strong>
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

const router = useRouter()
const accountStore = useAccountStore()
const loading = ref(false)
const todo = ref({ date: '', counts: {}, items: [], priority_leads: [] })

const dateLabel = computed(() => {
  const value = todo.value.date ? new Date(`${todo.value.date}T00:00:00`) : new Date()
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'long',
    day: 'numeric',
    weekday: 'long'
  }).format(value)
})

const priorityLeads = computed(() => (todo.value.priority_leads || []).slice(0, 5))
const abnormalAccounts = computed(() => accountStore.accounts.filter(account => account.status === '异常'))

const activeTasks = computed(() => {
  const counts = todo.value.counts || {}
  const tasks = []
  if (Number(counts.pending_review || 0)) {
    tasks.push({
      key: 'review',
      label: '等待审核',
      detail: '通过后进入发布队列',
      count: counts.pending_review,
      tone: 'attention',
      to: '/content?view=review'
    })
  }
  if (Number(counts.failed_jobs || 0)) {
    tasks.push({
      key: 'failed',
      label: '发布异常',
      detail: '查看失败原因并重试',
      count: counts.failed_jobs,
      tone: 'danger',
      to: '/insights?view=works'
    })
  }
  if (Number(counts.messages || 0)) {
    tasks.push({
      key: 'messages',
      label: '等待回复',
      detail: `高意向 ${counts.high_intent || 0}`,
      count: counts.messages,
      tone: 'attention',
      to: '/inbox?view=messages'
    })
  }
  if (Number(counts.scheduled_today || 0)) {
    tasks.push({
      key: 'scheduled',
      label: '今天定时发布',
      detail: '查看时间安排',
      count: counts.scheduled_today,
      tone: 'neutral',
      to: '/content?view=calendar'
    })
  }
  if (abnormalAccounts.value.length) {
    tasks.push({
      key: 'accounts',
      label: '账号异常',
      detail: abnormalAccounts.value.map(item => item.name).slice(0, 2).join('、'),
      count: abnormalAccounts.value.length,
      tone: 'danger',
      to: '/settings?view=accounts&filter=attention'
    })
  }
  return tasks
})

async function load() {
  loading.value = true
  try {
    const [todoRes] = await Promise.allSettled([
      http.get('/todo/summary'),
      accountStore.loadAccounts(true)
    ])
    if (todoRes.status === 'fulfilled') todo.value = todoRes.value.data || todo.value
  } finally {
    loading.value = false
  }
}

function openLead(lead) {
  router.push({ path: '/inbox', query: { view: 'messages', contact: lead.id, platform: lead.platform || '' } })
}

function avatarText(name) {
  return String(name || '访').trim().slice(0, 1).toUpperCase()
}

onMounted(load)
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;

.today-page { padding-bottom: 32px; }
.work-panel { overflow: hidden; }
.work-heading,
.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.work-heading { padding: 20px 24px 12px; }
.work-heading h2,
.section-heading h2 { color: $text-primary; font-size: 15px; font-weight: 500; }
.work-heading > span { color: $text-secondary; font-size: 13px; }
.work-list { border-top: 1px solid $border-lighter; }
.work-row,
.lead-row {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 16px 24px;
  color: inherit;
  border: 0;
  border-bottom: 1px solid $border-lighter;
  background: transparent;
  text-align: left;
  cursor: pointer;
  transition: background-color $duration-base $ease-out;
}
.work-row:last-child,
.lead-row:last-child { border-bottom: 0; }
.work-row:hover,
.lead-row:hover { background: $bg-color-raised; }
.work-mark { width: 6px; height: 28px; border-radius: 2px; background: $ink-300; }
.work-mark.is-danger { background: $danger-color; }
.work-mark.is-attention { background: $warning-color; }
.work-copy,
.lead-copy { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 4px; }
.work-copy strong,
.lead-copy strong { color: $text-primary; font-size: 14px; font-weight: 500; }
.work-copy span,
.lead-copy span { overflow: hidden; color: $text-secondary; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.work-count { min-width: 28px; color: $text-primary; font-size: 18px; text-align: right; }
.work-row .el-icon,
.lead-row .el-icon { color: $text-placeholder; }
.today-empty { display: flex; align-items: center; gap: 16px; padding: 28px 24px 32px; border-top: 1px solid $border-lighter; }
.empty-line { width: 44px; height: 1px; background: $primary-color; }
.today-empty div { flex: 1; }
.today-empty strong { font-weight: 500; }
.today-empty p { margin-top: 4px; color: $text-secondary; font-size: 13px; }
.leads-panel { margin-top: 28px; }
.section-heading { padding: 0 4px 10px; }
.lead-list { overflow: hidden; border: 1px solid $border-lighter; border-radius: 14px; background: $paper; }
.lead-state { color: $text-secondary; font-size: 12px; white-space: nowrap; }
@media (max-width: 720px) {
  .work-row, .lead-row { padding: 14px 16px; }
  .work-heading { padding-inline: 16px; }
  .lead-state { display: none; }
  .today-empty { align-items: flex-start; flex-wrap: wrap; }
}
</style>
