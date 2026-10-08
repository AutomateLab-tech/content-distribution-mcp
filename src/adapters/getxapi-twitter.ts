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

    const authToken = profile.credentials.GETXAPI_AUTH_TOKEN;
    if (!authToken) {
      return { channel: variant.channel, state: "failed", error: "GETXAPI_AUTH_TOKEN required in profile" };
    }
    if (!variant.body.trim() || Array.from(variant.body).length > 280) {
      return { channel: variant.channel, state: "failed", error: "GetXAPI tweet text must contain 1 to 280 characters; shorten the variant before publishing" };
    }

    const baseUrl = (profile.credentials.GETXAPI_BASE_URL || DEFAULT_GETXAPI_BASE_URL).replace(/\/+$/, "");
    const text = variant.body;

    const res = await fetch(`${baseUrl}/twitter/tweet/create`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${apiKey}`,
      },
      body: JSON.stringify({ auth_token: authToken, text }),
    });

    if (!res.ok) {
      // Remote error bodies can echo account credentials. Keep them out of logs.
      return { channel: variant.channel, state: "failed", error: `GetXAPI publish failed: HTTP ${res.status}` };
    }

    const data = await res.json() as { status?: string; data?: { id?: string } };
    const tweetId = data?.data?.id;
    if (data?.status !== "success" || typeof tweetId !== "string" || !/^\d+$/.test(tweetId)) {
      return { channel: variant.channel, state: "failed", error: "GetXAPI did not confirm a created tweet ID" };
    }
    const liveUrl = `https://x.com/i/web/status/${tweetId}`;

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
