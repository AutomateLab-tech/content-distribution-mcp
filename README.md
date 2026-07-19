# content-distribution-mcp

**Publish your content everywhere - without rewriting for every platform.**

A MCP server that distributes a single piece of content across 8+ channels (DEV.to, Hashnode, GitHub Discussions, Reddit, Bluesky, LinkedIn, Medium, Twitter) with **automatic platform-specific adaptation**, idempotent publishing, per-community anti-spam rules, and centralized state management.

## The Problem It Solves

Creating and publishing content at scale is friction-heavy:
- **Different formats**: Reddit strips formatting, Twitter has character limits, DEV.to supports embeds and rich media. Each needs customized copy.
- **Platform rules**: Subreddits enforce cooldowns and flair requirements. Communities have posting patterns and automoderator gates. LinkedIn suppresses external links.
- **State chaos**: Which posts went live where? What if a publish fails halfway? Did that Reddit post get auto-removed by spam filters?

This MCP handles distribution complexity. Write your core message once, generate platform-specific variants, publish everywhere safely.

## How It Works

1. **Your agent** generates channel-specific copy variants (rewritten titles, trimmed text, platform-appropriate tags, audience-matched tone).
2. **This MCP** publishes each variant with idempotency, OAuth, API retries, and scheduling - enforcing platform constraints automatically.
3. **You control** which platforms get what. The MCP returns per-channel hints (character limits, tag vocabularies, cooldowns) but leaves creative decisions to you.

No LLM calls inside. No walled-in agents. Just a clean API for multi-platform content distribution at scale.

## Install

```bash
npx @automatelab/content-distribution-mcp
```

Or add it permanently to your MCP host.

## Wire into your MCP host

**Claude Code** - add to `.claude/mcp.json`:

```json
{
  "mcpServers": {
    "content-distribution": {
      "command": "npx",
      "args": ["-y", "@automatelab/content-distribution-mcp"]
    }
  }
}
```

**Claude Desktop** - add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "content-distribution": {
      "command": "npx",
      "args": ["-y", "@automatelab/content-distribution-mcp"]
    }
  }
}
```

**n8n** - use the MCP Client node, point it at `npx @automatelab/content-distribution-mcp` over stdio.

**Cursor / Windsurf / any MCP host** - same `npx -y content-distribution-mcp` pattern.

## Configure credentials

The server reads credentials from a **Distribution Profile** stored in `~/.distribution-mcp/profiles.yaml`:

```yaml
# ~/.distribution-mcp/profiles.yaml
default:
  credentials:
    DEV_TO_API_KEY: "your-devto-api-key"
    HASHNODE_TOKEN: "your-hashnode-token"
    HASHNODE_PUBLICATION_ID: "your-pub-id"
    GITHUB_TOKEN: "ghp_..."
    GITHUB_DISCUSSION_REPO: "owner/repo"
    REDDIT_CLIENT_ID: "..."
    REDDIT_CLIENT_SECRET: "..."
    REDDIT_USERNAME: "..."
    REDDIT_PASSWORD: "..."
    BLUESKY_IDENTIFIER: "you.bsky.social"
    BLUESKY_PASSWORD: "..."
    XQUIK_API_KEY: "xq_..."
    XQUIK_ACCOUNT: "@your_connected_x_account"
  subreddits:
    - ClaudeAI
    - LocalLLaMA
