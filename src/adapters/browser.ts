import type { Variant, PublishResult, ChannelHints } from "../models.js";

type Platform = "medium" | "linkedin" | "twitter";

const COMPOSE_URLS: Record<Platform, (v: Variant) => string> = {
  medium: () => "https://medium.com/new-story",
  linkedin: () => "https://www.linkedin.com/post/new",
  twitter: (v) => `https://twitter.com/compose/tweet?text=${encodeURIComponent(v.body.slice(0, 280))}`,
};

const HINTS: Record<Platform, ChannelHints> = {
  medium: {
    max_length: 100_000,
    supported_md_features: ["bold", "italic", "code_inline", "code_block", "links", "headers", "images"],
    cta_placement: "bottom",
    canonical_url_supported: true,
    browser_only: true,
  },
  linkedin: {
    max_length: 3_000,
    supported_md_features: ["bold", "italic", "links"],
    cta_placement: "bottom",
    canonical_url_supported: false,
    browser_only: true,
  },
  twitter: {
    max_length: 280,
    supported_md_features: ["links"],
    cta_placement: "none",
    canonical_url_supported: false,
    browser_only: true,
  },
};

export function makeBrowserAdapter(platform: Platform) {
  return {
    hints(): ChannelHints {
      return HINTS[platform];
    },
    async publish(variant: Variant): Promise<PublishResult> {
      return {
        channel: variant.channel,
        state: "needs_browser",
        compose_url: COMPOSE_URLS[platform](variant),
      };
    },
    async unpublish(_liveUrl: string): Promise<[boolean, string]> {
      return [false, `${platform} posts must be deleted manually via the platform UI`];
    },
  };
}
