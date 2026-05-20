import type { Variant, PublishResult, ChannelHints } from "../models.js";
import type { Profile } from "../backends/base.js";

const BSKY = "https://bsky.social/xrpc";

export class BlueskyAdapter {
  hints(): ChannelHints {
    return {
      max_length: 300,
      supported_md_features: ["links"],
      cta_placement: "bottom",
      canonical_url_supported: false,
      browser_only: false,
    };
  }

  async publish(variant: Variant, profile: Profile): Promise<PublishResult> {
    const { BLUESKY_IDENTIFIER, BLUESKY_PASSWORD } = profile.credentials;
    if (!BLUESKY_IDENTIFIER || !BLUESKY_PASSWORD) {
      return { channel: variant.channel, state: "failed", error: "BLUESKY_IDENTIFIER and BLUESKY_PASSWORD required in profile" };
    }

    const sessionRes = await fetch(`${BSKY}/com.atproto.server.createSession`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identifier: BLUESKY_IDENTIFIER, password: BLUESKY_PASSWORD }),
    });
    if (!sessionRes.ok) return { channel: variant.channel, state: "failed", error: `Bluesky auth failed: ${sessionRes.status}` };
    const session = await sessionRes.json() as { accessJwt: string; did: string };

    const text = variant.body.slice(0, 300);
    const postRes = await fetch(`${BSKY}/com.atproto.repo.createRecord`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${session.accessJwt}` },
      body: JSON.stringify({
        repo: session.did,
        collection: "app.bsky.feed.post",
        record: { $type: "app.bsky.feed.post", text, createdAt: new Date().toISOString() },
      }),
    });

    if (!postRes.ok) {
      const err = await postRes.text();
      return { channel: variant.channel, state: "failed", error: `Bluesky post failed: ${err}` };
    }

    const postData = await postRes.json() as { uri: string };
    const parts = postData.uri.split("/");
    const rkey = parts[parts.length - 1];
    const webUrl = `https://bsky.app/profile/${session.did}/post/${rkey}`;

    return { channel: variant.channel, state: "live", live_url: webUrl, published_at: new Date().toISOString() };
  }

  async unpublish(_liveUrl: string, _profile: Profile): Promise<[boolean, string]> {
    return [false, "Bluesky post deletion not yet implemented — delete via bsky.app"];
  }
}
