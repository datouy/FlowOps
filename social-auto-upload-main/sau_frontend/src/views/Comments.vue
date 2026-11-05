<template>
  <div class="comment-page">
    <div class="page-head">
      <div>
        <h2>评论运营</h2>
        <p class="sub">评论同步 · 回复 · 点赞 · 删除 · 置顶</p>
      </div>
      <div class="head-actions">
        <el-tag v-if="lastSyncText" type="info" effect="plain" round>{{ lastSyncText }}</el-tag>
        <el-button :loading="loading" @click="loadComments">刷新</el-button>
        <el-button :loading="syncing" type="primary" @click="runSync">同步评论</el-button>
      </div>
    </div>

    <el-alert
      v-if="!hasComments && !loading"
      title="还没有同步到评论"
      description="点击右上角「同步评论」，系统会打开各平台创作者中心的评论管理页，把评论抓回来。同步之后才能回复、点赞、删除和置顶。"
      type="info"
      show-icon
      :closable="false"
      style="margin-bottom: 14px"
    />

    <el-card shadow="never" class="panel-card">
      <div class="filter-row">
        <el-select v-model="filters.platform" clearable placeholder="全部平台" @change="loadComments">
          <el-option v-for="item in platforms" :key="item" :label="item" :value="item" />
        </el-select>
        <el-select v-model="filters.account" clearable filterable placeholder="全部账号" @change="loadComments">
          <el-option v-for="item in accounts" :key="item" :label="item" :value="item" />
        </el-select>
        <el-input
          v-model="filters.keyword"
          clearable
          placeholder="搜索评论内容 / 作者"
          style="max-width: 260px"
          @keyup.enter="loadComments"
        />
        <el-button @click="loadComments">查询</el-button>
      </div>

      <el-table :data="comments" v-loading="loading" style="width: 100%" stripe>
        <el-table-column label="平台" width="88">
          <template #default="s">
            <el-tag size="small" effect="plain">{{ s.row.platform || '—' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="账号" width="130" prop="account" show-overflow-tooltip />
        <el-table-column label="作品" width="150" show-overflow-tooltip>
          <template #default="s">{{ s.row.source_title || '—' }}</template>
        </el-table-column>
        <el-table-column label="作者" width="120" prop="author" show-overflow-tooltip />
        <el-table-column label="评论内容" min-width="240">
          <template #default="s">
            <div class="comment-content">{{ s.row.content || '—' }}</div>
            <div v-if="s.row.reply_text" class="comment-replied">
              已回复：{{ s.row.reply_text }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="互动" width="120">
          <template #default="s">
            <span class="metric">赞 {{ s.row.like_count || 0 }}</span>
            <span class="metric">回 {{ s.row.reply_count || 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="s">
            <el-tag v-if="s.row.pinned" size="small" type="warning" effect="dark">已置顶</el-tag>
            <el-tag v-else-if="s.row.liked" size="small" type="success" effect="plain">已点赞</el-tag>
            <el-tag v-else-if="s.row.replied_at" size="small" type="info" effect="plain">已回复</el-tag>
            <span v-else class="muted">未处理</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="s">
            <el-button link type="primary" size="small" @click="openReply(s.row)">回复</el-button>
            <el-button
              link
              :type="s.row.liked ? 'success' : 'default'"
              size="small"
              @click="toggleLike(s.row)"
            >
              {{ s.row.liked ? '取消赞' : '点赞' }}
            </el-button>
            <el-button
              link
              :type="s.row.pinned ? 'warning' : 'default'"
              size="small"
              @click="togglePin(s.row)"
            >
              {{ s.row.pinned ? '取消置顶' : '置顶' }}
            </el-button>
            <el-button link type="danger" size="small" @click="removeComment(s.row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无评论" :image-size="60" />
        </template>
      </el-table>
    </el-card>

    <el-dialog v-model="replyDialog" title="回复评论" width="520px">
      <div v-if="replyTarget" class="reply-preview">
        <div class="reply-preview-author">{{ replyTarget.author || '未知作者' }}</div>
        <div class="reply-preview-content">{{ replyTarget.content }}</div>
      </div>
      <el-input
        v-model="replyText"
        type="textarea"
        :rows="4"
        maxlength="500"
        show-word-limit
        placeholder="输入回复内容，将直接发送到平台评论区"
      />
      <template #footer>
        <el-button @click="replyDialog = false">取消</el-button>
        <el-button type="primary" :loading="replying" @click="submitReply">发送回复</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { commentApi } from '@/api/comment'

const comments = ref([])
const capabilities = ref([])
const loading = ref(false)
const syncing = ref(false)
const lastSyncText = ref('')

const filters = ref({
  platform: '',
  account: '',
  keyword: ''
})

const replyDialog = ref(false)
const replyTarget = ref(null)
const replyText = ref('')
const replying = ref(false)

const platforms = computed(() => {
  const names = new Set()
  comments.value.forEach(item => item.platform && names.add(item.platform))
  capabilities.value.forEach(item => item.supported && names.add(item.name))
  return Array.from(names)
})

const accounts = computed(() => {
  const names = new Set()
  comments.value.forEach(item => item.account && names.add(item.account))
  return Array.from(names)
})

const hasComments = computed(() => comments.value.length > 0)

function supportsAction(platform, action) {
  const item = capabilities.value.find(entry => entry.name === platform)
  if (!item) return true
  return (item.actions || []).includes(action)
}

async function loadComments() {
  loading.value = true
  try {
    const params = {}
    if (filters.value.platform) params.platform = filters.value.platform
    if (filters.value.account) params.account = filters.value.account
    const res = await commentApi.list(params)
    const data = res.data || {}
    let items = data.items || []
    const keyword = filters.value.keyword.trim()
    if (keyword) {
      items = items.filter(item =>
        (item.content || '').includes(keyword) ||
        (item.author || '').includes(keyword)
      )
    }
    comments.value = items
    if (Array.isArray(data.capabilities)) {
      capabilities.value = data.capabilities
    }
  } catch (error) {
    console.warn('加载评论失败:', error)
  } finally {
    loading.value = false
  }
}

async function loadStatus() {
  try {
    const res = await commentApi.status()
    const data = res.data || {}
    if (data.last_run_at) {
      lastSyncText.value = `上次同步 ${data.last_run_at}`
    }
  } catch (error) {
    console.warn('读取评论同步状态失败:', error)
  }
}

async function runSync() {
  syncing.value = true
  try {
    const res = await commentApi.sync()
    const count = res.data?.synced || 0
    ElMessage.success(`同步完成，新增 ${count} 条评论`)
    await loadComments()
    await loadStatus()
  } catch (error) {
    console.warn('评论同步失败:', error)
  } finally {
    syncing.value = false
  }
}

function openReply(row) {
  replyTarget.value = row
  replyText.value = row.reply_text || ''
  replyDialog.value = true
}

async function submitReply() {
  const text = replyText.value.trim()
  if (!text) {
    ElMessage.warning('回复内容不能为空')
    return
  }
  replying.value = true
  try {
    await commentApi.reply(replyTarget.value.id, text)
    ElMessage.success('回复已发送')
    replyDialog.value = false
    await loadComments()
  } catch (error) {
    console.warn('回复评论失败:', error)
  } finally {
    replying.value = false
  }
}

async function toggleLike(row) {
  try {
    await commentApi.like(row.id, !row.liked)
    ElMessage.success(row.liked ? '已取消点赞' : '已点赞')
    await loadComments()
  } catch (error) {
    console.warn('点赞操作失败:', error)
  }
}

async function togglePin(row) {
  try {
    await commentApi.pin(row.id, !row.pinned)
    ElMessage.success(row.pinned ? '已取消置顶' : '评论已置顶')
    await loadComments()
  } catch (error) {
    console.warn('置顶操作失败:', error)
  }
}

async function removeComment(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除「${(row.content || '').slice(0, 20)}」这条评论吗？删除后平台上不可恢复。`,
      '删除评论',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch (error) {
    return
  }
  try {
    await commentApi.remove(row.id)
    ElMessage.success('评论已删除')
    await loadComments()
  } catch (error) {
    console.warn('删除评论失败:', error)
  }
}

onMounted(async () => {
  await loadComments()
  await loadStatus()
})
</script>

<style scoped>
.comment-page { padding: 6px 2px 20px; }
.page-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; margin-bottom: 14px;
}
.page-head h2 { margin: 0 0 4px; font-size: 20px; }
.page-head .sub { margin: 0; color: #909399; font-size: 13px; }
.head-actions { display: flex; align-items: center; gap: 8px; }
.panel-card { border-radius: 8px; }
.filter-row { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.comment-content { line-height: 1.5; word-break: break-word; }
.comment-replied {
  margin-top: 4px; font-size: 12px; color: #67c23a;
  background: #f0f9eb; padding: 3px 6px; border-radius: 4px;
}
.metric { margin-right: 8px; font-size: 12px; color: #909399; }
.muted { color: #c0c4cc; font-size: 12px; }
.reply-preview {
  margin-bottom: 12px; padding: 10px 12px; background: #f5f7fa; border-radius: 6px;
}
.reply-preview-author { font-weight: 600; margin-bottom: 4px; font-size: 13px; }
.reply-preview-content { color: #606266; font-size: 13px; line-height: 1.5; }
</style>
