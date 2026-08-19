type LocaleTree = Record<string, any>

function isLocaleTree(value: unknown): value is LocaleTree {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

// Keep project-specific legacy keys while letting the current upstream locale
// modules replace any overlapping translations.
export function mergeLocaleTrees<T extends LocaleTree, U extends LocaleTree>(
  legacy: T,
  upstream: U,
): T & U {
  const merged: LocaleTree = { ...legacy }

  for (const [key, value] of Object.entries(upstream)) {
    const legacyValue = merged[key]
    merged[key] = isLocaleTree(legacyValue) && isLocaleTree(value)
      ? mergeLocaleTrees(legacyValue, value)
      : value
  }

  return merged as T & U
}
