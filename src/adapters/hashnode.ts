import type { Variant, PublishResult, ChannelHints } from "../models.js";
import type { Profile } from "../backends/base.js";

const GQL = "https://gql.hashnode.com";

export class HashnodeAdapter {
  hints(): ChannelHints {
    return {
      max_length: 50_000,
      supported_md_features: ["bold", "italic", "code_inline", "code_block", "links", "headers", "images", "lists", "tables", "blockquote"],
      tag_vocab: ["ai", "automation", "mcp", "claude", "devops", "tutorial", "productivity", "llm"],
      cta_placement: "bottom",
      canonical_url_supported: true,
      browser_only: false,
    };
  }

  async publish(variant: Variant, profile: Profile): Promise<PublishResult> {
    const token = profile.credentials["HASHNODE_TOKEN"];
    const pubId = profile.credentials["HASHNODE_PUBLICATION_ID"];
    if (!token || !pubId) {
      return { channel: variant.channel, state: "failed", error: "HASHNODE_TOKEN or HASHNODE_PUBLICATION_ID not set in profile" };
    }

    const query = `
      mutation PublishPost($input: PublishPostInput!) {
        publishPost(input: $input) { post { url } }
      }
    `;
    const variables = {
      input: {
        title: variant.title,
        contentMarkdown: variant.body,
        tags: variant.tags.slice(0, 5).map(t => ({ slug: t.toLowerCase().replace(/\s+/g, "-"), name: t })),
        publicationId: pubId,
        ...(variant.canonical_url ? { originalArticleURL: variant.canonical_url } : {}),
        ...(variant.extras?.subtitle ? { subtitle: variant.extras.subtitle } : {}),
      },
    };

    const res = await fetch(GQL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": token },
      body: JSON.stringify({ query, variables }),
    });

    const json = await res.json() as { data?: { publishPost?: { post: { url: string } } }; errors?: unknown[] };
    const url = json.data?.publishPost?.post?.url;
    if (!url) return { channel: variant.channel, state: "failed", error: JSON.stringify(json.errors ?? "no URL returned") };

    return { channel: variant.channel, state: "live", live_url: url, published_at: new Date().toISOString() };
  }

  async unpublish(_liveUrl: string, _profile: Profile): Promise<[boolean, string]> {
    return [false, "Hashnode delete requires post ID — remove manually in the Hashnode dashboard"];
  }
}
