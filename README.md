# content-distribution-mcp

**Publish your content everywhere—without rewriting for every platform.**

A [Model Context Protocol](https://modelcontextprotocol.io/) server that distributes a single piece of content across 8+ channels (DEV.to, Hashnode, GitHub Discussions, Reddit, Bluesky, LinkedIn, Medium, Twitter) with **automatic platform-specific adaptation**, idempotent publishing, per-community anti-spam rules, and centralized state management.

## The Problem It Solves

Creating and publishing content at scale is friction-heavy:
- **Different formats**: Reddit strips formatting, Twitter has character limits, DEV.to supports embeds and rich media. Each needs customized copy.
- **Platform rules**: Subreddits enforce cooldowns and flair requirements. Communities have posting patterns and automoderator gates. LinkedIn suppresses external links.
- **State chaos**: Which posts went live where? What if a publish fails halfway? Did that Reddit post get auto-removed by spam filters?
- **Auth friction**: OAuth flows, API credentials, browser automation for platforms without APIs—managing it all is exhausting.

This MCP handles distribution complexity. Write your core message once, generate platform-specific variants, publish everywhere safely.

## How It Works

1. **Your agent** generates channel-specific copy variants (rewritten titles, trimmed text, platform-appropriate tags, audience-matched tone).
2. **This MCP** publishes each variant with idempotency, OAuth, API retries, and scheduling—enforcing platform constraints automatically.
3. **You control** which platforms get what. The MCP returns per-channel hints (character limits, tag vocabularies, cooldowns) but leaves creative decisions to you.

No LLM calls inside. No walled-in agents. Just a clean API for multi-platform content distribution at scale.

## Install

```bash
npx @automatelab/content-distribution-mcp
```

Or add it permanently to your MCP host.

## Wire into your MCP host

**Claude Code** — add to `.claude/mcp.json`:

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

**Claude Desktop** — add to `claude_desktop_config.json`:

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

**n8n** — use the MCP Client node, point it at `npx @automatelab/content-distribution-mcp` over stdio.

**Cursor / Windsurf / any MCP host** — same `npx -y content-distribution-mcp` pattern.

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
  subreddits:
    - ClaudeAI
    - LocalLLaMA
```

Only set credentials for channels you intend to use. LinkedIn, Medium, and Twitter/X return `needs_browser` with a compose URL — no credentials needed.

## MCP tool surface

Eight tools. No LLM calls inside the server.

| Tool | Purpose |
|---|---|
| `publish` | Immediate publish; idempotent on `(content.id, channel)` |
| `schedule` | Queue variants for `schedule_at`, publish the rest immediately |
| `drain` | Fire all scheduled posts due now — run from cron |
| `status` | Per-channel state for a content piece or channel |
| `unpublish` | Best-effort delete (DEV.to sets unpublished; others vary) |
| `hints` | Per-channel metadata: char limits, Markdown support, tag vocab |
| `list_profiles` | Names of configured distribution profiles |
| `list_subreddits` | Subreddit Catalog: cooldowns, flair vocab, last-posted |

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
| `twitter` / `x` | Browser fallback | returns `needs_browser` + compose URL |

## Example agent call

```jsonc
// publish tool
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

Re-running `publish` with the same `content.id` + `channel` pair returns the existing `live_url` immediately without making another platform API call. Safe to retry on failure.

## Scheduling

Variants with `schedule_at` (ISO-8601 with timezone, e.g. `"2026-05-21T09:00:00+00:00"`) are stored in `~/.distribution-mcp/scheduled.yaml` and fired on the next `drain` call. Run `drain` from cron:

```bash
# fire due posts every 5 minutes
*/5 * * * * npx -y content-distribution-mcp drain
```

Or call the `drain` MCP tool directly from an agent.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DISTRIBUTION_BACKEND` | `yaml` | State backend (`yaml` only in v1) |
| `DISTRIBUTION_BACKEND_DIR` | `~/.distribution-mcp` | Directory for YAML state files |

## Requirements

- Node.js 18 or later

## Architecture

```
Agent (Claude Code / n8n / Cursor / any MCP host)
  │  generates per-channel copy, calls MCP tools
  ▼
content-distribution-mcp  (this package, stdio transport)
  │  no LLM calls — pure I/O
  ├── adapters/   devto · hashnode · github-discussions · reddit · bluesky · browser
  └── backends/   yaml (post log · profiles · schedule queue · subreddit catalog)
```

## Works with any MCP client

No Anthropic-specific code anywhere. Verify:

```bash
grep -ri "anthropic" node_modules/content-distribution-mcp/dist/  # returns nothing
```

## Part of the AutomateLab stack

- [agency-os](https://github.com/AutomateLab-tech/agency-os) — control plane
- **content-distribution-mcp** — this package
- [automatelab.tech](https://automatelab.tech) — blog and tutorials

## License

MIT
