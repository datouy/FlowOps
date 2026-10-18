<template>
  <div class="review-page">
    <div class="page-head">
      <h2>审核中心</h2>
      <p class="sub">发布前需要主管确认的内容会出现在这里，通过后自动进入发布队列。</p>
    </div>
    <div class="filters">
      <el-radio-group v-model="status" @change="load">
        <el-radio-button label="pending_review">待审核</el-radio-button>
        <el-radio-button label="pending">已通过/排队中</el-radio-button>
        <el-radio-button label="rejected">已驳回</el-radio-button>
      </el-radio-group>
      <el-button @click="load">刷新</el-button>
    </div>
    <el-table :data="jobs" border stripe>
      <el-table-column prop="id" label="#" width="60" />
      <el-table-column prop="created_at" label="提交时间" width="156" />
      <el-table-column label="平台" width="86">
        <template #default="s"><el-tag size="small" :type="platformTag(s.row.type)">{{ platformName(s.row.type) }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
      <el-table-column prop="tags" label="话题" min-width="120" show-overflow-tooltip />
      <el-table-column prop="file_list" label="素材" min-width="140" show-overflow-tooltip />
      <el-table-column label="操作" width="170">
        <template #default="s">
          <template v-if="status==='pending_review'">
            <el-button size="small" type="success" @click="approve(s.row)">通过</el-button>
            <el-button size="small" type="danger" @click="reject(s.row)">驳回</el-button>
          </template>
          <span v-else class="muted">{{ statusText(s.row.status) }}</span>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-if="!jobs.length" description="当前没有内容" />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { http } from '@/utils/request'

const status = ref('pending_review')
const jobs = ref([])
const names = {1:'小红书',2:'视频号',3:'抖音',4:'快手',5:'微博',6:'百家号'}
const tags = {1:'info',2:'warning',3:'danger',4:'success',5:'danger',6:'primary'}
const platformName = t => names[t] || ('类型' + t)
const platformTag = t => tags[t] || 'info'
const statusText = s => ({pending_review:'待审核',pending:'已通过',rejected:'已驳回',running:'发布中',success:'已完成',failed:'失败'}[s] || s)

async function load() {
  try {
    const r = await http.get('/review/pending', { status: status.value })
    jobs.value = r.data
  } catch (e) { console.error(e) }
}

async function approve(row) {
  await http.post('/review/' + row.id + '/approve')
  ElMessage.success('已通过，任务已进入发布队列')
  load()
}

async function reject(row) {
  await http.post('/review/' + row.id + '/reject')
  ElMessage.success('已驳回')
  load()
}

onMounted(load)
</script>

<style scoped>
.review-page { padding: 8px 4px; }
.page-head { margin-bottom: 14px; }
.page-head h2 { margin: 0 0 6px; }
.sub { color: #909399; font-size: 13px; margin: 0; }
.filters { display: flex; gap: 10px; margin-bottom: 14px; }
.muted { color: #909399; font-size: 13px; }
</style>
