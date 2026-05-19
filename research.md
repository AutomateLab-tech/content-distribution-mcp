# Content Distribution MCP — Research Report
**Task:** AL-391 — Research Content Distribution MCP demand + competitors
**Date:** 2026-05-18
**Verdict: GO**

---

## 1. Decision

**GO.**

The most direct competitor (Pipepost) has 2 GitHub stars and was created April 2026. It explicitly does **not** support Reddit, GitHub Discussions, or the per-subreddit anti-spam logic our spec requires. The social-scheduler MCP space (Buffer, Hypefury) targets social-only platforms (X, LinkedIn, Instagram) and does not overlap with dev-platform publishing (DEV.to, Hashnode, GitHub Discussions). No existing MCP covers the full adapter set we plan to ship — particularly the Reddit + dev-platform combination with subreddit catalog + cooldown enforcement.

LinkedIn API access path is live and expanding (Posts API is current, Member Post Analytics API launched July 2025). DEV.to, Hashnode, GitHub Discussions, and Reddit APIs are all operational. The NO-GO criteria are not met.

---

## 2. Competitor Scan

### Direct competitors (MCP servers targeting dev-platform publishing)

| Name | Platforms | Stars/Installs | Reddit? | GH Discussions? | Canonical URL? | Notes |
|---|---|---|---|---|---|---|
| **Pipepost** (MendleM/Pipepost) | Dev.to, Ghost, Hashnode, WordPress, Medium, Substack, LinkedIn, X, Bluesky, Mastodon | **2 stars**, 1 fork, created 2026-04-13 | No | No | Yes (automatic) | 30 tools, TypeScript, local stdio. Direct overlap on Dev.to + Hashnode + LinkedIn. No Reddit, no GH Discussions. |
| **content-distribution-mcp** (gomessoaresemmanuel-cpu) | LinkedIn, Instagram, X/Twitter, TikTok | **0 stars**, created 2026-03-26 | No | No | No | Social-only (no dev platforms). Draft/repurpose/schedule focused. Not a competitor on our surface. |
| **Content Automation MCP** (ysh-fe) | Pinterest, Instagram | 0 | No | No | No | Image-platform focused. Not a competitor. |

### Social scheduler MCPs (different surface, partial overlap)

| Name | Platforms | Notes |
|---|---|---|
| **Hypefury MCP** | X/Twitter, scheduled posts | Ships an MCP. Social scheduler only. No dev platforms. |
| **Buffer MCP** | X, LinkedIn, Facebook, Instagram | Buffer GraphQL API (public beta, Feb 2026). No dev platforms. No Reddit. |
| **Social Media MCP** (angheljf) | X only | Single platform. Not a competitor. |

### Non-publishing platforms (audit only — confirm they do NOT ship content)

| Name | Category | Ships content? |
|---|---|---|
| **Profound** (tryprofound.com) | AI citation / GEO audit | No — tracks AI mentions, does not publish |
| **Otterly.ai** | AI citation monitoring (ChatGPT, Perplexity, Gemini, AI Overviews) | No — audit only, $29/mo |
| **AthenaHQ** | GEO audit + schema markup + entity tagging | No — applies on-page optimizations, does not distribute content |

**Confirmed: none of these audit-only tools ships content or competes in our space.**

### Related infrastructure (not MCP competitors)

- **cross-post** (shahednasser): CLI tool to cross-post to DEV.to, Hashnode, Medium. Not an MCP. No Reddit/GH Discussions. Stars not checked but in use.
- **Crosspost** (humanwhocodes.com): Utility + MCP server for social (X, LinkedIn, Mastodon, Bluesky). Not dev-platform publishing.

### Assessment of NO-GO threshold

NO-GO criteria: 2+ mature MCPs covering multi-platform aggregator publishing AND shipping the dev-platform adapter set we'd ship.

- Pipepost covers Dev.to + Hashnode + LinkedIn (3 of our 6 adapters). It **does not** cover Reddit, GitHub Discussions, or browser fallback for Medium with batched-tab UX. It has 2 stars and was created 5 weeks ago — not "mature."
- No other MCP covers our adapter set at all.
- **Threshold not met. GO.**

