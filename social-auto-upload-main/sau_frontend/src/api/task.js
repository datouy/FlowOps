import { http } from '@/utils/request'

const TERMINAL = new Set(['success', 'failed', 'cancelled'])

export const taskApi = {
  get(id, config = {}) {
    return http.get(`/api/v2/tasks/${id}`, null, { silent: true, ...config })
  },
  cancel(id) {
    return http.post(`/api/v2/tasks/${id}/cancel`)
  }
}

export async function waitForTask(taskOrId, {
  interval = 800,
  timeout = 15 * 60 * 1000,
  onUpdate = null
} = {}) {
  const id = typeof taskOrId === 'string' ? taskOrId : taskOrId?.id
  if (!id) throw new Error('任务缺少 ID')
  const deadline = Date.now() + timeout
  let task = typeof taskOrId === 'object' ? taskOrId : null
  while (Date.now() < deadline) {
    if (!task || !TERMINAL.has(task.status)) {
      const response = await taskApi.get(id)
      task = response.data
    }
    if (onUpdate) onUpdate(task)
    if (task.status === 'success') return task.result
    if (task.status === 'failed') throw new Error(task.error || '后台任务执行失败')
    if (task.status === 'cancelled') throw new Error('后台任务已取消')
    await new Promise(resolve => setTimeout(resolve, interval))
    task = null
  }
  throw new Error('后台任务等待超时，请到运行状态页查看进度')
}
