<template>
  <div class="ai-content">
    <div class="page-head">
      <div>
        <h2>AI 文案生成</h2>
        <p class="sub">选择行业，输入主题或产品信息，按平台风格生成标题、正文和话题。</p>
      </div>
      <el-tag :type="status.configured ? 'success' : 'danger'" effect="plain">
        {{ status.configured ? ('模型 ' + status.model) : 'AI 未配置' }}
      </el-tag>
    </div>

    <el-row :gutter="18">
      <el-col :xs="24" :lg="10">
        <el-card shadow="never">
          <template #header><span>生成条件</span></template>
          <el-form label-width="84px">
            <el-form-item label="行业">
              <el-select v-model="form.industry" style="width:100%">
                <el-option v-for="i in industries" :key="i" :label="i" :value="i" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="form.industry === '其他'" label="自定义行业">
              <el-input v-model="form.custom_industry" placeholder="例如：宠物服务、民宿、家政" />
            </el-form-item>
            <el-form-item label="发布平台">
              <el-select v-model="form.platform" style="width:100%">
                <el-option v-for="p in platforms" :key="p" :label="p" :value="p" />
              </el-select>
            </el-form-item>
            <el-form-item label="主题">
              <el-input v-model="form.topic" :placeholder="topicPlaceholder" />
            </el-form-item>
            <el-form-item :label="nameLabel">
              <el-input v-model="form.tour_name" :placeholder="namePlaceholder" />
            </el-form-item>
            <el-form-item label="目标人群">
              <el-input v-model="form.audience" placeholder="例如：25-35 岁女性、宝妈、上班族、本地家庭" />
            </el-form-item>
            <el-form-item :label="form.industry === '旅行社' ? '天数/价格' : '价格'">
              <el-input-number v-if="form.industry === '旅行社'" v-model="form.days" :min="1" :max="60" style="width:110px" />
              <el-input-number v-model="form.price" :min="0" style="width:140px;margin-left:10px" />
            </el-form-item>
            <el-form-item label="核心卖点">
              <el-input v-model="form.highlights" type="textarea" :rows="3" :placeholder="highlightsPlaceholder" />
            </el-form-item>
            <el-form-item label="语气">
              <el-select v-model="form.tone" style="width:100%">
                <el-option label="真实亲切" value="真实、亲切、像朋友推荐" />
                <el-option label="专业稳重" value="专业、稳重、信息清晰" />
                <el-option label="轻松活泼" value="轻松、活泼、适合年轻用户" />
                <el-option label="性价比导向" value="实在、突出性价比和费用透明" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="loading" @click="generate" style="width:100%">
                {{ loading ? '正在生成…' : '生成文案' }}
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="14">
        <el-card shadow="never" class="result-card">
          <template #header>
            <div class="result-head">
              <span>生成结果</span>
              <span v-if="result.industry" class="tip">{{ result.industry }} · {{ result.platform }}</span>
            </div>
          </template>
          <template v-if="result.title || result.body">
            <el-form label-width="58px">
              <el-form-item label="标题"><el-input v-model="result.title" /></el-form-item>
              <el-form-item label="正文"><el-input v-model="result.body" type="textarea" :rows="12" /></el-form-item>
              <el-form-item label="话题"><el-input v-model="tagsText" placeholder="多个话题用逗号分隔" /></el-form-item>
              <el-form-item>
                <el-button @click="copyAll">复制全文</el-button>
                <el-button type="success" @click="goPublish">带去发布中心</el-button>
                <el-button @click="generate" :loading="loading">重新生成</el-button>
              </el-form-item>
            </el-form>
            <div v-if="result.tip" class="result-tip">{{ result.tip }}</div>
          </template>
          <el-empty v-else description="填写条件后点击生成，结果会显示在这里" :image-size="80" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { http } from '@/utils/request'

const router = useRouter()
const platforms = ['小红书', '抖音', '视频号', '快手', '微博', '百家号']
const industries = ['旅行社', '餐饮美食', '美妆个护', '教育培训', '健身运动', '本地生活', '电商零售', '房产家居', '其他']
const status = ref({ configured: false, model: '', base_url: '' })
const loading = ref(false)
const form = ref({ industry: '旅行社', custom_industry: '', platform: '小红书', topic: '', tour_name: '', audience: '', days: 3, price: 0, highlights: '', tone: '真实、亲切、像朋友推荐' })
const result = ref({ title: '', body: '', tags: [], tip: '', platform: '', industry: '' })

const nameLabel = computed(() => form.value.industry === '旅行社' ? '线路名称' : '名称/产品')
const namePlaceholder = computed(() => form.value.industry === '旅行社' ? '例如：桂林阳朔5日纯玩团' : '例如：招牌菜、课程、产品名或门店名')
const topicPlaceholder = computed(() => form.value.industry === '旅行社' ? '例如：桂林秋季纯玩、亲子研学' : '例如：新品上市、到店体验、使用场景')
const highlightsPlaceholder = computed(() => form.value.industry === '旅行社' ? '例如：0购物、漓江竹筏、四钻住宿' : '例如：口味、成分、课程内容、服务项目或产品卖点')

const tagsText = computed({
  get: () => (result.value.tags || []).join('，'),
  set: (v) => { result.value.tags = v.split(/[,，\s]+/).map(t => t.trim().replace(/^#/, '')).filter(Boolean) }
})

async function loadStatus() {
  try {
    const r = await http.get('/ai/status')
    status.value = r.data
  } catch (e) { console.error(e) }
}

async function generate() {
  if (!form.value.topic && !form.value.tour_name) {
    ElMessage.warning('请先填写主题或名称')
    return
  }
  if (form.value.industry === '其他' && !form.value.custom_industry.trim()) {
    ElMessage.warning('请填写自定义行业')
    return
  }
  loading.value = true
  try {
    const payload = { ...form.value }
    const r = await http.post('/ai/generate', payload)
    result.value = r.data
    ElMessage.success('文案已生成')
  } catch (e) {
    ElMessage.error(e.message || '生成失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

async function copyAll() {
  const text = [result.value.title, '', result.value.body, '', (result.value.tags || []).map(t => '#' + t).join(' ')].join('\n')
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch (e) {
    ElMessage.error('复制失败，请手动选择文本')
  }
}

function goPublish() {
  router.push({
    path: '/publish-center',
    query: {
      title: result.value.title,
      body: result.value.body,
      tags: (result.value.tags || []).join(','),
      platform: result.value.platform
    }
  })
}

onMounted(loadStatus)
</script>

<style scoped>
.ai-content { padding: 8px 4px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.page-head h2 { margin: 0 0 6px; }
.sub { color: #909399; font-size: 13px; margin: 0; }
.result-card { min-height: 520px; }
.result-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.tip { color: #909399; font-size: 12px; font-weight: normal; }
.result-tip { color: #909399; font-size: 12px; background: #f5f7fa; border-radius: 6px; padding: 8px 10px; }
</style>
