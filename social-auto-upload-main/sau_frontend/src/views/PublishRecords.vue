<template>
  <div class="publish-records">
    <el-alert
      v-if="hasNoAccounts"
      title="当前没有登录账号，无法查看发布数据"
      description="请先到「账号管理」添加并登录账号，发布数据同步后即可在这里查看。"
      type="info"
      show-icon
      :closable="false"
      style="margin-bottom: 14px;"
    />
    <el-alert v-if="expiredAccounts.length" type="warning" :closable="false" class="account-alert">
      <template #title>
        <span>以下账号登录已过期，请到「账号管理」重新扫码：{{ expiredAccounts.join('、') }}</span>
      </template>
    </el-alert>

    <div class="toolbar">
      <el-radio-group v-model="view">
        <el-radio-button label="overview">数据看板</el-radio-button>
        <el-radio-button label="records">发布记录</el-radio-button>
        <el-radio-button label="jobs">任务队列</el-radio-button>
        <el-radio-button label="unmatched">待关联</el-radio-button>
      </el-radio-group>
      <div class="toolbar-actions">
        <el-tag size="small" :type="notify.configured ? 'success' : 'info'">{{ notify.configured ? '异常通知已配置' : '未配置异常通知' }}</el-tag>
        <el-button v-if="notify.configured" size="small" @click="testNotify">测试通知</el-button>
        <span v-if="lastFetchedAt" class="sync-time">上次同步 {{ formatTime(lastFetchedAt) }}</span>
        <el-button size="small" @click="triggerPatrol" :loading="patrolling">检查账号</el-button>
        <el-button size="small" @click="loadAll">刷新</el-button>
        <el-button size="small" type="primary" @click="triggerSync" :loading="syncing">
          {{ syncing ? '正在同步数据' : '立即同步数据' }}
        </el-button>
      </div>
    </div>

    <template v-if="view === 'overview'">
      <div class="metric-grid">
        <div class="metric-card">
          <span class="metric-label">总播放量</span>
          <strong>{{ formatNum(totals.plays) }}</strong>
          <small>{{ contentCount }} 条已回填作品</small>
        </div>
        <div class="metric-card">
          <span class="metric-label">总点赞</span>
          <strong>{{ formatNum(totals.likes) }}</strong>
          <small>互动率 {{ engagementRate }}%</small>
        </div>
        <div class="metric-card">
          <span class="metric-label">总评论</span>
          <strong>{{ formatNum(totals.comments) }}</strong>
          <small>收藏 {{ formatNum(totals.collects) }}</small>
        </div>
        <div class="metric-card">
          <span class="metric-label">涨粉</span>
          <strong>{{ formatNum(totals.followers) }}</strong>
          <small>按平台后台统计</small>
        </div>
      </div>

      <el-row :gutter="16">
        <el-col :xs="24" :lg="11">
          <el-card shadow="never" class="panel-card">
            <template #header><span>平台表现</span></template>
            <el-table :data="summary.platform_totals" size="small" stripe>
              <el-table-column label="平台" width="90">
                <template #default="s">
                  <el-tag size="small" :type="platformTag(s.row.platform)">{{ platformName(s.row.platform) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="contents" label="作品" width="64" />
              <el-table-column label="播放" min-width="90">
                <template #default="s">{{ formatNum(s.row.plays) }}</template>
              </el-table-column>
              <el-table-column label="点赞" min-width="70">
                <template #default="s">{{ formatNum(s.row.likes) }}</template>
              </el-table-column>
              <el-table-column label="涨粉" width="70">
                <template #default="s">{{ formatNum(s.row.followers) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
        <el-col :xs="24" :lg="13">
          <el-card shadow="never" class="panel-card">
            <template #header><span>账号表现</span></template>
            <el-table :data="summary.account_totals" size="small" stripe>
              <el-table-column prop="account" label="账号" min-width="110" show-overflow-tooltip />
              <el-table-column label="平台" width="86">
                <template #default="s">{{ s.row.name }}</template>
              </el-table-column>
              <el-table-column label="播放" min-width="90">
                <template #default="s">{{ formatNum(s.row.plays) }}</template>
              </el-table-column>
              <el-table-column label="互动" min-width="80">
                <template #default="s">{{ formatNum(s.row.likes + s.row.comments + s.row.collects) }}</template>
              </el-table-column>
              <el-table-column label="涨粉" width="70">
                <template #default="s">{{ formatNum(s.row.followers) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
      </el-row>

      <el-card v-if="summary.topic_totals?.length" shadow="never" class="panel-card">
        <template #header><span>内容话题表现</span></template>
        <el-table :data="summary.topic_totals" size="small" stripe>
          <el-table-column label="话题" min-width="150">
            <template #default="s"><el-tag size="small" effect="plain">#{{ s.row.topic }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="contents" label="作品" width="70" />
          <el-table-column label="平均播放" width="100">
            <template #default="s"><span class="strong-num">{{ formatNum(s.row.avg_plays) }}</span></template>
          </el-table-column>
          <el-table-column label="总播放" width="100">
            <template #default="s">{{ formatNum(s.row.plays) }}</template>
          </el-table-column>
          <el-table-column label="点赞" width="80">
            <template #default="s">{{ formatNum(s.row.likes) }}</template>
          </el-table-column>
          <el-table-column label="收藏" width="80">
            <template #default="s">{{ formatNum(s.row.collects) }}</template>
          </el-table-column>
          <el-table-column label="涨粉" width="80">
            <template #default="s">{{ formatNum(s.row.followers) }}</template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card shadow="never" class="panel-card top-card">
        <template #header><span>播放量 Top 作品</span></template>
        <el-table :data="summary.top_contents" size="small" stripe>
          <el-table-column type="index" label="#" width="52" />
          <el-table-column label="平台" width="84">
            <template #default="s"><el-tag size="small" :type="platformTag(s.row.platform)">{{ platformName(s.row.platform) }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="title" label="作品" min-width="220" show-overflow-tooltip />
          <el-table-column label="话题" min-width="150">
            <template #default="s">
              <el-tag v-for="topic in (s.row.topics || []).slice(0, 2)" :key="topic" size="small" effect="plain" class="topic-tag">#{{ topic }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="account" label="账号" width="100" show-overflow-tooltip />
          <el-table-column label="播放" width="88">
            <template #default="s"><span class="strong-num">{{ formatNum(s.row.plays) }}</span></template>
          </el-table-column>
          <el-table-column label="点赞" width="68">
            <template #default="s">{{ formatNum(s.row.likes) }}</template>
          </el-table-column>
          <el-table-column label="收藏" width="68">
            <template #default="s">{{ formatNum(s.row.collects) }}</template>
          </el-table-column>
          <el-table-column prop="published_at" label="发布时间" width="150" />
        </el-table>
        <el-empty v-if="!summary.top_contents.length" description="同步发布数据后，这里会展示作品表现" :image-size="70" />
      </el-card>
    </template>

    <el-table v-if="view === 'records'" :data="records" border stripe size="small">
      <el-table-column type="expand">
        <template #default="s">
          <div class="expand-stats">
            <el-empty v-if="!s.row.stats?.length" description="尚未匹配到平台数据" :image-size="50" />
            <el-table v-else :data="s.row.stats" size="small" border>
              <el-table-column prop="title" label="平台作品" min-width="220" show-overflow-tooltip />
              <el-table-column label="播放" width="90"><template #default="d">{{ formatNum(d.row.plays) }}</template></el-table-column>
              <el-table-column label="点赞" width="76"><template #default="d">{{ formatNum(d.row.likes) }}</template></el-table-column>
              <el-table-column label="评论" width="76"><template #default="d">{{ formatNum(d.row.comments) }}</template></el-table-column>
              <el-table-column label="收藏" width="76"><template #default="d">{{ formatNum(d.row.collects) }}</template></el-table-column>
              <el-table-column label="分享" width="76"><template #default="d">{{ formatNum(d.row.shares) }}</template></el-table-column>
              <el-table-column label="涨粉" width="76"><template #default="d">{{ formatNum(d.row.followers) }}</template></el-table-column>
              <el-table-column prop="fetched_at" label="同步时间" width="150" />
            </el-table>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="id" label="#" width="58" />
      <el-table-column prop="created_at" label="时间" width="152" />
      <el-table-column label="平台" width="84">
        <template #default="s"><el-tag size="small" :type="platformTag(s.row.type)">{{ platformName(s.row.type) }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="title" label="标题" min-width="170" show-overflow-tooltip />
      <el-table-column label="账号" min-width="100" show-overflow-tooltip>
        <template #default="s">{{ (s.row.account_names || []).join('、') || s.row.accounts }}</template>
      </el-table-column>
      <el-table-column label="播放" width="86" align="right">
        <template #default="s">{{ s.row.stats_summary?.plays ? formatNum(s.row.stats_summary.plays) : '—' }}</template>
      </el-table-column>
      <el-table-column label="点赞" width="72" align="right">
        <template #default="s">{{ s.row.stats_summary?.likes ? formatNum(s.row.stats_summary.likes) : '—' }}</template>
      </el-table-column>
      <el-table-column label="评论" width="72" align="right">
        <template #default="s">{{ s.row.stats_summary?.comments ? formatNum(s.row.stats_summary.comments) : '—' }}</template>
      </el-table-column>
      <el-table-column label="涨粉" width="72" align="right">
        <template #default="s">{{ s.row.stats_summary?.followers ? formatNum(s.row.stats_summary.followers) : '—' }}</template>
      </el-table-column>
      <el-table-column label="结果" width="84">
        <template #default="s">
          <el-tag size="small" :type="s.row.status==='success'?'success':(s.row.status==='running'?'warning':'danger')">
            {{ s.row.status==='success'?'成功':(s.row.status==='running'?'进行中':'失败') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="error" label="失败原因" min-width="160" show-overflow-tooltip />
    </el-table>

    <el-table v-if="view === 'jobs'" :data="jobs" row-key="id" border stripe size="small" @expand-change="onExpandJob">
      <el-table-column type="expand">
        <template #default="s">
          <div class="job-items">
            <div v-if="itemsLoading[s.row.id]" class="job-items-hint">正在加载子任务…</div>
            <template v-else>
              <div v-if="jobProgress[s.row.id]" class="job-items-hint">
                进度 {{ jobProgress[s.row.id].percent }}% ·
                成功 {{ jobProgress[s.row.id].success }} / 失败 {{ jobProgress[s.row.id].failed }} / 跳过 {{ jobProgress[s.row.id].skipped }}
              </div>
              <el-table v-if="(jobItems[s.row.id] || []).length" :data="jobItems[s.row.id]" size="small" border>
                <el-table-column prop="file" label="素材" min-width="150" show-overflow-tooltip />
                <el-table-column prop="account_name" label="账号" width="120" show-overflow-tooltip />
                <el-table-column label="状态" width="88">
                  <template #default="it">
                    <el-tag size="small" :type="itemTagType(it.row.status)">{{ itemStatusText(it.row.status) }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="attempts" label="尝试" width="60" />
                <el-table-column prop="remote_status" label="平台状态" width="96">
                  <template #default="it">{{ postStateText(it.row.remote_status) }}</template>
                </el-table-column>
                <el-table-column prop="remote_id" label="作品 ID" width="150" show-overflow-tooltip />
                <el-table-column label="作品操作" width="176" fixed="right">
                  <template #default="it">
                    <template v-if="it.row.remote_id && it.row.status === 'success' && s.row.type === 2">
                      <el-tag v-if="it.row.remote_delete_status === 'success'" size="small" type="info">已删除</el-tag>
                      <template v-else>
                        <el-button size="small" type="danger" plain @click="deleteRemotePost(s.row, it.row)">删除平台作品</el-button>
                        <el-button size="small" type="primary" plain @click="openReplacement(s.row, it.row)">更正并替换</el-button>
                      </template>
                      <div v-if="it.row.remote_delete_status === 'failed'" class="delete-error">
                        {{ it.row.remote_delete_error || '上次删除失败' }}
                      </div>
                    </template>
                    <span v-else class="muted-action">—</span>
                  </template>
                </el-table-column>
                <el-table-column prop="error" label="原因" min-width="220" show-overflow-tooltip />
              </el-table>
              <div v-else class="job-items-hint">暂无子任务（旧任务未展开，或任务还在排队）</div>
            </template>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="id" label="#" width="58" />
      <el-table-column prop="created_at" label="入队时间" width="152" />
      <el-table-column label="平台" width="84">
        <template #default="s"><el-tag size="small" :type="platformTag(s.row.type)">{{ platformName(s.row.type) }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="title" label="标题" min-width="170" show-overflow-tooltip />
      <el-table-column label="状态" width="92">
        <template #default="s">
          <el-tag size="small" :type="jobTagType(s.row.status)">
            {{ jobStatusText(s.row) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="retry_count" label="重试" width="58" />
      <el-table-column prop="result" label="结果/原因" min-width="190" show-overflow-tooltip />
      <el-table-column label="操作" width="130">
        <template #default="s">
          <el-button
            v-if="['pending','running','pending_review'].includes(s.row.status)"
            size="small" type="warning" @click="cancelJob(s.row)"
          >取消</el-button>
          <el-button
            v-else-if="['failed','partial_success'].includes(s.row.status)"
            size="small" type="primary" @click="retryFailedJob(s.row)"
          >重试失败项</el-button>
        </template>
      </el-table-column>
      <el-table-column prop="finished_at" label="完成时间" width="152" />
    </el-table>

    <template v-if="view === 'unmatched'">
      <el-alert type="info" :closable="false" class="account-alert"
        title="这些作品已从平台后台抓取，但未能自动匹配到发布记录。可手动选择对应的发布记录进行关联。" />
      <el-table :data="unmatched" border stripe size="small">
        <el-table-column prop="title" label="作品标题" min-width="200" show-overflow-tooltip />
        <el-table-column label="平台" width="86">
          <template #default="s"><el-tag size="small" :type="platformTag(s.row.platform)">{{ platformName(s.row.platform) }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="account" label="账号" width="110" show-overflow-tooltip />
        <el-table-column label="播放" width="80"><template #default="s">{{ formatNum(s.row.plays) }}</template></el-table-column>
        <el-table-column label="点赞" width="80"><template #default="s">{{ formatNum(s.row.likes) }}</template></el-table-column>
        <el-table-column prop="fetched_at" label="抓取时间" width="150" />
        <el-table-column label="操作" width="90">
          <template #default="s">
            <el-button size="small" type="primary" @click="openLink(s.row)">关联</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!unmatched.length" description="没有待关联的作品" :image-size="70" />
    </template>

    <el-dialog v-model="linkDialog" title="关联到发布记录" width="460px">
      <p class="link-hint">{{ linkStats?.title }}</p>
      <el-select v-model="linkRecordId" placeholder="选择发布记录" style="width:100%">
        <el-option v-for="r in records" :key="r.id" :label="'#' + r.id + ' ' + r.title" :value="r.id" />
      </el-select>
      <el-empty v-if="!records.length" description="暂无发布记录可关联" :image-size="60" />
      <template #footer>
        <el-button @click="linkDialog = false">取消</el-button>
        <el-button type="primary" :disabled="!linkRecordId" @click="linkContent">确认关联</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="replaceDialog" title="更正并替换视频号作品" width="560px" :close-on-click-modal="false">
      <el-alert
        type="warning" :closable="false" show-icon
        title="新作品发布并回查成功后，系统才会删除旧作品；新作品失败时旧作品保持不变。"
        style="margin-bottom:14px"
      />
      <el-form label-width="76px">
        <el-form-item label="原作品">
          <span class="replace-source">{{ replaceItem?.remote_id }}</span>
        </el-form-item>
        <el-form-item label="标题" required>
          <el-input v-model="replaceForm.title" maxlength="30" show-word-limit />
        </el-form-item>
        <el-form-item label="话题">
          <el-input v-model="replaceForm.tags" placeholder="多个话题用逗号分隔，最多 8 个" />
        </el-form-item>
        <el-form-item label="正文">
          <el-input v-model="replaceForm.desc" type="textarea" :rows="5" maxlength="1000" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="replaceDialog = false">取消</el-button>
        <el-button type="primary" :loading="replaceSubmitting" @click="submitReplacement">加入替换队列</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { http } from '@/utils/request'
import { useAccountStore } from '@/stores/account'

const view = ref('overview')
const accountStore = useAccountStore()
const hasNoAccounts = computed(() => !accountStore.accounts.length)
const jobs = ref([])
const unmatched = ref([])
const linkDialog = ref(false)
const linkStats = ref(null)
const linkRecordId = ref(null)
const replaceDialog = ref(false)
const replaceItem = ref(null)
const replaceSourceJob = ref(null)
const replaceSubmitting = ref(false)
const replaceForm = ref({ title: '', tags: '', desc: '' })
const notify = ref({ configured: false, type: 'auto' })
const accountStatus = ref([])
const patrolling = ref(false)
const summary = ref({
  records: [],
  platform_totals: [],
  account_totals: [],
  top_contents: [],
  topic_totals: [],
  last_fetched_at: null,
  sync: {},
})
let timer = null

const platformNames = {1:'小红书',2:'视频号',3:'抖音',4:'快手',5:'微博',6:'百家号'}
const platformTags = {1:'info',2:'warning',3:'danger',4:'success',5:'danger',6:'primary'}
const platformName = t => platformNames[t] || ('类型' + t)
const platformTag = t => platformTags[t] || 'info'
const records = computed(() => summary.value.records || [])
const rows = computed(() => summary.value.top_contents || [])
const contentCount = computed(() => (summary.value.platform_totals || []).reduce((sum, p) => sum + Number(p.contents || 0), 0))
const syncing = computed(() => Boolean(summary.value.sync?.running))
const lastFetchedAt = computed(() => summary.value.last_fetched_at)
const expiredAccounts = computed(() => accountStatus.value.filter(a => !a.alive).map(a => a.account_name || ('账号#' + a.account_id)))
const totals = computed(() => (summary.value.platform_totals || []).reduce((acc, p) => {
  for (const key of ['plays', 'likes', 'comments', 'collects', 'shares', 'followers']) acc[key] += Number(p[key] || 0)
  return acc
}, {plays:0, likes:0, comments:0, collects:0, shares:0, followers:0}))
const engagementRate = computed(() => {
  if (!totals.value.plays) return '0.0'
  return (((totals.value.likes + totals.value.comments + totals.value.collects) / totals.value.plays) * 100).toFixed(1)
})

function formatNum(n) {
  const value = Number(n || 0)
  if (value >= 100000000) return (value / 100000000).toFixed(1).replace(/\.0$/, '') + '亿'
  if (value >= 10000) return (value / 10000).toFixed(1).replace(/\.0$/, '') + '万'
  return value.toLocaleString('zh-CN')
}

function formatTime(value) {
  if (!value) return ''
  return String(value).replace('T', ' ').slice(0, 16)
}

function jobStatusText(row) {
  if (row.status === 'pending' && row.retry_count > 0) return '重试中'
  if (row.status === 'pending') return '排队中'
  if (row.status === 'running') return '发布中'
  if (row.status === 'success') return '成功'
  if (row.status === 'partial_success') return '部分成功'
  if (row.status === 'cancelled') return '已取消'
  if (row.status === 'rejected') return '已驳回'
  return '失败'
}

function jobTagType(status) {
  if (status === 'success') return 'success'
  if (status === 'partial_success') return 'warning'
  if (status === 'running' || status === 'pending') return 'warning'
  if (status === 'cancelled' || status === 'rejected') return 'info'
  return 'danger'
}

// ---- 子任务（账号级）明细 ----
const jobItems = ref({})
const jobProgress = ref({})
const itemsLoading = ref({})

function itemStatusText(status) {
  const map = {
    pending: '待发布', running: '发布中', success: '成功',
    failed: '失败', skipped: '已跳过', cancelled: '已取消',
  }
  return map[status] || status
}

function postStateText(state) {
  const map = {
    draft: '草稿', scheduled: '已预约', uploading: '上传中', transcoding: '转码中',
    review: '审核中', published: '已发表', failed: '审核失败', removed: '已下架',
    unknown: '状态未知', submitted: '已提交，待回查'
  }
  return map[state] || state || '—'
}

function itemTagType(status) {
  const map = { success: 'success', failed: 'danger', running: 'warning', skipped: 'info', cancelled: 'info' }
  return map[status] || 'info'
}

async function onExpandJob(row, expandedRows) {
  if (!Array.isArray(expandedRows) || !expandedRows.includes(row)) return
  if (jobItems.value[row.id]) return
  itemsLoading.value = { ...itemsLoading.value, [row.id]: true }
  try {
    const res = await http.get(`/publish/jobs/${row.id}/items`)
    jobItems.value = { ...jobItems.value, [row.id]: res.data.items || [] }
    jobProgress.value = { ...jobProgress.value, [row.id]: res.data.progress || null }
  } catch (error) {
    console.error('加载子任务失败:', error)
  } finally {
    itemsLoading.value = { ...itemsLoading.value, [row.id]: false }
  }
}

async function refreshJob(jobId) {
  const next = { ...jobItems.value }
  delete next[jobId]
  jobItems.value = next
  await loadAll()
}

async function cancelJob(row) {
  try {
    await http.post(`/publish/jobs/${row.id}/cancel`)
    ElMessage.success(`任务 #${row.id} 已取消（未开始的子任务不再发布）`)
    await refreshJob(row.id)
  } catch (error) {
    console.error('取消任务失败:', error)
  }
}

async function retryFailedJob(row) {
  try {
    const res = await http.post(`/publish/jobs/${row.id}/retry-failed`)
    ElMessage.success(res.msg || '已重新排队失败的子任务')
    await refreshJob(row.id)
  } catch (error) {
    console.error('重试失败子任务失败:', error)
  }
}

async function deleteRemotePost(job, item) {
  try {
    await ElMessageBox.confirm(
      `将永久删除视频号作品 ${item.remote_id}。\n账号：${item.account_name || '未知'}；任务：#${job.id}。\n删除后无法在平台恢复。`,
      '确认删除平台作品',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
        distinguishCancelAndClose: true,
      },
    )
  } catch (action) {
    if (action === 'cancel' || action === 'close') return
    return
  }
  try {
    const res = await http.delete(
      `/publish/jobs/${job.id}/items/${item.id}/remote-post`,
      null,
      { data: { remoteId: item.remote_id } },
    )
    ElMessage.success(res.msg || '平台作品已删除')
    await loadAll()
    const detail = await http.get(`/publish/jobs/${job.id}/items`)
    jobItems.value = { ...jobItems.value, [job.id]: detail.data.items || [] }
    jobProgress.value = { ...jobProgress.value, [job.id]: detail.data.progress || null }
  } catch (error) {
    ElMessage.error(error.message || '删除平台作品失败')
  }
}

function parseStoredJson(value, fallback) {
  try {
    return JSON.parse(value)
  } catch (_error) {
    return fallback
  }
}

function openReplacement(job, item) {
  const extra = parseStoredJson(job.extra || '{}', {})
  const tags = parseStoredJson(job.tags || '[]', [])
  replaceSourceJob.value = job
  replaceItem.value = item
  replaceForm.value = {
    title: job.title || '',
    tags: Array.isArray(tags) ? tags.join(', ') : String(tags || ''),
    desc: extra.desc || '',
  }
  replaceDialog.value = true
}

async function submitReplacement() {
  if (!replaceSourceJob.value || !replaceItem.value || replaceSubmitting.value) return
  const title = replaceForm.value.title.trim()
  const tags = replaceForm.value.tags
    .replace(/，/g, ',')
    .split(',')
    .map(item => item.trim().replace(/^#/, ''))
    .filter(Boolean)
  if (!title) {
    ElMessage.warning('更正后的标题不能为空')
    return
  }
  if (tags.length > 8) {
    ElMessage.warning('视频号话题最多 8 个')
    return
  }
  replaceSubmitting.value = true
  try {
    const res = await http.post(
      `/publish/jobs/${replaceSourceJob.value.id}/items/${replaceItem.value.id}/replacement`,
      { title, tags, desc: replaceForm.value.desc },
    )
    ElMessage.success(res.msg || '替换任务已排队')
    replaceDialog.value = false
    await loadAll()
  } catch (error) {
    ElMessage.error(error.message || '创建替换任务失败')
  } finally {
    replaceSubmitting.value = false
  }
}

async function loadAll() {
  if (hasNoAccounts.value) return
  try {
    const [stats, jobResult, accountResult, unmatchedResult, notifyResult] = await Promise.all([
      http.get('/stats/summary'),
      http.get('/jobs'),
      http.get('/account-status'),
      http.get('/stats/unmatched'),
      http.get('/notify/status'),
    ])
    summary.value = stats.data
    jobs.value = jobResult.data
    accountStatus.value = accountResult.data
    unmatched.value = unmatchedResult.data
    notify.value = notifyResult.data
  } catch (error) {
    console.error(error)
  }
}

async function triggerPatrol() {
  patrolling.value = true
  try {
    await http.post('/patrol/run')
    ElMessage.success('账号检查已开始')
    setTimeout(loadAll, 15000)
  } finally {
    patrolling.value = false
  }
}

async function testNotify() {
  try {
    await http.post('/notify/test')
    ElMessage.success('测试通知已发送')
  } catch (e) {
    ElMessage.error(e.message || '通知发送失败')
  }
}

function openLink(row) {
  linkStats.value = row
  linkRecordId.value = null
  linkDialog.value = true
}

async function linkContent() {
  if (!linkStats.value || !linkRecordId.value) return
  try {
    await http.post('/stats/link', { stats_id: linkStats.value.id, record_id: linkRecordId.value })
    ElMessage.success('关联成功')
    linkDialog.value = false
    await loadAll()
  } catch (error) {
    ElMessage.error(error.message || '关联失败')
  }
}

async function triggerSync() {
  if (syncing.value) return
  try {
    const response = await http.post('/stats/run')
    summary.value.sync = response.data || {running: true}
    ElMessage.success(response.msg || '数据同步已开始')
    const poll = setInterval(async () => {
      await loadAll()
      if (!summary.value.sync?.running) {
        clearInterval(poll)
        const result = summary.value.sync?.last_result
        if (result) {
          ElMessage.success('同步完成，更新 ' + (result.saved || 0) + ' 条作品数据')
        }
      }
    }, 4000)
  } catch (error) {
    console.error(error)
  }
}

onMounted(async () => {
  try {
    await accountStore.loadAccounts()
  } catch (error) {
    console.error('加载账号列表失败:', error)
  }
  loadAll()
  timer = setInterval(loadAll, 10000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.publish-records { padding: 8px 4px; }
.account-alert { margin-bottom: 16px; }
.toolbar { margin-bottom: 16px; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.toolbar-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sync-time { color: #909399; font-size: 12px; margin-right: 4px; }
.metric-grid { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 14px; margin-bottom: 16px; }
.metric-card { background: linear-gradient(135deg, #f7faff 0%, #fff 100%); border: 1px solid #e7edf5; border-radius: 10px; padding: 16px 18px; display: flex; flex-direction: column; gap: 5px; }
.metric-card strong { font-size: 28px; color: #1f2d3d; line-height: 1.2; }
.metric-card small { color: #909399; }
.metric-label { color: #606266; font-size: 13px; }
.panel-card { margin-bottom: 16px; }
.top-card :deep(.el-card__body) { padding-top: 8px; }
.strong-num { color: #f56c6c; font-weight: 600; }
.topic-tag { margin-right: 4px; }
.expand-stats { padding: 10px 28px 14px; }
.job-items { padding: 6px 14px 10px; }
.job-items-hint { font-size: 12px; color: #909399; margin-bottom: 6px; }
.delete-error { color: #f56c6c; font-size: 11px; line-height: 1.4; margin-top: 4px; }
.muted-action { color: #c0c4cc; }
.replace-source { color: #606266; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
@media (max-width: 900px) {
  .metric-grid { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
}
@media (max-width: 520px) {
  .metric-grid { grid-template-columns: 1fr; }
}
</style>
