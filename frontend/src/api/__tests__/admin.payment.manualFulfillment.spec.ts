import { beforeEach, describe, expect, it, vi } from 'vitest'

const { post } = vi.hoisted(() => ({ post: vi.fn() }))

vi.mock('@/api/client', () => ({ apiClient: { post } }))

import { adminPaymentAPI } from '@/api/admin/payment'

describe('admin manual fulfillment api', () => {
  beforeEach(() => post.mockReset())

  it('posts to the dedicated state-only endpoint', async () => {
    post.mockResolvedValue({ data: { id: 17, status: 'COMPLETED' } })

    await adminPaymentAPI.markManuallyFulfilled(17)

    expect(post).toHaveBeenCalledWith('/admin/payment/orders/17/mark-manually-fulfilled')
  })
})