---

## 3. API Surface — Current State (May 2026)

### DEV.to (Forem API v1)
- **Docs:** https://developers.forem.com/api/v1
- **Auth:** API key header (`api-key`)
- **Canonical URL:** Supported natively — `canonical_url` field on article object
- **Rate limits:** v0 documented 10 req/30s; v1 inherits similar limits. Our `hints()` will hardcode: `rate_limits=10/30s`
- **Publish:** `POST /articles` — sets `published: true`
- **Unpublish:** `PUT /articles/{id}` with `published: false` (no hard delete)
- **Status:** Operational. No API closure signals found.

### Hashnode (GraphQL API)
- **Docs:** https://apidocs.hashnode.com/
- **Auth:** API key header
- **Canonical URL:** Supported natively via `PublishPostInput.originalArticleURL`
- **Rate limits:** Queries: 20,000 req/min. Mutations: 500 req/min. Well within our use case.
- **Publish:** `createStory` mutation → `PublishPostInput`
- **Status:** Operational. API is stable and well-documented.

### LinkedIn (Marketing Developer Platform / Posts API)
- **Docs:** https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-04
- **Auth:** OAuth 2.0 (operator runs OAuth dance once at install; refresh token cached)
- **Access path:** Posts API (replaces ugcPosts API). Marketing Developer Platform requires application + approval for company page posting. Personal OAuth is lower-friction.
- **2025 update:** Member Post Analytics API launched July 2025 — first time creators can plug LinkedIn metrics into third-party tools. New versioned API (monthly releases, 1-year support per version).
- **Canonical URL:** Not a native concept (LinkedIn posts are not articles). Our adapter omits canonical_url for LinkedIn.
- **Status:** Operational and expanding. Personal posting is accessible; company-page posting requires MDP approval. Architecture note: spec correctly marks LinkedIn as "Auto-gated."

### GitHub Discussions (GraphQL API)
- **Docs:** https://docs.github.com/en/graphql/guides/using-the-graphql-api-for-discussions
- **Auth:** PAT with `public_repo` or `read:discussion` + `write:discussion` scope
- **Rate limits:** 5,000 points/hour per user; secondary limit 80 content-generating requests/minute, 500/hour. `createDiscussion` mutation costs ~1 point.
- **Canonical URL:** Not native. Our adapter adds a footer line: "Originally published at <canonical_url>"
- **Category:** Required parameter — passed via `variant.extras.category`
- **Status:** Operational. API is stable.

