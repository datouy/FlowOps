import { describe, expect, it } from 'vitest'
import {
  avatarText,
  deliveryTagType,
  deliveryText,
  intentTagType,
  intentText,
  segmentTagType,
  shortDateTime
} from './dm-format'

describe('dm-format', () => {
  it('maps lead labels and tag types', () => {
    expect(intentText('high')).toBe('高意向')
    expect(intentTagType('high')).toBe('danger')
    expect(segmentTagType('interested')).toBe('warning')
    expect(intentText('')).toBe('未识别')
  })

  it('maps delivery states', () => {
    expect(deliveryText('sent')).toBe('已发送')
    expect(deliveryTagType('failed')).toBe('danger')
  })

  it('formats avatar and datetime safely', () => {
    expect(avatarText(' 张三 ')).toBe('张')
    expect(shortDateTime('2026-11-16T10:20:30')).toBe('2026-11-16 10:20')
    expect(shortDateTime(null)).toBe('')
  })
})
