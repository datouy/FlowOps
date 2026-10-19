<template>
  <div class="dm-page">
    <div class="page-head">
      <div>
        <h2>私信自动回复</h2>
        <p class="sub">统一收件箱 · 银发/亲子/团建客群识别 · 规则自动承接 · 线索沉淀</p>
      </div>
      <div class="head-actions">
        <el-tag :type="settings.enabled ? 'success' : 'info'" effect="dark" round>
          {{ settings.enabled ? '自动回复运行中' : '自动回复已暂停' }}
        </el-tag>
        <el-tag :type="syncTagType" effect="plain" round>{{ syncTagText }}</el-tag>
        <el-button @click="helpDialog = true">接入说明</el-button>
        <el-button @click="openSimulate">模拟来信</el-button>
        <el-button :loading="syncing" @click="runDouyinSync">同步抖音私信</el-button>
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
        <small>价格 / 行程 / 报名 / 联系方式</small>
      </div>
      <div class="metric-card">
        <span>今日自动回复</span>
        <strong class="success">{{ analytics.today_auto || 0 }}</strong>
        <small>已发送 {{ analytics.sent || 0 }} · 待发送 {{ analytics.ready || 0 }}</small>
      </div>
      <div class="metric-card">
        <span>已沉淀线索</span>
        <strong>{{ analytics.qualified || 0 }}</strong>
        <small>银发 {{ analytics.silver || 0 }} · 亲子 {{ analytics.family || 0 }} · 团建 {{ analytics.team || 0 }}</small>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="main-tabs" @tab-change="handleTabChange">
      <el-tab-pane label="会话工作台" name="inbox">
        <div class="inbox-layout">
          <aside class="conversation-pane">
            <div class="conversation-filters">
              <el-input v-model="convFilters.q" clearable placeholder="搜索昵称 / 公司 / 标签 / 内容" @keyup.enter="loadConversations">
                <template #prefix>搜</template>
              </el-input>
              <div class="filter-row">
                <el-select v-model="convFilters.platform" clearable placeholder="全部平台" @change="loadConversations">
                  <el-option v-for="p in platforms" :key="p" :label="p" :value="p" />
                </el-select>
                <el-select v-model="convFilters.segment" clearable placeholder="全部客群" @change="loadConversations">
                  <el-option label="银发" value="silver" />
                  <el-option label="亲子" value="family" />
                  <el-option label="企业团建" value="team" />
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
                    <strong>{{ item.nickname || '匿名用户' }}</strong>
                    <span class="conversation-time">{{ shortTime(item.last_message_time) }}</span>
                  </div>
                  <div class="conversation-tags">
                    <el-tag size="small" effect="plain">{{ item.platform }}</el-tag>
                    <el-tag size="small" :type="segmentTagType(item.segment)" effect="light">{{ item.segment_label }}</el-tag>
                    <el-tag v-if="item.intent === 'high'" size="small" type="danger" effect="light">高意向</el-tag>
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
                      {{ currentContact.nickname || '匿名用户' }}
                      <el-tag size="small" :type="segmentTagType(currentContact.segment)">{{ currentContact.segment_label }}</el-tag>
                      <el-tag v-if="currentContact.intent === 'high'" size="small" type="danger">高意向</el-tag>
                    </div>
                    <div class="chat-sub">{{ currentContact.platform }} · {{ currentContact.account || '未填账号' }} · {{ currentContact.stage_label }}</div>
                  </div>
                </div>
                <div class="chat-actions">
                  <el-button size="small" @click="loadConversation(currentContact.id)">刷新</el-button>
                  <el-button size="small" type="primary" plain @click="openProfile(currentContact)">客户资料</el-button>
                </div>
              </div>

              <div ref="messageList" v-loading="loadingMessages" class="message-list">
                <div v-for="(msg, index) in messages" :key="msg.id || index" class="message-row" :class="msg.direction">
                  <div class="message-bubble">
                    <div class="message-text">{{ msg.content }}</div>
                    <div class="message-meta">
                      <span>{{ shortDateTime(msg.created_at) }}</span>
                      <el-tag v-if="msg.direction === 'outbound' && msg.auto_reply_status === 'auto'" size="small" effect="plain">自动</el-tag>
                      <el-tag v-if="msg.direction === 'outbound'" size="small" :type="deliveryTagType(msg.delivery_status)">
                        {{ deliveryText(msg.delivery_status) }}
                      </el-tag>
                      <el-button v-if="msg.direction === 'outbound' && msg.delivery_status !== 'sent'" link size="small" @click="copyText(msg.content, '回复已复制')">复制</el-button>
                    </div>
                    <div v-if="msg.source_content" class="message-source">来源内容：{{ msg.source_content }}</div>
                  </div>
                </div>
                <el-empty v-if="!loadingMessages && !messages.length" description="还没有消息" :image-size="70" />
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
                  placeholder="回复客户。Ctrl + Enter 发送；价格、余位和服务承诺请先核对。"
                  @keydown.ctrl.enter.prevent="sendReply"
                />
                <div class="composer-footer">
                  <span class="connector-note">{{ connectorNote }}</span>
                  <el-button type="primary" :loading="sending" @click="sendReply">发送并记录</el-button>
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
              <el-form-item label="转人工词">
                <el-input v-model="autoForm.stop_keywords" placeholder="人工,转人工,投诉" />
              </el-form-item>
              <el-form-item label="发送连接器">
                <el-input v-model="autoForm.outbound_webhook" placeholder="https://你的发送服务/dm/reply" />
                <span class="form-hint">抖音使用本地登录会话直接发送；其他平台可填写发送连接器</span>
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
                  <span>抖音私信本地同步</span>
                  <span class="card-sub">Playwright 只读适配器</span>
                </div>
                <el-tag size="small" effect="plain" type="success">只读</el-tag>
              </div>
            </template>
            <el-form label-width="92px" class="settings-form">
              <el-form-item label="自动同步">
                <el-switch v-model="syncForm.enabled" />
                <span class="form-hint">开启后按间隔读取创作者中心私信列表</span>
              </el-form-item>
              <el-form-item label="抖音账号">
                <el-select v-model="syncForm.account_id" style="width: 100%">
                  <el-option :value="0" label="自动选择最近登录的抖音账号" />
                  <el-option
                    v-for="account in syncAccounts"
                    :key="account.id"
                    :value="account.id"
                    :label="account.name + (account.has_cookie ? '' : '（Cookie 缺失）')"
                    :disabled="!account.has_cookie"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="同步间隔">
                <el-input-number v-model="syncForm.interval_seconds" :min="5" :max="3600" :step="5" controls-position="right" />
                <span class="form-hint">秒，最短 5 秒</span>
              </el-form-item>
              <el-form-item label="同步范围">
                <el-switch v-model="syncForm.recent_only" />
                <span class="form-hint">只处理近 {{ syncStatus.recent_window_hours || 48 }} 小时有更新的会话</span>
              </el-form-item>
              <el-form-item>
                <el-button :loading="savingSync" @click="saveSyncSettings">保存同步设置</el-button>
                <el-button type="primary" :loading="syncing" @click="runDouyinSync">立即同步</el-button>
              </el-form-item>
            </el-form>
            <div class="sync-summary">
              <div><span>扫描会话</span><strong>{{ syncStatus.last_scanned || 0 }}</strong></div>
              <div><span>新增私信</span><strong class="success">{{ syncStatus.last_imported || 0 }}</strong></div>
              <div><span>重复跳过</span><strong>{{ syncStatus.last_duplicates || 0 }}</strong></div>
              <div><span>条件跳过</span><strong>{{ syncStatus.last_skipped || 0 }}</strong></div>
            </div>
            <div class="sync-meta">
              <span>账号：{{ syncSelectedAccount?.name || '未选择' }}</span>
              <span>上次成功：{{ syncStatus.last_success_at || '尚未同步' }}</span>
              <span>下次计划：{{ syncStatus.next_run_at || '未安排' }}</span>
              <span v-if="syncSkipSummary">本次跳过：{{ syncSkipSummary }}</span>
            </div>
            <el-alert
              v-if="syncStatus.last_error"
              title="最近一次同步失败"
              type="error"
              :closable="false"
              show-icon
              :description="syncStatus.last_error"
            />
            <el-alert
              class="sync-note"
              title="抖音同步为只读，回复通过本地登录会话直发"
              type="info"
              :closable="false"
              show-icon
              description="同步时不会点开会话或标记已读；手动回复和启用发送的规则回复会打开对应会话并点击平台发送。适配器依赖抖音创作者中心网页版结构，平台改版时可能需要维护。"
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
              title="抖音支持通过已登录网页会话直接发送"
              type="warning"
              :closable="false"
              show-icon
              description="抖音回复会直接发送到平台并校验发送结果；其他平台仍可使用自有发送连接器。发送失败时不会误标为已发送。"
            />
            <div class="ingress-actions">
              <el-button @click="helpDialog = true">查看接入格式</el-button>
              <el-button @click="openSimulate">测试一条来信</el-button>
            </div>
          </el-card>
        </div>

        <el-card shadow="never" class="rules-card">
          <template #header>
            <div class="card-head">
              <div>
                <span>回复规则</span>
                <span class="card-sub">按优先级从高到低匹配；未命中时走“首次承接”或 AI 兜底</span>
              </div>
              <div>
                <el-button size="small" @click="openSimulate">测试规则</el-button>
                <el-button size="small" type="primary" @click="openRule()">新建规则</el-button>
              </div>
            </div>
          </template>
          <el-table :data="rules" border stripe size="small">
            <el-table-column prop="name" label="规则" min-width="170" show-overflow-tooltip />
            <el-table-column label="客群" width="82">
              <template #default="scope"><el-tag size="small" :type="segmentTagType(scope.row.segment)">{{ scope.row.segment_label }}</el-tag></template>
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
                <el-select v-model="leadFilters.segment" clearable placeholder="全部客群" @change="loadLeads">
                  <el-option label="银发" value="silver" />
                  <el-option label="亲子" value="family" />
                  <el-option label="企业团建" value="team" />
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
            <el-table-column label="客群 / 意向" width="128">
              <template #default="scope">
                <el-tag size="small" :type="segmentTagType(scope.row.segment)">{{ scope.row.segment_label }}</el-tag>
                <el-tag v-if="scope.row.intent === 'high'" size="small" type="danger" style="margin-left:4px">高</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="stage_label" label="阶段" width="88" />
            <el-table-column prop="contact_info" label="联系方式" width="130" show-overflow-tooltip>
              <template #default="scope">{{ scope.row.contact_info || '未留资' }}</template>
            </el-table-column>
            <el-table-column label="线索信息" min-width="230">
              <template #default="scope">
                <template v-if="scope.row.segment === 'team' && (scope.row.company_name || scope.row.origin || scope.row.traveler_count || scope.row.budget || scope.row.travel_date)">
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

    <el-dialog v-model="simulateDialog" title="模拟一条私信" width="660px">
      <el-form label-width="78px">
        <el-form-item label="平台">
          <el-select v-model="simulateForm.platform" style="width: 180px">
            <el-option v-for="p in platforms" :key="p" :label="p" :value="p" />
          </el-select>
        </el-form-item>
        <el-form-item label="昵称"><el-input v-model="simulateForm.nickname" placeholder="例如：王姐" /></el-form-item>
        <el-form-item label="来信内容">
          <el-input v-model="simulateForm.content" type="textarea" :rows="4" placeholder="例如：公司团建30人，从上海出发，预算3万，12月中旬，要方案和报价" @input="simulateResult = null" />
          <span class="form-hint">也支持测试：带爸妈去桂林，3个人从杭州出发，10月20日，多少钱？</span>
        </el-form-item>
      </el-form>
      <div v-if="simulateResult" class="simulate-result">
        <div class="simulate-tags">
          <el-tag :type="segmentTagType(simulateResult.segment)">{{ simulateResult.segment_label }}</el-tag>
          <el-tag :type="simulateResult.intent === 'high' ? 'danger' : 'warning'">{{ intentText(simulateResult.intent) }}</el-tag>
          <el-tag effect="plain">{{ simulateResult.rule ? ('命中：' + simulateResult.rule.name) : '未命中规则' }}</el-tag>
        </div>
        <div class="simulate-reply">{{ simulateResult.reply || '没有生成回复。请检查规则或 AI 兜底。' }}</div>
        <div v-if="simulateResult.contacts && simulateResult.segment === 'team'" class="simulate-fields">
          识别字段：{{ simulateResult.contacts.company_name || '公司待确认' }} · {{ simulateResult.contacts.traveler_count ? simulateResult.contacts.traveler_count + '人' : '人数待确认' }} · {{ simulateResult.contacts.origin || '出发地待确认' }} · {{ simulateResult.contacts.budget || '预算待确认' }} · {{ simulateResult.contacts.travel_date || '档期待确认' }}
        </div>
        <div v-else-if="simulateResult.contacts" class="simulate-fields">
          识别字段：{{ simulateResult.contacts.origin || '出发地待确认' }} · {{ simulateResult.contacts.traveler_count || '人数待确认' }} · {{ simulateResult.contacts.travel_date || '日期待确认' }}
        </div>
      </div>
      <template #footer>
        <el-button @click="simulateDialog = false">关闭</el-button>
        <el-button :loading="testing" @click="testSimulate">测试匹配</el-button>
        <el-button type="primary" :loading="writingSimulate" @click="writeSimulate">写入收件箱并回复</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="importDialog" title="批量导入消息" width="620px">
      <p class="dialog-hint">每行一条：<code>平台|账号|用户|留言内容|来源内容(可选)|用户ID(可选)</code></p>
      <el-input v-model="importText" type="textarea" :rows="8" placeholder="抖音|陆大头|小明|桂林五日游多少钱？&#10;小红书|亲子号|豆豆妈|3岁宝宝桂林怎么玩？|亲子游笔记" />
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
        <el-form-item label="规则名称"><el-input v-model="ruleForm.name" placeholder="例如：银发 · 价格问询" /></el-form-item>
        <el-form-item label="适用客群">
          <el-select v-model="ruleForm.segment" style="width: 180px">
            <el-option label="通用" value="" />
            <el-option label="银发" value="silver" />
            <el-option label="亲子" value="family" />
            <el-option label="企业团建" value="team" />
          </el-select>
          <span class="form-hint">具体客群规则会优先于通用规则</span>
        </el-form-item>
        <el-form-item label="关键词">
          <el-input v-model="ruleForm.keywords" type="textarea" :rows="3" placeholder="用逗号或换行分隔，例如：多少钱,价格,报价" />
          <span class="form-hint">留空表示该客群的所有来信都匹配</span>
        </el-form-item>
        <el-form-item label="匹配方式">
          <el-radio-group v-model="ruleForm.match_type">
            <el-radio label="any">命中任一关键词</el-radio>
            <el-radio label="all">命中全部关键词</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="回复内容">
          <el-input v-model="ruleForm.reply_template" type="textarea" :rows="6" placeholder="支持变量：{greeting} {nickname} {origin} {travelers} {travel_date} {company} {budget}" />
          <div class="token-row">
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
            <el-form-item label="客群">
              <el-select v-model="profileForm.segment">
                <el-option label="银发" value="silver" />
                <el-option label="亲子" value="family" />
                <el-option label="企业团建" value="team" />
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
  "content": "带爸妈去桂林，3个人从杭州出发，10月20日，多少钱？",
  "source_content": "银发慢游视频"
}</pre>
        <el-button size="small" @click="copyText(webhookExample, '请求示例已复制')">复制请求示例</el-button>
      </div>
      <div class="help-section">
        <h4>2. 自动回复如何落地</h4>
        <p>系统先识别客群和意向，再按关键词与优先级匹配规则。命中后生成一条回复：</p>
        <ul>
          <li>抖音会根据客户昵称打开对应会话，通过当前已登录账号直接点击发送。</li>
          <li>发送成功后才标记为“已发送”；登录失效或平台限制会明确显示失败原因。</li>
          <li>其他平台可以继续配置发送连接器。</li>
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
import { ElMessage, ElMessageBox } from 'element-plus'
import { http } from '@/utils/request'

const requestBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'
const apiBase = requestBase.startsWith('http')
  ? requestBase.replace(/\/$/, '')
  : (/^(localhost|127\.0\.0\.1)$/.test(window.location.hostname)
      ? 'http://127.0.0.1:5409'
      : `${window.location.origin}${requestBase.replace(/\/$/, '')}`)
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
const autoForm = reactive({
  enabled: false, reply_comments: false, weekdayList: ['1', '2', '3', '4', '5', '6', '7'],
  working_start: '08:00', working_end: '23:00', cooldown_minutes: 30, max_per_day: 3,
  ai_fallback: false, stop_keywords: '人工,转人工,投诉,退款', outbound_webhook: '', inbound_secret: '',
})
const savingSettings = ref(false)
const syncStatus = ref({ enabled: 0, running: false, accounts: [] })
const syncForm = reactive({ enabled: false, account_id: 0, interval_seconds: 5, recent_only: true })
const savingSync = ref(false)
const syncing = ref(false)
const rules = ref([])
const conversations = ref([])
const convFilters = reactive({ q: '', platform: '', segment: '', status: '' })
const loadingConversations = ref(false)
const currentContact = ref(null)
const messages = ref([])
const loadingMessages = ref(false)
const messageList = ref(null)
const templates = ref([])
const templateId = ref(null)
const replyText = ref('')
const drafting = ref(false)
const sending = ref(false)
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
  reply_template: '', priority: 10, cooldown_minutes: 0,
  once_per_contact: false, enabled: true,
})
const simulateDialog = ref(false)
const simulateForm = reactive({ platform: '抖音', nickname: '王姐', content: '' })
const simulateResult = ref(null)
const testing = ref(false)
const writingSimulate = ref(false)
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
  "content": "带爸妈去桂林，3个人从杭州出发，10月20日，多少钱？",
  "source_content": "银发慢游视频"
}`)
const connectorNote = computed(() => currentContact.value?.platform === '抖音'
  ? '抖音将通过当前登录账号直接发送并校验结果'
  : (autoForm.outbound_webhook
      ? '已配置发送连接器，发送结果会回写到消息状态'
      : '未配置发送连接器：发送后保存为“待人工发送”，可在消息旁复制回复'))
const syncAccounts = computed(() => syncStatus.value.accounts || [])
const syncSelectedAccount = computed(() => syncStatus.value.selected_account || null)
const syncSkipSummary = computed(() => Object.entries(syncStatus.value.last_skip_reasons || {})
  .map(([reason, count]) => `${reason} ${count}`).join(' · '))
const syncTagText = computed(() => {
  if (syncStatus.value.last_error) return '抖音同步异常'
  if (syncing.value || syncStatus.value.running) return '抖音同步中'
  return syncStatus.value.enabled ? '抖音定时同步已开启' : '抖音定时同步已关闭'
})
const syncTagType = computed(() => {
  if (syncStatus.value.last_error) return 'danger'
  if (syncing.value || syncStatus.value.running) return 'warning'
  return syncStatus.value.enabled ? 'success' : 'info'
})
const availableTemplates = computed(() => {
  const segment = currentContact.value?.segment
  if (!segment || segment === 'unknown') return templates.value.filter(t => !t.segment)
  return templates.value.filter(t => !t.segment || t.segment === segment)
})
const quickReplies = computed(() => {
  const segment = currentContact.value?.segment
  if (segment === 'silver') {
    return [
      { label: '问清三件事', text: '叔叔阿姨这边几位出行、从哪出发、大概几号走？我先按这三点发您合适的银发行程。' },
      { label: '发避坑清单', text: '给您发一份带爸妈报团自查清单：看是否赶早、每天车程、购物店数量、酒店电梯和无障碍、意外险和药箱。' },
      { label: '说明慢游标准', text: '我们这条银发线不赶早、不进购物店，每天步行和车程会提前写清楚。合适的话我再把详细行程发您。' },
    ]
  }
  if (segment === 'family') {
    return [
      { label: '问孩子年龄', text: '孩子今年几岁啦？不同年龄适合的线路和儿童餐、亲子房安排不一样，我按年龄给您发合适的。' },
      { label: '问清出行信息', text: '您这边几位大人、几位小朋友，从哪出发、大概几号走？我按人数和日期给您算准价。' },
      { label: '说明带娃安排', text: '亲子线会控制单日车程，安排儿童餐和领队带玩，大人也留自由时间。您把出行信息发我，我发详细行程。' },
    ]
  }
  if (segment === 'team') {
    return [
      { label: '问清四项', text: '方便说一下大概多少人、从哪出发、预算范围、想几月走吗？我再确认是否过夜，按这几项给你发3套适配方案，不硬推。' },
      { label: '发方案和明细', text: '可以。我按人数、出发地、预算和日期筛3套方案；车、餐、保险、领队和物料是否全含会列在明细里，方便你直接对比。' },
      { label: '发避坑清单', text: '给你一份企业团建自查：车餐保险领队是否全含、有没有隐形消费、强度是否适合全员、雨天备用方案、合同发票和意外险谁负责。' },
    ]
  }
  return [
    { label: '问清三件事', text: '方便说一下几位出行、从哪出发、大概几号走吗？带爸妈还是带孩子？我按您的情况发合适线路，不硬推。' },
    { label: '发线路资料', text: '可以的，我先按您的出行人数、出发城市和日期找一版最合适的行程，您看合适再聊价格。' },
  ]
})

function segmentTagType(segment) {
  if (segment === 'silver') return 'warning'
  if (segment === 'family') return 'success'
  if (segment === 'team') return 'primary'
  return 'info'
}

function intentText(intent) {
  return { high: '高意向', medium: '中意向', low: '低意向' }[intent] || intent || '未识别'
}

function deliveryText(status) {
  return { sent: '已发送', ready: '待发送', pending: '发送中', failed: '失败', received: '已接收' }[status] || status || ''
}

function deliveryTagType(status) {
  return { sent: 'success', ready: 'warning', pending: 'info', failed: 'danger' }[status] || 'info'
}

function avatarText(name) {
  return String(name || '客').trim().slice(0, 1).toUpperCase()
}

function shortTime(value) {
  if (!value) return ''
  const text = String(value)
  const today = new Date().toISOString().slice(0, 10)
  if (text.slice(0, 10) === today) return text.slice(11, 16)
  return text.slice(5, 10)
}

function shortDateTime(value) {
  if (!value) return ''
  const text = String(value)
  return text.slice(0, 16)
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
    const res = await http.get('/dm/analytics')
    analytics.value = res.data || {}
  } catch (error) { console.error(error) }
}

async function loadSettings() {
  try {
    const res = await http.get('/dm/settings')
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
    autoForm.stop_keywords = data.stop_keywords || ''
    autoForm.outbound_webhook = data.outbound_webhook || ''
    autoForm.inbound_secret = data.inbound_secret || ''
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
    }
    delete payload.weekdayList
    const res = await http.put('/dm/settings', payload)
    settings.value = res.data || settings.value
    ElMessage.success('自动回复设置已保存')
    loadAnalytics()
  } catch (error) { console.error(error) }
  finally { savingSettings.value = false }
}

async function loadSyncStatus() {
  try {
    const res = await http.get('/dm/sync/status')
    const data = res.data || {}
    syncStatus.value = data
    syncForm.enabled = Boolean(data.enabled)
    syncForm.account_id = Number(data.account_id || 0)
    syncForm.interval_seconds = Number(data.interval_seconds || 5)
    syncForm.recent_only = Boolean(data.recent_only)
  } catch (error) { console.error(error) }
}

async function saveSyncSettings() {
  savingSync.value = true
  try {
    const res = await http.put('/dm/sync/settings', {
      enabled: syncForm.enabled ? 1 : 0,
      account_id: Number(syncForm.account_id || 0),
      interval_seconds: Number(syncForm.interval_seconds || 5),
      recent_only: syncForm.recent_only ? 1 : 0,
      max_conversations: Number(syncStatus.value.max_conversations || 20),
    })
    syncStatus.value = res.data || syncStatus.value
    ElMessage.success('抖音同步设置已保存')
  } catch (error) { console.error(error) }
  finally { savingSync.value = false }
}

async function runDouyinSync() {
  if (syncing.value) return
  syncing.value = true
  try {
    const res = await http.post('/dm/sync/run', {}, { timeout: 120000 })
    const data = res.data || {}
    ElMessage.success(`同步完成：扫描 ${data.scanned || 0}，新增 ${data.imported || 0}，重复 ${data.duplicates || 0}`)
    await Promise.all([loadSyncStatus(), loadConversations(), loadAnalytics(), loadLeads()])
    if (currentContact.value?.id) await loadConversation(currentContact.value.id)
  } catch (error) {
    console.error(error)
    await loadSyncStatus()
  } finally { syncing.value = false }
}

async function loadRules() {
  try {
    const res = await http.get('/dm/rules')
    rules.value = res.data || []
  } catch (error) { console.error(error) }
}

async function loadTemplates() {
  try {
    const res = await http.get('/message-templates')
    templates.value = res.data || []
  } catch (error) { console.error(error) }
}

async function loadConversations() {
  loadingConversations.value = true
  try {
    const params = {}
    Object.entries(convFilters).forEach(([key, value]) => { if (value) params[key] = value })
    const res = await http.get('/dm/conversations', params)
    conversations.value = res.data || []
    if (currentContact.value) {
      const latest = conversations.value.find(item => item.id === currentContact.value.id)
      if (latest) currentContact.value = { ...currentContact.value, ...latest }
    } else if (conversations.value.length) {
      selectConversation(conversations.value[0])
    }
  } catch (error) { console.error(error) }
  finally { loadingConversations.value = false }
}

async function loadLeads() {
  loadingLeads.value = true
  try {
    const params = {}
    Object.entries(leadFilters).forEach(([key, value]) => { if (value) params[key] = value })
    const res = await http.get('/dm/conversations', params)
    leads.value = res.data || []
  } catch (error) { console.error(error) }
  finally { loadingLeads.value = false }
}

async function selectConversation(item) {
  currentContact.value = item
  templateId.value = null
  await loadConversation(item.id)
}

async function loadConversation(id) {
  loadingMessages.value = true
  try {
    const res = await http.get(`/dm/conversations/${id}`)
    currentContact.value = res.data.contact
    messages.value = res.data.messages || []
    await nextTick()
    scrollToBottom()
  } catch (error) { console.error(error) }
  finally { loadingMessages.value = false }
}

function scrollToBottom() {
  if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight
}

function applyMessageTemplate(id) {
  const template = templates.value.find(item => item.id === id)
  if (template) replyText.value = template.content
}

async function generateDraft() {
  if (!currentContact.value) return
  drafting.value = true
  try {
    const res = await http.post(`/dm/conversations/${currentContact.value.id}/ai-draft`)
    replyText.value = res.data.reply || ''
    ElMessage.success('AI 草稿已生成，请核对价格和承诺')
  } catch (error) { console.error(error) }
  finally { drafting.value = false }
}

async function sendReply() {
  if (!currentContact.value) return
  const text = replyText.value.trim()
  if (!text) return ElMessage.warning('请先输入回复内容')
  sending.value = true
  try {
    const res = await http.post(`/dm/conversations/${currentContact.value.id}/reply`, { text })
    const status = res.data?.delivery_status
    if (status === 'sent') ElMessage.success(currentContact.value?.platform === '抖音' ? '回复已发送到抖音' : '回复已通过连接器发送')
    else if (status === 'failed') ElMessage.error(res.data?.delivery_error || '回复发送失败')
    else ElMessage.warning('回复已记录为待人工发送，可在消息旁复制')
    replyText.value = ''
    templateId.value = null
    await Promise.all([loadConversation(currentContact.value.id), loadConversations(), loadAnalytics()])
  } catch (error) { console.error(error) }
  finally { sending.value = false }
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
    await http.put(`/dm/contacts/${profileForm.value.id}`, profileForm.value)
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
  if (!ruleForm.reply_template.trim()) return ElMessage.warning('请填写回复内容')
  savingRule.value = true
  try {
    const payload = { ...ruleForm }
    if (ruleForm.id) await http.put(`/dm/rules/${ruleForm.id}`, payload)
    else await http.post('/dm/rules', payload)
    ElMessage.success('规则已保存')
    ruleDialog.value = false
    loadRules()
  } catch (error) { console.error(error) }
  finally { savingRule.value = false }
}

async function toggleRule(row) {
  try {
    await http.put(`/dm/rules/${row.id}`, row)
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
    await http.delete(`/dm/rules/${row.id}`)
    ElMessage.success('规则已删除')
    loadRules()
  } catch (error) { console.error(error) }
}

function openSimulate() {
  simulateForm.platform = '抖音'
  simulateForm.nickname = '王姐'
  simulateForm.content = ''
  simulateResult.value = null
  simulateDialog.value = true
}

async function testSimulate() {
  if (!simulateForm.content.trim()) return ElMessage.warning('请先输入一条客户来信')
  testing.value = true
  try {
    const res = await http.post('/dm/simulate', simulateForm)
    simulateResult.value = res.data
  } catch (error) { console.error(error) }
  finally { testing.value = false }
}

async function writeSimulate() {
  if (!simulateForm.content.trim()) return ElMessage.warning('请先输入一条客户来信')
  writingSimulate.value = true
  try {
    const now = Date.now()
    const res = await http.post('/dm/inbound', {
      platform: simulateForm.platform,
      account: '规则测试账号',
      external_user_id: `sim-user-${now}`,
      external_message_id: `sim-msg-${now}`,
      author: simulateForm.nickname || '模拟客户',
      msg_type: 'dm',
      content: simulateForm.content,
      source_content: '规则测试',
      auto_reply: true,
    })
    const auto = res.data?.auto_reply
    if (auto?.status === 'sent') ElMessage.success('已写入收件箱，并标记为已发送')
    else if (auto?.status === 'skipped') ElMessage.warning('已写入收件箱，但未自动回复：' + (auto.reason || '未命中规则'))
    else ElMessage.success('已写入收件箱，回复为待人工发送')
    simulateDialog.value = false
    await Promise.all([loadConversations(), loadAnalytics(), loadLeads()])
    const conversation = conversations.value.find(item => item.id === res.data.contact_id)
    if (conversation) {
      activeTab.value = 'inbox'
      selectConversation(conversation)
    }
  } catch (error) { console.error(error) }
  finally { writingSimulate.value = false }
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
    const res = await http.post('/dm/import', { items, auto_reply: importAutoReply.value })
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
  window.open(`${apiBase}/dm/export${query}`, '_blank')
}

onMounted(async () => {
  await Promise.all([
    loadSettings(), loadAnalytics(), loadConversations(), loadRules(), loadTemplates(),
    loadSyncStatus(),
  ])
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
.inbox-layout { display: grid; grid-template-columns: 330px minmax(0, 1fr); gap: 14px; height: calc(100vh - 320px); min-height: 600px; }
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
.message-meta { display: flex; align-items: center; gap: 6px; margin-top: 6px; color: #a0a4ab; font-size: 11px; flex-wrap: wrap; }
.message-source { margin-top: 6px; color: #909399; font-size: 11px; }
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
