import { describe, expect, it, vi } from 'vitest'

const authStore = vi.hoisted(() => ({
  checkAuth: vi.fn(),
  isAuthenticated: false,
  isAdmin: false,
  isSimpleMode: false,
}))

const appStore = vi.hoisted(() => ({
  siteName: 'Sub2API',
  backendModeEnabled: false,
  cachedPublicSettings: null as null | Record<string, unknown>,
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authStore,
}))

vi.mock('@/stores/app', () => ({
  useAppStore: () => appStore,
}))

vi.mock('@/stores/adminSettings', () => ({
  useAdminSettingsStore: () => ({
    customMenuItems: [],
  }),
}))

vi.mock('@/composables/useNavigationLoading', () => ({
  useNavigationLoadingState: () => ({
    startNavigation: vi.fn(),
    endNavigation: vi.fn(),
    isLoading: { value: false },
  }),
}))

vi.mock('@/composables/useRoutePrefetch', () => ({
  useRoutePrefetch: () => ({
    triggerPrefetch: vi.fn(),
    cancelPendingPrefetch: vi.fn(),
    resetPrefetchState: vi.fn(),
  }),
}))

describe('router thesis public routes', () => {
  it('registers only the thesis landing route', async () => {
    const { default: router } = await import('@/router')
    const routeMap = new Map(router.getRoutes().map((record) => [record.name, record]))

    expect(routeMap.get('ThesisLanding')?.path).toBe('/thesis')
    expect(routeMap.get('ThesisQuickStart')).toBeUndefined()
    expect(routeMap.get('ThesisTutorial')).toBeUndefined()
    expect(routeMap.get('ThesisLiteratureGuide')).toBeUndefined()
    expect(routeMap.get('ThesisExamples')).toBeUndefined()
    expect(routeMap.get('ThesisExampleDetail')).toBeUndefined()

    expect(routeMap.get('ThesisLanding')?.meta.requiresAuth).toBe(false)
  })
})
