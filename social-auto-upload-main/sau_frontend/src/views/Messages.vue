<template>
  <div class="dm-page">
    <el-alert
      v-if="hasNoAccounts"
      title="当前没有登录账号，无法查看私信"
      description="请先到「账号管理」添加并登录账号，登录后私信会自动同步显示。"
      type="info"
      show-icon
      :closable="false"
      style="margin-bottom: 14px;"
    />
    <el-alert
      v-for="problem in dmProblems"
      :key="problem.platform + '|' + problem.kind"
      :title="`${problem.platform}私信同步有问题`"
      :description="problem.message"
      :type="problem.kind === 'no_accounts' ? 'info' : 'warning'"
      show-icon
      :closable="false"
      style="margin-bottom: 10px;"
    />
    <div class="page-head">
      <div>
        <h2>私信自动回复</h2>
        <p class="sub">统一收件箱 · 意向识别 · 自定义回复规则 · 线索沉淀</p>
      </div>
      <div class="head-actions">
        <el-tag :type="settings.enabled ? 'success' : 'info'" effect="dark" round>
          {{ settings.enabled ? '自动回复运行中' : '自动回复已暂停' }}
        </el-tag>
        <el-tag :type="syncTagType" effect="plain" round>{{ syncTagText }}</el-tag>
        <el-tag v-if="tierSummaryText" :type="nightMode ? 'warning' : 'info'" effect="plain" round>
          {{ tierSummaryText }}
        </el-tag>
        <el-tag type="success" effect="plain" round>界面 5 秒自动刷新</el-tag>
        <el-button @click="helpDialog = true">接入说明</el-button>
        <el-button :loading="syncing || syncAnyRunning" :disabled="syncAnyRunning" @click="runAllPlatformSync">同步全部私信</el-button>
        <el-button type="primary" @click="activeTab = 'rules'">回复规则</el-button>
      </div>
    </div>

    <div class="metric-grid">
      <div class="metric-card" @click="jumpToPending">
        <span>待人工处理</span>
        <strong>{{ analytics.pending || 0 }}</strong>
        <small>有客户来信尚未人工回复</small>
      </div>
      <div class="metric-card" @click="jumpToHighIntent">
        <span>高意向客户</span>
        <strong class="danger">{{ analytics.high_intent || 0 }}</strong>
        <small>价格 / 预约 / 购买 / 联系方式</small>
      </div>
      <div class="metric-card">
        <span>今日自动回复</span>
        <strong class="success">{{ analytics.today_auto || 0 }}</strong>
        <small>已发送 {{ analytics.sent || 0 }} · 待发送 {{ analytics.ready || 0 }}</small>
      </div>
      <div class="metric-card">
        <span>已沉淀线索</span>
        <strong>{{ analytics.qualified || 0 }}</strong>
        <small>意向客户 {{ analytics.interested || 0 }} · 重点客户 {{ analytics.qualified || 0 }}</small>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="main-tabs" @tab-change="handleTabChange">
      <el-tab-pane label="会话工作台" name="inbox">
        <div class="inbox-layout">
          <aside class="conversation-pane">
            <div class="conversation-filters">
              <el-input v-model="convFilters.q" clearable placeholder="搜索用户 ID / 昵称 / 群名 / 内容" @keyup.enter="loadConversations">
                <template #prefix>搜</template>
              </el-input>
              <div class="filter-row">
                <el-select v-model="accountFilterKey" clearable filterable placeholder="全部账号" @change="handleAccountFilterChange">
                  <el-option v-for="account in allMessageAccounts" :key="account.key" :label="account.label" :value="account.key" />
                </el-select>
                <el-select v-model="convFilters.platform" clearable placeholder="全部平台" @change="handlePlatformFilterChange">
                  <el-option v-for="p in platforms" :key="p" :label="p" :value="p" />
                </el-select>
              </div>
              <div class="filter-row">
                <el-select v-model="convFilters.segment" clearable placeholder="全部类型" @change="loadConversations">
                  <el-option label="新客户" value="new" />
                  <el-option label="意向客户" value="interested" />
                  <el-option label="老客户" value="existing" />
                  <el-option label="未分类" value="unknown" />
                </el-select>
                <el-select v-model="convFilters.status" clearable placeholder="全部状态" @change="loadConversations">
                  <el-option label="待人工" value="pending" />
                  <el-option label="高意向" value="high" />
                </el-select>
              </div>
              <div class="filter-row actions">
                <el-button size="small" @click="loadConversations">刷新</el-button>
                <el-button size="small" @click="importDialog = true">批量导入</el-button>
              </div>
            </div>

            <div v-loading="loadingConversations" class="conversation-list">
              <div
                v-for="item in conversations"
                :key="item.id"
                class="conversation-item"
                :class="{ active: currentContact && currentContact.id === item.id }"
                role="button"
                tabindex="0"
                @click="selectConversation(item)"
                @keyup.enter="selectConversation(item)"
              >
                <el-avatar :size="40" class="avatar">{{ avatarText(item.nickname) }}</el-avatar>
                <div class="conversation-main">
                  <div class="conversation-top">
                    <strong>{{ item.nickname || item.external_user_id || '未知用户' }}</strong>
                    <span class="conversation-time">{{ shortTime(item.last_message_time) }}</span>
                  </div>
                  <div class="conversation-id">ID：{{ item.external_user_id || '未获取' }}</div>
                  <div class="conversation-tags">
                    <el-tag size="small" effect="plain">{{ item.platform }}</el-tag>
                    <el-tag v-if="item.is_group" size="small" type="warning" effect="dark">群聊</el-tag>
                    <el-tag size="small" type="warning" effect="plain">账号：{{ item.account || '未填写' }}</el-tag>
                    <el-tag size="small" :type="segmentTagType(item.segment)" effect="light">{{ item.segment_label }}</el-tag>
                    <el-tag size="small" :type="intentTagType(item.intent)" effect="light">{{ intentText(item.intent) }}</el-tag>
                    <el-tag v-if="item.needs_reply" size="small" type="danger" effect="dark">未回复</el-tag>
                  </div>
                  <div class="conversation-preview">
                    <span v-if="item.last_direction === 'outbound'" class="reply-mark">我：</span>
                    {{ item.last_content }}
                  </div>
                </div>
                <el-badge v-if="item.unread_count" :value="item.unread_count" class="conv-badge" />
              </div>
              <el-empty v-if="!loadingConversations && !conversations.length" description="暂无会话" :image-size="70" />
            </div>
          </aside>

          <section class="chat-pane">
            <template v-if="currentContact">
              <div class="chat-head">
                <div class="chat-user">
                  <el-avatar :size="42">{{ avatarText(currentContact.nickname) }}</el-avatar>
                  <div>
                    <div class="chat-name">
                      {{ currentContact.nickname || currentContact.external_user_id || '未知用户' }}
                      <el-tag v-if="currentContact.is_group" size="small" type="warning" effect="dark">群聊</el-tag>
                      <el-tag size="small" type="warning" effect="plain">账号：{{ currentContact.account || '未填写' }}</el-tag>
                      <el-tag size="small" :type="segmentTagType(currentContact.segment)">{{ currentContact.segment_label }}</el-tag>
                      <el-tag size="small" :type="intentTagType(currentContact.intent)">{{ intentText(currentContact.intent) }}</el-tag>
                      <el-tag v-if="currentContact.needs_reply" size="small" type="danger" effect="dark">未回复</el-tag>
                    </div>
                    <div class="chat-sub">ID：{{ currentContact.external_user_id || '未获取' }} · {{ currentContact.platform }} · {{ currentContact.contact_type_label || '私聊' }} · {{ currentContact.stage_label }}</div>
                  </div>
                </div>
                <div class="chat-actions">
                  <el-button size="small" @click="loadConversation(currentContact.id)">刷新</el-button>
                  <el-button size="small" type="primary" plain @click="openProfile(currentContact)">客户资料</el-button>
                </div>
              </div>

              <div ref="messageList" v-loading="loadingMessages" class="message-list">
                <div v-for="(msg, index) in visibleMessages" :key="msg.id || index" class="message-row" :class="msg.direction">
                  <div class="message-bubble">
                    <div v-if="!mediaOnlyMessage(msg)" class="message-text">{{ msg.content }}</div>
                    <div v-if="msg.media_type" class="message-media">
                      <el-image
                        v-if="['image', 'emotion', 'sticker'].includes(msg.media_type) && msg.media_path"
                        :src="mediaUrl(msg.media_path)"
                        :preview-src-list="[mediaUrl(msg.media_path)]"
                        :preview-teleported="true"
                        fit="contain"
                        class="message-image"
                      />
                      <video
                        v-else-if="msg.media_type === 'video' && msg.media_path"
                        :src="mediaUrl(msg.media_path)"
                        :poster="msg.media_thumb_path ? mediaUrl(msg.media_thumb_path) : undefined"
                        controls
                        preload="metadata"
                        class="message-video"
                      />
                      <template v-else-if="msg.media_type === 'video' && msg.media_thumb_path">
                        <el-image
                          :src="mediaUrl(msg.media_thumb_path)"
                          :preview-src-list="[mediaUrl(msg.media_thumb_path)]"
                          :preview-teleported="true"
                          fit="contain"
                          class="message-image"
                        />
                        <div class="media-note">平台仅提供视频封面，原视频暂不可缓存</div>
                      </template>
                      <div v-else class="media-fallback">
                        <el-tag size="small" effect="plain">{{ mediaTypeLabel(msg.media_type) }}暂未缓存</el-tag>
                      </div>
                      <div v-if="msg.media_path" class="media-actions">
                        <a :href="mediaUrl(msg.media_path, true)" target="_blank" rel="noopener">下载 / 打开</a>
                      </div>
                    </div>
                    <div class="message-meta">
                      <span>{{ shortDateTime(msg.created_at) }}</span>
                      <el-tag v-if="msg.direction === 'outbound' && msg.auto_reply_status === 'auto'" size="small" effect="plain">自动</el-tag>
                      <el-tag v-if="msg.direction === 'outbound'" size="small" :type="deliveryTagType(msg.delivery_status)">
                        {{ deliveryText(msg.delivery_status) }}
                      </el-tag>
                      <el-button v-if="msg.direction === 'outbound' && msg.delivery_status !== 'sent'" link size="small" @click="copyText(msg.content, '回复已复制')">复制</el-button>
                    </div>
                    <div v-if="msg.direction === 'outbound' && msg.delivery_status === 'failed' && msg.delivery_error" class="message-error">
                      失败原因：{{ msg.delivery_error }}
                    </div>
                    <div v-if="msg.source_content" class="message-source">来源内容：{{ msg.source_content }}</div>
                  </div>
                </div>
                <el-empty v-if="!loadingMessages && !visibleMessages.length" description="还没有文字或图片消息" :image-size="70" />
              </div>

              <div class="composer">
                <div class="composer-tools">
                  <el-select v-model="templateId" clearable filterable placeholder="选择快捷话术" @change="applyMessageTemplate">
                    <el-option v-for="t in availableTemplates" :key="t.id" :label="t.name" :value="t.id" />
                  </el-select>
                  <el-button :loading="drafting" @click="generateDraft">AI 起草</el-button>
                  <el-button v-for="quick in quickReplies" :key="quick.label" link @click="replyText = quick.text">{{ quick.label }}</el-button>
                </div>
                <el-input
                  v-model="replyText"
                  type="textarea"
                  :rows="4"
                  resize="none"
                  placeholder="回复客户。Enter 发送并记录，Shift + Enter 换行。"
                  @keydown="handleComposerKeydown"
                />
                <div class="composer-footer">
                  <span class="connector-note">{{ connectorNote }}</span>
                  <div class="composer-send-actions">
                    <input
                      v-if="mediaSendConfig"
                      ref="mediaInput"
                      type="file"
                      class="hidden-file-input"
                      :accept="mediaSendConfig.accept"
                      @change="sendMediaFile"
                    />
                    <el-button
                      v-if="mediaSendConfig"
                      :loading="sendingMedia"
                      :title="mediaSendConfig.hint"
                      @click="mediaInput?.click()"
                    >{{ mediaSendConfig.button }}</el-button>
                    <el-button type="primary" :loading="sending" @click="sendReply">发送并记录</el-button>
                  </div>
                </div>
              </div>
            </template>
            <el-empty v-else description="从左侧选择一个客户会话" :image-size="100" />
          </section>
        </div>
      </el-tab-pane>

      <el-tab-pane label="自动回复规则" name="rules">
        <div class="rule-layout">
          <el-card shadow="never" class="settings-card">
            <template #header>
              <div class="card-head">
                <span>自动回复设置</span>
                <el-tag size="small" :type="settings.enabled ? 'success' : 'info'">{{ settings.enabled ? '运行中' : '已暂停' }}</el-tag>
              </div>
            </template>
            <el-form label-width="106px" class="settings-form">
              <el-form-item label="总开关">
                <el-switch v-model="autoForm.enabled" />
                <span class="form-hint">关闭后仍保留会话与线索，只是不再自动回复</span>
              </el-form-item>
              <el-form-item label="允许回复评论">
                <el-switch v-model="autoForm.reply_comments" />
                <span class="form-hint">默认只回复私信</span>
              </el-form-item>
              <el-form-item label="发送工作日">
                <el-checkbox-group v-model="autoForm.weekdayList">
                  <el-checkbox v-for="d in weekdays" :key="d.value" :value="d.value">{{ d.label }}</el-checkbox>
                </el-checkbox-group>
              </el-form-item>
              <el-form-item label="发送时段">
                <div class="time-range">
                  <el-time-picker v-model="autoForm.working_start" value-format="HH:mm" format="HH:mm" placeholder="开始" />
                  <span>至</span>
                  <el-time-picker v-model="autoForm.working_end" value-format="HH:mm" format="HH:mm" placeholder="结束" />
                </div>
              </el-form-item>
              <el-form-item label="客户冷却">
                <el-input-number v-model="autoForm.cooldown_minutes" :min="0" :max="1440" :step="5" controls-position="right" />
                <span class="form-hint">分钟内不重复自动回复</span>
              </el-form-item>
              <el-form-item label="每日上限">
                <el-input-number v-model="autoForm.max_per_day" :min="0" :max="100" controls-position="right" />
                <span class="form-hint">每个客户每天最多自动回复条数</span>
              </el-form-item>
              <el-form-item label="AI 兜底">
                <el-switch v-model="autoForm.ai_fallback" />
                <span class="form-hint">没有命中规则时调用 AI，会产生接口费用</span>
              </el-form-item>
              <el-form-item label="AI 每日上限">
                <el-input-number v-model="autoForm.ai_max_per_day" :min="0" :max="500" :step="5" controls-position="right" />
                <span class="form-hint">0 不限制；全局按天计数，防止 AI 接口费用失控</span>
              </el-form-item>
              <el-form-item label="AI 配置">
                <el-tag size="small" :type="aiStatus.configured ? 'success' : 'warning'" effect="plain">{{ aiStatusText }}</el-tag>
                <span class="form-hint">未配置时 AI 兜底与 AI 规则会自动跳过，请在 .env 设置 WB_API_KEY</span>
              </el-form-item>
              <el-form-item label="转人工词">
                <el-input v-model="autoForm.stop_keywords" placeholder="人工,转人工,投诉" />
              </el-form-item>
              <el-form-item label="发送连接器">
                <el-input v-model="autoForm.outbound_webhook" placeholder="https://你的发送服务/dm/reply" />
                <span class="form-hint">抖音、小红书、视频号使用本地登录会话直接发送；其他平台可填写发送连接器</span>
              </el-form-item>
              <el-form-item label="接入密钥">
                <el-input v-model="autoForm.inbound_secret" show-password placeholder="可选，用于校验 Webhook 来源" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="savingSettings" @click="saveSettings">保存设置</el-button>
              </el-form-item>
            </el-form>
          </el-card>

          <el-card shadow="never" class="sync-card">
            <template #header>
              <div class="card-head">
                <div>
                  <span>{{ activeSyncMeta.title }}</span>
                  <span class="card-sub">{{ activeSyncAdapter }}</span>
                </div>
                <el-tag size="small" effect="plain" type="success">只读</el-tag>
              </div>
            </template>
            <el-form label-width="92px" class="settings-form">
              <el-form-item label="同步平台">
                <el-radio-group v-model="syncPlatform" size="small">
                  <el-radio-button value="抖音">抖音</el-radio-button>
                  <el-radio-button value="小红书">小红书</el-radio-button>
                  <el-radio-button value="视频号">视频号</el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="自动同步">
                <el-switch v-model="activeSyncForm.enabled" />
                <span class="form-hint">开启后按间隔读取当前平台的私信列表</span>
              </el-form-item>
              <el-form-item label="同步账号">
                <el-select v-model="activeSyncForm.account_ids" multiple collapse-tags collapse-tags-tooltip filterable style="width: 100%" placeholder="不选则同步全部已登录账号">
                  <el-option
                    v-for="account in syncAccounts"
                    :key="account.id"
                    :value="account.id"
                    :label="account.name + (account.has_cookie ? '' : '（Cookie 缺失）')"
                    :disabled="!account.has_cookie"
                  />
                </el-select>
                <span class="form-hint">可多选；{{ activeSyncMeta.accountHint }}</span>
              </el-form-item>
              <el-form-item label="同步模式">
                <el-input-number v-model="activeSyncForm.interval_seconds" :min="activeSyncMeta.minInterval" :max="3600" :step="1" controls-position="right" />
                <span class="form-hint">0 = 持续同步，上一轮结束后立即开始下一轮；&gt;0 为间隔秒数</span>
              </el-form-item>
              <el-form-item label="同步范围">
                <div class="sync-range">
                  <el-switch
                    v-model="activeSyncForm.recent_only"
                    active-text="按时间范围"
                    inline-prompt
                  />
                  <el-input-number
                    v-model="activeSyncForm.history_days"
                    :min="1"
                    :max="30"
                    :step="1"
                    size="small"
                    controls-position="right"
                    :disabled="!activeSyncForm.recent_only"
                  />
                  <span class="form-hint">
                    {{ activeSyncForm.recent_only
                      ? `天；每次回补最近 ${activeSyncForm.history_days || 7} 天的聊天记录`
                      : '关闭后尽量拉取平台能给出的全部历史' }}
                  </span>
                </div>
              </el-form-item>
              <el-form-item>
                <el-button :loading="savingSync" @click="saveSyncSettings">保存同步设置</el-button>
                <el-button type="primary" :loading="syncing" @click="runPlatformSync">同步当前平台</el-button>
              </el-form-item>
            </el-form>
            <div class="sync-summary">
              <div><span>扫描会话</span><strong>{{ activeSyncStatus.last_scanned || 0 }}</strong></div>
              <div><span>新增私信</span><strong class="success">{{ activeSyncStatus.last_imported || 0 }}</strong></div>
              <div><span>重复跳过</span><strong>{{ activeSyncStatus.last_duplicates || 0 }}</strong></div>
              <div><span>条件跳过</span><strong>{{ activeSyncStatus.last_skipped || 0 }}</strong></div>
            </div>
            <div class="sync-meta">
              <span>同步账号：{{ syncSelectedAccountNames }}</span>
              <span>历史窗口：{{ activeSyncStatus.recent_only ? `近 ${activeSyncStatus.history_days || 7} 天` : '不限' }}</span>
              <span>上次成功：{{ activeSyncStatus.last_success_at || '尚未同步' }}</span>
              <span>下次计划：{{ activeSyncStatus.next_run_at || '未安排' }}</span>
              <span v-if="syncSkipSummary">本次跳过：{{ syncSkipSummary }}</span>
              <span v-if="storageHealth" :title="storageHealth.path">
                聊天记录已落库 {{ storageHealth.counts?.messages || 0 }} 条
                · 最新 {{ storageHealth.latest_message_at || '—' }}
                · WAL {{ storageHealth.wal_size || 0 }} B
              </span>
            </div>
            <el-alert
              v-if="activeSyncStatus.last_error"
              :title="activeSyncErrorTitle"
              :type="activeSyncErrorType"
              :closable="false"
              show-icon
              :description="activeSyncStatus.last_error"
            />
            <el-alert
              class="sync-note"
              title="同步为只读，回复通过本地登录会话直发"
              type="info"
              :closable="false"
              show-icon
              :description="activeSyncDescription"
            />
          </el-card>

          <el-card shadow="never" class="ingress-card">
            <template #header><span>接入与发送</span></template>
            <div class="ingress-row">
              <span class="ingress-label">收信 Webhook</span>
              <code class="webhook-url">{{ webhookUrl }}</code>
              <el-button size="small" @click="copyText(webhookUrl, 'Webhook 地址已复制')">复制</el-button>
            </div>
            <p class="ingress-tip">把平台侧的新私信事件转发到这里。建议带上稳定的 <code>external_user_id</code> 与 <code>external_message_id</code>，用于合并客户和防止重复回复。</p>
            <div class="delivery-summary">
              <div><span>已发送</span><strong class="success">{{ analytics.sent || 0 }}</strong></div>
              <div><span>待人工发送</span><strong class="warning">{{ analytics.ready || 0 }}</strong></div>
              <div><span>发送失败</span><strong class="danger">{{ analytics.failed || 0 }}</strong></div>
            </div>
            <el-alert
              title="抖音、小红书、视频号支持通过已登录网页会话直接发送"
              type="warning"
              :closable="false"
              show-icon
              description="三类平台回复都会校验接口或页面结果并按来源账号发送；视频号 Cookie 失效时会提示重新扫码。其他平台仍可使用自有发送连接器，发送失败时不会误标为已发送。"
            />
            <div class="ingress-actions">
              <el-button @click="helpDialog = true">查看接入格式</el-button>
            </div>
          </el-card>
        </div>

        <el-card shadow="never" class="rules-card">
          <template #header>
            <div class="card-head">
              <div>
                <span>回复规则</span>
                <span class="card-sub">按优先级从高到低匹配；AI 规则按关键词命中后调用 AI，未命中时走 AI 兜底</span>
              </div>
              <div>
                <el-button size="small" type="primary" @click="openRule()">新建规则</el-button>
              </div>
            </div>
          </template>
          <el-table :data="rules" border stripe size="small">
            <el-table-column prop="name" label="规则" min-width="170" show-overflow-tooltip />
            <el-table-column label="类型" width="82">
              <template #default="scope"><el-tag size="small" :type="segmentTagType(scope.row.segment)">{{ scope.row.segment_label }}</el-tag></template>
            </el-table-column>
            <el-table-column label="回复方式" width="92">
              <template #default="scope">
                <el-tag size="small" :type="scope.row.reply_mode === 'ai' ? 'success' : 'info'">{{ scope.row.reply_mode === 'ai' ? 'AI 智能' : '固定模板' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="keywords" label="关键词" min-width="220" show-overflow-tooltip>
              <template #default="scope">{{ scope.row.keywords || '兜底：全部来信' }}</template>
            </el-table-column>
            <el-table-column prop="priority" label="优先级" width="76" />
            <el-table-column label="启用" width="76">
              <template #default="scope"><el-switch v-model="scope.row.enabled" :active-value="1" :inactive-value="0" @change="toggleRule(scope.row)" /></template>
            </el-table-column>
            <el-table-column label="操作" width="126" fixed="right">
              <template #default="scope">
                <el-button link type="primary" @click="openRule(scope.row)">编辑</el-button>
                <el-button link type="danger" @click="removeRule(scope.row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="客户线索" name="leads">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <div>
                <span>客户线索池</span>
                <span class="card-sub">会话自动沉淀人数、出发地、日期、公司、预算、联系方式与跟进阶段</span>
              </div>
              <div class="lead-actions">
            <el-select v-model="leadFilters.segment" clearable placeholder="全部类型" @change="loadLeads">
                  <el-option label="新客户" value="new" />
                  <el-option label="意向客户" value="interested" />
                  <el-option label="老客户" value="existing" />
                  <el-option label="未分类" value="unknown" />
                </el-select>
                <el-select v-model="leadFilters.stage" clearable placeholder="全部阶段" @change="loadLeads">
                  <el-option v-for="(label, key) in stageMap" :key="key" :label="label" :value="key" />
                </el-select>
                <el-button @click="exportLeads">导出 CSV</el-button>
              </div>
            </div>
          </template>
          <el-table :data="leads" border stripe size="small" v-loading="loadingLeads">
            <el-table-column prop="nickname" label="客户" width="130" show-overflow-tooltip />
            <el-table-column prop="platform" label="平台" width="72" />
            <el-table-column label="类型 / 意向" width="128">
              <template #default="scope">
                <el-tag size="small" :type="segmentTagType(scope.row.segment)">{{ scope.row.segment_label }}</el-tag>
                <el-tag size="small" :type="intentTagType(scope.row.intent)" style="margin-left:4px">{{ intentText(scope.row.intent) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="stage_label" label="阶段" width="88" />
            <el-table-column prop="contact_info" label="联系方式" width="130" show-overflow-tooltip>
              <template #default="scope">{{ scope.row.contact_info || '未留资' }}</template>
            </el-table-column>
            <el-table-column label="线索信息" min-width="230">
              <template #default="scope">
                <template v-if="scope.row.company_name || scope.row.budget">
                  <div>{{ scope.row.company_name || '公司待确认' }} · {{ scope.row.traveler_count ? scope.row.traveler_count + '人' : '人数待确认' }}</div>
                  <div class="muted">{{ scope.row.budget || '预算待确认' }} · {{ scope.row.travel_date || '档期待确认' }} · {{ scope.row.origin || '出发地待确认' }}</div>
                </template>
                <span v-else-if="scope.row.origin || scope.row.traveler_count || scope.row.travel_date">
                  {{ scope.row.origin || '出发地待确认' }} · {{ scope.row.traveler_count || '人数待确认' }} · {{ scope.row.travel_date || '日期待确认' }}
                </span>
                <span v-else class="muted">待确认</span>
              </template>
            </el-table-column>
            <el-table-column prop="last_message_at" label="最后联系" width="152" />
            <el-table-column label="操作" width="132" fixed="right">
              <template #default="scope">
                <el-button link type="primary" @click="openFromLead(scope.row)">打开会话</el-button>
                <el-button link @click="openProfile(scope.row)">资料</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!loadingLeads && !leads.length" description="还没有客户线索" :image-size="80" />
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="importDialog" title="批量导入消息" width="620px">
      <p class="dialog-hint">每行一条：<code>平台|账号|用户|留言内容|来源内容(可选)|用户ID(可选)</code></p>
      <el-input v-model="importText" type="textarea" :rows="8" placeholder="抖音|账号名|客户昵称|我想了解这个产品&#10;小红书|账号名|另一位客户|有优惠活动吗？|来源内容" />
      <div class="import-option">
        <el-switch v-model="importAutoReply" />
        <span>导入后对命中的规则自动回复</span>
      </div>
      <template #footer>
        <el-button @click="importDialog = false">取消</el-button>
        <el-button type="primary" :loading="importing" @click="doImport">导入</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="ruleDialog" :title="ruleForm.id ? '编辑回复规则' : '新建回复规则'" width="760px">
      <el-form label-width="86px">
        <el-form-item label="规则名称"><el-input v-model="ruleForm.name" placeholder="例如：价格咨询、预约咨询、售后问题" /></el-form-item>
        <el-form-item label="客户类型">
          <el-select v-model="ruleForm.segment" style="width: 180px">
            <el-option label="通用" value="" />
            <el-option label="新客户" value="new" />
            <el-option label="意向客户" value="interested" />
            <el-option label="老客户" value="existing" />
          </el-select>
          <span class="form-hint">具体客户类型规则会优先于通用规则</span>
        </el-form-item>
        <el-form-item label="回复方式">
          <el-radio-group v-model="ruleForm.reply_mode">
            <el-radio label="template">固定模板</el-radio>
            <el-radio label="ai">AI 智能回复</el-radio>
          </el-radio-group>
          <span class="form-hint">AI 模式按关键词命中后调用 AI 生成，每日用量受全局上限约束</span>
        </el-form-item>
        <el-form-item v-if="ruleForm.reply_mode === 'ai'" label="AI 口径">
          <el-input v-model="ruleForm.ai_prompt" type="textarea" :rows="2" placeholder="可选，例如：不要提价格，先要联系方式" />
        </el-form-item>
        <el-form-item label="关键词">
          <el-input v-model="ruleForm.keywords" type="textarea" :rows="3" placeholder="用逗号或换行分隔，例如：多少钱,价格,报价" />
          <span class="form-hint">留空表示该类型的所有来信都匹配</span>
        </el-form-item>
        <el-form-item label="匹配方式">
          <el-radio-group v-model="ruleForm.match_type">
            <el-radio label="any">命中任一关键词</el-radio>
            <el-radio label="all">命中全部关键词</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="回复内容">
          <el-input v-model="ruleForm.reply_template" type="textarea" :rows="6" :placeholder="ruleForm.reply_mode === 'ai' ? 'AI 模式可选：生成失败时回退使用这段固定文案' : '支持变量：{greeting} {nickname} {origin} {travelers} {travel_date} {company} {budget}'" />
          <div v-if="ruleForm.reply_mode !== 'ai'" class="token-row">
            <el-tag v-for="token in tokens" :key="token" size="small" effect="plain">{{ token }}</el-tag>
          </div>
        </el-form-item>
        <el-form-item label="优先级"><el-input-number v-model="ruleForm.priority" :min="0" :max="9999" /></el-form-item>
        <el-form-item label="单客冷却">
          <el-input-number v-model="ruleForm.cooldown_minutes" :min="0" :max="1440" :step="5" />
          <span class="form-hint">0 表示跟随全局设置</span>
        </el-form-item>
        <el-form-item label="其他">
          <el-checkbox v-model="ruleForm.once_per_contact">同一客户只自动回复一次</el-checkbox>
          <el-checkbox v-model="ruleForm.enabled">启用此规则</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ruleDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingRule" @click="saveRule">保存规则</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="profileVisible" title="客户资料" size="420px">
      <template v-if="profileForm">
        <div class="profile-meta">
          <el-avatar :size="48">{{ avatarText(profileForm.nickname) }}</el-avatar>
          <div>
            <strong>{{ profileForm.nickname }}</strong>
            <div class="muted">{{ profileForm.platform }} · {{ profileForm.account || '未填账号' }}</div>
            <div class="muted">用户 ID：{{ profileForm.external_user_id }}</div>
          </div>
        </div>
        <el-form label-position="top" class="profile-form">
          <el-form-item label="昵称"><el-input v-model="profileForm.nickname" /></el-form-item>
          <div class="two-col">
            <el-form-item label="客户类型">
              <el-select v-model="profileForm.segment">
                <el-option label="新客户" value="new" />
                <el-option label="意向客户" value="interested" />
                <el-option label="老客户" value="existing" />
                <el-option label="未分类" value="unknown" />
              </el-select>
            </el-form-item>
            <el-form-item label="跟进阶段">
              <el-select v-model="profileForm.stage">
                <el-option v-for="(label, key) in stageMap" :key="key" :label="label" :value="key" />
              </el-select>
            </el-form-item>
          </div>
          <el-form-item label="联系方式"><el-input v-model="profileForm.contact_info" placeholder="手机号 / 微信" /></el-form-item>
          <div class="two-col">
            <el-form-item label="公司 / 单位"><el-input v-model="profileForm.company_name" placeholder="例如：XX科技" /></el-form-item>
            <el-form-item label="预算范围"><el-input v-model="profileForm.budget" placeholder="例如：人均800 / 总预算3万" /></el-form-item>
          </div>
          <div class="two-col">
            <el-form-item label="出发地"><el-input v-model="profileForm.origin" /></el-form-item>
            <el-form-item label="出行人数"><el-input-number v-model="profileForm.traveler_count" :min="0" :max="999" /></el-form-item>
          </div>
          <el-form-item label="意向日期"><el-input v-model="profileForm.travel_date" placeholder="例如：10月20日 / 周末" /></el-form-item>
          <el-form-item label="来源内容"><el-input v-model="profileForm.source_content" /></el-form-item>
          <el-form-item label="标签"><el-input v-model="profileForm.tags" placeholder="逗号分隔" /></el-form-item>
          <el-form-item label="跟进备注"><el-input v-model="profileForm.notes" type="textarea" :rows="4" /></el-form-item>
          <el-form-item label="自动回复">
            <el-switch v-model="profileForm.auto_reply_enabled" :active-value="1" :inactive-value="0" />
            <span class="form-hint">该客户命中转人工词时可单独关闭</span>
          </el-form-item>
        </el-form>
      </template>
      <div class="drawer-actions">
        <el-button @click="profileVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingProfile" @click="saveProfile">保存资料</el-button>
      </div>
    </el-drawer>

    <el-dialog v-model="helpDialog" title="私信接入说明" width="780px">
      <div class="help-section">
        <h4>1. 把平台私信转发到这里</h4>
        <pre class="code-block">POST {{ webhookUrl }}
Content-Type: application/json
X-YunCe-Secret: {{ settings.inbound_secret ? '你的接入密钥' : '（未设置可省略）' }}

{
  "platform": "抖音",
  "account": "你的账号名",
  "external_user_id": "平台内稳定用户ID",
  "external_message_id": "平台内消息ID",
  "author": "王姐",
  "msg_type": "dm",
  "content": "这个产品有哪些规格？多少钱？可以预约吗？",
  "source_content": "产品介绍视频"
}</pre>
        <el-button size="small" @click="copyText(webhookExample, '请求示例已复制')">复制请求示例</el-button>
      </div>
      <div class="help-section">
        <h4>2. 自动回复如何落地</h4>
        <p>系统先识别意向，再按关键词与优先级匹配你设置的规则。命中后生成一条回复：</p>
        <ul>
          <li>抖音会根据客户昵称打开对应会话，通过当前已登录账号直接点击发送。</li>
          <li>小红书和视频号会按稳定的会话 ID 直接调用平台私信接口，不依赖昵称匹配。</li>
          <li>发送成功后才标记为“已发送”；登录失效或平台限制会明确显示失败原因。</li>
          <li>快手、微博、百家号等其他平台可以继续配置发送连接器。</li>
        </ul>
      </div>
      <div class="help-section">
        <h4>3. 发送连接器收到的数据</h4>
        <pre class="code-block">{
  "event": "dm.reply",
  "platform": "抖音",
  "account": "你的账号名",
  "external_user_id": "平台内稳定用户ID",
  "conversation_id": 12,
  "message_id": 88,
  "text": "宝子，带爸妈出门……"
}</pre>
      </div>
      <template #footer><el-button type="primary" @click="helpDialog = false">知道了</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { dmApi } from '@/api/dm'
import { useAccountStore } from '@/stores/account'
import { API_BASE, withToken } from '@/config'
import {
  avatarText, deliveryTagType, deliveryText, intentTagType, intentText,
  segmentTagType, shortDateTime, shortTime,
} from '@/utils/dm-format'
import { useLivePolling } from '@/composables/useLivePolling'

const route = useRoute()
const apiBase = API_BASE
const accountStore = useAccountStore()
const hasNoAccounts = computed(() => !accountStore.accounts.some(account => account.status === '正常'))
const platforms = ['抖音', '快手', '视频号', '小红书', '微博', '百家号']
const weekdays = [
  { value: '1', label: '一' }, { value: '2', label: '二' }, { value: '3', label: '三' },
  { value: '4', label: '四' }, { value: '5', label: '五' }, { value: '6', label: '六' },
  { value: '7', label: '日' },
]
const stageMap = {
  new: '新咨询', engaged: '已互动', qualified: '已留资',
  contacted: '已联系', converted: '已成交', lost: '已流失',
}
const tokens = ['{greeting}', '{nickname}', '{origin}', '{travelers}', '{travel_date}', '{company}', '{budget}', '{platform}', '{segment}']

const activeTab = ref('inbox')
const analytics = ref({})
const settings = ref({ enabled: 0 })
const aiStatus = ref({ configured: false, model: '', base_url: '' })
const autoForm = reactive({
  enabled: false, reply_comments: false, weekdayList: ['1', '2', '3', '4', '5', '6', '7'],
  working_start: '08:00', working_end: '23:00', cooldown_minutes: 30, max_per_day: 3,
  ai_fallback: false, ai_max_per_day: 50, stop_keywords: '人工,转人工,投诉,退款',
  outbound_webhook: '', inbound_secret: '',
})
const savingSettings = ref(false)
const syncStatus = ref({ enabled: 0, running: false, accounts: [] })
const xhsSyncStatus = ref({ enabled: 0, running: false, accounts: [] })
const channelsSyncStatus = ref({ enabled: 0, running: false, accounts: [] })

// 同步健康度（/dm/health）：平台级 + 账号级，用来解释"为什么没有新消息"
const dmHealth = ref({ platforms: [], problems: [] })
const dmProblems = computed(() => dmHealth.value.problems || [])

async function loadDmHealth() {
  try {
    const res = await dmApi.getHealth()
    dmHealth.value = res.data || { platforms: [], problems: [] }
  } catch (error) {
    // 健康度探测失败不阻塞列表刷新
    console.warn('加载私信同步健康度失败:', error)
  }
}
const syncForm = reactive({ enabled: false, account_ids: [], interval_seconds: 0, recent_only: true, history_days: 7 })
const xhsSyncForm = reactive({ enabled: false, account_ids: [], interval_seconds: 0, recent_only: true, history_days: 7 })
const channelsSyncForm = reactive({ enabled: false, account_ids: [], interval_seconds: 0, recent_only: true, history_days: 7 })
const syncPlatform = ref('抖音')
const savingSync = ref(false)
const syncing = ref(false)
const rules = ref([])
const storageHealth = ref(null)
const conversations = ref([])
const convFilters = reactive({ q: '', account: '', platform: '', segment: '', status: '' })
const accountFilterKey = ref('')
const loadingConversations = ref(false)
const currentContact = ref(null)
const messages = ref([])
const visibleMessages = computed(() => messages.value.filter(message => {
  if (currentContact.value?.platform !== '抖音') return true
  const mediaType = String(message.media_type || '').toLowerCase()
  const text = String(message.content || '').trim()
  return !['video', 'share'].includes(mediaType)
    && !['[视频]', '[分享视频]', '分享[视频]'].includes(text)
    && !text.startsWith('分享视频无法查看')
    && !text.startsWith('该内容为视频')
    && !text.startsWith('该内容为分享内容')
}))
const loadingMessages = ref(false)
const messageList = ref(null)
const lastMessageId = ref(0)
const liveTick = ref(0)
const templates = ref([])
const templateId = ref(null)
const replyText = ref('')
const drafting = ref(false)
const sending = ref(false)
const sendingMedia = ref(false)
const mediaInput = ref(null)
const leads = ref([])
const leadFilters = reactive({ segment: '', stage: '' })
const loadingLeads = ref(false)
const profileVisible = ref(false)
const profileForm = ref(null)
const savingProfile = ref(false)
const ruleDialog = ref(false)
const savingRule = ref(false)
const ruleForm = reactive({
  id: null, name: '', segment: '', keywords: '', match_type: 'any',
  reply_mode: 'template', ai_prompt: '', reply_template: '', priority: 10, cooldown_minutes: 0,
  once_per_contact: false, enabled: true,
})
const importDialog = ref(false)
const importText = ref('')
const importAutoReply = ref(false)
const importing = ref(false)
const helpDialog = ref(false)
const webhookUrl = computed(() => `${apiBase}/dm/inbound`)
const webhookExample = computed(() => `POST ${webhookUrl.value}
Content-Type: application/json
X-YunCe-Secret: ${settings.value.inbound_secret ? '你的接入密钥' : '（未设置可省略）'}

{
  "platform": "抖音",
  "account": "你的账号名",
  "external_user_id": "平台内稳定用户ID",
  "external_message_id": "平台内消息ID",
  "author": "王姐",
  "msg_type": "dm",
  "content": "这个产品有哪些规格？多少钱？可以预约吗？",
  "source_content": "产品介绍视频"
}`)
const syncPlatformMeta = {
  '抖音': {
    prefix: '/dm/sync', minInterval: 0, defaultInterval: 0,
    title: '抖音私信本地同步', accountHint: '不选时同步全部已登录且 Cookie 可用的抖音账号',
  },
  '小红书': {
    prefix: '/dm/xhs', minInterval: 0, defaultInterval: 0,
    title: '小红书私信同步', accountHint: '不选时同步全部已登录且 Cookie 可用的小红书账号',
  },
  '视频号': {
    prefix: '/dm/channels', minInterval: 0, defaultInterval: 0,
    title: '视频号私信同步', accountHint: '不选时同步全部已登录且 Cookie 可用的视频号账号',
  },
}
const syncRunTargets = [
  { platform: '抖音', prefix: '/dm/sync' },
  { platform: '小红书', prefix: '/dm/xhs' },
  { platform: '视频号', prefix: '/dm/channels' },
]
const directPlatforms = new Set(['抖音', '小红书', '视频号'])
const mediaSendConfig = computed(() => {
  const platform = currentContact.value?.platform
  if (platform === '视频号') {
    return {
      platform,
      accept: 'image/*,video/*,.pdf,.doc,.docx,.xls,.xlsx,.zip,.txt',
      button: '发送图片/附件',
      hint: '单文件最大 25MB',
    }
  }
  if (platform === '小红书') {
    return {
      platform,
      accept: 'image/*,video/*',
      button: '发送图片/视频',
      hint: '图片最大 10MB，视频最大 100MB',
    }
  }
  return null
})
const activeSyncMeta = computed(() => syncPlatformMeta[syncPlatform.value])
const activeSyncStatus = computed(() => {
  if (syncPlatform.value === '小红书') return xhsSyncStatus.value
  if (syncPlatform.value === '视频号') return channelsSyncStatus.value
  return syncStatus.value
})
const activeSyncForm = computed(() => {
  if (syncPlatform.value === '小红书') return xhsSyncForm
  if (syncPlatform.value === '视频号') return channelsSyncForm
  return syncForm
})
const activeSyncAdapter = computed(() => syncPlatform.value === '抖音'
  ? 'Playwright 只读适配器'
  : '网页 API 只读同步')
const activeSyncDescription = computed(() => ({
  '抖音': '支持多账号独立同步，消息按来源账号隔离存储；同步时不会点开会话或标记已读。手动回复会使用该会话所属账号登录并发送。适配器依赖抖音创作者中心网页版结构，平台改版时可能需要维护。',
  '小红书': '通过小红书主站网页登录态读取私信，消息按来源账号隔离；同步只生成待人工回复。手动回复按会话 ID 精确定位并直接发送，不依赖客户昵称。重新登录时必须使用接收私信的账号扫码。',
  '视频号': '通过视频号助手网页登录态读取私信，消息按来源账号隔离；同步只生成待人工回复。手动回复按 sessionId 精确定位客户。Cookie 失效时必须重新扫码登录后才能同步或发送。',
}[syncPlatform.value]))
const connectorNote = computed(() => directPlatforms.has(currentContact.value?.platform)
  ? `${currentContact.value?.platform}将通过当前登录账号直接发送并校验结果`
  : (autoForm.outbound_webhook
      ? '已配置发送连接器，发送结果会回写到消息状态'
      : '未配置发送连接器：发送后保存为“待人工发送”，可在消息旁复制回复'))
const syncAccounts = computed(() => activeSyncStatus.value.accounts || [])
const allMessageAccounts = computed(() => [
  ...(syncStatus.value.accounts || []).map(account => ({ ...account, platform: '抖音' })),
  ...(xhsSyncStatus.value.accounts || []).map(account => ({ ...account, platform: '小红书' })),
  ...(channelsSyncStatus.value.accounts || []).map(account => ({ ...account, platform: '视频号' })),
].map(account => ({
  ...account,
  key: `${account.platform}\u0000${account.name}\u0000${account.id}`,
  label: `${account.platform} · ${account.name}`,
})))
const syncSelectedAccounts = computed(() => {
  const accounts = activeSyncStatus.value.accounts || []
  const ids = (activeSyncForm.value.account_ids || []).map(Number)
  if (ids.length) return accounts.filter(account => ids.includes(Number(account.id)))
  return accounts.filter(account => account.has_cookie)
})
const syncSelectedAccountNames = computed(() => {
  const names = syncSelectedAccounts.value.map(account => account.name)
  return names.length ? names.join('、') : '未选择'
})
const syncSkipSummary = computed(() => Object.entries(activeSyncStatus.value.last_skip_reasons || {})
  .map(([reason, count]) => `${reason} ${count}`).join(' · '))
// 没登录账号时 last_error 只是"还没配置"的提示，用 info 样式而不是红色报错
const activeSyncErrorType = computed(() => (
  activeSyncStatus.value.last_error_is_account_hint ? 'info' : 'error'
))
const activeSyncErrorTitle = computed(() => (
  activeSyncStatus.value.last_error_is_account_hint
    ? '尚未登录该平台账号'
    : '最近一次同步异常'
))
const syncTargetStates = computed(() => [
  { platform: '抖音', status: syncStatus.value },
  { platform: '小红书', status: xhsSyncStatus.value },
  { platform: '视频号', status: channelsSyncStatus.value },
])
const syncAnyRunning = computed(() => syncTargetStates.value.some(item => item.status.running))
const syncAllEnabled = computed(() => syncTargetStates.value.every(item => item.status.enabled))
const syncEnabledCount = computed(() => syncTargetStates.value.filter(item => item.status.enabled).length)
const syncAllHasError = computed(() => syncTargetStates.value.some(
  // "还没登录账号"属于未配置状态，不算同步异常
  item => item.status.last_error && !item.status.last_error_is_account_hint,
))
const syncAccountCount = computed(() => syncTargetStates.value.reduce(
  (count, item) => count + Number(item.status.account_count || 0), 0,
))
const syncTagText = computed(() => {
  if (syncAnyRunning.value || syncing.value) return '全部平台同步中'
  if (syncAllHasError.value) return '部分平台同步异常'
  if (syncAllEnabled.value) return `全部平台自动同步已开启（${syncAccountCount.value} 个账号）`
  if (syncEnabledCount.value) return `部分平台自动同步已开启（${syncEnabledCount.value}/${syncTargetStates.value.length}）`
  return '全部平台自动同步已关闭'
})
const syncTagType = computed(() => {
  if (syncAllHasError.value) return 'danger'
  if (syncAnyRunning.value || syncing.value) return 'warning'
  return syncAllEnabled.value ? 'success' : 'info'
})
// 账号分级：活跃账号高频、冷账号低频，夜间还会整体降频。
const tierSummary = computed(() => {
  const totals = { active: 0, cold: 0 }
  for (const item of syncTargetStates.value) {
    const summary = item.status.tier_summary || {}
    totals.active += Number(summary.active || 0)
    totals.cold += Number(summary.cold || 0)
  }
  return totals
})
const nightMode = computed(() => syncTargetStates.value.some(
  item => item.status.night_mode,
))
const tierSummaryText = computed(() => {
  const { active, cold } = tierSummary.value
  if (!active && !cold) return ''
  const base = `活跃账号 ${active} · 冷账号 ${cold}`
  return nightMode.value ? `${base} · 夜间降频中` : base
})
const aiStatusText = computed(() => aiStatus.value.configured
  ? `已配置（${aiStatus.value.model || '默认模型'}）`
  : '未配置 API Key，AI 回复不可用')
const availableTemplates = computed(() => {
  const segment = currentContact.value?.segment || 'unknown'
  const items = [
    ...templates.value.map(item => ({
      id: 'template-' + item.id, name: item.name, content: item.content,
      label: item.name, text: item.content, segment: item.segment || ''
    })),
    ...rules.value.filter(item => item.enabled && item.reply_mode !== 'ai').map(item => ({
      id: 'rule-' + item.id, name: item.name, content: item.reply_template,
      label: item.name, text: item.reply_template, segment: item.segment || ''
    }))
  ]
  return items.filter(item => !item.segment || item.segment === 'unknown' || segment === 'unknown' || item.segment === segment)
})
const quickReplies = computed(() => availableTemplates.value.slice(0, 6))

function handleComposerKeydown(event) {
  if (event.isComposing || event.keyCode === 229 || event.key !== 'Enter' || event.shiftKey) return
  event.preventDefault()
  sendReply()
}

async function copyText(text, success = '已复制') {
  const value = String(text || '')
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
  } catch {
    const area = document.createElement('textarea')
    area.value = value
    document.body.appendChild(area)
    area.select()
    document.execCommand('copy')
    area.remove()
  }
  ElMessage.success(success)
}

async function loadAnalytics() {
  try {
    const res = await dmApi.getAnalytics()
    analytics.value = res.data || {}
  } catch (error) { console.error(error) }
}

async function loadAiStatus() {
  try {
    const res = await dmApi.getAiStatus()
    aiStatus.value = res.data || {}
  } catch (error) { console.error(error) }
}

async function loadSettings() {
  try {
    const res = await dmApi.getSettings()
    const data = res.data || {}
    settings.value = data
    autoForm.enabled = Boolean(data.enabled)
    autoForm.reply_comments = Boolean(data.reply_comments)
    autoForm.weekdayList = String(data.weekdays || '1,2,3,4,5,6,7').split(',').filter(Boolean)
    autoForm.working_start = data.working_start || '08:00'
    autoForm.working_end = data.working_end || '23:00'
    autoForm.cooldown_minutes = Number(data.cooldown_minutes || 0)
    autoForm.max_per_day = Number(data.max_per_day || 0)
    autoForm.ai_fallback = Boolean(data.ai_fallback)
    autoForm.ai_max_per_day = Number(data.ai_max_per_day ?? 50)
    autoForm.stop_keywords = data.stop_keywords || ''
    autoForm.outbound_webhook = data.outbound_webhook || ''
    autoForm.inbound_secret = data.inbound_secret || ''
    loadAiStatus()
  } catch (error) { console.error(error) }
}

async function saveSettings() {
  savingSettings.value = true
  try {
    const payload = {
      ...autoForm,
      weekdays: autoForm.weekdayList.join(','),
      enabled: autoForm.enabled ? 1 : 0,
      reply_comments: autoForm.reply_comments ? 1 : 0,
      ai_fallback: autoForm.ai_fallback ? 1 : 0,
      ai_max_per_day: Number(autoForm.ai_max_per_day || 0),
    }
    delete payload.weekdayList
    const res = await dmApi.saveSettings(payload)
    settings.value = res.data || settings.value
    ElMessage.success('自动回复设置已保存')
    loadAnalytics()
  } catch (error) { console.error(error) }
  finally { savingSettings.value = false }
}

function hydrateSyncForm(form, data, defaultInterval) {
  form.enabled = Boolean(data.enabled)
  form.account_ids = Array.isArray(data.account_ids)
    ? data.account_ids.map(Number).filter(Boolean)
    : []
  form.interval_seconds = Number(data.interval_seconds ?? defaultInterval)
  form.recent_only = Boolean(data.recent_only)
  form.history_days = Number(data.history_days ?? 7)
}

function setSyncStatus(platform, data) {
  if (platform === '小红书') xhsSyncStatus.value = data
  else if (platform === '视频号') channelsSyncStatus.value = data
  else syncStatus.value = data
}

async function loadSyncStatus() {  loadDmHealth()
  if (hasNoAccounts.value) return []
  const targets = [
    ['抖音', '/dm/sync/status', syncForm, 0],
    ['小红书', '/dm/xhs/status', xhsSyncForm, 0],
    ['视频号', '/dm/channels/status', channelsSyncForm, 0],
  ]
  const rows = await Promise.all(targets.map(async ([platform, url, form, defaultInterval]) => {
    try {
      const res = await dmApi.getSyncStatus(url)
      const data = res.data || {}
      setSyncStatus(platform, data)
      hydrateSyncForm(form, data, defaultInterval)
      return [platform, data]
    } catch (error) {
      console.error(error)
      return [platform, null]
    }
  }))
  return rows
}

async function saveSyncSettings() {
  savingSync.value = true
  try {
    const meta = activeSyncMeta.value
    const form = activeSyncForm.value
    const res = await dmApi.saveSyncSettings(meta.prefix, {
      enabled: form.enabled ? 1 : 0,
      account_ids: (form.account_ids || []).map(Number).filter(Boolean),
      interval_seconds: Number(form.interval_seconds ?? meta.defaultInterval),
      recent_only: form.recent_only ? 1 : 0,
      history_days: Number(form.history_days ?? 7),
      max_conversations: Number(activeSyncStatus.value.max_conversations || 50),
    })
    setSyncStatus(syncPlatform.value, res.data || activeSyncStatus.value)
    ElMessage.success(`${syncPlatform.value}同步设置已保存`)
  } catch (error) { console.error(error) }
  finally { savingSync.value = false }
}

async function runPlatformSync() {
  if (syncing.value) return
  syncing.value = true
  try {
    const platform = syncPlatform.value
    const res = await dmApi.runSync(activeSyncMeta.value.prefix)
    const data = res.data || {}
    const summary = `扫描 ${data.scanned || 0}，新增 ${data.imported || 0}，重复 ${data.duplicates || 0}`
    if (data.partial) ElMessage.warning(`部分账号同步失败：${summary}。${data.error || ''}`)
    else ElMessage.success(`${platform}同步完成：${summary}`)
    await Promise.all([loadSyncStatus(), loadConversations(), loadAnalytics(), loadLeads()])
    if (currentContact.value?.id) await loadConversation(currentContact.value.id)
  } catch (error) {
    console.error(error)
    await loadSyncStatus()
  } finally { syncing.value = false }
}

async function runAllPlatformSync() {
  if (syncing.value || syncAnyRunning.value) return
  syncing.value = true
  try {
    const results = await Promise.all(syncRunTargets.map(async target => {
      try {
        const res = await dmApi.runSync(target.prefix, { silent: true })
        return { platform: target.platform, ok: true, data: res.data || {} }
      } catch (error) {
        return {
          platform: target.platform,
          ok: false,
          error: error.response?.data?.msg || error.message || '同步失败',
        }
      }
    }))
    const succeeded = results.filter(item => item.ok)
    const failed = results.filter(item => !item.ok)
    const partial = succeeded.filter(item => item.data.partial)
    const scanned = succeeded.reduce((sum, item) => sum + Number(item.data.scanned || 0), 0)
    const imported = succeeded.reduce((sum, item) => sum + Number(item.data.imported || 0), 0)
    const duplicates = succeeded.reduce((sum, item) => sum + Number(item.data.duplicates || 0), 0)

    await Promise.all([loadSyncStatus(), loadConversations(), loadAnalytics(), loadLeads()])
    if (currentContact.value?.id) await loadConversation(currentContact.value.id)

    if (failed.length || partial.length) {
      const details = [
        ...failed.map(item => `${item.platform}：${item.error}`),
        ...partial.map(item => `${item.platform}：部分账号失败`),
      ].join('；')
      ElMessage.warning(`全平台同步完成 ${succeeded.length} 个平台；${details}。扫描 ${scanned}，新增 ${imported}，重复 ${duplicates}`)
    } else {
      ElMessage.success(`全部平台同步完成：扫描 ${scanned}，新增 ${imported}，重复 ${duplicates}`)
    }
  } finally {
    syncing.value = false
  }
}

function handleAccountFilterChange(key) {
  const account = allMessageAccounts.value.find(item => item.key === key)
  if (account) {
    convFilters.account = account.name
    convFilters.platform = account.platform
  } else {
    convFilters.account = ''
  }
  loadConversations()
}

function handlePlatformFilterChange() {
  accountFilterKey.value = ''
  convFilters.account = ''
  loadConversations()
}

async function refreshLive() {
  if (hasNoAccounts.value) return
  const contactId = currentContact.value?.id
  const previousLastMessageTime = currentContact.value?.last_message_time || ''
  liveTick.value += 1
  try {
    await Promise.all([loadSyncStatus(), loadConversations(true)])
    if (liveTick.value % 10 === 1) loadAnalytics()
    if (liveTick.value % 30 === 1) loadStorageHealth()
    if (contactId) {
      const latest = conversations.value.find(item => item.id === contactId)
      const latestId = Number(latest?.last_message_id || 0)
      const latestTime = latest?.last_message_time || ''
      const timeChanged = Boolean(
        previousLastMessageTime && latestTime && latestTime !== previousLastMessageTime
      )
      if (latestId > lastMessageId.value || timeChanged) {
        await loadConversation(contactId, true, !timeChanged)
      }
      if (Number(currentContact.value?.unread_count || 0) > 0) {
        await markConversationRead(contactId)
      }
    }
  } catch (error) { console.error(error) }
}

async function loadRules() {
  try {
    const res = await dmApi.getRules()
    rules.value = res.data || []
  } catch (error) { console.error(error) }
}

async function loadTemplates() {
  try {
    const res = await dmApi.getTemplates()
    templates.value = res.data || []
  } catch (error) { console.error(error) }
}

async function loadStorageHealth() {
  try {
    const res = await dmApi.getStorageHealth()
    storageHealth.value = res.data || null
  } catch (error) { console.error(error) }
}

async function loadConversations(silent = false) {
  if (hasNoAccounts.value) return
  if (!silent) loadingConversations.value = true
  try {
    const params = {}
    Object.entries(convFilters).forEach(([key, value]) => { if (value) params[key] = value })
    const res = await dmApi.getConversations(params)
    conversations.value = res.data || []
    if (currentContact.value) {
      const latest = conversations.value.find(item => item.id === currentContact.value.id)
      if (latest) currentContact.value = { ...currentContact.value, ...latest }
      else {
        currentContact.value = null
        messages.value = []
        replyText.value = ''
      }
    } else if (conversations.value.length) {
      selectConversation(conversations.value[0], false)
    }
  } catch (error) { console.error(error) }
  finally { if (!silent) loadingConversations.value = false }
}

async function loadLeads() {
  if (hasNoAccounts.value) return
  loadingLeads.value = true
  try {
    const params = {}
    Object.entries(leadFilters).forEach(([key, value]) => { if (value) params[key] = value })
    const res = await dmApi.getLeads(params)
    leads.value = res.data || []
  } catch (error) { console.error(error) }
  finally { loadingLeads.value = false }
}

async function selectConversation(item, markRead = true) {
  currentContact.value = item
  templateId.value = null
  lastMessageId.value = 0
  await loadConversation(item.id)
  if (markRead && Number(item.unread_count || 0) > 0) {
    await markConversationRead(item.id)
  }
}

async function markConversationRead(id) {
  try {
    await dmApi.markConversationRead(id)
    const item = conversations.value.find(conversation => conversation.id === id)
    if (item) item.unread_count = 0
    if (currentContact.value?.id === id) currentContact.value.unread_count = 0
    await loadAnalytics()
  } catch (error) {
    console.error(error)
  }
}

async function loadConversation(id, silent = false, incremental = false) {
  if (!silent) loadingMessages.value = true
  try {
    const params = incremental && lastMessageId.value
      ? { after: lastMessageId.value }
      : undefined
    const res = await dmApi.getConversation(id, params)
    currentContact.value = res.data.contact
    const incoming = res.data.messages || []
    if (incremental && messages.value.length) {
      const byId = new Map(messages.value.map(message => [message.id, message]))
      for (const message of incoming) byId.set(message.id, message)
      messages.value = [...byId.values()].sort((a, b) => (
        String(a.created_at || '').localeCompare(String(b.created_at || ''))
        || Number(a.id || 0) - Number(b.id || 0)
      ))
    } else {
      messages.value = incoming
    }
    if (incoming.length) {
      lastMessageId.value = Math.max(
        lastMessageId.value,
        ...incoming.map(item => Number(item.id || 0)),
      )
    }
    await nextTick()
    scrollToBottom()
  } catch (error) { console.error(error) }
  finally { if (!silent) loadingMessages.value = false }
}

function scrollToBottom() {
  if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight
}

function applyMessageTemplate(id) {
  const template = availableTemplates.value.find(item => item.id === id)
  if (template) replyText.value = template.content
}

async function generateDraft() {
  if (!currentContact.value) return
  drafting.value = true
  try {
    const res = await dmApi.generateDraft(currentContact.value.id)
    replyText.value = res.data.reply || ''
    ElMessage.success('AI 草稿已生成，请核对价格和承诺')
  } catch (error) { console.error(error) }
  finally { drafting.value = false }
}

async function sendReply() {
  if (!currentContact.value || sending.value) return
  const text = replyText.value.trim()
  if (!text) return ElMessage.warning('请先输入回复内容')
  sending.value = true
  replyText.value = ''
  templateId.value = null
  try {
    const res = await dmApi.sendReply(currentContact.value.id, text)
    const status = res.data?.delivery_status
    if (status === 'sent') {
      const message = { '抖音': '回复已发送到抖音', '小红书': '回复已发送到小红书', '视频号': '回复已发送到视频号' }[currentContact.value?.platform]
      ElMessage.success(message || '回复已通过连接器发送')
    }
    else if (status === 'failed') {
      replyText.value = text
      ElMessage.error(res.data?.delivery_error || '回复发送失败')
    }
    else ElMessage.warning('回复已记录为待人工发送，可在消息旁复制')
    await Promise.all([loadConversation(currentContact.value.id), loadConversations(), loadAnalytics()])
  } catch (error) {
    replyText.value = text
    ElMessage.error(error.message || '回复发送失败，内容已恢复')
  }
  finally { sending.value = false }
}

function mediaUrl(path, download = false) {
  const safePath = String(path || '').split('/').map(encodeURIComponent).join('/')
  const query = download ? '?download=1' : ''
  return withToken(`${apiBase}/dm/media/${safePath}${query}`)
}

function mediaOnlyMessage(message) {
  if (!message?.media_type) return false
  return ['[图片]', '[视频]', '[表情]', '[文件]', '[音频]', '[非文本消息]'].includes(message.content)
    || /^该内容为.*无法.*展示$/.test(String(message.content || ''))
}

function mediaTypeLabel(type) {
  return { image: '图片', video: '视频', emotion: '表情', sticker: '表情', file: '文件', audio: '音频' }[type] || '媒体'
}

async function sendMediaFile(event) {
  const file = event?.target?.files?.[0]
  event.target.value = ''
  const config = mediaSendConfig.value
  if (!file || !currentContact.value || !config || sendingMedia.value) return
  const isVideo = file.type.startsWith('video/') || /\.(mp4|mov|m4v)$/i.test(file.name)
  let maxBytes = 25 * 1024 * 1024
  let maxLabel = '25MB'
  if (config.platform === '小红书') {
    maxBytes = isVideo ? 100 * 1024 * 1024 : 10 * 1024 * 1024
    maxLabel = isVideo ? '100MB' : '10MB'
  }
  if (file.size > maxBytes) {
    ElMessage.warning(`${config.platform}私信${isVideo ? '视频' : ''}附件最大 ${maxLabel}`)
    return
  }
  sendingMedia.value = true
  try {
    const upload = await dmApi.uploadMedia(file)
    const res = await dmApi.sendMedia(currentContact.value.id, upload.data)
    const status = res.data?.delivery_status
    if (status === 'sent') ElMessage.success(`媒体已发送到${config.platform}`)
    else ElMessage.error(res.data?.delivery_error || '媒体发送失败')
    await Promise.all([loadConversation(currentContact.value.id), loadConversations(), loadAnalytics()])
  } catch (error) {
    ElMessage.error(error.message || '媒体上传或发送失败')
  } finally {
    sendingMedia.value = false
  }
}

function openProfile(contact) {
  if (!contact) return
  profileForm.value = {
    ...contact,
    nickname: contact.nickname || '',
    segment: contact.segment || 'unknown',
    stage: contact.stage || 'new',
    contact_info: contact.contact_info || '',
    traveler_count: Number(contact.traveler_count || 0),
    origin: contact.origin || '',
    travel_date: contact.travel_date || '',
    company_name: contact.company_name || '',
    budget: contact.budget || '',
    source_content: contact.source_content || '',
    tags: contact.tags || '',
    notes: contact.notes || '',
    auto_reply_enabled: contact.auto_reply_enabled ? 1 : 0,
  }
  profileVisible.value = true
}

async function saveProfile() {
  if (!profileForm.value) return
  savingProfile.value = true
  try {
    await dmApi.updateContact(profileForm.value.id, profileForm.value)
    ElMessage.success('客户资料已保存')
    profileVisible.value = false
    await Promise.all([loadConversation(profileForm.value.id), loadConversations(), loadLeads(), loadAnalytics()])
  } catch (error) { console.error(error) }
  finally { savingProfile.value = false }
}

function openFromLead(row) {
  activeTab.value = 'inbox'
  selectConversation(row)
}

function jumpToPending() {
  activeTab.value = 'inbox'
  convFilters.status = 'pending'
  loadConversations()
}

function jumpToHighIntent() {
  activeTab.value = 'inbox'
  convFilters.status = 'high'
  loadConversations()
}

function handleTabChange(name) {
  if (name === 'leads') loadLeads()
  if (name === 'rules') {
    loadRules()
    loadSettings()
    loadSyncStatus()
  }
}

function openRule(row = null) {
  ruleForm.id = row?.id || null
  ruleForm.name = row?.name || ''
  ruleForm.segment = row?.segment || ''
  ruleForm.reply_mode = row?.reply_mode || 'template'
  ruleForm.ai_prompt = row?.ai_prompt || ''
  ruleForm.keywords = row?.keywords || ''
  ruleForm.match_type = row?.match_type || 'any'
  ruleForm.reply_template = row?.reply_template || ''
  ruleForm.priority = Number(row?.priority ?? 10)
  ruleForm.cooldown_minutes = Number(row?.cooldown_minutes || 0)
  ruleForm.once_per_contact = Boolean(row?.once_per_contact)
  ruleForm.enabled = row ? Boolean(row.enabled) : true
  ruleDialog.value = true
}

async function saveRule() {
  if (!ruleForm.name.trim()) return ElMessage.warning('请填写规则名称')
  if (ruleForm.reply_mode === 'template' && !ruleForm.reply_template.trim()) {
    return ElMessage.warning('请填写回复内容')
  }
  savingRule.value = true
  try {
    const payload = { ...ruleForm }
    await dmApi.saveRule(ruleForm.id, payload)
    ElMessage.success('规则已保存')
    ruleDialog.value = false
    loadRules()
  } catch (error) { console.error(error) }
  finally { savingRule.value = false }
}

async function toggleRule(row) {
  try {
    await dmApi.saveRule(row.id, row)
    ElMessage.success(row.enabled ? '规则已启用' : '规则已停用')
  } catch (error) {
    row.enabled = row.enabled ? 0 : 1
  }
}

async function removeRule(row) {
  try {
    await ElMessageBox.confirm(`确定删除规则「${row.name}」吗？`, '删除规则', { type: 'warning' })
  } catch { return }
  try {
    await dmApi.deleteRule(row.id)
    ElMessage.success('规则已删除')
    loadRules()
  } catch (error) { console.error(error) }
}

async function doImport() {
  const lines = importText.value.split('\n').map(line => line.trim()).filter(Boolean)
  if (!lines.length) return ElMessage.warning('没有可导入的内容')
  const items = []
  const failed = []
  lines.forEach((line, index) => {
    const parts = line.split('|').map(item => item.trim())
    if (parts.length < 4 || !parts[3]) {
      failed.push(index + 1)
      return
    }
    items.push({
      platform: parts[0] || '抖音',
      account: parts[1] || '',
      author: parts[2] || '匿名用户',
      content: parts[3],
      source_content: parts[4] || '',
      external_user_id: parts[5] || parts[2] || '',
      msg_type: 'dm',
    })
  })
  if (!items.length) return ElMessage.warning('请按“平台|账号|用户|留言内容”的格式填写')
  importing.value = true
  try {
    const res = await dmApi.importMessages({ items, auto_reply: importAutoReply.value })
    const data = res.data || {}
    ElMessage.success(`导入 ${data.added || 0} 条${data.duplicates ? `，跳过重复 ${data.duplicates} 条` : ''}`)
    if (failed.length) ElMessage.warning(`第 ${failed.join('、')} 行格式不完整，已跳过`)
    importDialog.value = false
    importText.value = ''
    await Promise.all([loadConversations(), loadAnalytics(), loadLeads()])
  } catch (error) { console.error(error) }
  finally { importing.value = false }
}

function exportLeads() {
  const query = leadFilters.segment ? `?segment=${encodeURIComponent(leadFilters.segment)}` : ''
  window.open(withToken(`${apiBase}/dm/export${query}`), '_blank')
}

useLivePolling(refreshLive, { interval: 1000, isActive: () => activeTab.value === 'inbox' })

onMounted(async () => {
  try {
    await accountStore.loadAccounts()
  } catch (error) {
    console.error('加载账号列表失败:', error)
  }
  if (hasNoAccounts.value) return
  await Promise.all([
    loadSettings(), loadAnalytics(), loadConversations(), loadRules(), loadTemplates(),
    loadSyncStatus(), loadStorageHealth(),
  ])
  // 从「今日优先跟进」跳转时携带 ?contact=<id>，直接打开对应会话。
  const contactId = Number(route.query.contact || 0)
  if (contactId) {
    const target = conversations.value.find(item => Number(item.id) === contactId)
    if (target) {
      activeTab.value = 'inbox'
      await selectConversation(target, false)
    }
  }
})
</script>

<style scoped>
.dm-page { padding: 6px 4px 20px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
.page-head h2 { margin: 0 0 6px; font-size: 22px; color: #303133; }
.sub { margin: 0; color: #909399; font-size: 13px; }
.head-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

.metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 14px; }
.metric-card { background: #fff; border: 1px solid #ebeef5; border-radius: 10px; padding: 15px 17px; display: flex; flex-direction: column; gap: 4px; cursor: pointer; transition: .2s; }
.metric-card:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(64, 158, 255, .1); }
.metric-card span { color: #606266; font-size: 13px; }
.metric-card strong { font-size: 27px; line-height: 1.15; color: #303133; }
.metric-card small { color: #a0a4ab; font-size: 12px; }
.metric-card strong.danger { color: #f56c6c; }
.metric-card strong.success { color: #67c23a; }

.main-tabs { background: transparent; }
.inbox-layout { display: grid; grid-template-columns: 330px minmax(0, 1fr); gap: 14px; height: calc(100vh - 250px); min-height: 700px; }
.conversation-pane, .chat-pane { background: #fff; border: 1px solid #ebeef5; border-radius: 10px; overflow: hidden; }
.conversation-pane { display: flex; flex-direction: column; }
.conversation-filters { padding: 12px; border-bottom: 1px solid #ebeef5; display: flex; flex-direction: column; gap: 8px; }
.filter-row { display: flex; gap: 6px; align-items: center; }
.filter-row .el-select { flex: 1; min-width: 0; }
.filter-row.actions { justify-content: flex-end; }
.conversation-list { flex: 1; overflow-y: auto; }
.conversation-item { position: relative; display: flex; gap: 10px; padding: 12px 13px; border-bottom: 1px solid #f2f4f7; cursor: pointer; transition: .15s; }
.conversation-item:hover { background: #f7faff; }
.conversation-item.active { background: #ecf5ff; box-shadow: inset 3px 0 0 #409eff; }
.conversation-main { min-width: 0; flex: 1; }
.conversation-top { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
.conversation-top strong { font-size: 14px; color: #303133; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conversation-time { flex: none; font-size: 11px; color: #a0a4ab; }
.conversation-id { margin-top: 3px; color: #8a8f98; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conversation-tags { display: flex; gap: 4px; margin: 4px 0; flex-wrap: wrap; }
.conversation-preview { color: #7a7f87; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.reply-mark { color: #409eff; }
.conv-badge { position: absolute; right: 12px; bottom: 12px; }
.conv-badge :deep(.el-badge__content) { transform: none; }

.chat-pane { display: flex; flex-direction: column; }
.chat-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 13px 16px; border-bottom: 1px solid #ebeef5; }
.chat-user { display: flex; align-items: center; gap: 10px; min-width: 0; }
.chat-name { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-weight: 600; color: #303133; }
.chat-sub { color: #909399; font-size: 12px; margin-top: 3px; }
.chat-actions { display: flex; gap: 6px; flex: none; }
.message-list { flex: 1; overflow-y: auto; padding: 18px; background: #f7f8fa; display: flex; flex-direction: column; gap: 12px; }
.message-row { display: flex; }
.message-row.inbound { justify-content: flex-start; }
.message-row.outbound { justify-content: flex-end; }
.message-bubble { max-width: 74%; background: #fff; border: 1px solid #e9ecf2; padding: 10px 12px; border-radius: 10px; box-shadow: 0 1px 2px rgba(0,0,0,.03); }
.message-row.outbound .message-bubble { background: #e8f3ff; border-color: #cfe5ff; }
.message-text { white-space: pre-wrap; word-break: break-word; line-height: 1.65; color: #303133; font-size: 14px; }
.message-media { margin-top: 4px; }
.message-image { display: block; max-width: min(360px, 58vw); max-height: 320px; border-radius: 8px; cursor: zoom-in; }
.message-video { display: block; width: min(390px, 60vw); max-height: 360px; border-radius: 8px; background: #111; }
.media-fallback { padding: 8px 2px; }
.media-note { margin-top: 4px; color: #909399; font-size: 12px; }
.media-actions { margin-top: 5px; font-size: 12px; }
.media-actions a { color: #409eff; text-decoration: none; }
.composer-send-actions { display: flex; align-items: center; gap: 8px; }
.hidden-file-input { display: none; }
.message-meta { display: flex; align-items: center; gap: 6px; margin-top: 6px; color: #a0a4ab; font-size: 11px; flex-wrap: wrap; }
.message-source { margin-top: 6px; color: #909399; font-size: 11px; }
.message-error { margin-top: 6px; color: #f56c6c; font-size: 11px; line-height: 1.5; word-break: break-word; }
.composer { border-top: 1px solid #ebeef5; padding: 12px 14px; background: #fff; }
.composer-tools { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.composer-tools .el-select { width: 180px; }
.composer-footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 8px; }
.connector-note { color: #909399; font-size: 12px; }

.rule-layout { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(360px, .9fr); gap: 14px; margin-bottom: 14px; }
.settings-card, .ingress-card, .rules-card, .sync-card { border-radius: 10px; }
.card-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.card-sub { margin-left: 8px; color: #a0a4ab; font-size: 12px; font-weight: 400; }
.settings-form { padding-top: 4px; }
.settings-form :deep(.el-form-item) { margin-bottom: 13px; }
.form-hint { margin-left: 8px; color: #a0a4ab; font-size: 12px; line-height: 1.4; }
.time-range { display: flex; align-items: center; gap: 8px; }
.time-range .el-date-editor { width: 120px; }
.ingress-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ingress-label { color: #606266; font-size: 13px; }
.webhook-url { background: #f5f7fa; border: 1px solid #ebeef5; padding: 5px 8px; border-radius: 5px; font-size: 12px; color: #409eff; word-break: break-all; }
.ingress-tip { color: #606266; font-size: 13px; line-height: 1.7; margin: 12px 0; }
.ingress-tip code { background: #f5f7fa; padding: 1px 4px; border-radius: 3px; }
.delivery-summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 14px 0; }
.delivery-summary div { background: #f7f8fa; border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 3px; }
.delivery-summary span { color: #909399; font-size: 12px; }
.delivery-summary strong { font-size: 20px; }
.delivery-summary strong.success { color: #67c23a; }
.delivery-summary strong.warning { color: #e6a23c; }
.delivery-summary strong.danger { color: #f56c6c; }
.ingress-actions { display: flex; gap: 8px; margin-top: 14px; }
.sync-card { margin-bottom: 14px; }
.sync-summary { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 4px 0 10px; }
.sync-summary div { background: #f7f8fa; border-radius: 8px; padding: 9px 10px; display: flex; flex-direction: column; gap: 2px; }
.sync-summary span { color: #909399; font-size: 11px; }
.sync-summary strong { font-size: 18px; }
.sync-summary strong.success { color: #67c23a; }
.sync-meta { display: flex; flex-wrap: wrap; gap: 6px 14px; color: #909399; font-size: 12px; margin-bottom: 10px; }
.sync-range { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sync-range .el-input-number { width: 108px; }
.sync-note { margin-top: 10px; }
.lead-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.muted { color: #a0a4ab; }

.simulate-result { border: 1px solid #ebeef5; border-radius: 8px; background: #f8fafc; padding: 12px; }
.simulate-tags { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 9px; }
.simulate-reply { color: #303133; line-height: 1.7; white-space: pre-wrap; }
.simulate-fields { margin-top: 8px; color: #909399; font-size: 12px; }
.dialog-hint { color: #909399; font-size: 12px; margin: 0 0 10px; }
.dialog-hint code { background: #f5f7fa; padding: 2px 5px; border-radius: 3px; }
.import-option { display: flex; align-items: center; gap: 8px; margin-top: 12px; color: #606266; font-size: 13px; }
.token-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 7px; }
.profile-meta { display: flex; gap: 12px; align-items: center; padding: 4px 0 16px; border-bottom: 1px solid #ebeef5; margin-bottom: 16px; }
.profile-meta strong { display: block; margin-bottom: 4px; }
.profile-form :deep(.el-form-item) { margin-bottom: 13px; }
.profile-form .el-select { width: 100%; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.drawer-actions { display: flex; justify-content: flex-end; gap: 8px; padding-top: 14px; border-top: 1px solid #ebeef5; }
.help-section { margin-bottom: 18px; }
.help-section h4 { margin: 0 0 8px; color: #303133; }
.help-section p, .help-section li { color: #606266; font-size: 13px; line-height: 1.8; }
.help-section ul { padding-left: 20px; margin: 6px 0; }
.code-block { background: #1f2937; color: #dce6f3; padding: 12px; border-radius: 8px; font-size: 12px; line-height: 1.65; overflow-x: auto; white-space: pre-wrap; word-break: break-all; }

@media (max-width: 1200px) {
  .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .rule-layout { grid-template-columns: 1fr; }
  .inbox-layout { grid-template-columns: 290px minmax(0, 1fr); }
}

@media (max-width: 860px) {
  .page-head { flex-direction: column; }
  .inbox-layout { grid-template-columns: 1fr; height: auto; }
  .conversation-pane { max-height: 380px; }
  .chat-pane { min-height: 620px; }
  .message-bubble { max-width: 90%; }
  .metric-grid { grid-template-columns: 1fr 1fr; }
}
</style>
