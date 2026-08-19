import type { RouteMeta } from 'vue-router'
import { resolveDocumentTitle } from '@/router/title'
import type { PublicSettings } from '@/types'

type RouteSeoInput = {
  route: {
    name?: string | symbol | null
    path: string
    meta: RouteMeta
  }
  titleOverride?: string
  siteName?: string
  settings?: PublicSettings | null
  origin?: string
}

export type ResolvedRouteSeo = {
  title: string
  description: string
  robots: string
  canonicalUrl: string
}

export function resolveRouteSeo(input: RouteSeoInput): ResolvedRouteSeo {
  const {
    route,
    titleOverride,
    siteName,
    settings,
    origin,
  } = input
  const normalizedSiteName = siteName?.trim() || settings?.site_name?.trim() || 'Sub2API'
  const resolvedTitle = resolveDocumentTitle(
    titleOverride || route.meta.title,
    normalizedSiteName,
    route.meta.titleKey as string | undefined,
  )

  return {
    title: resolvedTitle,
    description: resolveSeoDescription(route, normalizedSiteName, settings),
    robots: resolveRobotsDirective(route),
    canonicalUrl: buildCanonicalUrl(origin, route.path),
  }
}

function resolveSeoDescription(
  route: RouteSeoInput['route'],
  siteName: string,
  settings?: PublicSettings | null
): string {
  const meta = route.meta
  if (typeof meta.description === 'string' && meta.description.trim()) {
    return meta.description.trim()
  }
  if (route.name === 'Home') {
    return buildHomeDescription(siteName, settings)
  }
  if (route.name === 'Login') {
    return `Log in to your ${siteName} workspace.`
  }
  if (route.name === 'Register') {
    return `Create a ${siteName} account.`
  }
  if (settings?.site_subtitle?.trim()) {
    return settings.site_subtitle.trim()
  }
  return 'Unified AI gateway for subscription quota distribution.'
}

function resolveRobotsDirective(route: RouteSeoInput['route']): string {
  if (route.meta.noindex === true) {
    return 'noindex,follow'
  }
  if (route.meta.requiresAdmin === true || route.meta.requiresAuth !== false) {
    return 'noindex,nofollow'
  }
  return 'index,follow'
}

function buildCanonicalUrl(origin: string | undefined, path: string): string {
  const normalizedOrigin = (origin || '').trim().replace(/\/+$/, '')
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  if (!normalizedOrigin) {
    return normalizedPath
  }
  return `${normalizedOrigin}${normalizedPath}`
}

function buildHomeDescription(siteName: string, settings?: PublicSettings | null): string {
  const subtitle = settings?.site_subtitle?.trim() || ''
  if (!subtitle) {
    return `${siteName} helps you access AI workflows, subscriptions, and operational guidance in one place.`
  }
  if (subtitle.toLowerCase().includes(siteName.toLowerCase())) {
    return subtitle
  }
  return `${siteName} - ${subtitle}`
}
