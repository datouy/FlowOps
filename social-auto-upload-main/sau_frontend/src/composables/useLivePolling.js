import { onActivated, onBeforeUnmount, onDeactivated, onMounted } from 'vue'

export function useLivePolling(task, { interval = 5000, isActive = () => true } = {}) {
  let timer = null
  let running = false

  async function tick() {
    if (document.hidden || running || !isActive()) return
    running = true
    try {
      await task()
    } finally {
      running = false
    }
  }

  function start() {
    if (timer !== null) return
    timer = window.setInterval(tick, interval)
  }

  function stop() {
    if (timer === null) return
    window.clearInterval(timer)
    timer = null
  }

  onMounted(start)
  onActivated(start)
  onDeactivated(stop)
  onBeforeUnmount(stop)

  return { tick }
}
