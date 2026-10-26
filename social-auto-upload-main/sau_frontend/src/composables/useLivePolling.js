import { onBeforeUnmount, onMounted } from 'vue'

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

  onMounted(() => {
    timer = window.setInterval(tick, interval)
  })

  onBeforeUnmount(() => {
    if (timer) window.clearInterval(timer)
    timer = null
  })

  return { tick }
}
