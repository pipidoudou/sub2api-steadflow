import { describe, expect, it } from 'vitest'
import { buildRouteSchemas } from '@/seo/schema'
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
  thesis_vertical_brand_domain: '',
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

describe('buildRouteSchemas', () => {
  it('builds organization and website schema for home page', () => {
    const schemas = buildRouteSchemas({
      route: {
        name: 'Home',
        path: '/home',
      },
      title: 'AI API Gateway Platform - Sub2API',
      description: 'Unified AI gateway',
      canonicalUrl: 'https://sub2api.test/home',
      siteName: settings.site_name,
      settings,
    })

    expect(schemas).toHaveLength(3)
    expect(schemas[0]['@type']).toBe('Organization')
    expect(schemas[1]['@type']).toBe('WebSite')
    expect(schemas[2]['@type']).toBe('WebPage')
  })

  it('builds article-like educational schema for thesis page', () => {
    const schemas = buildRouteSchemas({
      route: {
        name: 'ThesisLanding',
        path: '/thesis',
      },
      title: 'AI Thesis Writing Guide - Sub2API',
      description: 'Learn AI thesis workflows',
      canonicalUrl: 'https://sub2api.test/thesis',
      siteName: settings.site_name,
      settings,
    })

    const educational = schemas.find((item) => item['@type'] === 'EducationalWebPage')
    expect(educational).toBeDefined()
    expect(educational?.name).toBe('AI Thesis Writing Guide - Sub2API')
  })
})
