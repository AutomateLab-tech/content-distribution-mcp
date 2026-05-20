import type { Variant, PublishResult, ChannelHints } from "../models.js";
import type { Profile } from "../backends/base.js";

const API = "https://dev.to/api";

export class DevToAdapter {
  hints(): ChannelHints {
    return {
      max_length: 100_000,
      supported_md_features: ["bold", "italic", "code_inline", "code_block", "links", "headers", "images", "lists", "tables", "blockquote"],
      tag_vocab: ["ai", "automation", "mcp", "claude", "llm", "devops", "tutorial", "productivity", "javascript", "python"],
      cta_placement: "bottom",
      canonical_url_supported: true,
      browser_only: false,
    };
  }

  async publish(variant: Variant, profile: Profile): Promise<PublishResult> {
    const apiKey = profile.credentials["DEV_TO_API_KEY"];
    if (!apiKey) return { channel: variant.channel, state: "failed", error: "DEV_TO_API_KEY not set in profile" };

    const res = await fetch(`${API}/articles`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "api-key": apiKey },
      body: JSON.stringify({
        article: {
          title: variant.title,
          body_markdown: variant.body,
          tags: variant.tags.slice(0, 4),
          ...(variant.canonical_url ? { canonical_url: variant.canonical_url } : {}),
          published: true,
          ...(variant.extras?.series ? { series: variant.extras.series } : {}),
        },
      }),
    });

    if (!res.ok) {
      const text = await res.text();
      return { channel: variant.channel, state: "failed", error: `devto ${res.status}: ${text}` };
    }

    const json = await res.json() as { url: string };
    return { channel: variant.channel, state: "live", live_url: json.url, published_at: new Date().toISOString() };
  }

  async unpublish(liveUrl: string, profile: Profile): Promise<[boolean, string | undefined]> {
    const apiKey = profile.credentials["DEV_TO_API_KEY"];
    if (!apiKey) return [false, "DEV_TO_API_KEY not set in profile"];

    const res = await fetch(`${API}/articles/me/published?per_page=100`, {
      headers: { "api-key": apiKey },
    });
    if (!res.ok) return [false, `devto ${res.status}`];

    const articles = await res.json() as Array<{ id: number; slug: string }>;
    const article = articles.find(a => liveUrl.includes(a.slug));
    if (!article) return [false, "article not found in published list"];

    const upRes = await fetch(`${API}/articles/${article.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "api-key": apiKey },
      body: JSON.stringify({ article: { published: false } }),
    });
    return upRes.ok ? [true, undefined] : [false, `devto unpublish ${upRes.status}`];
  }
}
