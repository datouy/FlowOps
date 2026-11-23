<template>
  <section class="account-security">
    <header class="section-heading">
      <div>
        <h2>账号安全</h2>
        <p>使用认证器应用增加第二道登录保护。验证码每 30 秒更新，同一验证码不能重复使用。</p>
      </div>
      <el-tag :type="store.session.mfaEnabled ? 'success' : 'info'" effect="plain">
        {{ store.session.mfaEnabled ? '双因素已启用' : '双因素未启用' }}
      </el-tag>
    </header>

    <el-alert
      v-if="store.session.source !== 'session'"
      type="info"
      :closable="false"
      show-icon
      title="个人双因素认证仅适用于实名会话"
      description="请使用个人账号登录。全局引导令牌和旧用户令牌应只用于迁移或应急。"
    />

    <template v-else>
      <el-alert
        v-if="store.session.mustChangePassword"
        type="warning"
        :closable="false"
        show-icon
        title="请先完成首次密码修改，再配置双因素认证"
      />

      <div v-else-if="!store.session.mfaEnabled" class="security-card">
        <template v-if="!setupInfo">
          <h3>启用认证器</h3>
          <p>支持 Google Authenticator、Microsoft Authenticator、1Password 等兼容 TOTP 的应用。</p>
          <label class="field-label" for="mfa-current-password">当前密码</label>
          <el-input
            id="mfa-current-password"
            v-model="currentPassword"
            type="password"
            show-password
            autocomplete="current-password"
          />
          <el-button type="primary" :loading="loading" class="card-action" @click="startSetup">
            生成绑定信息
          </el-button>
        </template>

        <template v-else>
          <h3>扫描或手动录入</h3>
          <div class="setup-grid">
            <img v-if="setupInfo.qrDataUrl" :src="setupInfo.qrDataUrl" alt="双因素认证二维码" />
            <div>
              <p>在认证器中添加账号 <strong>{{ store.session.user }}</strong>。</p>
              <div class="secret-value">{{ setupInfo.secret }}</div>
              <p class="muted">如果无法扫码，请手动输入上面的密钥，类型选择“基于时间”。</p>
            </div>
          </div>
          <label class="field-label" for="mfa-setup-code">输入认证器显示的 6 位验证码</label>
          <el-input
            id="mfa-setup-code"
            v-model="setupCode"
            inputmode="numeric"
            maxlength="6"
            autocomplete="one-time-code"
            @keyup.enter="confirmSetup"
          />
          <div class="button-row">
            <el-button @click="setupInfo = null">取消</el-button>
            <el-button type="primary" :loading="loading" @click="confirmSetup">确认启用</el-button>
          </div>
        </template>
      </div>

      <div v-else class="security-card">
        <h3>关闭双因素认证</h3>
        <p>关闭前需要验证当前密码和认证器中的动态验证码。关闭后其他会话会被撤销。</p>
        <label class="field-label" for="disable-password">当前密码</label>
        <el-input
          id="disable-password"
          v-model="currentPassword"
          type="password"
          show-password
          autocomplete="current-password"
        />
        <label class="field-label" for="disable-code">动态验证码</label>
        <el-input
          id="disable-code"
          v-model="code"
          inputmode="numeric"
          maxlength="6"
          autocomplete="one-time-code"
        />
        <el-button type="danger" plain :loading="loading" class="card-action" @click="disable">
          关闭双因素认证
        </el-button>
      </div>
    </template>
  </section>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { userApi } from '@/api/user'
import { useUserStore } from '@/stores/user'

const store = useUserStore()
const loading = ref(false)
const currentPassword = ref('')
const setupCode = ref('')
const code = ref('')
const setupInfo = ref(null)

async function startSetup() {
  loading.value = true
  try {
    const response = await userApi.setupMfa(currentPassword.value)
    setupInfo.value = response.data
    setupCode.value = ''
  } finally {
    loading.value = false
  }
}

async function confirmSetup() {
  if (!/^\d{6}$/.test(String(setupCode.value || '').replace(/\s+/g, ''))) {
    ElMessage.error('请输入 6 位动态验证码')
    return
  }
  loading.value = true
  try {
    await userApi.confirmMfa(String(setupCode.value).replace(/\s+/g, ''))
    store.session.mfaEnabled = true
    setupInfo.value = null
    currentPassword.value = ''
    setupCode.value = ''
    ElMessage.success('双因素认证已启用')
  } finally {
    loading.value = false
  }
}

async function disable() {
  loading.value = true
  try {
    await userApi.disableMfa({
      currentPassword: currentPassword.value,
      code: String(code.value || '').replace(/\s+/g, '')
    })
    store.session.mfaEnabled = false
    currentPassword.value = ''
    code.value = ''
    ElMessage.success('双因素认证已关闭')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;

.account-security { display: flex; flex-direction: column; gap: 18px; }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.section-heading h2 { margin: 0; font-size: 20px; font-weight: 600; }
.section-heading p { margin: 7px 0 0; color: $text-secondary; font-size: 13px; line-height: 1.7; }
.security-card { padding: 22px; border: 1px solid $border-lighter; border-radius: 14px; background: $paper; }
.security-card h3 { margin: 0 0 8px; font-size: 16px; font-weight: 600; }
.security-card > p { margin: 0 0 18px; color: $text-secondary; font-size: 13px; line-height: 1.7; }
.field-label { display: block; margin: 14px 0 8px; color: $text-regular; font-size: 13px; font-weight: 500; }
.card-action { margin-top: 18px; }
.setup-grid { display: grid; grid-template-columns: 180px minmax(0, 1fr); align-items: center; gap: 20px; margin: 18px 0; }
.setup-grid img { width: 180px; height: 180px; border-radius: 12px; background: #fff; }
.secret-value { margin: 14px 0; padding: 12px; overflow-wrap: anywhere; color: $text-primary; border: 1px dashed $border-base; border-radius: 8px; background: $bg-color-raised; font-family: ui-monospace, monospace; font-size: 13px; letter-spacing: 0.06em; }
.muted { color: $text-secondary; font-size: 12px; line-height: 1.6; }
.button-row { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
@media (max-width: 720px) { .setup-grid { grid-template-columns: 1fr; } .section-heading { flex-direction: column; } }
</style>
