import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import path from "path";
import { buildAdapterMap } from "./adapters/index.js";
import { YamlBackend } from "./backends/yaml.js";
import * as scheduler from "./scheduler.js";
import type { Content, Variant } from "./models.js";
import type { StateBackend } from "./backends/base.js";

const ContentSchema = z.object({
  id: z.string().describe("Stable identifier, e.g. 'my-post@2026-05-20'"),
  title: z.string(),
  subtitle: z.string().optional(),
  body_md: z.string().describe("Full body in Markdown"),
  cover_image: z.string().url().optional(),
  tags: z.array(z.string()).default([]),
  canonical_url: z.string().url().optional(),
  cta_block: z.string().optional(),
  author: z.string(),
  source_task_id: z.string().optional(),
});

const VariantSchema = z.object({
  channel: z.string().describe("e.g. 'devto:main', 'reddit:ClaudeAI', 'linkedin:personal'"),
  title: z.string(),
  body: z.string().describe("Channel-adapted body (Markdown or plain text per channel)"),
  tags: z.array(z.string()).default([]),
  canonical_url: z.string().url().optional(),
  cta_block: z.string().optional(),
  schedule_at: z.string().optional().describe("ISO-8601 with timezone offset for future publishing"),
  extras: z.record(z.unknown()).default({}).describe("Channel-specific knobs: flair (Reddit), category (GitHub Discussions), repo, series"),
});

function buildBackend(): StateBackend {
  const name = (process.env.DISTRIBUTION_BACKEND ?? "yaml").toLowerCase();
  if (name === "yaml") {
    const dir = process.env.DISTRIBUTION_BACKEND_DIR
      ?? path.join(process.env.HOME ?? process.env.USERPROFILE ?? "~", ".distribution-mcp");
    return new YamlBackend(dir);
  }
  throw new Error(`Unknown DISTRIBUTION_BACKEND=${name}. Valid values: 'yaml'`);
}

export function createServer() {
  const server = new McpServer({ name: "content-distribution-mcp", version: "1.0.0" });
  const adapters = buildAdapterMap();
  const backend = buildBackend();

  server.tool(
    "publish",
    "Publish one or more channel variants immediately. Idempotent on (content.id, channel) — safe to re-run.",
    {
      content: ContentSchema,
      variants: z.array(VariantSchema),
      profile_name: z.string().describe("Name of the distribution profile (credentials store)"),
    },
    async ({ content, variants, profile_name }) => {
      const profile = backend.loadProfile(profile_name);
      const results = await scheduler.publishImmediate(content as Content, variants as Variant[], profile, adapters, backend);
      return { content: [{ type: "text", text: JSON.stringify(results, null, 2) }] };
    },
  );

  server.tool(
    "schedule",
    "Enqueue variants with schedule_at for future publishing; publish variants without schedule_at immediately.",
    {
      content: ContentSchema,
      variants: z.array(VariantSchema),
      profile_name: z.string(),
    },
    async ({ content, variants, profile_name }) => {
      const profile = backend.loadProfile(profile_name);
      const results = await scheduler.scheduleVariants(content as Content, variants as Variant[], profile, adapters, backend);
      return { content: [{ type: "text", text: JSON.stringify(results, null, 2) }] };
    },
  );

  server.tool(
    "drain",
    "Fire all scheduled posts due at or before now. Idempotent and safe to call from cron.",
    { now: z.string().optional().describe("ISO-8601 boundary; defaults to current UTC time") },
    async ({ now }) => {
      const results = await scheduler.drain(adapters, backend, now ? new Date(now) : undefined);
      return { content: [{ type: "text", text: JSON.stringify(results, null, 2) }] };
    },
  );

  server.tool(
    "status",
    "Return publish state for content pieces. Query by content_id, channel, or both.",
    {
      content_id: z.string().optional(),
      channel: z.string().optional(),
    },
    ({ content_id, channel }) => {
      const entries = backend.listPostLog({ content_id, channel });
      const results = entries.map(e => ({
        channel: e.channel,
        state: e.state,
        live_url: e.published_url ?? null,
        published_at: e.updated_at ?? null,
        error: e.error ?? null,
        content_id: e.content_id,
        retry_count: e.retry_count ?? null,
        next_retry_at: e.next_retry_at ?? null,
      }));
      return { content: [{ type: "text", text: JSON.stringify(results, null, 2) }] };
    },
  );

  server.tool(
    "unpublish",
    "Best-effort delete of a published post. DEV.to sets published=false; others may not support API deletion.",
    { live_url: z.string(), channel: z.string() },
    async ({ live_url, channel }) => {
      const platform = channel.split(":")[0];
      const adapter = adapters[platform] as { unpublish?(url: string, profile: unknown): Promise<[boolean, string | undefined]> } | undefined;
      if (!adapter?.unpublish) {
        return { content: [{ type: "text", text: JSON.stringify({ success: false, error: `no adapter for '${channel}'` }) }] };
      }
      let profile;
      try {
        const profiles = backend.listProfiles();
        profile = profiles.length ? backend.loadProfile(profiles[0]) : { name: "default", credentials: {} };
      } catch {
        profile = { name: "default", credentials: {} };
      }
      const [success, error] = await adapter.unpublish(live_url, profile);
      return { content: [{ type: "text", text: JSON.stringify({ success, error: error ?? null }) }] };
    },
  );

  server.tool(
    "hints",
    "Return static per-channel metadata: char limits, Markdown support, tag vocab, CTA placement.",
    { channel: z.string().describe("e.g. 'devto', 'reddit', 'hashnode', 'bluesky'") },
    ({ channel }) => {
      const platform = channel.split(":")[0];
      const adapter = adapters[platform] as { hints?(): unknown } | undefined;
      if (!adapter?.hints) {
        throw new Error(`No adapter for '${channel}'. Available: ${Object.keys(adapters).filter(k => !k.includes("-")).join(", ")}`);
      }
      return { content: [{ type: "text", text: JSON.stringify(adapter.hints(), null, 2) }] };
    },
  );

  server.tool(
    "list_profiles",
    "Return all distribution profile names configured in the StateBackend.",
    {},
    () => {
      const profiles = backend.listProfiles();
      return { content: [{ type: "text", text: JSON.stringify(profiles, null, 2) }] };
    },
  );

  server.tool(
    "list_subreddits",
    "Return all subreddits in the Subreddit Catalog with cooldown, flair vocab, and last-posted metadata.",
    { profile_name: z.string().optional() },
    ({ profile_name }) => {
      let subreddits = backend.listSubreddits();
      if (profile_name) {
        const profile = backend.loadProfile(profile_name);
        const allowed = new Set([
          ...(profile.subreddits ?? []),
          ...(profile.channels ?? [])
            .filter(c => c.channel.startsWith("reddit:"))
            .map(c => c.channel.split(":")[1]),
        ]);
        if (allowed.size > 0) subreddits = subreddits.filter(s => allowed.has(s.subreddit));
      }
      return { content: [{ type: "text", text: JSON.stringify(subreddits, null, 2) }] };
    },
  );

  return server;
}