```

Only set credentials for channels you intend to use. LinkedIn and Medium return `needs_browser` with a compose URL. Twitter/X can publish through Hermes Tweet when `XQUIK_API_KEY` or `HERMES_TWEET_API_KEY` is configured, and otherwise keeps the browser compose fallback.

## MCP tool surface

Eight tools, dot-notation names form a navigable tree (`post.*`, `channel.*`, `profile.*`, `subreddit.*`). Every tool declares an `outputSchema` (callers can type-check responses) and MCP `annotations` (read-only / destructive / idempotent / open-world hints). No LLM calls inside the server.

| Tool | Purpose |
|---|---|
| `post_publish` | Immediate publish; idempotent on `(content.id, channel)` |
| `post_schedule` | Queue variants for `schedule_at`, publish the rest immediately |
| `post_drain` | Fire all scheduled posts due now - run from cron |
| `post_status` | Per-channel state for a content piece or channel |
| `post_unpublish` | Best-effort delete (DEV.to sets unpublished; others vary) |
| `channel_hints` | Per-channel metadata: char limits, Markdown support, tag vocab |
| `profile_list` | Names of configured distribution profiles |
| `subreddit_list` | Subreddit Catalog: cooldowns, flair vocab, last-posted |

> **v2.2.0 breaking change.** Tools were renamed from flat names (`publish`, `schedule`, ...) to dot-notation (`post_publish`, `post_schedule`, ...). Update any prompts, agent skills, or n8n nodes that referenced the old names.

## Channels

| Channel key | Tier | Auth |
|---|---|---|
| `devto` | Auto | `DEV_TO_API_KEY` |
| `hashnode` | Auto | `HASHNODE_TOKEN` + `HASHNODE_PUBLICATION_ID` |
| `github_discussions` | Auto | `GITHUB_TOKEN` + `GITHUB_DISCUSSION_REPO` |
| `reddit` | Auto-gated | `REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD` |
| `bluesky` | Auto | `BLUESKY_IDENTIFIER` + `BLUESKY_PASSWORD` |
| `linkedin` | Browser fallback | returns `needs_browser` + compose URL |
| `medium` | Browser fallback | returns `needs_browser` + compose URL |
| `twitter` / `x` | Auto or browser fallback | `XQUIK_API_KEY` or `HERMES_TWEET_API_KEY`, plus `XQUIK_ACCOUNT` or `HERMES_TWEET_ACCOUNT`; falls back to `needs_browser` when no key is configured |

### Twitter/X via Hermes Tweet

The `twitter` and `x` channels can publish automatically through Hermes Tweet and Xquik. Add these fields to the selected Distribution Profile:

```yaml
default:
  credentials:
    XQUIK_API_KEY: "xq_your_key"
    XQUIK_ACCOUNT: "@your_connected_x_account"
```

`HERMES_TWEET_API_KEY` and `HERMES_TWEET_ACCOUNT` are accepted aliases. `XQUIK_BASE_URL` can point to another compatible deployment when needed. If no Hermes Tweet key is configured, the adapter returns the original browser compose URL instead of failing.

## Example agent call

```jsonc
// post_publish tool
{
  "content": {
    "id": "n8n-webhook-setup@2026-05-20",
    "title": "How to set up an n8n webhook",
    "body_md": "...",
    "tags": ["automation", "n8n", "tutorial"],
    "canonical_url": "https://yourblog.com/n8n-webhook-setup",
    "author": "You"
  },
  "variants": [
    {
      "channel": "devto:main",
      "title": "How to set up an n8n webhook",
      "body": "...",
      "tags": ["automation", "n8n", "tutorial", "devops"],
      "canonical_url": "https://yourblog.com/n8n-webhook-setup",
      "extras": {}
    },
    {
      "channel": "reddit:ClaudeAI",
      "title": "Built a webhook automation with n8n",
      "body": "Here's how I set it up...",
      "tags": [],
      "extras": { "flair": "Project" }
    }
  ],
  "profile_name": "default"
}
```

## Idempotency

Re-running `post_publish` with the same `content.id` + `channel` pair returns the existing `live_url` immediately without making another platform API call. Safe to retry on failure.

## Scheduling

Variants with `schedule_at` (ISO-8601 with timezone, e.g. `"2026-05-21T09:00:00+00:00"`) are stored in `~/.distribution-mcp/scheduled.yaml` and fired on the next `post_drain` call. Run `drain` from cron:

```bash
# fire due posts every 5 minutes
*/5 * * * * npx -y content-distribution-mcp drain
```

Or call the `post_drain` MCP tool directly from an agent.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DISTRIBUTION_BACKEND` | `yaml` | State backend (`yaml` only in v1) |
| `DISTRIBUTION_BACKEND_DIR` | `~/.distribution-mcp` | Directory for YAML state files |
| `XQUIK_API_KEY` / `HERMES_TWEET_API_KEY` | unset | Optional Hermes Tweet key for automated Twitter/X publishing |
| `XQUIK_ACCOUNT` / `HERMES_TWEET_ACCOUNT` | unset | Connected X account used when a `twitter` / `x` channel has no account suffix |
| `XQUIK_BASE_URL` | `https://xquik.com` | Optional compatible Hermes Tweet base URL |

## Requirements

- Node.js 18 or later

## Architecture

```
Agent (Claude Code / n8n / Cursor / any MCP host)
  │  generates per-channel copy, calls MCP tools
  ▼
content-distribution-mcp  (this package, stdio transport)
  │  no LLM calls - pure I/O
  ├── adapters/   devto · hashnode · github-discussions · reddit · bluesky · browser
  └── backends/   yaml (post log · profiles · schedule queue · subreddit catalog)
```

## Works with any MCP client

No Anthropic-specific code anywhere. Verify:

```bash
grep -ri "anthropic" node_modules/content-distribution-mcp/dist/  # returns nothing
```

## Part of the AutomateLab stack

- [agency-os](https://github.com/AutomateLab-tech/agency-os) - control plane
- **content-distribution-mcp** - this package
- [automatelab.tech](https://automatelab.tech) - blog and tutorials

## License

MIT
