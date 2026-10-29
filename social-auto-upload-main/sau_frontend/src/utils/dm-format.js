export function segmentTagType(segment) {
  return { new: 'success', interested: 'warning', existing: 'info' }[segment] || 'info'
}

export function intentText(intent) {
  return { high: '高意向', medium: '中意向', low: '低意向' }[intent] || intent || '未识别'
}

export function intentTagType(intent) {
  return { high: 'danger', medium: 'warning', low: 'info' }[intent] || 'info'
}

export function deliveryText(status) {
  return {
    sent: '已发送',
    ready: '待发送',
    pending: '发送中',
    failed: '失败',
    received: '已接收',
  }[status] || status || ''
}

export function deliveryTagType(status) {
  return { sent: 'success', ready: 'warning', pending: 'info', failed: 'danger' }[status] || 'info'
}

export function avatarText(name) {
  return String(name || '客').trim().slice(0, 1).toUpperCase()
}

export function shortTime(value) {
  if (!value) return ''
  const text = String(value)
  const now = new Date()
  const today = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
  ].join('-')
  if (text.slice(0, 10) === today) return text.slice(11, 16)
  return text.slice(5, 10)
}

export function shortDateTime(value) {
  if (!value) return ''
  return String(value).slice(0, 16)
}
