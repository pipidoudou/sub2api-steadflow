import { describe, expect, it } from 'vitest'

import { isSetupBypassRoute } from '@/utils/publicSetupRoute'

describe('isSetupBypassRoute', () => {
  it('allows setup and the thesis landing before setup completes', () => {
    expect(isSetupBypassRoute('/setup')).toBe(true)
    expect(isSetupBypassRoute('/thesis')).toBe(true)
  })

  it('rejects unrelated routes', () => {
    expect(isSetupBypassRoute('/home')).toBe(false)
    expect(isSetupBypassRoute('/dashboard')).toBe(false)
    expect(isSetupBypassRoute('/payment')).toBe(false)
    expect(isSetupBypassRoute('/thesis-admin')).toBe(false)
  })
})
