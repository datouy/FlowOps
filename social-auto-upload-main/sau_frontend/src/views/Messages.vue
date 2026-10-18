<template>
  <div class="messages-page">
    <div class="page-head">
      <div>
        <h2>统一消息中心</h2>
        <p class="sub">聚合各平台评论与私信，优先处理高意向咨询，回复后可直接登记线索。</p>
      </div>
      <div>
        <el-button type="primary" @click="openAdd">新增消息</el-button>
        <el-button @click="importDialog=true">批量导入</el-button>
        <el-button @click="openCapture">浏览器采集</el-button>
      </div>
    </div>

    <div class="filters">
      <el-select v-model="filters.platform" placeholder="全部平台" clearable @change="load">
        <el-option v-for="p in platforms" :key="p" :label="p" :value="p" />
      </el-select>
      <el-select v-model="filters.status" placeholder="全部状态" clearable @change="load">
        <el-option label="待处理" value="new" />
        <el-option label="已回复" value="replied" />
        <el-option label="已忽略" value="ignored" />
      </el-select>
      <el-select v-model="filters.intent" placeholder="全部意向" clearable @change="load">
        <el-option label="高意向" value="high" />
        <el-option label="中意向" value="medium" />
        <el-option label="低意向" value="low" />
      </el-select>
      <el-button @click="load">刷新</el-button>
    </div>

    <el-table :data="messages" border stripe>
      <el-table-column prop="platform" label="平台" width="86" />
      <el-table-column prop="author" label="用户" width="110" show-overflow-tooltip />
      <el-table-column prop="content" label="留言内容" min-width="220" show-overflow-tooltip />
      <el-table-column label="意向" width="86">
        <template #default="s"><el-tag size="small" :type="intentType(s.row.intent)">{{ intentText(s.row.intent) }}</el-tag></template>
      </el-table-column>
      <el-table-column label="状态" width="86">
        <template #default="s"><el-tag size="small" :type="s.row.status==='new'?'warning':(s.row.status==='replied'?'success':'info')">{{ statusText(s.row.status) }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="created_at" label="时间" width="150" />
      <el-table-column label="操作" width="210">
        <template #default="s">
          <el-button size="small" type="primary" @click="openReply(s.row)">回复</el-button>
          <el-button size="small" @click="mark(s.row, 'ignored')">忽略</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-if="!messages.length" description="暂无消息，可手动新增或批量导入" />

    <el-dialog v-model="addDialog" title="新增消息" width="520px">
      <el-form label-width="76px">
        <el-form-item label="平台"><el-select v-model="form.platform" style="width:100%"><el-option v-for="p in platforms" :key="p" :label="p" :value="p" /></el-select></el-form-item>
        <el-form-item label="账号"><el-input v-model="form.account" /></el-form-item>
        <el-form-item label="用户"><el-input v-model="form.author" placeholder="昵称/手机号" /></el-form-item>
        <el-form-item label="内容"><el-input v-model="form.content" type="textarea" :rows="4" /></el-form-item>
        <el-form-item label="来源内容"><el-input v-model="form.source_content" placeholder="哪条视频/笔记" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="addDialog=false">取消</el-button><el-button type="primary" @click="saveMessage">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="importDialog" title="批量导入消息" width="560px">
      <p class="hint">每行一条，格式：平台|账号|用户|留言内容。例如：抖音|陆大头|小明|桂林五日游多少钱？</p>
      <el-input v-model="importText" type="textarea" :rows="8" placeholder="抖音|账号|用户|留言" />
      <template #footer><el-button @click="importDialog=false">取消</el-button><el-button type="primary" @click="doImport">导入</el-button></template>
    </el-dialog>

    <el-dialog v-model="replyDialog" title="回复消息" width="560px">
      <div class="quote">{{ current?.content }}</div>
      <el-select v-model="templateId" placeholder="使用快捷回复模板" clearable style="width:100%;margin-bottom:10px" @change="applyTemplate">
        <el-option v-for="t in templates" :key="t.id" :label="t.name" :value="t.id" />
      </el-select>
      <el-input v-model="replyText" type="textarea" :rows="5" placeholder="回复内容" />
      <template #footer>
        <el-button @click="genReply" :loading="generating">AI 生成回复</el-button>
        <el-button @click="replyDialog=false">取消</el-button>
        <el-button type="primary" @click="saveReply">标记已回复</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { http } from '@/utils/request'

