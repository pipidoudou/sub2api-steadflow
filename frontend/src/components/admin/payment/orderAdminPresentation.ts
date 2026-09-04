import type { PaymentOrder } from '@/types/payment'

export function adminOrderStatusLabelKey(order: PaymentOrder): string | undefined {
  if (order.status === 'EXPIRED') return 'payment.admin.paymentTimeout'
  if (order.order_type === 'subscription' && order.status === 'PAID') {
    return 'payment.admin.pendingFulfillment'
  }
  return undefined
}

export function canMarkManuallyFulfilled(order: PaymentOrder): boolean {
  return order.order_type === 'subscription' && order.status === 'PAID'
}