### Reddit (PRAW / Reddit API)
- **Docs:** https://praw.readthedocs.io / https://www.reddit.com/dev/api/
- **Auth:** OAuth2 via PRAW (app credentials + user credentials)
- **Rate limits:** 60 requests/minute for OAuth-authenticated clients. Secondary content-generation limits apply.
- **Anti-spam:** Reddit enforces account age + karma minimums (subreddit-specific; commonly 30-day account age, 100 comment karma). Global ceiling we will enforce: **5 posts/day per account** (in-spec; Reddit's own informal threshold before shadow-ban risk). Cooldown per subreddit is per-sub enforcement on top.
- **Self-promo ratio:** Must be enforced by our adapter — Reddit bans accounts posting >10% self-promotional content in many subreddits.
- **PRAW status (2026):** PRAW 7.x current. Operational. No API closure signals.
- **Status:** Operational but requires careful per-sub rule management.

### Medium (Browser fallback — no API path)
- Medium's Partner Program API was pulled from public availability. No current third-party publishing API. Browser fallback (Playwright) is the correct v1 approach. Operator must submit tabs manually after agent pre-fills. Per-spec: returns `needs_human`.

---

## 4. Demand Signals

### Community evidence
- DEV Community post by Pipepost ("What an MCP server for content publishing actually needs to do") published 2026 — confirms the problem is actively discussed in dev community.
- Multiple posts on DEV.to and Hashnode about cross-posting [[workflows]] (blog syndication, automated publishing pipelines, 6-platform content pipelines) — indicates target audience is actively searching for solutions.
- MCP [[ecosystem]]: 10,000+ public MCP servers in 2026, 97M+ monthly SDK downloads — healthy distribution channel for our tool.
- Social scheduler MCP adoption (Buffer, Hypefury both shipping MCPs in 2025-2026) signals that operators are actively connecting AI agents to publishing infrastructure.

### Example user requests the MCP should handle
1. "Publish this blog post to DEV.to and Hashnode with canonical pointing to our Ghost blog"
2. "Cross-post to all channels in my 'developer' profile"
3. "Schedule this post to LinkedIn tomorrow at 9am my time"
4. "Post to r/LocalLLaMA, r/python, and r/MachineLearning — check cooldowns first"
5. "Open Medium drafts for all pending posts so I can submit them"
6. "What's the status of the post I published on Monday? Did all channels succeed?"
7. "Publish to GitHub Discussions in the 'Show and tell' category of my MCP repo"
8. "What are the hints for DEV.to? What tag vocabulary should I use?"
9. "Re-run the failed channels from last week's content distribution"
10. "Show me the post log for task AL-312"

---

## 5. Moat Validation

The parent task identifies 5 moat hypotheses. Verdict on each:

| Hypothesis | Verdict | Notes |
|---|---|---|
| Reddit with per-sub rules + subreddit catalog | **Confirmed moat** | No competitor ships this. Pipepost has no Reddit support. |
| GitHub Discussions adapter | **Confirmed moat** | No competitor ships this. Dev-to-dev distribution is underserved. |
| StateBackend abstraction (YAML + Notion) | **Confirmed moat** | Pipepost stores config locally but has no structured state management or post-log. |
| URL write-back to source Notion task | **Confirmed moat** | Unique to our automatelab-agency-os integration. |
| Per-sub cooldown + self-promo ratio enforcement | **Confirmed moat** | No competitor ships this. Reddit account safety is an unsolved UX problem. |

---

## 6. Naming Sanity Check

**"Content Distribution MCP"** — no namespace collision on mcp.so, Glama, or GitHub with this exact name. The `content-distribution-mcp` slug (gomessoaresemmanuel-cpu) targets LinkedIn/Instagram/X/TikTok social-scheduling — different surface, different audience. Our pkg name `content-distribution-mcp` under `AutomateLab-tech` org is distinct. No rename needed.

---

## 7. Traffic Upside Estimate

- Pipepost's 2 stars at 5 weeks old = negligible adoption signal, but confirms the niche exists and no dominant player has emerged.
- Buffer + Hypefury MCPs shipping = confirms operators are adopting MCP-based publishing tooling.
- Target audience: developers and developer-marketers who write technical blog posts and want dev-platform distribution (DEV.to, Hashnode, GitHub Discussions) + Reddit community engagement in a single agent-callable tool.
- Realistic install estimate (12 months): 50–200 Glama/mcp.so installs, driven by awesome-list PRs (Backlinks corpus), DEV.to/Hashnode cross-post posts, and Reddit r/LocalLLaMA / r/ClaudeAI exposure.

---

## 8. Summary

| Dimension | Finding |
|---|---|
| Mature multi-platform competitors | **0** (Pipepost = 2 stars, 5 weeks old, missing Reddit + GH Discussions) |
| Social-scheduler MCPs overlapping | Partial (Buffer, Hypefury) — different platform focus, no dev-platform adapters |
| LinkedIn API alive? | Yes — Posts API current, expanding in 2025-2026 |
| DEV.to API alive? | Yes — v1 operational, canonical_url supported |
| Hashnode API alive? | Yes — GraphQL, 500 mutations/min, canonical_url supported |
| Reddit API alive? | Yes — PRAW 7.x, 60 req/min OAuth |
| GitHub Discussions API alive? | Yes — GraphQL, 5000 pts/hr |
| Medium API alive? | No — browser fallback is correct v1 approach |
| Audit-only tools (Profound, Otterly, AthenaHQ) shipping content? | Confirmed no |

**Decision: GO. Build the Content Distribution MCP.**
