import type { PublicSettings } from '@/types'

type SchemaRoute = {
  name?: string | symbol | null
  path: string
}

type BuildRouteSchemasInput = {
  route: SchemaRoute
  title: string
  description: string
  canonicalUrl: string
  siteName: string
  settings?: PublicSettings | null
}

type SchemaObject = Record<string, unknown>

export function buildRouteSchemas(input: BuildRouteSchemasInput): SchemaObject[] {
const { route, title, description, canonicalUrl, siteName, settings } = input
  const publicHelpRoot = 'https://china-models.com/help/'
  const baseSchemas: SchemaObject[] = [
    {
      '@context': 'https://schema.org',
      '@type': 'Organization',
      name: siteName,
      url: canonicalRoot(canonicalUrl),
    },
    {
      '@context': 'https://schema.org',
      '@type': 'WebSite',
      name: siteName,
      url: canonicalRoot(canonicalUrl),
      potentialAction: {
      '@type': 'SearchAction',
        target: `${publicHelpRoot}?q={search_term_string}`,
        'query-input': 'required name=search_term_string',
      },
    },
  ]

  const pageType = route.name === 'ThesisLanding' ? 'EducationalWebPage' : 'WebPage'
  const pageSchema: SchemaObject = {
    '@context': 'https://schema.org',
    '@type': pageType,
    name: title,
    description,
    url: canonicalUrl,
    isPartOf: canonicalRoot(canonicalUrl),
  }

  if (route.name === 'Home' && settings?.site_subtitle) {
    pageSchema.about = settings.site_subtitle
  }

  return [...baseSchemas, pageSchema]
}

function canonicalRoot(url: string): string {
  try {
    const parsed = new URL(url, 'https://example.com')
    if (!parsed.host || parsed.host === 'example.com') {
      return url
    }
    return `${parsed.protocol}//${parsed.host}`
  } catch {
    return url
  }
}
