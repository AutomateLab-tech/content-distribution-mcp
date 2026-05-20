import type { Variant, PublishResult, ChannelHints } from "../models.js";
import type { Profile } from "../backends/base.js";

export class RedditAdapter {
  hints(): ChannelHints {
    return {
      max_length: 40_000,
      supported_md_features: ["bold", "italic", "code_inline", "links", "lists"],
      cta_placement: "none",
      canonical_url_supported: false,
      browser_only: false,
    };
  }

  async publish(variant: Variant, profile: Profile): Promise<PublishResult> {
    const { REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD } = profile.credentials;
    if (!REDDIT_CLIENT_ID || !REDDIT_CLIENT_SECRET || !REDDIT_USERNAME || !REDDIT_PASSWORD) {
      return { channel: variant.channel, state: "failed", error: "Reddit credentials required: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD" };
    }

    const subreddit = variant.channel.split(":")[1]?.replace(/^r\//, "") ?? "test";
    const ua = `content-distribution-mcp/1.0 by ${REDDIT_USERNAME}`;

    const tokenRes = await fetch("https://www.reddit.com/api/v1/access_token", {
      method: "POST",
      headers: {
        "Authorization": `Basic ${Buffer.from(`${REDDIT_CLIENT_ID}:${REDDIT_CLIENT_SECRET}`).toString("base64")}`,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": ua,
      },
      body: `grant_type=password&username=${encodeURIComponent(REDDIT_USERNAME)}&password=${encodeURIComponent(REDDIT_PASSWORD)}`,
    });

    if (!tokenRes.ok) return { channel: variant.channel, state: "failed", error: `Reddit auth failed: ${tokenRes.status}` };
    const { access_token } = await tokenRes.json() as { access_token: string };

    const form = new URLSearchParams({
      sr: subreddit,
      title: variant.title,
      resubmit: "true",
      nsfw: "false",
      ...(variant.extras?.url
        ? { kind: "link", url: variant.extras.url as string }
        : { kind: "self", text: variant.body }),
      ...(variant.extras?.flair ? { flair_text: variant.extras.flair as string } : {}),
    });

    const submitRes = await fetch("https://oauth.reddit.com/api/submit", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${access_token}`,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": ua,
      },
      body: form.toString(),
    });

    const submitJson = await submitRes.json() as { json?: { data?: { url?: string }; errors?: unknown[] } };
    const errors = submitJson.json?.errors;
    if (errors?.length) return { channel: variant.channel, state: "failed", error: JSON.stringify(errors) };

    const postUrl = submitJson.json?.data?.url;
    if (!postUrl) return { channel: variant.channel, state: "failed", error: "No URL in Reddit response" };

    return { channel: variant.channel, state: "live", live_url: postUrl, published_at: new Date().toISOString() };
  }

  async unpublish(_liveUrl: string, _profile: Profile): Promise<[boolean, string]> {
    return [false, "Reddit post deletion requires the post fullname (t3_xxx) — delete manually via Reddit or the API"];
  }
}