const platforms = ['抖音', '快手', '视频号', '小红书', '微博', '百家号']
const messages = ref([])
const templates = ref([])
const filters = ref({ platform: '', status: '', intent: '' })
const addDialog = ref(false)
const importDialog = ref(false)
const replyDialog = ref(false)
const form = ref({ platform: '抖音', account: '', author: '', content: '', source_content: '' })
const importText = ref('')
const current = ref(null)
const replyText = ref('')
const templateId = ref(null)
const generating = ref(false)

const intentText = t => ({ high: '高意向', medium: '中意向', low: '低意向' }[t] || t)
const intentType = t => ({ high: 'danger', medium: 'warning', low: 'info' }[t] || 'info')
const statusText = s => ({ new: '待处理', replied: '已回复', ignored: '已忽略' }[s] || s)

async function load() {
  try {
    const params = {}
    Object.entries(filters.value).forEach(([k, v]) => { if (v) params[k] = v })
    const [m, t] = await Promise.all([http.get('/messages', params), http.get('/message-templates')])
    messages.value = m.data
    templates.value = t.data
  } catch (e) { console.error(e) }
}

function openAdd() {
  form.value = { platform: '抖音', account: '', author: '', content: '', source_content: '' }
  addDialog.value = true
}

async function saveMessage() {
  if (!form.value.content) return ElMessage.warning('请填写留言内容')
  await http.post('/messages', form.value)
  addDialog.value = false
  ElMessage.success('已保存')
  load()
}

async function doImport() {
  const items = importText.value.split('\n').map(line => line.trim()).filter(Boolean).map(line => {
    const [platform, account, author, ...rest] = line.split('|')
    return { platform, account, author, content: rest.join('|') || author }
  })
  if (!items.length) return ElMessage.warning('没有可导入的内容')
  const r = await http.post('/messages', { items })
  importDialog.value = false
  importText.value = ''
  ElMessage.success('已导入 ' + r.data.added + ' 条')
  load()
}

async function openCapture() {
  const platform = filters.value.platform || '抖音'
  try {
    const r = await http.post('/messages/open-capture', { platform, open: true })
    ElMessage.success(r.msg || '已打开平台页面')
  } catch (e) {
    ElMessage.error(e.message || '打开失败')
  }
}

function openReply(row) {
  current.value = row
  replyText.value = row.reply_text || ''
  templateId.value = null
  replyDialog.value = true
}

function applyTemplate(id) {
  const t = templates.value.find(x => x.id === id)
  if (t) replyText.value = t.content
}

async function genReply() {
  generating.value = true
  try {
    const r = await http.post('/messages/' + current.value.id + '/ai-reply')
    replyText.value = r.data.reply
  } catch (e) { ElMessage.error(e.message || '生成失败') }
  finally { generating.value = false }
}

async function saveReply() {
  await http.put('/messages/' + current.value.id, { status: 'replied', reply_text: replyText.value })
  replyDialog.value = false
  ElMessage.success('已标记为已回复')
  load()
}

async function mark(row, status) {
  await http.put('/messages/' + row.id, { status })
  load()
}

onMounted(load)
</script>

<style scoped>
.messages-page { padding: 8px 4px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
.page-head h2 { margin: 0 0 6px; }
.sub { color: #909399; font-size: 13px; margin: 0; }
.filters { display: flex; gap: 10px; margin-bottom: 14px; }
.quote { background: #f5f7fa; padding: 10px; border-radius: 6px; margin-bottom: 12px; }
.hint { color: #909399; font-size: 12px; margin: 0 0 10px; }
</style>
