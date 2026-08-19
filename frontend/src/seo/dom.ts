import type { RouteLocationNormalizedLoaded } from 'vue-router'
import { i18n } from '@/i18n'
import { resolveRouteSeo } from '@/seo/routeSeo'
import { buildRouteSchemas } from '@/seo/schema'
import type { PublicSettings } from '@/types'

type ApplyRouteSeoOptions = {
  route: RouteLocationNormalizedLoaded
  siteName?: string
  settings?: PublicSettings | null
  titleOverride?: string
}

type ApplySeoOptions = {
  title: string
  description: string
  robots: string
  canonicalUrl: string
}

export function applyRouteSeo(options: ApplyRouteSeoOptions): void {
  const { route, siteName, settings, titleOverride } = options
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  const seo = resolveRouteSeo({
    route,
    titleOverride,
    siteName,
    settings,
    origin,
  })
  applySeoMeta(seo)
  applyStructuredData(buildRouteSchemas({
    route,
    title: seo.title,
    description: seo.description,
    canonicalUrl: seo.canonicalUrl,
    siteName: siteName || settings?.site_name || 'Sub2API',
    settings,
  }))
}

export function applySeoMeta(options: ApplySeoOptions): void {
  if (typeof document === 'undefined') {
    return
  }

  document.title = options.title
  document.documentElement.setAttribute('lang', i18n.global.locale.value || 'en')

  upsertMetaTag('name', 'description', options.description)
  upsertMetaTag('name', 'robots', options.robots)
  upsertMetaTag('property', 'og:title', options.title)
  upsertMetaTag('property', 'og:description', options.description)
  upsertMetaTag('property', 'og:type', 'website')
  upsertMetaTag('property', 'og:url', options.canonicalUrl)
  upsertMetaTag('name', 'twitter:card', 'summary_large_image')
  upsertMetaTag('name', 'twitter:title', options.title)
  upsertMetaTag('name', 'twitter:description', options.description)
  upsertLinkTag('canonical', options.canonicalUrl)
}

function upsertMetaTag(attr: 'name' | 'property', value: string, content: string): void {
  let tag = document.head.querySelector<HTMLMetaElement>(`meta[${attr}="${value}"]`)
  if (!tag) {
    tag = document.createElement('meta')
    tag.setAttribute(attr, value)
    document.head.appendChild(tag)
  }
  tag.setAttribute('content', content)
}

function upsertLinkTag(rel: string, href: string): void {
  let tag = document.head.querySelector<HTMLLinkElement>(`link[rel="${rel}"]`)
  if (!tag) {
    tag = document.createElement('link')
    tag.setAttribute('rel', rel)
    document.head.appendChild(tag)
  }
  tag.setAttribute('href', href)
}

function applyStructuredData(schemas: Array<Record<string, unknown>>): void {
  const existing = document.head.querySelectorAll('script[data-seo-schema="true"]')
  existing.forEach((node) => node.remove())

  schemas.forEach((schema) => {
    const script = document.createElement('script')
    script.type = 'application/ld+json'
    script.dataset.seoSchema = 'true'
    script.textContent = JSON.stringify(schema)
    document.head.appendChild(script)
  })
}
