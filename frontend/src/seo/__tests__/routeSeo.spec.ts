import { describe, expect, it } from 'vitest'
import { resolveRouteSeo } from '@/seo/routeSeo'
import type { PublicSettings } from '@/types'

const settings: PublicSettings = {
  registration_enabled: true,
  email_verify_enabled: true,
  force_email_on_third_party_signup: false,
  registration_email_suffix_whitelist: [],
  promo_code_enabled: false,
  password_reset_enabled: true,
  invitation_code_enabled: false,
  login_agreement_enabled: false,
  login_agreement_mode: 'modal',
  login_agreement_updated_at: '',
  login_agreement_revision: '',
  login_agreement_documents: [],
  turnstile_enabled: false,
  turnstile_site_key: '',
  site_name: 'Sub2API',
  site_logo: '',
  site_subtitle: 'AI API Gateway Platform',
  api_base_url: 'https://api.sub2api.test',
  contact_info: '',
  doc_url: '',
  home_content: '',
  hide_ccs_import_button: false,
  thesis_vertical_enabled: true,
  thesis_vertical_brand_domain: 'paper.sub2api.test',
  thesis_vertical_primary_plan_ids: [],
  thesis_vertical_codex_guide_url: '',
  thesis_vertical_skill_pack_url: '',
  thesis_vertical_support_disciplines: [],
  payment_enabled: false,
  risk_control_enabled: false,
  table_default_page_size: 20,
  table_page_size_options: [10, 20, 50],
  custom_menu_items: [],
  custom_endpoints: [],
  linuxdo_oauth_enabled: false,
  dingtalk_oauth_enabled: false,
  wechat_oauth_enabled: false,
  wechat_oauth_open_enabled: false,
  wechat_oauth_mp_enabled: false,
  wechat_oauth_mobile_enabled: false,
  oidc_oauth_enabled: false,
  oidc_oauth_provider_name: 'OIDC',
  github_oauth_enabled: false,
  google_oauth_enabled: false,
  backend_mode_enabled: false,
  version: '1.0.0',
  balance_low_notify_enabled: false,
  account_quota_notify_enabled: false,
  balance_low_notify_threshold: 0,
  channel_monitor_enabled: false,
  channel_monitor_default_interval_seconds: 60,
  available_channels_enabled: false,
  affiliate_enabled: false,
}

describe('resolveRouteSeo', () => {
  it('returns indexable SEO metadata for public landing pages', () => {
    const seo = resolveRouteSeo({
      route: {
        name: 'Home',
        path: '/home',
        meta: {
          requiresAuth: false,
          title: 'AI API Gateway Platform',
          description: 'Unified AI gateway for subscription quota distribution.'
        }
      },
      siteName: settings.site_name,
      settings,
      origin: 'https://sub2api.test'
    })

    expect(seo.title).toBe('AI API Gateway Platform - Sub2API')
    expect(seo.description).toBe('Unified AI gateway for subscription quota distribution.')
    expect(seo.robots).toBe('index,follow')
    expect(seo.canonicalUrl).toBe('https://sub2api.test/home')
  })

  it('marks login and callback pages as noindex', () => {
    const seo = resolveRouteSeo({
      route: {
        name: 'Login',
        path: '/login',
        meta: {
          requiresAuth: false,
          title: 'Login',
          noindex: true
        }
      },
      siteName: settings.site_name,
      settings,
      origin: 'https://sub2api.test'
    })

    expect(seo.robots).toBe('noindex,follow')
    expect(seo.description).toBe('Log in to your Sub2API workspace.')
  })

  it('marks authenticated application routes as noindex,nofollow', () => {
    const seo = resolveRouteSeo({
      route: {
        name: 'Dashboard',
        path: '/dashboard',
        meta: {
          requiresAuth: true,
          title: 'Dashboard'
        }
      },
      siteName: settings.site_name,
      settings,
      origin: 'https://sub2api.test'
    })

    expect(seo.robots).toBe('noindex,nofollow')
  })

  it('accepts a dynamic title override for content pages', () => {
    const seo = resolveRouteSeo({
      route: {
        name: 'LegalDocument',
        path: '/legal/privacy-policy',
        meta: {
          requiresAuth: false,
          title: 'Legal Document',
          description: 'Read the latest legal document.'
        }
      },
      titleOverride: 'Privacy Policy',
      siteName: settings.site_name,
      settings,
      origin: 'https://sub2api.test'
    })

    expect(seo.title).toBe('Privacy Policy - Sub2API')
    expect(seo.canonicalUrl).toBe('https://sub2api.test/legal/privacy-policy')
  })
})
