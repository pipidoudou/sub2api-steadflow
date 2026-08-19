import { describe, expect, it } from "vitest";

import {
  appendAuthSourceDefaultsToUpdateRequest,
  type SystemSettings,
  type UpdateSettingsRequest,
} from "@/api/admin/settings";

describe("thesis vertical settings typing", () => {
  it("accepts thesis vertical fields on system settings payloads", () => {
    const settings: Partial<SystemSettings> = {
      thesis_vertical_enabled: true,
      thesis_vertical_brand_domain: "china-model.com",
      thesis_vertical_primary_plan_ids: [101, 102],
      thesis_vertical_codex_guide_url: "https://china-model.com/codex-guide",
      thesis_vertical_skill_pack_url: "https://china-model.com/skill-pack",
      thesis_vertical_support_disciplines: ["medicine", "education"],
    };

    expect(settings.thesis_vertical_enabled).toBe(true);
    expect(settings.thesis_vertical_primary_plan_ids).toEqual([101, 102]);
    expect(settings.thesis_vertical_support_disciplines).toEqual([
      "medicine",
      "education",
    ]);
  });

  it("keeps thesis vertical fields intact when composing admin update payloads", () => {
    const payload: UpdateSettingsRequest = {
      thesis_vertical_enabled: true,
      thesis_vertical_brand_domain: "china-model.com",
      thesis_vertical_primary_plan_ids: [101, 102],
      thesis_vertical_codex_guide_url: " https://china-model.com/codex-guide ",
      thesis_vertical_skill_pack_url: "https://china-model.com/skill-pack",
      thesis_vertical_support_disciplines: ["medicine", "education"],
    };

    appendAuthSourceDefaultsToUpdateRequest(payload, {
      email: {
        balance: 0,
        concurrency: 5,
        subscriptions: [],
        grant_on_signup: false,
        grant_on_first_bind: false,
        platform_quotas: {},
      },
      linuxdo: {
        balance: 0,
        concurrency: 5,
        subscriptions: [],
        grant_on_signup: false,
        grant_on_first_bind: false,
        platform_quotas: {},
      },
      oidc: {
        balance: 0,
        concurrency: 5,
        subscriptions: [],
        grant_on_signup: false,
        grant_on_first_bind: false,
        platform_quotas: {},
      },
      wechat: {
        balance: 0,
        concurrency: 5,
        subscriptions: [],
        grant_on_signup: false,
        grant_on_first_bind: false,
        platform_quotas: {},
      },
      github: {
        balance: 0,
        concurrency: 5,
        subscriptions: [],
        grant_on_signup: false,
        grant_on_first_bind: false,
        platform_quotas: {},
      },
      google: {
        balance: 0,
        concurrency: 5,
        subscriptions: [],
        grant_on_signup: false,
        grant_on_first_bind: false,
        platform_quotas: {},
      },
      dingtalk: {
        balance: 0,
        concurrency: 5,
        subscriptions: [],
        grant_on_signup: false,
        grant_on_first_bind: false,
        platform_quotas: {},
      },
    });

    expect(payload.thesis_vertical_enabled).toBe(true);
    expect(payload.thesis_vertical_brand_domain).toBe("china-model.com");
    expect(payload.thesis_vertical_primary_plan_ids).toEqual([101, 102]);
    expect(payload.thesis_vertical_codex_guide_url).toBe(
      " https://china-model.com/codex-guide ",
    );
    expect(payload.thesis_vertical_skill_pack_url).toBe(
      "https://china-model.com/skill-pack",
    );
    expect(payload.thesis_vertical_support_disciplines).toEqual([
      "medicine",
      "education",
    ]);
  });
});
