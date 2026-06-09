import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import path from "path";
import { buildAdapterMap } from "./adapters/index.js";
import { YamlBackend } from "./backends/yaml.js";
import * as scheduler from "./scheduler.js";
import type { Content, Variant } from "./models.js";
import type { StateBackend } from "./backends/base.js";

// ---------------------------------------------------------------------------
// Tool naming convention: dot-notation forms a navigable tree.
//   post.*      - lifecycle ops on a content piece (publish, schedule, drain, status, unpublish)
//   channel.*   - per-channel metadata (hints)
//   profile.*   - distribution profile catalog
//   subreddit.* - Subreddit Catalog
// Renamed in v2.2.0 from the previous flat names (publish, schedule, ...).
// ---------------------------------------------------------------------------

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
  channel: z.string().describe("Channel slug in the form 'platform' or 'platform:account', e.g. 'devto:main', 'reddit:ClaudeAI', 'linkedin:personal'. Use subreddit_list for reddit subreddit options."),
  title: z.string().describe("Channel-specific title for this variant; can differ from content.title to fit platform norms, e.g. shorter for DEV.to or question-form for Reddit."),
  body: z.string().describe("Channel-adapted body in Markdown or plain text per channel. Use channel_hints to check whether the channel supports Markdown."),
  tags: z.array(z.string()).default([]).describe("Channel-specific tags for this variant; overrides content.tags when present; each adapter truncates or converts to platform limits."),
  canonical_url: z.string().url().optional().describe("Canonical URL override for this variant; overrides content.canonical_url for this channel only when set."),
  cta_block: z.string().optional().describe("CTA block override for this variant; overrides content.cta_block for this channel only; appended to body before publishing."),
  schedule_at: z.string().optional().describe("ISO-8601 datetime with timezone offset for future publishing, e.g. '2026-05-21T09:00:00+01:00'. Omit for immediate publishing."),
  extras: z.record(z.unknown()).default({}).describe("Channel-specific knobs: flair (Reddit), category (GitHub Discussions), repo and series (Hashnode). Keys and types vary by adapter; use channel_hints to discover supported extras."),
});

// --- Output schemas (raw shapes for registerTool) ----------------------------

const publishResultShape = {
  channel: z.string().describe("The variant's channel slug."),
  state: z.enum(["live", "queued", "needs_browser", "failed"]).describe("Outcome of the publish attempt."),
  live_url: z.string().nullable().describe("Public URL of the live post; null when not live."),
  draft_path: z.string().nullable().describe("Local draft path for browser-fallback channels; null when not used."),
  compose_url: z.string().nullable().describe("Platform compose URL for browser-fallback channels; null when not used."),
  error: z.string().nullable().describe("Failure message; null on success."),
  published_at: z.string().nullable().describe("UTC ISO-8601 publish timestamp; null when not yet published."),
} as const;

const publishOutputShape = {
  results: z.array(z.object(publishResultShape)).describe("Per-variant results, one entry per input variant in the same order."),
} as const;

const scheduleOutputShape = publishOutputShape;
const drainOutputShape = publishOutputShape;

const statusEntryShape = {
  channel: z.string().describe("Channel slug the entry belongs to, e.g. 'devto:main'."),
  state: z.enum(["live", "queued", "needs_browser", "failed"]).describe("Current publish state: live=published, queued=scheduled for future drain, needs_browser=manual step required, failed=last attempt errored."),
  live_url: z.string().nullable().describe("Public URL of the live post; null when not yet published."),
  published_at: z.string().nullable().describe("UTC ISO-8601 timestamp of the successful publish; null when not yet live."),
  error: z.string().nullable().describe("Last error message from the platform; null when no error."),
  content_id: z.string().describe("Stable content identifier passed to post_publish / post_schedule; matches the content.id field."),
  retry_count: z.number().nullable().describe("Number of publish attempts made so far; null for first attempt."),
  next_retry_at: z.string().nullable().describe("UTC ISO-8601 of the next scheduled retry; null when not queued for retry."),
} as const;

const statusOutputShape = {
  results: z.array(z.object(statusEntryShape)).describe("Publish-log entries matching the filter."),
} as const;

const unpublishOutputShape = {
  success: z.boolean().describe("Whether the retract succeeded on the platform side."),
  error: z.string().nullable().describe("Platform error message; null on success."),
} as const;

const hintsOutputShape = {
  max_length: z.number().optional().describe("Max post length in characters; omitted when the channel has no limit."),
  supported_md_features: z.array(z.string()).describe("Markdown features the channel renders, e.g. 'links', 'code_blocks'."),
  tag_vocab: z.array(z.string()).optional().describe("Canonical tag vocabulary; omitted when the channel accepts free-form tags."),
  cta_placement: z.enum(["top", "bottom", "footer", "none"]).describe("Where the CTA block lands on this channel."),
  canonical_url_supported: z.boolean().describe("Whether the channel honours canonical_url natively."),
  browser_only: z.boolean().describe("True when posting requires the browser-fallback flow (no public API)."),
} as const;

const profileListOutputShape = {
  profiles: z.array(z.string()).describe("Configured distribution profile names."),
} as const;

