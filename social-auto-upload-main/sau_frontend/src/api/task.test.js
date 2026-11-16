import { beforeEach, describe, expect, it, vi } from 'vitest'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))

vi.mock('@/utils/request', () => ({
  http: {
    get,
    post: vi.fn()
  }
}))

import { waitForTask } from './task'

describe('waitForTask', () => {
  beforeEach(() => {
    get.mockReset()
  })

  it('polls until success and returns the result', async () => {
    get
      .mockResolvedValueOnce({ data: { id: 't1', status: 'running', progress: 40 } })
      .mockResolvedValueOnce({ data: { id: 't1', status: 'success', result: { title: 'ok' } } })

    await expect(waitForTask({ id: 't1', status: 'pending' }, { interval: 0 }))
      .resolves.toEqual({ title: 'ok' })
  })

  it('surfaces background failures', async () => {
    get.mockResolvedValueOnce({ data: { id: 't2', status: 'failed', error: 'boom' } })
    await expect(waitForTask({ id: 't2', status: 'pending' }, { interval: 0 }))
      .rejects.toThrow('boom')
  })
})
