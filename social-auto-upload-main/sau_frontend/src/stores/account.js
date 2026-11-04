import { defineStore } from 'pinia'
import { ref } from 'vue'
import { accountApi } from '@/api/account'

export const useAccountStore = defineStore('account', () => {
  // 存储所有账号信息
  const accounts = ref([])
  const accountsLoaded = ref(false)
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
        sessionGroup: ''
      }
    })
    accountsLoaded.value = true
    accountsLoadedAt = Date.now()
  }

  // 把 v2 结构化账号直接转为页面使用的统一结构。
  const setDetailedAccounts = (detailed) => {
    accounts.value = (detailed || []).map(item => ({
      id: item.id,
      type: item.platform?.type,
      filePath: item.cookie?.name || '',
      name: item.name || '',
      status: item.status === -1 ? '验证中' : (item.status === 1 ? '正常' : '异常'),
      platform: item.platform?.label || '未知',
      lastCheckAt: item.lastCheckAt || '',
      failReason: item.failReason || '',
      identity: item.identity?.masked || '',
      identityBound: Boolean(item.identity?.bound),
      sessionGroup: item.cookie?.sessionGroup || ''
    }))
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
    setAccounts,
    setDetailedAccounts,
    applyHealth,
    loadHealth,
    loadAccounts,
    addAccount,
    updateAccount,
    deleteAccount,
    getAccountsByPlatform
  }
})
