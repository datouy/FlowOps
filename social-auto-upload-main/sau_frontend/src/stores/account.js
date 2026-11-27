import { defineStore } from 'pinia'
import { ref } from 'vue'
import { accountApi } from '@/api/account'

export const useAccountStore = defineStore('account', () => {
  // 存储所有账号信息
  const accounts = ref([])
  const accountsLoaded = ref(false)
  const keeper = ref({
    enabled: false,
    running: false,
    activeWindow: [8, 23],
    warnDays: 30,
    lastRound: { at: '', results: [] }
  })
  // 会话保活摘要的默认值（未触达过的账号就是这套空值）
  const emptySession = () => ({
    daysLeft: null,
    lastOutcome: '',
    lastTouchedAt: '',
    nextDueAt: '',
    warningAt: '',
    sessionExpiresAt: '',
    sessionIssuedAt: '',
    renewed30d: 0,
    deviceRefreshed30d: 0
  })
  let accountsLoadedAt = 0
  let accountsPromise = null
  const ACCOUNT_CACHE_TTL_MS = 5000
  
  // 平台类型映射
  const platformTypes = {
    1: '小红书',
    2: '视频号',
    3: '抖音',
    4: '快手',
    5: '微博',
    6: '百家号'
  }
  
  // 设置账号列表
  const setAccounts = (accountsData) => {
    // 转换后端返回的数据格式为前端使用的格式
    accounts.value = accountsData.map(item => {
      return {
        id: item[0],
        type: item[1],
        filePath: item[2],
        name: item[3],
        status: item[4] === -1 ? '验证中' : (item[4] === 1 ? '正常' : '异常'),
        platform: platformTypes[item[1]] || '未知',
        // v2 账号服务补充的健康度字段（由 loadHealth 填充，缺省时页面显示"未校验"）
        lastCheckAt: '',
        failReason: '',
        identity: '',
        identityBound: false,
        sessionGroup: '',
        session: emptySession(),
        automation: { configured: false, paused: false, pauseUntil: '', reason: '', updatedAt: '' }
      }
    })
    accountsLoaded.value = true
    accountsLoadedAt = Date.now()
  }

  // 把 v2 结构化账号直接转为页面使用的统一结构。
  //
  // 关键点：**内容没变的行要复用原对象**。以前每次轮询都整表重建，Vue 看到的
  // 是一批全新对象，账号卡片、表格、以及依赖 accounts 的 computed 全部重算，
  // 页面挂久了叠加轮询就会出现明显的掉帧甚至"页面无响应"。
  const rowSignature = (row) => JSON.stringify([
    row.id, row.name, row.status, row.platform, row.lastCheckAt, row.failReason,
    row.errorKind, row.failStreak, row.identity, row.identityBound, row.sessionGroup,
    row.capabilities, row.automation, row.cookie?.size, row.cookie?.updatedAt,
    row.session?.daysLeft, row.session?.lastOutcome, row.session?.lastTouchedAt
  ])

  const setDetailedAccounts = (detailed) => {
    const previous = new Map(accounts.value.map(item => [item.id, item]))
    const next = (detailed || []).map(item => {
      const row = {
        id: item.id,
        type: item.platform?.type,
        filePath: item.cookie?.name || '',
        name: item.name || '',
        status: item.status === -1 ? '验证中' : (item.status === 1 ? '正常' : '异常'),
        platform: item.platform?.label || '未知',
        lastCheckAt: item.lastCheckAt || '',
        failReason: item.failReason || '',
        errorKind: item.errorKind || '',
        failStreak: Number(item.failStreak || 0),
        identity: item.identity?.masked || '',
        identityBound: Boolean(item.identity?.bound),
        sessionGroup: item.cookie?.sessionGroup || '',
        session: item.session ? { ...emptySession(), ...item.session } : emptySession(),
        cookie: item.cookie || {},
        environment: item.environment || {},
        capabilities: item.capabilities || null,
        automation: item.automation || { configured: false, paused: false, pauseUntil: '', reason: '', updatedAt: '' }
      }
      const old = previous.get(row.id)
      // 内容一致就复用以外的字段，保持引用稳定，避免整块重渲染
      if (old && rowSignature(old) === rowSignature(row)) return old
      return row
    })
    const unchanged = next.length === accounts.value.length
      && next.every((row, index) => row === accounts.value[index])
    if (!unchanged) accounts.value = next
    accountsLoaded.value = true
    accountsLoadedAt = Date.now()
    return accounts.value
  }

  // 把 v2 结构化账号里的健康度合并进现有列表（不改 5 列老结构，按 id 对齐）
  const applyHealth = (detailed) => {
    if (!Array.isArray(detailed)) return accounts.value
    // 空数组代表账号已经全部删除，必须清掉本地列表；不能保留旧账号。
    return setDetailedAccounts(detailed)
  }

  // 拉取健康度并合并；失败不打断账号列表展示（v2 接口不可用时自动降级）
  const loadHealth = async (platform = null) => {
    try {
      const response = await accountApi.getAccountsDetailed(
        platform ? { platform } : {}
      )
      const detailed = response?.data?.accounts || []
      return applyHealth(detailed)
    } catch (error) {
      console.warn('加载账号健康度失败（已降级为旧接口数据）:', error)
      return accounts.value
    }
  }

  // 把保活后台返回的会话摘要按 accountId 合并进账号行。
  //
  // 一次请求拿全量（/api/v2/keeper/status），不给每个账号单独打接口：
  // 账号多的时候 N+1 会让"打开账号管理页"变慢，也会把后端线程占满。
  const applySessions = (payload) => {
    const data = payload || {}
    keeper.value = {
      enabled: Boolean(data.enabled),
      running: Boolean(data.running),
      activeWindow: Array.isArray(data.activeWindow) ? data.activeWindow : [8, 23],
      warnDays: Number(data.warnDays || 30),
      lastRound: data.lastRound || { at: '', results: [] }
    }
    const byId = new Map(
      (data.accounts || []).map(item => [Number(item.accountId), item])
    )
    if (!byId.size) return accounts.value
    let changed = false
    const merged = accounts.value.map(row => {
      const item = byId.get(Number(row.id))
      if (!item) return row
      const session = {
        ...emptySession(),
        daysLeft: item.daysLeft ?? null,
        lastOutcome: item.lastOutcome || '',
        lastTouchedAt: item.lastTouchedAt || '',
        nextDueAt: item.nextDueAt || '',
        warningAt: item.warningAt || '',
        sessionExpiresAt: item.sessionExpiresAt || '',
        renewed30d: Number(item.renewed30d || 0),
        deviceRefreshed30d: Number(item.deviceRefreshed30d || 0)
      }
      const current = row.session || {}
      const same = ['daysLeft', 'lastOutcome', 'lastTouchedAt', 'nextDueAt', 'warningAt',
        'sessionExpiresAt', 'renewed30d', 'deviceRefreshed30d']
        .every(key => current[key] === session[key])
      // 内容一致就保持同一引用，避免整个账号行重新渲染
      if (same) return row
      changed = true
      return { ...row, session }
    })
    if (changed) accounts.value = merged
    return accounts.value
  }

  // 拉取会话保活摘要；后端不可用时保持原样，不阻塞页面
  const loadSessions = async () => {
    try {
      const response = await accountApi.getKeeperStatus()
      return applySessions(response?.data || {})
    } catch (error) {
      console.warn('加载会话保活状态失败（账号列表继续可用）:', error)
      return accounts.value
    }
  }

  // 首次访问需要账号数据的页面时加载一次，避免直接刷新页面误判为没有账号
  const loadAccounts = async (force = false) => {
    if (
      !force && accountsLoaded.value
      && Date.now() - accountsLoadedAt < ACCOUNT_CACHE_TTL_MS
    ) return accounts.value
    if (accountsPromise) return accountsPromise
    accountsPromise = accountApi.getAccountsDetailed()
      .then(response => {
        if (response.code !== 200) throw new Error(response.msg || '获取账号列表失败')
        return setDetailedAccounts(response.data?.accounts || [])
      })
      .catch(async error => {
        console.warn('v2 账号接口不可用，降级为旧列表:', error)
        const response = await accountApi.getAccounts()
        if (response.code !== 200) throw new Error(response.msg || '获取账号列表失败')
        setAccounts(response.data || [])
        return accounts.value
      })
      .finally(() => { accountsPromise = null })
    return accountsPromise
  }
  
  // 添加账号
  const addAccount = (account) => {
    accounts.value.push(account)
  }
  
  // 更新账号
  const updateAccount = (id, updatedAccount) => {
    const index = accounts.value.findIndex(acc => acc.id === id)
    if (index !== -1) {
      accounts.value[index] = { ...accounts.value[index], ...updatedAccount }
    }
  }
  
  // 删除账号
  const deleteAccount = (id) => {
    accounts.value = accounts.value.filter(acc => acc.id !== id)
  }
  
  // 根据平台获取账号
  const getAccountsByPlatform = (platform) => {
    return accounts.value.filter(acc => acc.platform === platform)
  }
  
  return {
    accounts,
    accountsLoaded,
    keeper,
    setAccounts,
    setDetailedAccounts,
    applyHealth,
    loadHealth,
    applySessions,
    loadSessions,
    loadAccounts,
    addAccount,
    updateAccount,
    deleteAccount,
    getAccountsByPlatform
  }
})
