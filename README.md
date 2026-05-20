# Content Distribution MCP

A model-agnostic [Model Context Protocol](https://modelcontextprotocol.io/) server that takes a finished piece of content and routes it to developer-community platforms - DEV.to, Hashnode, GitHub Discussions, Bluesky, Reddit, LinkedIn, Medium, and Twitter - with idempotent state management and dual [[notion]]/YAML backends.

The server makes no LLM calls of any kind. All copy transformation is the caller's responsibility. The MCP hands back per-channel constraints via the `hints()` tool; the agent decides what to do with them.

## Works with any MCP client

This is not a Claude-only tool, and not a single-host tool. The server speaks standard MCP (stdio or SSE transport) and works unchanged with:

- **Claude Code** - via the `content-distribution` skill that ships with this repo, or any Claude Code custom skill
- **[[n8n]]** - via the MCP node, dropping `publish()` and `schedule()` calls into any workflow
- **Cursor** - via the MCP client built into Cursor agents
- **plain Python** - via the `mcp` client library, with any LLM SDK (OpenAI, [[anthropic]], Gemini, local Ollama, none at all)
- **custom integrations** - anything that speaks MCP over stdio or SSE

The MCP server has zero Anthropic-specific code. There is no `anthropic` import anywhere in `src/`. Verify with:

```bash
grep -ri "anthropic" src/  # returns nothing
```

The host process supplies credentials (constructor args, env vars, or via the StateBackend's Profile lookup). The host process supplies LLM-generated `Variant` text. The MCP supplies idempotent I/O.

## Install

```bash
pip install content-distribution-mcp
```

Bluesky extras:

```bash
pip install content-distribution-mcp[bluesky]
```

## Quickstart

```bash
# Start the server (stdio transport, the default)
content-distribution-mcp serve

# Provision Notion state databases (one-time)
content-distribution-mcp provision-notion

# Fire any due scheduled posts (one-shot, cron-friendly)
content-distribution-mcp drain
```

Wire into your MCP host of choice. For Claude Code:

```jsonc
// .claude/mcp.json
{
  "mcpServers": {
    "content-distribution": {
      "command": "content-distribution-mcp",
      "args": ["serve"]
    }
  }
}
```

For n8n: install the MCP Client node, point it at `content-distribution-mcp serve` over stdio, and call `publish` / `schedule` from any workflow.

For plain Python:

```python
from mcp import Client
client = Client("content-distribution-mcp", ["serve"])
await client.call("publish", {
    "content": {...},
    "variants": [{...}],
    "profile_name": "default",
})
```

## MCP tool surface

Eight tools. Full docstrings in [spec.md](spec.md#12-mcp-tool-surface).

| Tool | Purpose |
|---|---|
| `publish` | Immediate publish; idempotent on `(content.id, variant.channel)` |
| `schedule` | Queue variants for `schedule_at` |
| `drain` | Fire any due scheduled posts |
| `status` | Per-variant state for a content piece |
| `unpublish` | Best-effort delete (DEV.to / GitHub Discussions only - Reddit is honor-system) |
| `hints` | Static per-channel metadata: char limits, tag vocabulary, canonical-URL support, posting times |
| `list_profiles` | Configured Distribution Profiles |
| `list_subreddits` | Curated Subreddit Catalog entries |

## Architecture

```
+------------------------------------------------------+
| Agent Layer                                          |
|   (Claude Code, n8n, Cursor, plain Python, any host) |
|   Reads source content                               |
|   Generates per-channel copy (LLM work lives here)   |
|   Calls MCP tools                                    |
+------------------------------------------------------+
                          |
                          v  (MCP protocol - stdio or SSE)
+------------------------------------------------------+
| Content Distribution MCP Server                      |
|   No LLM calls. Pure I/O.                            |
|   Adapters, state, idempotency, scheduling, retries  |
+------------------------------------------------------+
            |                        |
            v                        v
+---------------------+    +---------------------+
| Channel Adapters    |    | StateBackend        |
| devto / hashnode    |    | NotionBackend       |
| github_disc / reddit|    | YamlBackend         |
| linkedin / medium   |    +---------------------+
+---------------------+
```

See [spec.md](spec.md) for the full data model, idempotency design, scheduling semantics, and integration notes.

## Backends

- **`YamlBackend`** - four YAML files in `~/.distribution-mcp/`. Zero-config; right for solo/local use.
- **`NotionBackend`** - three Notion databases (Distribution Profiles, Subreddit Catalog, Post Log) plus URL write-back to source tasks. Right for team/agency use.

Both implement the same `StateBackend` Protocol. The MCP picks the backend from a constructor argument; no caller code changes when you swap them.

## Channels

| Channel | Tier | Notes |
|---|---|---|
| DEV.to | Auto | Forem API v1, native `canonical_url` |
| Hashnode | Auto | GraphQL, native `originalArticleURL` |
| GitHub Discussions | Auto | GraphQL per-repo, footer for canonical (no native field) |
| Bluesky | Auto | atproto SDK, canonical link appended to post text |
| Reddit | Manual (browser) | Plain-text draft + pre-filled submit URL, mark-live CLI. No credentials needed. |
| Medium | Manual (browser) | Plain-text draft + compose URL, mark-live CLI |
| LinkedIn | Auto | OAuth 2.0 Posts API. Run `content-distribution-mcp linkedin install` once. |
| Twitter / X | Manual (browser) | Free-tier API unusable; plain-text draft + compose URL, mark-live CLI |

## Part of the AutomateLab stack

- [agency-os](https://github.com/automatelab-tech/agency-os) - Control plane and Notion integration
- publishing-skills - Upstream content production (e.g. `al-write-blog-post`)
- **content-distribution-mcp** - This repo
- [ai-seo-mcp](https://github.com/automatelab-tech/ai-seo-mcp) - Post-publish AI-citation audit
- [automatelab.tech](https://automatelab.tech) - Blog and tutorials

These integrate by convention, not by import. Each is usable standalone with any MCP host.

## License

MIT.
