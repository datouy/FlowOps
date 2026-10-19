<template>
  <div class="ai-content">
    <div class="page-head">
      <div>
        <h2>AI 文案生成</h2>
        <p class="sub">适用于产品、服务、门店、课程或活动。填写名称、受众和卖点，即可按平台风格生成内容。</p>
      </div>
      <el-tag :type="status.configured ? 'success' : 'danger'" effect="plain">
        {{ status.configured ? ('模型 ' + status.model) : 'AI 未配置' }}
      </el-tag>
    </div>

    <el-row :gutter="18">
      <el-col :xs="24" :lg="10">
        <el-card shadow="never">
          <template #header><span>生成条件</span></template>
          <el-form label-width="96px">
            <el-form-item label="行业/类型">
              <el-select v-model="form.industry" style="width:100%">
                <el-option v-for="item in industries" :key="item" :label="item" :value="item" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="form.industry === '其他'" label="自定义行业">
              <el-input v-model="form.custom_industry" placeholder="例如：宠物服务、软件、家居、教育" />
            </el-form-item>
            <el-form-item label="发布平台">
              <el-select v-model="form.platform" style="width:100%">
                <el-option v-for="platform in platforms" :key="platform" :label="platform" :value="platform" />
              </el-select>
            </el-form-item>
            <el-form-item label="产品/服务名称">
              <el-input v-model="form.business_name" placeholder="例如：新品名称、课程名称、服务套餐" />
            </el-form-item>
            <el-form-item label="主题">
              <el-input v-model="form.topic" placeholder="例如：新品上市、使用场景、活动推广、品牌介绍" />
            </el-form-item>
            <el-form-item label="目标人群">
              <el-input v-model="form.audience" placeholder="例如：年轻上班族、新手父母、企业客户" />
            </el-form-item>
            <el-form-item label="规格/周期">
              <el-input v-model="form.specification" placeholder="选填，例如：标准版、3个月、单次服务" />
            </el-form-item>
            <el-form-item label="价格/定位">
              <el-input v-model="form.price" placeholder="选填，例如：299元、面议、中高端" />
            </el-form-item>
            <el-form-item label="核心卖点">
              <el-input v-model="form.highlights" type="textarea" :rows="3" placeholder="填写真实功能、服务内容、使用体验或差异化价值" />
            </el-form-item>
            <el-form-item label="语气">
              <el-select v-model="form.tone" style="width:100%">
                <el-option label="真实亲切" value="真实、亲切、像朋友推荐" />
                <el-option label="专业稳重" value="专业、稳重、信息清晰" />
                <el-option label="轻松活泼" value="轻松、活泼、适合年轻用户" />
                <el-option label="简洁直接" value="简洁、直接、重点突出" />
                <el-option label="高性价比" value="实在、突出价格透明和性价比" />
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
const industries = ['通用', '餐饮美食', '美妆个护', '教育培训', '健身运动', '本地生活', '电商零售', '房产家居', '旅行社', '其他']
const status = ref({ configured: false, model: '', base_url: '' })
const loading = ref(false)
const form = ref({
  industry: '通用',
  custom_industry: '',
  platform: '小红书',
  business_name: '',
  topic: '',
  audience: '',
  specification: '',
  price: '',
  highlights: '',
  tone: '真实、亲切、像朋友推荐'
})
const result = ref({ title: '', body: '', tags: [], tip: '', platform: '', industry: '' })

const tagsText = computed({
  get: () => (result.value.tags || []).join('，'),
  set: value => {
    result.value.tags = String(value || '').split(/[,，\s]+/).map(item => item.trim().replace(/^#/, '')).filter(Boolean)
  }
})

async function loadStatus() {
  try {
    const response = await http.get('/ai/status')
    status.value = response.data
  } catch (error) {
    console.error(error)
  }
}

async function generate() {
  if (!form.value.topic && !form.value.business_name) {
    return ElMessage.warning('请先填写主题或产品/服务名称')
  }
  if (form.value.industry === '其他' && !form.value.custom_industry.trim()) {
    return ElMessage.warning('请填写自定义行业')
  }
  loading.value = true
  try {
    const response = await http.post('/ai/generate', form.value)
    result.value = response.data
    ElMessage.success('文案已生成')
  } catch (error) {
    ElMessage.error(error.message || '生成失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

async function copyAll() {
  const text = [result.value.title, '', result.value.body, '', (result.value.tags || []).map(tag => '#' + tag).join(' ')].join('\n')
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch (error) {
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
