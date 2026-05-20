import type { Variant, PublishResult } from "./models.js";
import type { Profile, StateBackend } from "./backends/base.js";

interface Adapter {
  publish(variant: Variant, profile: Profile): Promise<PublishResult>;
}

const RETRY_LIMITS: Record<string, number> = { reddit: 1 };
const DEFAULT_RETRIES = 3;

export async function publishWithRetry(
  adapter: Adapter,
  variant: Variant,
  profile: Profile,
  backend: StateBackend,
  contentId: string,
): Promise<PublishResult> {
  const existing = backend.getPostLog(contentId, variant.channel);
  if (existing?.state === "live" && existing.published_url) {
    return {
      channel: variant.channel,
      state: "live",
      live_url: existing.published_url,
      published_at: existing.updated_at,
    };
  }

  const platform = variant.channel.split(":")[0];
  const maxRetries = RETRY_LIMITS[platform] ?? DEFAULT_RETRIES;
  let lastResult: PublishResult = { channel: variant.channel, state: "failed", error: "unknown" };

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      lastResult = await adapter.publish(variant, profile);
    } catch (err) {
      lastResult = { channel: variant.channel, state: "failed", error: String(err) };
    }

    const { state } = lastResult;
    if (state === "live" || state === "needs_browser" || state === "queued") break;

    const error = lastResult.error ?? "";
    const isPermanent = /4\d\d/.test(error) && !error.includes("429");
    if (isPermanent || attempt === maxRetries) break;

    await new Promise(r => setTimeout(r, Math.pow(2, attempt) * 1_000));
  }

  backend.markPublished({
    content_id: contentId,
    channel: variant.channel,
    state: lastResult.state,
    published_url: lastResult.live_url,
    error: lastResult.error,
    updated_at: new Date().toISOString(),
  });

  return lastResult;
}
