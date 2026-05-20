import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import path from "path";
import { buildAdapterMap } from "./adapters/index.js";
import { YamlBackend } from "./backends/yaml.js";
import * as scheduler from "./scheduler.js";
import type { Content, Variant } from "./models.js";
import type { StateBackend } from "./backends/base.js";

const ContentSchema = z.object({
  id: z.string().describe("Stable identifier, e.g. 'my-post@2026-05-20'. Used as the idempotency key together with channel; must be unique per content piece."),
  title: z.string().describe("Title of the content piece; used as the post title on most channels."),
  subtitle: z.string().optional().describe("Optional subtitle; used on Hashnode and DEV.to subtitle fields; ignored by channels that do not support subtitles."),
  body_md: z.string().describe("Full body in Markdown. Each channel adapter converts or truncates to platform requirements."),
  cover_image: z.string().url().optional().describe("Optional URL of a cover image; used as the header image on Hashnode and DEV.to; omit if no image is available."),
  tags: z.array(z.string()).default([]).describe("Canonical tag list; each channel adapter normalizes, truncates, or converts to platform tag syntax."),
  canonical_url: z.string().url().optional().describe("Canonical URL of the authoritative source; set as canonical_url on DEV.to and Hashnode to signal the original for SEO; omit when publishing first to that channel."),
  cta_block: z.string().optional().describe("Optional call-to-action block appended to the post body on channels that support it; formatted as Markdown."),
  author: z.string().describe("Author display name; used in author metadata on channels that accept it."),
  source_task_id: z.string().optional().describe("Optional tracing ID (e.g. Notion task ID); stored in publish state for correlation but never sent to platforms."),
});

