import type { Content, Variant, PublishResult } from "./models.js";
import type { Profile, StateBackend } from "./backends/base.js";
import { publishWithRetry } from "./idempotency.js";

interface Adapter {
  publish(variant: Variant, profile: Profile): Promise<PublishResult>;
}

export async function publishImmediate(
  content: Content,
  variants: Variant[],
  profile: Profile,
  adapters: Record<string, Adapter>,
  backend: StateBackend,
): Promise<PublishResult[]> {
  return Promise.all(
    variants.map(async (variant) => {
      const platform = variant.channel.split(":")[0];
      const adapter = adapters[platform];
      if (!adapter) return { channel: variant.channel, state: "failed" as const, error: `no-adapter: ${platform}` };
      return publishWithRetry(adapter, variant, profile, backend, content.id);
    }),
  );
}

export async function scheduleVariants(
  content: Content,
  variants: Variant[],
  profile: Profile,
  adapters: Record<string, Adapter>,
  backend: StateBackend,
): Promise<Record<string, PublishResult | string>> {
  const results: Record<string, PublishResult | string> = {};
  await Promise.all(
    variants.map(async (variant) => {
      if (variant.schedule_at) {
        results[variant.channel] = backend.enqueueScheduled(
          content.id,
          variant.channel,
          { content, variant, profile_name: profile.name },
          variant.schedule_at,
        );
      } else {
        const platform = variant.channel.split(":")[0];
        const adapter = adapters[platform];
        if (!adapter) {
          results[variant.channel] = { channel: variant.channel, state: "failed" as const, error: `no-adapter: ${platform}` };
        } else {
          results[variant.channel] = await publishWithRetry(adapter, variant, profile, backend, content.id);
        }
      }
    }),
  );
  return results;
}

export async function drain(
  adapters: Record<string, Adapter>,
  backend: StateBackend,
  now?: Date,
): Promise<PublishResult[]> {
  const boundary = (now ?? new Date()).toISOString();
  const due = backend.listScheduled(boundary);
  const results: PublishResult[] = [];

  for (const item of due) {
    backend.dequeueScheduled(item.id);
    const payload = item.variant as { content: Content; variant: Variant; profile_name?: string };
    const profileName = payload.profile_name ?? "default";
    let profile: Profile;
    try {
      profile = backend.loadProfile(profileName);
    } catch {
      results.push({ channel: item.channel, state: "failed", error: `profile '${profileName}' not found` });
      continue;
    }
    const platform = item.channel.split(":")[0];
    const adapter = adapters[platform];
    if (!adapter) {
      results.push({ channel: item.channel, state: "failed", error: `no-adapter: ${platform}` });
      continue;
    }
    results.push(await publishWithRetry(adapter, payload.variant, profile, backend, payload.content.id));
  }

  return results;
}