const subredditEntryShape = {
  subreddit: z.string().describe("Subreddit name without the 'r/' prefix."),
  cooldown_days: z.number().optional().describe("Minimum days between posts to this subreddit."),
  flair_vocab: z.array(z.string()).optional().describe("Allowed flair IDs / labels."),
  last_posted_at: z.string().nullable().optional().describe("UTC ISO-8601 of the last successful post; null when never posted."),
  notes: z.string().optional().describe("Free-text notes about this subreddit; omitted when not set."),
} as const;

const subredditListOutputShape = {
  subreddits: z.array(z.object(subredditEntryShape)).describe("Subreddit Catalog entries matching the filter."),
} as const;

// --- Tool result helpers -----------------------------------------------------

type ToolResponse = {
  content: [{ type: "text"; text: string }];
  structuredContent?: Record<string, unknown>;
  isError?: boolean;
};

function ok(payload: Record<string, unknown>): ToolResponse {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    structuredContent: payload,
  };
}

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
  const server = new McpServer({ name: "content-distribution-mcp", version: "3.0.0" });
  const adapters = buildAdapterMap();
  const backend = buildBackend();

  // --- post_publish ---
  server.registerTool(
    "post_publish",
    {
      title: "Publish variants to one or more channels immediately",
      description: "Publish one or more channel variants immediately. Side effects: makes external HTTP requests to each channel platform; writes publish state to the local YAML backend; requires valid credentials in the named profile. Idempotent on (content.id, channel) — re-running with the same IDs returns cached state without re-posting. Use post_publish for immediate-only delivery; use post_schedule when any variant needs a future schedule_at; use post_drain to flush a previously built queue.",
      inputSchema: {
        content: ContentSchema.describe("Content piece to publish: stable id (idempotency key), title, body_md, tags, and optional cover_image / canonical_url / cta_block / author fields. The id + channel pair is the deduplication key — the same id will not be re-posted."),
        variants: z.array(VariantSchema).describe("One or more channel-specific publish targets. Each entry specifies the channel slug (e.g. 'devto:main', 'reddit:ClaudeAI'), the adapted title and body, optional schedule_at for future delivery, and channel extras such as flair. Use channel_hints to check per-channel constraints before composing."),
        profile_name: z.string().describe("Name of the distribution profile (credentials store). Use profile_list to discover available names."),
      },
      outputSchema: publishOutputShape,
      annotations: {
        title: "Publish variants to one or more channels immediately",
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async ({ content, variants, profile_name }) => {
      const profile = backend.loadProfile(profile_name);
      const results = await scheduler.publishImmediate(content as Content, variants as Variant[], profile, adapters, backend);
      return ok({ results });
    },
  );

  // --- post_schedule ---
  server.registerTool(
    "post_schedule",
    {
      title: "Schedule variants for future publishing",
      description: "Enqueue channel variants with schedule_at for future publishing; variants without schedule_at are published immediately. Side effects: writes entries to the local YAML schedule store; makes external HTTP requests for any immediately-published variants; requires credentials in the named profile. Idempotent on (content.id, channel). Use post_schedule when any variant needs a future publish time; use post_publish for all-immediate delivery; use post_drain to process the scheduled queue later.",
      inputSchema: {
        content: ContentSchema.describe("Content piece to publish: stable id (idempotency key), title, body_md, tags, and optional cover_image / canonical_url / cta_block / author fields. The id + channel pair is the deduplication key — the same id will not be re-posted."),
        variants: z.array(VariantSchema).describe("One or more channel-specific publish targets. Each entry specifies the channel slug, the adapted title and body, and optionally schedule_at (ISO-8601 with timezone) for future delivery. Variants without schedule_at are published immediately; variants with schedule_at are queued for post_drain. Use channel_hints to check per-channel constraints."),
        profile_name: z.string().describe("Name of the distribution profile (credentials store). Use profile_list to discover available names."),
      },
      outputSchema: scheduleOutputShape,
      annotations: {
        title: "Schedule variants for future publishing",
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async ({ content, variants, profile_name }) => {
      const profile = backend.loadProfile(profile_name);
      const results = await scheduler.scheduleVariants(content as Content, variants as Variant[], profile, adapters, backend);
      return ok({ results });
    },
  );

  // --- post_drain ---
  server.registerTool(
    "post_drain",
    {
      title: "Fire all scheduled posts due now",
      description: "Fire all scheduled posts due at or before the given time boundary. Side effects: makes external HTTP requests for each due entry; writes results to the YAML backend. Idempotent — already-published (content.id, channel) pairs are skipped; no-op when no entries are due. Safe to call from cron. Use post_drain on a recurring schedule to flush the queue; use post_publish or post_schedule to add new content; use post_status to inspect results after drain runs.",
      inputSchema: {
        now: z.string().optional().describe("ISO-8601 datetime boundary, e.g. '2026-05-21T09:00:00Z'; defaults to current UTC time when omitted."),
      },
      outputSchema: drainOutputShape,
      annotations: {
        title: "Fire all scheduled posts due now",
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async ({ now }) => {
      const results = await scheduler.drain(adapters, backend, now ? new Date(now) : undefined);
      return ok({ results });
    },
  );

  // --- post_status ---
  server.registerTool(
    "post_status",
    {
      title: "Read publish state for content pieces",
      description: "Return publish state for content pieces. Filters by content_id, channel, or both; returns all entries when neither is given. Side effects: read-only; no external HTTP calls; no auth needed. Deterministic given unchanged backend state. Use post_status to inspect what has been published, what is queued, or what errored; use post_publish, post_schedule, or post_drain to change state.",
      inputSchema: {
        content_id: z.string().optional().describe("Filter to a specific content piece by its stable ID; omit to return state for all content."),
        channel: z.string().optional().describe("Filter to a specific channel slug, e.g. 'devto', 'reddit:ClaudeAI'; omit to return state for all channels."),
      },
      outputSchema: statusOutputShape,
      annotations: {
        title: "Read publish state for content pieces",
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
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
      return ok({ results });
    },
  );

  // --- post_unpublish ---
  server.registerTool(
    "post_unpublish",
    {
      title: "Retract a published post (best-effort)",
      description: "Best-effort delete of a published post on the target platform. Side effects: makes an external HTTP DELETE or update request; DEV.to sets published=false (soft delete); platforms without a delete API return success=false without error. Non-idempotent — calling on an already-deleted URL may return a platform 404. Use post_unpublish to retract a live post; use post_status first to obtain the live_url; use post_publish to re-publish after an unpublish.",
      inputSchema: {
        live_url: z.string().describe("URL of the live published post to retract, e.g. 'https://dev.to/user/post-slug'."),
        channel: z.string().describe("Channel slug the post was published to, e.g. 'devto', 'hashnode', 'reddit:ClaudeAI'."),
      },
      outputSchema: unpublishOutputShape,
      annotations: {
        title: "Retract a published post (best-effort)",
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    async ({ live_url, channel }) => {
      const platform = channel.split(":")[0];
      const adapter = adapters[platform] as { unpublish?(url: string, profile: unknown): Promise<[boolean, string | undefined]> } | undefined;
      if (!adapter?.unpublish) {
        return ok({ success: false, error: `no adapter for '${channel}'` });
      }
      let profile;
      try {
        const profiles = backend.listProfiles();
        profile = profiles.length ? backend.loadProfile(profiles[0]) : { name: "default", credentials: {} };
      } catch {
        profile = { name: "default", credentials: {} };
      }
      const [success, error] = await adapter.unpublish(live_url, profile);
      return ok({ success, error: error ?? null });
    },
  );

  // --- channel_hints ---
  server.registerTool(
    "channel_hints",
    {
      title: "Static per-channel metadata",
      description: "Return static per-channel metadata: character limits, Markdown support flags, tag vocabulary, and CTA placement rules. Side effects: read-only; no external HTTP calls; no auth needed. Fully deterministic — returns compile-time adapter constants. Use channel_hints before composing a variant body to understand channel constraints; use post_publish or post_schedule once you have a valid variant.",
      inputSchema: {
        channel: z.string().describe("Channel platform name, e.g. 'devto', 'reddit', 'hashnode', 'bluesky'. Use the platform prefix only, not the full 'platform:account' form."),
      },
      outputSchema: hintsOutputShape,
      annotations: {
        title: "Static per-channel metadata",
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    ({ channel }) => {
      const platform = channel.split(":")[0];
      const adapter = adapters[platform] as { hints?(): unknown } | undefined;
      if (!adapter?.hints) {
        throw new Error(`No adapter for '${channel}'. Available: ${Object.keys(adapters).filter(k => !k.includes("-")).join(", ")}`);
      }
      return ok(adapter.hints() as Record<string, unknown>);
    },
  );

  // --- profile_list ---
  server.registerTool(
    "profile_list",
    {
      title: "List configured distribution profiles",
      description: "Return all distribution profile names configured in the YAML backend. Side effects: read-only; no external HTTP calls. Deterministic given backend state. Use profile_list to discover available profiles before calling post_publish, post_schedule, or subreddit_list; then pass the chosen name as profile_name.",
      inputSchema: {},
      outputSchema: profileListOutputShape,
      annotations: {
        title: "List configured distribution profiles",
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    () => {
      const profiles = backend.listProfiles();
      return ok({ profiles });
    },
  );

  // --- subreddit_list ---
  server.registerTool(
    "subreddit_list",
    {
      title: "List Subreddit Catalog entries",
      description: "Return all subreddits in the Subreddit Catalog with cooldown windows, flair vocabulary, and last-posted metadata. Optionally filtered to subreddits allowed by the named profile. Side effects: read-only; no external HTTP calls. Deterministic given backend state. Use subreddit_list to select a subreddit and obtain flair IDs before composing a reddit: channel variant; pass flair in variant.extras.flair.",
      inputSchema: {
        profile_name: z.string().optional().describe("Optional profile name to filter subreddits to those allowed by that profile; omit to return the full catalog."),
      },
      outputSchema: subredditListOutputShape,
      annotations: {
        title: "List Subreddit Catalog entries",
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
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
      return ok({ subreddits });
    },
  );

  return server;
}
