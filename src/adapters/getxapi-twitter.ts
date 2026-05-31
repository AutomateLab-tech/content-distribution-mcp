import type { Variant, PublishResult, ChannelHints } from "../models.js";
import type { Profile } from "../backends/base.js";

const DEFAULT_GETXAPI_BASE_URL = "https://api.getxapi.com";

export class GetXAPITwitterAdapter {
  hints(): ChannelHints {
    return {
      max_length: 280,
      supported_md_features: ["links"],
      cta_placement: "bottom",
      canonical_url_supported: false,
      browser_only: false,
    };
  }

  async publish(variant: Variant, profile: Profile): Promise<PublishResult> {
    const apiKey = profile.credentials.GETXAPI_API_KEY;
    if (!apiKey) {
      return { channel: variant.channel, state: "failed", error: "GETXAPI_API_KEY required in profile" };
    }

    const enableActions = profile.credentials.GETXAPI_ENABLE_ACTIONS === "true";
    if (!enableActions) {
      return { channel: variant.channel, state: "failed", error: "GETXAPI_ENABLE_ACTIONS must be true to publish writes" };
    }

    const baseUrl = (profile.credentials.GETXAPI_BASE_URL || DEFAULT_GETXAPI_BASE_URL).replace(/\/+$/, "");
    const text = variant.body.slice(0, 280);

    const res = await fetch(`${baseUrl}/twitter/tweet/create`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${apiKey}`,
      },
      body: JSON.stringify({ text }),
    });

    if (!res.ok) {
      const err = await res.text();
      return { channel: variant.channel, state: "failed", error: `GetXAPI publish failed: ${res.status} ${err.slice(0, 180)}` };
    }

    const data = await res.json() as { id?: string; tweet_id?: string; url?: string };
    const tweetId = data.id || data.tweet_id;
    const liveUrl = data.url || (tweetId ? `https://x.com/i/web/status/${tweetId}` : "");

    return {
      channel: variant.channel,
      state: "live",
      live_url: liveUrl,
      published_at: new Date().toISOString(),
    };
  }

  async unpublish(_liveUrl: string, _profile: Profile): Promise<[boolean, string]> {
    return [false, "GetXAPI tweet deletion not yet implemented"];
  }
}
