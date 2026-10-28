import { defineStore } from 'pinia'
import { ref } from 'vue'
import { accountApi } from '@/api/account'

export const useAccountStore = defineStore('account', () => {
  // 存储所有账号信息
  const accounts = ref([])
  const accountsLoaded = ref(false)
  let accountsPromise = null
  
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
        platform: platformTypes[item[1]] || '未知'
      }
    })
    accountsLoaded.value = true
  }

  // 首次访问需要账号数据的页面时加载一次，避免直接刷新页面误判为没有账号
  const loadAccounts = async () => {
    if (accountsLoaded.value) return accounts.value
    if (accountsPromise) return accountsPromise
    accountsPromise = accountApi.getAccounts()
      .then((response) => {
        if (response.code !== 200) {
          throw new Error(response.msg || '获取账号列表失败')
        }
        setAccounts(response.data || [])
        return accounts.value
      })
      .finally(() => {
        accountsPromise = null
      })
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
    loadAccounts,
    addAccount,
    updateAccount,
    deleteAccount,
    getAccountsByPlatform
  }
})
