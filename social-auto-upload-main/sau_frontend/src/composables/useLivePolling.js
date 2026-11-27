import { onActivated, onBeforeUnmount, onDeactivated, onMounted } from 'vue'

/**
 * 页面级轮询：只在页面可见、且上一轮跑完时才发起下一轮。
 *
 * 旧实现用 setInterval 死循环：浏览器把标签页放到后台后定时器照样触发，
 * 每次都要走一遍请求 + 响应式更新；页面挂久了又会堆积大量微任务，主线程
 * 一旦被长任务占住，用户就会看到"页面无响应"。
 *
 * 现在改成：
 *  - 链式 setTimeout：上一次任务结束后才开始计时，不会重叠；
 *  - document.hidden 直接不排期，切回前台时立即补一次；
 *  - 组件停用时彻底停表。
 */
export function useLivePolling(
  task,
  { interval = 5000, isActive = () => true, refreshOnVisible = true } = {}
) {
  let timer = null
  let running = false
  let stopped = true

  const clear = () => {
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
  }

  const schedule = (delay = interval) => {
    if (stopped) return
    clear()
    timer = window.setTimeout(tick, Math.max(1000, delay))
  }

  async function tick() {
    timer = null
    if (stopped) return
    if (running || document.hidden || !isActive()) {
      schedule()
      return
    }
    running = true
    try {
      await task()
    } catch (error) {
      // 单轮失败不能把轮询停掉，交给下一轮重试
      console.warn('[polling] 本轮刷新失败，将按间隔重试:', error)
    } finally {
      running = false
      schedule()
    }
  }

  function onVisibilityChange() {
    if (stopped) return
    if (document.hidden) {
      clear()
      return
    }
    if (refreshOnVisible) {
      tick()
    } else {
      schedule()
    }
  }

  function start() {
    if (!stopped) return
    stopped = false
    document.addEventListener('visibilitychange', onVisibilityChange)
    schedule()
  }

  function stop() {
    stopped = true
    clear()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  }

  onMounted(start)
  onActivated(start)
  onDeactivated(stop)
  onBeforeUnmount(stop)

  return { tick }
}