const VariantSchema = z.object({
  channel: z.string().describe("Channel slug in the form 'platform' or 'platform:account', e.g. 'devto:main', 'reddit:ClaudeAI', 'linkedin:personal'. Use list_subreddits for reddit subreddit options."),
  title: z.string().describe("Channel-specific title for this variant; can differ from content.title to fit platform norms, e.g. shorter for DEV.to or question-form for Reddit."),
  body: z.string().describe("Channel-adapted body in Markdown or plain text per channel. Use hints to check whether the channel supports Markdown."),
  tags: z.array(z.string()).default([]).describe("Channel-specific tags for this variant; overrides content.tags when present; each adapter truncates or converts to platform limits."),
  canonical_url: z.string().url().optional().describe("Canonical URL override for this variant; overrides content.canonical_url for this channel only when set."),
  cta_block: z.string().optional().describe("CTA block override for this variant; overrides content.cta_block for this channel only; appended to body before publishing."),
  schedule_at: z.string().optional().describe("ISO-8601 datetime with timezone offset for future publishing, e.g. '2026-05-21T09:00:00+01:00'. Omit for immediate publishing."),
  extras: z.record(z.unknown()).default({}).describe("Channel-specific knobs: flair (Reddit), category (GitHub Discussions), repo and series (Hashnode). Keys and types vary by adapter; use hints to discover supported extras."),
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
    "Publish one or more channel variants immediately. Side effects: makes external HTTP requests to each channel platform; writes publish state to the local YAML backend; requires valid credentials in the named profile. Idempotent on (content.id, channel) — re-running with the same IDs returns cached state without re-posting. Use publish for immediate-only delivery; use schedule when any variant needs a future schedule_at; use drain to flush a previously built queue.",
    {
      content: ContentSchema,
      variants: z.array(VariantSchema),
      profile_name: z.string().describe("Name of the distribution profile (credentials store). Use list_profiles to discover available names."),
    },
    async ({ content, variants, profile_name }) => {
      const profile = backend.loadProfile(profile_name);
      const results = await scheduler.publishImmediate(content as Content, variants as Variant[], profile, adapters, backend);
      return { content: [{ type: "text", text: JSON.stringify(results, null, 2) }] };
    },
  );

  server.tool(
    "schedule",
    "Enqueue channel variants with schedule_at for future publishing; variants without schedule_at are published immediately. Side effects: writes entries to the local YAML schedule store; makes external HTTP requests for any immediately-published variants; requires credentials in the named profile. Idempotent on (content.id, channel). Use schedule when any variant needs a future publish time; use publish for all-immediate delivery; use drain to process the scheduled queue later.",
    {
      content: ContentSchema,
      variants: z.array(VariantSchema),
      profile_name: z.string().describe("Name of the distribution profile (credentials store). Use list_profiles to discover available names."),
    },
    async ({ content, variants, profile_name }) => {
      const profile = backend.loadProfile(profile_name);
      const results = await scheduler.scheduleVariants(content as Content, variants as Variant[], profile, adapters, backend);
      return { content: [{ type: "text", text: JSON.stringify(results, null, 2) }] };
    },
  );

  server.tool(
    "drain",
    "Fire all scheduled posts due at or before the given time boundary. Side effects: makes external HTTP requests for each due entry; writes results to the YAML backend. Idempotent — already-published (content.id, channel) pairs are skipped; no-op when no entries are due. Safe to call from cron. Use drain on a recurring schedule to flush the queue; use publish or schedule to add new content; use status to inspect results after drain runs.",
    { now: z.string().optional().describe("ISO-8601 datetime boundary, e.g. '2026-05-21T09:00:00Z'; defaults to current UTC time when omitted.") },
    async ({ now }) => {
      const results = await scheduler.drain(adapters, backend, now ? new Date(now) : undefined);
      return { content: [{ type: "text", text: JSON.stringify(results, null, 2) }] };
    },
  );

  server.tool(
    "status",
    "Return publish state for content pieces. Filters by content_id, channel, or both; returns all entries when neither is given. Side effects: read-only; no external HTTP calls; no auth needed. Deterministic given unchanged backend state. Use status to inspect what has been published, what is queued, or what errored; use publish, schedule, or drain to change state.",
    {
      content_id: z.string().optional().describe("Filter to a specific content piece by its stable ID; omit to return state for all content."),
      channel: z.string().optional().describe("Filter to a specific channel slug, e.g. 'devto', 'reddit:ClaudeAI'; omit to return state for all channels."),
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
    "Best-effort delete of a published post on the target platform. Side effects: makes an external HTTP DELETE or update request; DEV.to sets published=false (soft delete); platforms without a delete API return success=false without error. Non-idempotent — calling on an already-deleted URL may return a platform 404. Use unpublish to retract a live post; use status first to obtain the live_url; use publish to re-publish after an unpublish.",
    {
      live_url: z.string().describe("URL of the live published post to retract, e.g. 'https://dev.to/user/post-slug'."),
      channel: z.string().describe("Channel slug the post was published to, e.g. 'devto', 'hashnode', 'reddit:ClaudeAI'."),
    },
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
    "Return static per-channel metadata: character limits, Markdown support flags, tag vocabulary, and CTA placement rules. Side effects: read-only; no external HTTP calls; no auth needed. Fully deterministic — returns compile-time adapter constants. Use hints before composing a variant body to understand channel constraints; use publish or schedule once you have a valid variant.",
    { channel: z.string().describe("Channel platform name, e.g. 'devto', 'reddit', 'hashnode', 'bluesky'. Use the platform prefix only, not the full 'platform:account' form.") },
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
    "Return all distribution profile names configured in the YAML backend. Side effects: read-only; no external HTTP calls. Deterministic given backend state. Use list_profiles to discover available profiles before calling publish, schedule, or list_subreddits; then pass the chosen name as profile_name.",
    {},
    () => {
      const profiles = backend.listProfiles();
      return { content: [{ type: "text", text: JSON.stringify(profiles, null, 2) }] };
    },
  );

  server.tool(
    "list_subreddits",
    "Return all subreddits in the Subreddit Catalog with cooldown windows, flair vocabulary, and last-posted metadata. Optionally filtered to subreddits allowed by the named profile. Side effects: read-only; no external HTTP calls. Deterministic given backend state. Use list_subreddits to select a subreddit and obtain flair IDs before composing a reddit: channel variant; pass flair in variant.extras.flair.",
    { profile_name: z.string().optional().describe("Optional profile name to filter subreddits to those allowed by that profile; omit to return the full catalog.") },
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
