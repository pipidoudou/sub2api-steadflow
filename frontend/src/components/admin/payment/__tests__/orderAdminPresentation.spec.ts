import { describe, expect, it } from 'vitest'
import type { PaymentOrder } from '@/types/payment'
import { adminOrderStatusLabelKey, canMarkManuallyFulfilled } from '../orderAdminPresentation'

const order = (overrides: Partial<PaymentOrder>): PaymentOrder => ({
  id: 1,
  user_id: 2,
  amount: 10,
  pay_amount: 10,
  fee_rate: 0,
  payment_type: 'alipay',
  out_trade_no: 'sub2_test',
  status: 'PAID',
  order_type: 'subscription',
  created_at: '2026-08-31T00:00:00Z',
  expires_at: '2026-08-31T00:30:00Z',
  refund_amount: 0,
  ...overrides,
})

describe('admin order presentation', () => {
  it('labels paid subscription orders as pending fulfillment', () => {
    expect(adminOrderStatusLabelKey(order({}))).toBe('payment.admin.pendingFulfillment')
  })

  it('labels expired orders as payment timeout', () => {
    expect(adminOrderStatusLabelKey(order({ status: 'EXPIRED' }))).toBe('payment.admin.paymentTimeout')
  })

  it('keeps paid balance orders on the normal paid label', () => {
    expect(adminOrderStatusLabelKey(order({ order_type: 'balance' }))).toBeUndefined()
  })

  it('allows manual confirmation only for paid subscriptions', () => {
    expect(canMarkManuallyFulfilled(order({}))).toBe(true)
    expect(canMarkManuallyFulfilled(order({ status: 'FAILED' }))).toBe(false)
    expect(canMarkManuallyFulfilled(order({ order_type: 'balance' }))).toBe(false)
  })
})
