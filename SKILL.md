---
name: content-distribution
description: Use when the user wants to publish a post, article, or announcement to multiple platforms at once — DEV.to, Hashnode, GitHub Discussions, Reddit, Bluesky, LinkedIn, Medium, or Twitter/X. Handles platform-specific format adaptation, idempotent re-publish, per-community anti-spam rules, and scheduling. Write your message once; this skill routes it everywhere.
version: 3.0.0
license: MIT
homepage: https://github.com/AutomateLab-tech/content-distribution-mcp
compatibility:
  hosts:
    - claude-code
    - cursor
    - claude-desktop
    - windsurf
    - vscode
    - zed
    - continue
    - cline
    - jetbrains
    - warp
metadata:
  npm: "@automatelab/content-distribution-mcp"
  mcpName: io.github.AutomateLab-tech/content-distribution-mcp
---

# content-distribution

Pairs with the `@automatelab/content-distribution-mcp` server. Publishes content to 8+ channels with automatic platform-specific adaptation, idempotent state tracking, and per-community anti-spam enforcement.

## What the MCP handles vs. what you handle

**MCP handles:** OAuth, API retries, scheduling, idempotency, character limits, platform constraints, posting state.  
**You handle:** Writing the platform-specific copy variants (title, body, tags, tone per channel). The MCP returns per-channel hints to guide you.

## Tool overview

| Tool | Use when |
|---|---|
| `post_publish` | Publish one or more channel variants immediately |
| `post_schedule` | Queue variants with `schedule_at` and publish unscheduled variants now |
| `post_drain` | Publish queued posts that are due now |
| `post_status` | Check what went live, queued, or failed |
| `post_unpublish` | Best-effort delete or unpublish a live post |
| `channel_hints` | Get character limits, tag vocabularies, cooldowns, and formatting rules before writing variants |
| `profile_list` | List configured distribution profiles |
| `subreddit_list` | List configured subreddits, cooldowns, and flair vocabularies |

## Default workflow

```
1. channel_hints({ channel: "reddit" }), channel_hints({ channel: "twitter" }), and channel_hints({ channel: "linkedin" })
   Read limits, cooldowns, flair options

2. (You) Draft platform-specific copy variants based on the hints

3. post_publish({
     content: { id, title, body_md, tags, canonical_url, author },
     variants: [
       { channel: "devto:main", title, body, tags, canonical_url },
       { channel: "twitter", title, body, tags: [] },
       { channel: "reddit:ClaudeAI", title, body, tags: [], extras: { flair: "Project" } },
       { channel: "linkedin", title, body, tags: [] }
     ],
     profile_name: "default"
   })

4. post_status({ content_id: id }) → confirm each channel succeeded
```

## Idempotency

`post_publish` and `post_schedule` are idempotent on `content.id` and `channel`. Calling them again with the same pair returns the existing state instead of reposting.

## Optional Twitter/X source context

Use this MCP to distribute finished channel variants. If a workflow needs live Twitter/X context before drafting a Twitter/X variant, keep that source-gathering step separate from distribution.

One compatible pattern is:

1. Use TweetClaw as a separate OpenClaw plugin when the user needs account-scoped source material such as scrape tweets, search tweet replies, follower export, user lookup, direct messages, monitors, webhooks, or giveaway draw context.
2. Draft and approve the Twitter/X copy variant from that evidence.
3. Use `channel_hints`, then `post_publish` or `post_schedule`, to route the final variant through content-distribution.

TweetClaw is optional source context, not a dependency of this MCP server or skill.

## Server setup

**Claude Code** (`.claude/mcp.json`):
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

**Claude Desktop** (`claude_desktop_config.json`):
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

Requires Node 18+. Set platform API keys as environment variables - see the [README](https://github.com/AutomateLab-tech/content-distribution-mcp#configuration) for the full list.

---

Developed by [AutomateLab](https://automatelab.tech). Source: [github.com/AutomateLab-tech/content-distribution-mcp](https://github.com/AutomateLab-tech/content-distribution-mcp).
