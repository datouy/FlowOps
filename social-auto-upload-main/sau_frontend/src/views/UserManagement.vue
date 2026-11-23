<template>
  <section class="user-management">
    <header class="section-heading">
      <div>
        <h2>团队与权限</h2>
        <p>每个成员使用独立账号；禁用账号、修改角色或密码会立即撤销现有会话。</p>
      </div>
      <div class="heading-actions">
        <el-button :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" @click="openCreate">新增成员</el-button>
      </div>
    </header>

    <el-alert
      v-if="!authConfig.personalAuthEnabled"
      type="warning"
      :closable="false"
      show-icon
      title="实名登录尚未启用"
      description="可先创建成员；部署时设置 YUNCE_PERSONAL_AUTH=1 并重启 Web 服务后，成员才能使用个人密码登录。"
    />

    <div class="summary-grid">
      <article><span>有效成员</span><strong>{{ authConfig.activeUsers || 0 }}</strong></article>
      <article><span>有效管理员</span><strong>{{ authConfig.activeAdmins || 0 }}</strong></article>
      <article><span>在线会话</span><strong>{{ authConfig.liveSessions || 0 }}</strong></article>
      <article><span>会话有效期</span><strong>{{ authConfig.sessionTtlHours || 12 }}h</strong></article>
      <article><span>双因素用户</span><strong>{{ authConfig.mfaEnabledUsers || 0 }}</strong></article>
      <article><span>MFA 管理员</span><strong>{{ authConfig.mfaEnabledAdmins || 0 }}</strong></article>
      <article><span>锁定账号</span><strong>{{ authConfig.lockedAccounts || 0 }}</strong></article>
    </div>

    <el-table :data="users" v-loading="loading" stripe>
      <el-table-column label="成员" min-width="180">
        <template #default="{ row }">
          <div class="member-cell">
            <strong>{{ row.displayName || row.username }}</strong>
            <span>{{ row.username }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="角色" width="110">
        <template #default="{ row }">
          <el-tag :type="roleType(row.role)" effect="plain">{{ roleLabels[row.role] || row.role }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="账号范围" min-width="220">
        <template #default="{ row }">
          <span v-if="row.role === 'admin'" class="muted">全部账号</span>
          <span v-else-if="row.scope?.length" class="scope-list">{{ row.scope.join('、') }}</span>
          <span v-else class="danger-text">未配置，访问将被拒绝</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="105">
        <template #default="{ row }">
          <el-tag :type="row.disabled ? 'info' : 'success'" effect="plain">
            {{ row.disabled ? '已禁用' : '有效' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="首次改密" width="105">
        <template #default="{ row }">{{ row.mustChangePassword ? '待完成' : '已完成' }}</template>
      </el-table-column>
      <el-table-column label="双因素" width="95">
        <template #default="{ row }">
          <el-tag :type="row.mfaEnabled ? 'success' : 'info'" effect="plain">
            {{ row.mfaEnabled ? '已启用' : '未启用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="lastLoginAt" label="最近登录" width="170" />
      <el-table-column label="操作" width="390" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link @click="resetPassword(row)">重置密码</el-button>
          <el-button link @click="resetMfa(row)">重置双因素</el-button>
          <el-button link @click="unlockLogin(row)">解除锁定</el-button>
          <el-button link @click="revokeSessions(row)">踢下线</el-button>
          <el-button link :type="row.disabled ? 'success' : 'danger'" @click="toggleDisabled(row)">
            {{ row.disabled ? '启用' : '禁用' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑成员' : '新增成员'" width="min(560px, calc(100vw - 32px))">
      <el-form label-position="top">
        <el-form-item label="登录名">
          <el-input v-model="form.username" :disabled="Boolean(editingId)" placeholder="例如 zhangsan 或 employee01" />
        </el-form-item>
        <el-form-item label="显示名称">
          <el-input v-model="form.displayName" placeholder="团队成员姓名" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option label="剪辑：内容与素材" value="cutter" />
            <el-option label="审核：仅审核决定" value="reviewer" />
            <el-option label="运营：发布与消息" value="operator" />
            <el-option label="管理员：账号、团队与审计" value="admin" />
          </el-select>
        </el-form-item>
        <el-form-item label="账号范围">
          <el-input
            v-model="form.scopeText"
            type="textarea"
            :rows="3"
            :disabled="form.role === 'admin'"
            placeholder="填写账号 ID、账号名或 Cookie 文件名，每行一个；非管理员必填"
          />
        </el-form-item>
        <template v-if="!editingId">
          <el-form-item label="初始密码">
            <el-input v-model="form.password" type="password" show-password autocomplete="new-password" placeholder="至少 12 个字符" />
          </el-form-item>
          <el-checkbox v-model="form.mustChangePassword">首次登录必须修改密码</el-checkbox>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { userApi } from '@/api/user'

const loading = ref(false)
const saving = ref(false)
const users = ref([])
const dialogVisible = ref(false)
const editingId = ref(null)
const authConfig = ref({})
const form = ref({
  username: '',
  displayName: '',
  role: 'operator',
  scopeText: '',
  password: '',
  mustChangePassword: true
})

const roleLabels = { cutter: '剪辑', reviewer: '审核', operator: '运营', admin: '管理员', viewer: '只读' }
const roleTypes = { cutter: 'warning', reviewer: 'success', operator: 'primary', admin: 'danger', viewer: 'info' }
const roleType = role => roleTypes[role] || 'info'

function resetForm() {
  form.value = {
    username: '',
    displayName: '',
    role: 'operator',
    scopeText: '',
    password: '',
    mustChangePassword: true
  }
}

async function load() {
  loading.value = true
  try {
    const [configResponse, usersResponse] = await Promise.all([
      userApi.getAuthStatus(),
      userApi.listUsers()
    ])
    authConfig.value = configResponse.data || {}
    users.value = usersResponse.data || []
  } finally {
    loading.value = false
  }
}

function openCreate() {
  resetForm()
  editingId.value = null
  dialogVisible.value = true
}

function openEdit(row) {
  editingId.value = row.id
  form.value = {
    username: row.username,
    displayName: row.displayName || '',
    role: row.role,
    scopeText: (row.scope || []).join('\n'),
    password: '',
    mustChangePassword: Boolean(row.mustChangePassword)
  }
  dialogVisible.value = true
}

function normalizedScope() {
  return String(form.value.scopeText || '')
    .split(/[\n,]/)
    .map(item => item.trim())
    .filter(Boolean)
}

async function save() {
  saving.value = true
  try {
    const scope = form.value.role === 'admin' ? [] : normalizedScope()
    if (form.value.role !== 'admin' && !scope.length) {
      ElMessage.error('非管理员必须配置至少一个账号范围')
      return
    }
    if (!editingId.value) {
      await userApi.createUser({
        username: form.value.username.trim(),
        displayName: form.value.displayName.trim(),
        role: form.value.role,
        scope,
        password: form.value.password,
        mustChangePassword: form.value.mustChangePassword
      })
    } else {
      await userApi.updateUser(editingId.value, {
        displayName: form.value.displayName.trim(),
        role: form.value.role,
        scope
      })
    }
    ElMessage.success('成员信息已保存')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function toggleDisabled(row) {
  try {
    await ElMessageBox.confirm(
      row.disabled ? `确定启用 ${row.username}？` : `禁用后 ${row.username} 的现有会话会立即失效。`,
      row.disabled ? '启用成员' : '禁用成员',
      { type: row.disabled ? 'info' : 'warning' }
    )
    await userApi.updateUser(row.id, { disabled: !row.disabled })
    ElMessage.success(row.disabled ? '成员已启用' : '成员已禁用')
    await load()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') throw error
  }
}

async function resetPassword(row) {
  try {
    const { value } = await ElMessageBox.prompt(
      `为 ${row.username} 设置临时密码。成员下次登录后必须修改。`,
      '重置密码',
      {
        inputType: 'password',
        inputPlaceholder: '至少 12 个字符',
        inputValidator: value => String(value || '').length >= 12 || '密码至少 12 个字符'
      }
    )
    await userApi.resetPassword(row.id, value)
    ElMessage.success('密码已重置，现有会话已撤销')
    await load()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') throw error
  }
}

async function revokeSessions(row) {
  try {
    await ElMessageBox.confirm(`撤销 ${row.username} 的所有登录会话？`, '强制下线', { type: 'warning' })
    const response = await userApi.revokeSessions(row.id)
    ElMessage.success(`已撤销 ${response.data?.revoked || 0} 个会话`)
    await load()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') throw error
  }
}

async function resetMfa(row) {
  try {
    await ElMessageBox.confirm(
      `重置 ${row.username} 的双因素认证？现有会话会被撤销，成员需要重新绑定认证器。`,
      '重置双因素认证',
      { type: 'warning' }
    )
    await userApi.resetMfa(row.id)
    ElMessage.success('双因素认证已重置')
    await load()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') throw error
  }
}

async function unlockLogin(row) {
  try {
    const response = await userApi.unlockLogin(row.id)
    ElMessage.success(response.data?.unlocked ? '账号登录锁定已解除' : '账号当前没有持久锁定')
    await load()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') throw error
  }
}

onMounted(load)
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;

.user-management { display: flex; flex-direction: column; gap: 18px; }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.section-heading h2 { margin: 0; font-size: 20px; font-weight: 600; }
.section-heading p { margin: 7px 0 0; color: $text-secondary; font-size: 13px; line-height: 1.6; }
.heading-actions { display: flex; gap: 8px; flex: none; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; }
.summary-grid article { padding: 16px; border: 1px solid $border-lighter; border-radius: 12px; background: $paper; }
.summary-grid span { color: $text-secondary; font-size: 12px; }
.summary-grid strong { display: block; margin-top: 8px; font-size: 24px; font-weight: 600; }
.member-cell { display: flex; flex-direction: column; gap: 3px; }
.member-cell span, .muted { color: $text-secondary; font-size: 12px; }
.scope-list { word-break: break-all; }
.danger-text { color: var(--el-color-danger); font-size: 12px; }
@media (max-width: 900px) {
  .section-heading { flex-direction: column; }
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
