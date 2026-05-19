# Content Distribution MCP - v1 Specification

**Document status:** Draft for human review
**Task:** AL-392
**Date:** 2026-05-18
**Research basis:** AL-391 research.md (GO verdict, 2026-05-18)

---

## 1. Positioning

**Public name:** Content Distribution MCP

**Elevator pitch:** A model-agnostic MCP server that takes finished content and routes it to developer-community platforms - DEV.to, Hashnode, GitHub Discussions, Reddit (per-subreddit), LinkedIn, and Medium (browser fallback) - with idempotent state management, per-subreddit anti-spam rules, and dual Notion/YAML backends.

**GEO / entity-authority framing:** "Content Distribution MCP" should become the canonical entity name used in AI-search results when developers ask how to automate cross-posting via an AI agent. The README, the automatelab blog post, and the DEV.to cross-post announcement will all reinforce this entity anchor. Bidirectional links to the four-product stack (agency-os, publishing-skills, content-distribution-mcp, ai-seo-mcp) build topical clustering for AI-citation engines. Entity disambiguation: this MCP targets developer-community platforms; it is not a social scheduler (Buffer, Hypefury) and not a content-audit tool (Profound, Otterly, AthenaHQ).

**Repository:** `github.com/AutomateLab-tech/content-distribution-mcp`

**License:** MIT. Rationale: the MCP engine ships as a public tool, and MIT maximizes adoption in the MCP ecosystem. Config and platform credentials never live in this repo; proprietary value is in the agency-os integration layer and the curated subreddit catalog, both of which live in the private `automatelab` repo. MIT creates no competitive risk.

**Package name:** `content-distribution-mcp` (PyPI). Install via `pip install content-distribution-mcp`. Browser fallback extras via `pip install content-distribution-mcp[browser]`.

---

## 2. Scope

### v1 Inclusions

**Channel adapters shipped in v1:**

- **DEV.to** - Auto tier. Full API publishing with native canonical_url support.
- **Hashnode** - Auto tier. GraphQL-based publishing with native canonical_url support via `originalArticleURL`.
- **GitHub Discussions** - Auto tier. GraphQL-based, per-repo. No native canonical support - adapter adds reference footer.
- **Reddit** - Auto-gated tier. Per-subreddit, not per-platform. See Section 8 for full rules.
- **LinkedIn** - Auto-gated tier. Operator runs OAuth dance once at install. Personal and company-page paths. See Section 9.
- **Medium** - Manual tier (browser fallback only). Playwright pre-fill + batched-tab UX. See Section 10.

**State backends in v1:**

- `NotionBackend` - Three new Notion databases (Distribution Profiles, Subreddit Catalog, Post Log). URL write-back to source task on success.
- `YamlBackend` - Four YAML files in `~/.distribution-mcp/`. Zero-config for local/solo use.

**Core features in v1:**

- Idempotent publish with partial-failure recovery
- Per-channel scheduling with drain worker
- `hints()` tool for caller-side variant formatting decisions
- `status()` tool with per-variant state
- Subreddit Catalog with per-sub cooldown + self-promo ratio enforcement
- Global Reddit 5-posts/day cap
- URL write-back to Notion source task (NotionBackend only)
- Retry policy: transient errors (5xx, 429) with exponential backoff, 3 attempts max

### v1 Exclusions

**Platforms deferred or dropped:**

- **X / Twitter** - Post-v1. Thread structure and the current API pricing model warrant a dedicated MCP. Dropped from v1 entirely.
- **Mastodon** - Post-v1. Instance fragmentation requires distinct configuration design.
- **Bluesky** - Post-v1. AT Protocol design is still stabilizing.
- **Quora** - Dropped entirely. No API path. Browser fallback is possible but the platform's content moderation and audience fit are poor for dev-community publishing. Not planned for v2.
- **Native Medium adapter** - Post-v1 (v2 candidate if Medium reopens partner API).

**Backends deferred:**

- `SqliteBackend` - Post-v1 candidate if public adoption surfaces demand for zero-config without YAML friction.

**Features deferred:**

- Engagement analytics, comment monitoring, reply automation
- Web dashboard (status lives in Notion views or `status` tool output)
- Multi-workspace / multi-tenant support
- AI-driven per-platform copy variation (this lives upstream in publishing-skills / the orchestrating skill, not in this MCP)

---

## 3. Architecture Overview

The MCP operates in three layers. The agent layer (a Claude Code skill, [[n8n]] workflow, plain Python script, or any MCP-capable host) is responsible for producing content and per-channel variant text. The MCP layer handles all I/O: API calls, state persistence, idempotency, scheduling, and error handling. It makes no LLM calls of any kind. The adapter layer is a set of thin, single-platform modules that translate a finished Variant struct into platform-specific API calls.

```
+------------------------------------------------------+
| Agent Layer                                          |
|   (Claude Code skill, n8n, plain Python, any host)   |
|   Reads source content                               |
|   Generates per-channel copy (LLM work lives here)   |
|   Calls MCP tools: publish, schedule, status, hints  |
+------------------------------------------------------+
                          |
                          v  (MCP protocol - stdio or SSE)
+------------------------------------------------------+
| Content Distribution MCP Server                      |
|   No LLM calls. Pure I/O.                           |
|   Orchestrates: adapters, state, idempotency,        |
|   scheduling, retries, URL write-back                |
|   Tools: publish, schedule, drain, status,           |
|          unpublish, hints, list_profiles,            |
|          list_subreddits                             |
|   Exposes hints() for caller-side transform logic    |
+------------------------------------------------------+
            |                        |
            v                        v
+---------------------+    +---------------------+
| Channel Adapters    |    | StateBackend        |
| (one per platform)  |    | (interface)         |
|   devto.py          |    |   NotionBackend     |
|   hashnode.py       |    |   YamlBackend       |
|   github_disc.py    |    +---------------------+
|   reddit.py         |
|   linkedin.py       |
|   medium_browser.py |
+---------------------+
```

**Key architectural constraint:** Nothing inside the MCP server or any adapter reads environment variables directly or calls an LLM. Credentials arrive as constructor arguments to the backend or adapter. This constraint keeps the public-repo strip mechanical - nothing in `src/` ever references `config/automatelab/*` or `.env` paths.

**hints() design note:** The MCP exposes a `hints()` tool that returns static per-channel metadata - character limits, tag vocabulary, canonical URL support flag, self-promo rules, typical best-posting-times, link formatting norms. This data is hardcoded per adapter and does not change at runtime. The naming is intentional: `hints`, not `transform`, because all copy transformation is LLM work that belongs in the caller. The MCP hands back constraints; the agent decides what to do with them.

---

## 4. Canonical Data Model

The data model has two core structs. The `Content` struct is the source-of-truth object representing the original piece. The `Variant` struct represents a platform-specific version, produced by the calling agent.

### Content

```python
@dataclass
class Content:
    id: str              # Stable content hash (SHA-256 of canonical_url or body_md)
    title: str           # Original title
    subtitle: str | None # Subheading / deck
    body_md: str         # Full Markdown body of the original piece
    cover_image: str | None  # URL of cover image
    tags: list[str]      # Author-supplied tags (not yet platform-adapted)
    canonical_url: str   # The authoritative URL for rel=canonical signaling
    cta_block: str | None    # Call-to-action appended by adapters that support it
    author: str          # Display name for by-line where platform requires it
    source_task_id: str | None  # Notion task ID (e.g. AL-312) for write-back
```

`content.id` is the idempotency anchor. It must be stable across re-runs. For automatelab, the SHA-256 of `canonical_url` is the recommended derivation. For standalone use, callers may supply any stable unique string.

`canonical_url` drives rel=canonical signaling. Adapters that support canonical URL (DEV.to, Hashnode) use it directly. Adapters that do not (GitHub Discussions, Reddit, LinkedIn) either include it in a reference footer or omit it entirely, depending on platform norms. The calling agent should always supply this field.

### Variant

```python
@dataclass
class Variant:
    channel: str         # e.g. "devto", "hashnode", "reddit:LocalLLaMA", "linkedin"
    title: str           # Platform-adapted title
    body: str            # Platform-adapted body (Markdown or platform native)
    tags: list[str]      # Platform-adapted tag list
    canonical_url: str | None  # May override Content.canonical_url per variant
    cta_block: str | None      # May override Content.cta_block per variant
    schedule_at: str | None    # ISO-8601 datetime; None = publish immediately
    extras: dict         # Platform-specific fields (see per-adapter notes below)
```

`extras` is the escape hatch for platform-specific parameters that do not belong in the canonical model. Examples: `extras["category"] = "Show and tell"` for GitHub Discussions; `extras["subreddit_flair"] = "Tutorial"` for Reddit (optional, adapter also resolves from catalog); `extras["post_as"] = "company_page"` for LinkedIn.

`channel` for Reddit uses a compound format: `"reddit:<subreddit>"` where `<subreddit>` is the bare subreddit name without `r/` prefix, matching the Subreddit Catalog entry title. This means a single `publish()` call with five Reddit variants fires five separate Subreddit Catalog lookups and five separate PRAW submissions.

### PostLogEntry

```python
@dataclass
class PostLogEntry:
    content_id: str
    channel: str
    state: str           # "pending" | "queued" | "live" | "failed" | "needs_browser" | "taken_down"
    live_url: str | None
    source_task_id: str | None
    posted_at: str | None    # ISO-8601
    error: str | None
    retry_count: int
    next_retry_at: str | None
```

---

## 5. Adapter Interface

All adapters implement the following protocol. Adapters are thin - they do not hold state, do not read config files, and do not make retry decisions. The MCP server orchestrator handles all of that.

```python
class ChannelAdapter(Protocol):

    def hints(self) -> ChannelHints:
        """Return static channel metadata for caller-side formatting decisions."""
        ...

    def can_publish(self, variant: Variant) -> tuple[bool, str | None]:
        """
        Pre-flight check without side effects.
        Returns (True, None) if variant appears publishable.
        Returns (False, reason) if a known blocker exists (e.g. title too long,
        missing required extras field, account age check failed).
        """
        ...

    def publish(
        self,
        variant: Variant,
        profile: Profile,
        state_backend: StateBackend,
    ) -> PublishResult:
        """
        Publish variant to platform.
        Must be idempotent: if state_backend.claim_idempotency_key() returns
        an existing live_url, return it without re-publishing.
        Returns PublishResult with state in:
            "live" | "needs_browser" | "failed" | "partial"
        """
        ...

    def unpublish(self, live_url: str) -> tuple[bool, str | None]:
        """
        Attempt to remove the post at live_url.
        Returns (True, None) on success, (False, reason) on failure.
        Note: some platforms (LinkedIn) do not support delete via API at all tiers.
        """
        ...
```

### ChannelHints

```python
@dataclass
class ChannelHints:
    channel: str
    title_max_chars: int | None
    body_max_chars: int | None
    tags_max_count: int | None
    tag_max_chars: int | None
    supports_canonical_url: bool
    supports_cover_image: bool
    canonical_url_field: str | None   # API field name, for documentation
    self_promo_note: str | None
    best_times_utc: list[str] | None  # Informational only
    extras_schema: dict               # JSON Schema fragment for required extras
```

### Per-adapter notes

**DEV.to (Forem API v1)**

- `canonical_url` is a first-class field on the article object. Set it from `variant.canonical_url or content.canonical_url`.
- Publish via `POST /articles` with `published: true`.
- Unpublish via `PUT /articles/{id}` with `published: false` (platform does not support hard delete).
- Rate limit hint: 10 requests per 30 seconds (hardcoded in `hints()`).
- No required `extras` fields.
- Auth: `api-key` header. Profile carries the key.

> OPEN: DEV.to series support - should the adapter support posting into a DEV.to series via `extras["series"]`? Defer to v1.1 unless demand signals emerge.

**Hashnode (GraphQL API)**

- `canonical_url` maps to `PublishPostInput.originalArticleURL`.
- Publish via `createStory` mutation.
- Rate limits are generous (500 mutations/minute) - retry budget is essentially unlimited for our use case.
- Required `extras`: `publication_id` (the Hashnode publication ID - stored in Profile).
- Auth: `Authorization` header. Profile carries the key.
- Unpublish: check if `removePost` mutation is available in current API version.

> OPEN: Hashnode's API versioning cadence - confirm whether `removePost` is a stable mutation or deprecated. Check apidocs.hashnode.com before implementing `unpublish()`.

**GitHub Discussions (GraphQL API)**

- No canonical URL support. Adapter appends a footer: `---\n*Originally published at [<canonical_url>](<canonical_url>)*`.
- Required `extras`: `category` (GitHub Discussions category name, e.g. "Show and tell"). The adapter resolves the category ID via `repositoryDiscussionCategories` query before posting.
- Required profile fields: `github_owner`, `github_repo`, `github_pat`.
- Rate limits: 5,000 points/hour, 80 content-generating requests/minute. `createDiscussion` costs ~1 point per call.
- Auth: PAT with `public_repo` and `write:discussion` scopes.
- Unpublish: `deleteDiscussion` mutation (requires `admin:discussion` scope - document this in README).

> OPEN: Should the GitHub Discussions adapter support cross-posting to multiple repos in a single variant? The current model supports one repo per variant. Multi-repo would require multiple variants with different `extras["github_repo"]` values. Confirm with operator.

**Reddit (PRAW)**

- `channel` format: `"reddit:<subreddit>"`. The subreddit name is the bare name (no `r/`).
- Full Reddit-specific flow documented in Section 8.
- Auth: PRAW credentials in Profile (client_id, client_secret, username, password or refresh_token).
- Unpublish: PRAW `submission.delete()`. Only works for own posts within ~60 minutes of posting; some subreddits lock posts sooner.
- No canonical URL support on Reddit. Adapter submits a link post pointing at `content.canonical_url`, or a text post with footer if canonical_url is the source being republished. Determined by `extras["post_type"]` ("link" | "text", default "text").

**LinkedIn (Posts API)**

- Full LinkedIn-specific flow documented in Section 9.
- Canonical URL is not a native LinkedIn concept. Adapter omits it.
- `extras["post_as"]`: "personal" (default) | "company_page". Company page requires `extras["company_urn"]` and MDP approval.
- Auth: OAuth 2.0 refresh token in Profile (see Section 9).
- Unpublish: `DELETE /posts/{id}` via Posts API. Works for personal posts; company-page delete requires owner-level permissions.

**Medium (browser fallback)**

- Full browser fallback flow documented in Section 10.
- `publish()` always returns `state=needs_browser`.
- `unpublish()` returns `(False, "medium_browser_only")` - no programmatic unpublish path.

---

## 6. StateBackend Interface

The `StateBackend` protocol is the single persistence contract. All state reads and writes flow through it. The MCP server never touches the file system or Notion API directly.

```python
class StateBackend(Protocol):

    def load_profile(self, name: str) -> Profile:
        """Load a named distribution profile. Raises ProfileNotFound if absent."""
        ...

    def save_profile(self, profile: Profile) -> None:
        """Persist a profile (upsert by name)."""
        ...

    def claim_idempotency_key(
        self, content_id: str, channel: str
    ) -> str | None:
        """
        Atomically claim the (content_id, channel) key.
        Returns existing live_url if already published (state=live).
        Returns None if key is unclaimed - caller proceeds to publish.
        For YamlBackend: file lock around post-log.yaml read/write.
        For NotionBackend: check Post Log DB for existing live row.
        """
        ...

    def mark_published(
        self, content_id: str, channel: str, live_url: str,
        source_task_id: str | None = None
    ) -> None:
        """Record a successful publish. Triggers URL write-back for NotionBackend."""
        ...

    def query_post_log(
        self, content_id: str | None = None, task_id: str | None = None
    ) -> list[PostLogEntry]:
        """Return log entries matching content_id or task_id (or both)."""
        ...

    def enqueue_scheduled(self, entry: PendingPost) -> None:
        """Add a pending post to the scheduler queue."""
        ...

    def drain_scheduled(self, as_of: str) -> list[PendingPost]:
        """
        Return all queued posts with schedule_at <= as_of (ISO-8601).
        Atomically marks returned entries as in_progress to prevent double-drain.
        """
        ...

    def update_state(
        self, content_id: str, channel: str, state: str,
        live_url: str | None = None, error: str | None = None,
        retry_count: int | None = None
    ) -> None:
        """Update state on an existing post log entry."""
        ...
```

### NotionBackend semantics

`NotionBackend` uses three Notion databases. Database IDs are passed as constructor arguments and never hardcoded in `src/`.

- `mark_published()` writes a new row to Post Log AND appends `- [<channel>](<live_url>)` to the source task's Done log section via the Notion API. This URL write-back is the primary integration point with agency-os.
- `claim_idempotency_key()` queries Post Log for a row matching `(content_id, channel, state=live)`. Notion API calls are not truly atomic, but the probability of a race condition for our single-operator use case is negligible. If concurrent distribution is ever needed, a lock table can be added to v2.
- `drain_scheduled()` queries Post Log for rows with `state=queued` and `schedule_at <= as_of`, then bulk-updates them to `state=in_progress`.

### YamlBackend semantics

`YamlBackend` operates on four files in `~/.distribution-mcp/`:

- `profiles.yaml` - list of Profile objects
- `subreddits.yaml` - list of SubredditEntry objects
- `post-log.yaml` - append-only list of PostLogEntry objects
- `pending.yaml` - scheduler queue, list of PendingPost objects

`claim_idempotency_key()` acquires an advisory file lock (via `filelock` library) on `post-log.yaml`, searches for a matching live entry, writes a new in-progress entry if none found, and releases the lock. The lock duration is milliseconds.

`mark_published()` updates the in-progress entry to live with the URL. No Notion write-back (YAML backend has no concept of a source Notion task).

All YAML files use a top-level `entries:` list key. This makes appends safe: append to the list, never rewrite the file from scratch (prevents data loss on crash mid-write).

---

## 7. Idempotency Strategy

**Key derivation:** The idempotency key for every publish attempt is the tuple `(content.id, variant.channel)`. For Reddit variants, `variant.channel` includes the subreddit name (e.g. `"reddit:LocalLLaMA"`), so each subreddit is a separate idempotency key.

**Re-run behavior:** If `claim_idempotency_key()` returns an existing `live_url`, the MCP returns that URL immediately with `state=live` and does not make any platform API call. This means `publish()` is safe to call multiple times without risk of duplicate posts.

**Partial failure recovery:** When a `publish()` call covers multiple variants (e.g. DEV.to, Hashnode, three subreddits, LinkedIn), each variant is processed independently. A failure on variant N does not abort variants N+1 through M. The overall `publish()` response is a map of channel to result. On re-run, only the failed channels are re-attempted (the successful ones return immediately via the idempotency check). No manual intervention is required to retry.

**State machine for a single (content_id, channel) pair:**

```
[unclaimed]
    |
    v  claim_idempotency_key() -> None (new)
[in_progress]
    |
    +---> [live]          publish() succeeded
    +---> [failed]        4xx or permanent error (no retry)
    +---> [needs_browser] Medium or any needs_human adapter
    +---> [queued]        schedule() call with future schedule_at
    +---> [taken_down]    unpublish() succeeded
```

**Transient retry policy:** Errors classified as transient (HTTP 5xx, HTTP 429, network timeout) are retried with exponential backoff: 30s, 2m, 8m. After three attempts without success the entry transitions to `state=failed` with the last error stored. The MCP server exposes these entries via `status()` so the operator can inspect and trigger a manual retry via a fresh `publish()` call.

**Permanent failure examples:** HTTP 401 (bad credentials), HTTP 403 (not allowed to post in subreddit), Reddit `RATELIMIT` when cooldown has not elapsed, LinkedIn API rejection for missing company-page authorization. These go straight to `state=failed` with no retry.

---

## 8. Reddit-Specific Rules

Reddit is the most operationally complex adapter because it is effectively N separate channels with N independent rule sets, all sharing one account's global reputation. The Subreddit Catalog is the per-subreddit rule store.

### Global account cap

Before any per-subreddit check, the Reddit adapter enforces a global ceiling of **5 posts per day per Reddit account**. This ceiling is based on Reddit's informal threshold below which the risk of shadow-ban or rate-limit action is low. The adapter counts posts from the current day in the Post Log (all entries with `channel` starting with `"reddit:"`, `state=live`, `posted_at >= today_UTC_00:00`). If this count is already 5, all remaining Reddit variants in the current run are refused with `state=failed` and error `"global_daily_cap_reached"`.

### Per-subreddit cooldown

Each SubredditEntry in the Subreddit Catalog has a `posting_cooldown_days` integer. After posting to a subreddit, the adapter writes `last_posted_at` back to the catalog entry. On any subsequent attempt, the adapter checks `last_posted_at + posting_cooldown_days` against the current UTC time. If the cooldown has not elapsed, the variant is refused with `state=failed` and error `"subreddit_cooldown: <subreddit> - next eligible <datetime>"`.

### Self-promotion ratio

Many subreddits prohibit content that is predominantly self-promotional. The Subreddit Catalog `self_promo_ratio_max` field (float, 0.0 to 1.0, default 0.1 representing 10%) encodes the per-subreddit threshold. The adapter queries the user's last 10 posts in that subreddit via PRAW's `redditor.submissions.new(limit=10)`, filters to submissions in that subreddit, and computes the ratio of posts whose URL or domain matches the operator's owned-domain list (stored in Profile). If the computed ratio meets or exceeds the threshold, the variant is refused with `state=failed` and error `"self_promo_ratio_exceeded"`.

> OPEN: The owned-domain list is not yet formalized in the Profile schema. It should be a `list[str]` field of domains the operator controls (e.g. `["automatelab.tech", "AutomateLab-tech.github.io"]`). Add to Profile schema before implementation.

### Account age and karma minimums

Many subreddits enforce minimum account age and minimum karma via AutoModerator, but these checks are not exposed via the Reddit API before submission. The adapter cannot pre-validate these. Instead, when a post is rejected by AutoModerator (typically a removal message in the inbox within seconds of posting), the adapter should detect the removal via PRAW's `submission.removed` flag check (polling with 2s delay, 3 polls) and transition to `state=failed` with error `"automod_removed"`.

> OPEN: The AutoModerator removal detection pattern (poll `submission.removed` after posting) needs validation against PRAW 7.x behavior. Some subreddits use `locked` instead of `removed` for AutoMod actions. This may need a unit test with a sandbox subreddit.

### Flair handling

The SubredditEntry `flair_vocab` field is a list of allowed flair strings for the subreddit. If `flair_vocab` is non-empty and `variant.extras["subreddit_flair"]` is not supplied by the caller, the adapter uses the first entry in `flair_vocab` as the default. If `variant.extras["subreddit_flair"]` is supplied but not present in `flair_vocab`, the adapter logs a warning and falls back to the first entry. Flairs are applied via `submission.flair.select()` using PRAW's flair ID lookup.

### Subreddit Catalog schema

See Section 13 for the full Notion DB and YAML schema definitions.

### Multi-subreddit profiles

A Profile that includes Reddit must carry a `subreddits` field: a list of subreddit names (without `r/`) that this profile is authorized to post to. The MCP rejects a `"reddit:<name>"` variant if `<name>` is not in `profile.subreddits`. This prevents accidental posting to subreddits not curated by the operator.

---

## 9. LinkedIn Flow

### OAuth dance (one-time per operator)

LinkedIn OAuth 2.0 requires an interactive authorization step that cannot be automated. The flow is:

1. Operator runs `content-distribution-mcp oauth linkedin` (CLI helper, not an MCP tool).
2. CLI prints an authorization URL. Operator opens it in their browser.
3. Operator authorizes the application. LinkedIn redirects to the local callback listener.
4. CLI captures the authorization code, exchanges it for an access token and refresh token.
5. Tokens are stored in the named Profile via the configured StateBackend.

This flow runs once. Access tokens expire after 60 days; refresh tokens after 365 days. The adapter handles refresh automatically: on any 401 response, it attempts `POST /oauth/v2/accessToken` with `grant_type=refresh_token`. On success, it persists the new tokens via `state_backend.save_profile()`. On failure (expired refresh token), it returns `state=failed` with error `"linkedin_refresh_expired - rerun oauth"`.

> OPEN: LinkedIn's OAuth callback requires a registered redirect_uri. The CLI helper needs a local HTTP server for the callback (e.g. `http://localhost:8765/callback`). This redirect_uri must be registered in the LinkedIn Developer Portal app settings. Document this in the README installation guide. Consider whether to hardcode port 8765 or make it configurable.

### Personal vs company-page paths

**Personal (default):** Uses Posts API with the authenticated member's `personUrn` as the `author`. No MDP approval required. `extras["post_as"]` omitted or `"personal"`.

**Company page:** Uses Posts API with the company's `organizationUrn` as the `author`. Requires the operator's LinkedIn account to be a Page Admin. Also requires Marketing Developer Platform (MDP) approval for the app. This is a human-gated step - the operator must request MDP access and wait for LinkedIn's review (typically 1-4 weeks). `extras["post_as"] = "company_page"` and `extras["company_urn"] = "urn:li:organization:XXXXX"`.

The adapter detects which path to use from `extras["post_as"]` and selects the correct `author` URN accordingly. If company-page is requested but the stored profile does not include a company URN, `can_publish()` returns `(False, "missing company_urn in extras")`.

### Post format

LinkedIn posts are plain text with optional media. The adapter uses the text body from `variant.body`. Markdown is stripped (LinkedIn does not render Markdown). The canonical URL, if relevant, can be included as a trailing link in the post body - this is left to the calling agent's variant generation, not the adapter. `hints()` for LinkedIn returns `supports_canonical_url: False` and a note suggesting callers embed the URL at the end of the post body if desired.

> OPEN: LinkedIn now supports native "article" posts (long-form) via the Articles API, separate from the standard Posts API. Should we support article-type posting in v1? The draft spec and research do not address this distinction. Recommendation: defer to v1.1; v1 publishes posts only.

---

## 10. Browser Fallback (Medium)

Medium's Partner Program API was pulled from public availability. There is no current third-party publishing API. The correct v1 approach is a browser-assisted flow where the agent pre-fills a draft and the operator submits.

### Publish flow

1. `publish()` is called with a Medium variant.
2. The adapter writes the variant body to a draft file: `~/.distribution-mcp/drafts/<content_id>/medium.md`.
3. The adapter generates Medium's compose URL: `https://medium.com/new-story`.
4. `publish()` returns `PublishResult(state="needs_browser", draft_path=..., compose_url=..., instructions="Open compose_url, paste from draft_path, publish")`.

### Batched-tabs UX

When a `publish()` call includes Medium alongside other channels, the MCP server collects all `needs_browser` results and returns them together in the overall response. The calling skill (e.g. the public `content-distribution` skill, or its private flavor `al-content-distribution`) opens all compose URLs at once as new browser tabs via Playwright (`browser.new_page()` per URL). If Playwright is available, the skill can pre-fill the title and body fields. The operator then reviews and submits each tab.

After the operator manually submits a post and has the live URL, they call `content-distribution-mcp mark-live medium <content_id> <live_url>` via the CLI (or the MCP tool `publish` with `extras["existing_url"]` set) to update the Post Log. This is the only manual state-update path in v1.

### Playwright dependency

Playwright is an optional dependency: `pip install content-distribution-mcp[browser]`. If the `browser` extra is not installed and a Medium variant is submitted, the adapter still succeeds (returning `needs_browser`) but does not open tabs. The draft file is still written. Instructions are still returned. The tab-opening enhancement requires `[browser]`.

> OPEN: Playwright tab pre-fill for Medium requires identifying the correct DOM selectors for Medium's compose editor. Medium periodically redesigns their editor. The Playwright script should be wrapped in a try/except so pre-fill failure degrades gracefully (tabs open, but operator fills manually). Add a `--no-prefill` flag to the CLI for operators who prefer manual paste.

---

## 11. Scheduling

### Immediate publish

Calling `publish(content, variants)` with no `schedule_at` on any variant fires all variants in parallel (using `asyncio.gather` for API calls). Each result is independent. Parallel execution is bounded by per-channel rate limits: DEV.to (10/30s) is the tightest constraint, but since each content piece has at most 1-2 DEV.to variants per publish call, this is not a practical bottleneck.

### Scheduled publish

Calling `schedule(content, variants)` with `schedule_at` set on one or more variants enqueues those variants to the StateBackend's pending queue. The variant is stored as a `PendingPost` entry with `state=queued`. The content and variant objects are serialized into the entry (full snapshot, not a reference) so the drain worker does not need the original caller to be present.

### Drain worker

Two modes:

**In-process long-poll:** While the MCP server is running inside any host process (an n8n workflow, a Claude Code skill, a Cursor session, a custom Python orchestrator - the MCP makes no assumption about the host), a background asyncio task polls `drain_scheduled(as_of=now())` every 60 seconds and fires any due posts via the normal `publish()` path.

**CLI drain:** `content-distribution-mcp drain` runs as a one-shot drain (fire all due posts, exit) or persistent daemon (`--daemon`). This is suitable for cron:

```
*/5 * * * * content-distribution-mcp drain >> ~/.distribution-mcp/drain.log 2>&1
```

### Timezone handling

`schedule_at` must be ISO-8601 with timezone offset (e.g. `2026-05-20T09:00:00+09:00`). The drain worker compares against `datetime.now(UTC)`. The `hints()` tool for each channel includes a `best_times_utc` list - informational suggestions for the calling agent's variant generation. Local-TZ defaulting is the operator's responsibility at variant-generation time; the MCP stores and drains against UTC.

> OPEN: Should the `schedule()` MCP tool accept a shorthand like `"tomorrow 9am"` (natural language) and resolve it server-side? This would require TZ config on the MCP server. Recommendation: no - keep the MCP interface strict (ISO-8601 only). Callers can use Python's `dateutil` or an LLM step to resolve natural language before calling the MCP.

---

## 12. MCP Tool Surface

The MCP exposes eight tools. Tool docstrings below are the canonical spec - they will be used verbatim as FastMCP tool descriptions.

### `publish`

```
publish(content: Content, variants: list[Variant], profile_name: str) -> dict[str, PublishResult]

Publish one or more channel variants of a content piece immediately.

Returns a map of channel -> PublishResult. Each result has:
  state:    "live" | "needs_browser" | "failed"
  live_url: str | None  (present when state=live)
  draft_path: str | None  (present when state=needs_browser)
  compose_url: str | None  (present when state=needs_browser)
  error:    str | None  (present when state=failed)

Idempotent: re-running with the same content.id + channel returns the
existing live_url without re-publishing.

Partial failure: all variants are attempted independently. A failure on
one channel does not abort others.
```

### `schedule`

```
schedule(content: Content, variants: list[Variant], profile_name: str) -> dict[str, ScheduleResult]

Enqueue channel variants for future publishing.

Each variant must have schedule_at set (ISO-8601 with timezone offset).
Variants without schedule_at are rejected with error "missing_schedule_at".

Returns a map of channel -> ScheduleResult with state="queued" and
scheduled_for timestamp. Enqueued posts are drained by the worker.
```

### `drain`

```
drain(profile_name: str | None = None) -> DrainResult

Fire all scheduled posts due at or before now.

profile_name: if supplied, drains only posts from that profile.
              If None, drains all due posts from all profiles.

Returns DrainResult with counts: fired, succeeded, failed, still_pending.
Intended for CLI use (content-distribution-mcp drain) and monitoring.
```

### `status`

```
status(content_id: str) -> dict[str, PostLogEntry]

Return the current publish state for every channel a content piece
has been submitted to.

Returns a map of channel -> PostLogEntry. Includes live, failed,
queued, needs_browser, and taken_down entries.
```

### `unpublish`

```
unpublish(content_id: str, channel: str, profile_name: str) -> UnpublishResult

Attempt to remove the published post for (content_id, channel).

Returns UnpublishResult with:
  success:  bool
  reason:   str | None  (error or platform limitation note)

Note: not all platforms support delete. LinkedIn personal posts support
delete; company pages may not. Medium browser-posted content cannot be
programmatically deleted. Reddit delete works within ~60 minutes.
```

### `hints`

```
hints(channel: str) -> ChannelHints

Return static metadata for a channel. Used by callers to format
variants before calling publish().

channel: bare channel name ("devto", "hashnode", "linkedin",
         "github_discussions", "reddit", "medium").
         For Reddit, pass "reddit" for generic hints or
         "reddit:<subreddit>" for subreddit-specific hints
         (includes flair vocab from Subreddit Catalog).

Returns ChannelHints with title/body limits, canonical URL support,
tag constraints, required extras fields, and informational best times.
No LLM calls. Static data.
```

### `list_profiles`

```
list_profiles() -> list[ProfileSummary]

Return all distribution profiles in the configured StateBackend.

ProfileSummary includes: name, channels, subreddits (if any),
default_canonical, default_schedule. Does NOT include credentials.
```

### `list_subreddits`

```
list_subreddits(profile_name: str | None = None) -> list[SubredditSummary]

Return all subreddits in the Subreddit Catalog.

profile_name: if supplied, filters to subreddits in that profile's
              allowed list.

SubredditSummary includes: name, posting_cooldown_days,
self_promo_ratio_max, flair_vocab, last_posted_at, next_eligible_at.
next_eligible_at is computed as last_posted_at + posting_cooldown_days.
```

---

## 13. Profiles and Subreddit Catalog

### Distribution Profiles

**Notion DB schema (for NotionBackend):**

| Property | Type | Notes |
|---|---|---|
| Title | title | Profile name (e.g. "automatelab-developer") |
| Channels | multi-select | Options: devto, hashnode, linkedin, github_discussions, reddit, medium |
| Subreddits | multi-select | Bare subreddit names this profile is authorized to post to |
| Owned Domains | rich text | Newline-separated domains for self-promo ratio check |
| Default Canonical | select | source, first-published, per-channel |
| Default Schedule | rich text | ISO-8601 time-of-day hint (informational) |
| Default CTA Block | rich text | Appended to variants that support it |
| LinkedIn Tokens | rich text | Encrypted blob (access_token, refresh_token, expires_at) |
| GitHub PAT | rich text | Encrypted or referenced from `.env` |
| DEV.to API Key | rich text | Encrypted or referenced from `.env` |
| Hashnode API Key | rich text | Encrypted or referenced from `.env` |
| Reddit Credentials | rich text | JSON blob: client_id, client_secret, username, refresh_token |

> OPEN: Credential storage in Notion is not ideal for security. The recommended pattern for NotionBackend is to store a placeholder like `env:DEVTO_API_KEY` in the Notion field and have the backend resolve it from the environment at load time. This keeps secrets out of Notion while keeping profiles there. Formalize this resolution pattern before v1 implementation.

**YAML shape (for YamlBackend, mirroring Notion 1:1):**

```yaml
entries:
  - name: automatelab-developer
    channels:
      - devto
      - hashnode
      - github_discussions
      - reddit
      - linkedin
    subreddits:
      - LocalLLaMA
      - MachineLearning
      - Python
    owned_domains:
      - automatelab.tech
    default_canonical: source
    default_schedule: "09:00+09:00"
    default_cta_block: |
      ---
      Follow [AutomateLab](https://automatelab.tech) for more automation guides.
    devto_api_key: env:DEVTO_API_KEY
    hashnode_api_key: env:HASHNODE_API_KEY
    github_pat: env:GITHUB_PAT
    reddit_client_id: env:REDDIT_CLIENT_ID
    reddit_client_secret: env:REDDIT_CLIENT_SECRET
    reddit_username: env:REDDIT_USERNAME
    reddit_refresh_token: env:REDDIT_REFRESH_TOKEN
    linkedin_access_token: env:LINKEDIN_ACCESS_TOKEN
    linkedin_refresh_token: env:LINKEDIN_REFRESH_TOKEN
    linkedin_token_expires_at: "2026-07-18T00:00:00Z"
```

### Subreddit Catalog

**Notion DB schema (for NotionBackend):**

| Property | Type | Notes |
|---|---|---|
| Title | title | Subreddit name without r/ prefix (e.g. "LocalLLaMA") |
| Rules Summary | rich text | Human-readable posting rules for operator reference |
| Self-Promo Ratio Max | number | Float 0.0-1.0 (default 0.10) |
| Posting Cooldown Days | number | Integer days between posts (default 7) |
| Flair Vocab | multi-select | Allowed flair strings |
| Last Posted At | date | Updated by adapter on successful post |
| Account Age Min Days | number | Documented minimum (informational; AutoMod enforced) |
| Karma Min | number | Documented minimum (informational; AutoMod enforced) |

**YAML shape:**

```yaml
entries:
  - name: LocalLLaMA
    rules_summary: "Weekly thread for project showcases. Self-promo OK in designated threads."
    self_promo_ratio_max: 0.10
    posting_cooldown_days: 14
    flair_vocab:
      - Project
      - Tutorial
      - Discussion
    last_posted_at: "2026-05-10T08:30:00Z"
    account_age_min_days: 30
    karma_min: 100
```

### Post Log

**Notion DB schema (for NotionBackend):**

| Property | Type | Notes |
|---|---|---|
| Title | title | Auto-generated: "<content_id> @ <channel>" |
| Content ID | text | content.id value |
| Channel | select | Channel identifier including subreddit for Reddit |
| State | select | pending, queued, live, failed, needs_browser, taken_down |
| Live URL | url | Present when state=live |
| Source Task | relation | Relation to Tasks DB |
| Posted At | date (datetime) | UTC timestamp of successful publish |
| Error | rich text | Last error message |
| Retry Count | number | Number of attempts made |
| Next Retry At | date (datetime) | For transient failures |

---

## 14. Integration: agency-os, publishing-skills, ai-seo-mcp

The Content Distribution MCP is designed as a standalone tool with optional integration into the AutomateLab four-product stack. Integration is via a soft-dependency adapter pattern: nothing in `src/content_distribution_mcp/` imports from agency-os or any other product. Integration points are:

### agency-os

agency-os is the control plane. It provides:

- `source_task_id` values (e.g. "AL-312") that flow into `Content.source_task_id`.
- The Notion workspace where NotionBackend's three databases live.
- The `content-distribution` Claude Code skill (public, ships with this repo) is the reference orchestrating caller. The private flavor `al-content-distribution` (in the `automatelab` repo) wraps it with project-specific defaults. Any MCP-capable host - an n8n workflow, a Cursor agent, a plain Python script using `mcp` client libraries, a custom integration - is a peer and can replace either.

The integration is purely by convention: the MCP writes back to the source task's Notion page via `mark_published()`. If `source_task_id` is `None` (standalone use, no agency-os), the write-back step is skipped silently.

### publishing-skills

publishing-skills (the `al-write-blog-post` skill) is the upstream content producer. Its output - a Ghost blog post URL, a Markdown draft, tags, title - is the input to a distribution run. The orchestrating skill (`content-distribution` publicly, or `al-content-distribution` privately) wraps both: it calls `al-write-blog-post`, receives the published Ghost URL as `canonical_url`, constructs a `Content` object, generates per-channel `Variant` objects (via an LLM call - the caller picks the model; the MCP never makes LLM calls itself), then calls this MCP's `publish()` tool.

Dependency direction: the orchestrating skill depends on both `al-write-blog-post` and `content-distribution-mcp`. Neither depends on the other. This keeps the MCP's public API clean.

### ai-seo-mcp

ai-seo-mcp is a post-publish audit gate. After `publish()` returns `live_url` values, the orchestrating skill optionally calls `ai-seo-mcp audit(url)` on the canonical URL (the Ghost blog post) and on any DEV.to / Hashnode cross-posts. This checks entity authority signals, schema markup, and AI-citation readiness.

The integration point is: ai-seo-mcp reads the live URLs returned by content-distribution-mcp and audits them. No API dependency between the two MCPs. The orchestrating skill is the coordinator.

### Dependency diagram

```
[Ghost blog post (al-write-blog-post)]
            |
            v  canonical_url
[content-distribution skill]  <-- source_task_id from agency-os
            |
            +---> [Content Distribution MCP]  ---> DEV.to
            |                                 ---> Hashnode
            |                                 ---> LinkedIn
            |                                 ---> Reddit
            |                                 ---> GH Discussions
            |                                 ---> Medium (browser)
            |
            +---> [ai-seo-mcp audit]  <-- live_urls from above
```

---

## 15. Cross-linking Strategy

Entity authority in AI search is built through consistent entity mentions across multiple high-trust sources. The four-product stack (agency-os, publishing-skills, content-distribution-mcp, ai-seo-mcp) should be cross-linked bidirectionally.

### README links (content-distribution-mcp repo)

The README will include a "Part of the AutomateLab stack" section with links to:
- `github.com/AutomateLab-tech/agency-os` - "Control plane and Notion integration"
- The publishing-skills skill documentation
- `github.com/AutomateLab-tech/ai-seo-mcp` - "AI citation audit after publish"
- `automatelab.tech` - "Blog and tutorials"

### Website links (automatelab.tech/tools)

The `/tools/content-distribution-mcp` page (to be created by `al-deploy`) will cross-link to the other three tools and to the blog post announcing the MCP. The blog post will be written via `al-write-blog-post` and will itself be cross-posted using the Content Distribution MCP (dog-fooding the launch).

### Blog mentions

The launch blog post on automatelab.tech will mention all four tools and link to the GitHub repos. This post will be cross-posted to DEV.to and Hashnode with canonical pointing back to the Ghost post. The cross-post is itself the first live demonstration of the MCP.

### Bidirectionality requirement

For each new product page or blog post that mentions this MCP, the content-distribution-mcp README should be updated to link back. This is a manual step in the `al-deploy` post-publish checklist.

---

## 16. Out of Scope (Post-v1)

### Dropped entirely

- **Quora** - No viable API path. Browser automation is possible but the platform's content moderation for AI-automation-related content, combined with the audience fit (Q&A format vs. article format), makes it a poor investment. Dropped from v1 and not planned for v2.

### Deferred to post-v1

- **X / Twitter** - The thread structure (1-tweet, thread, quote-tweet) and the current API pricing model ($5,000/month for Basic access) make this a separate concern that warrants its own MCP. When built, it would be a separate adapter that can be optionally installed alongside this one.
- **Bluesky** - AT Protocol and PDS architecture are still evolving. The API is functional but the developer ecosystem conventions are not stable enough to commit to a v1 interface.
- **Mastodon** - Instance fragmentation (each instance has a different domain, different API endpoint, different moderation rules) requires a distinct configuration design (multi-instance profiles). Post-v1 candidate.
- **AI-driven copy variation** - Generating per-platform adapted copy inside the MCP contradicts the model-agnostic design principle. Copy variation belongs in publishing-skills or in the orchestrating skill upstream. This MCP receives finished variants and ships them; it does not generate them.
- **SqliteBackend** - If public adoption surfaces demand for zero-config local use without YAML friction, a SQLite backend is the v2 candidate. Not needed for v1 (YAML covers the same use case).
- **Native Medium adapter** - Dependent on Medium reopening partner API access. Not on Medium's public roadmap as of May 2026.
- **Engagement analytics, comment monitoring, reply automation** - These are a separate product surface. Post-v1.
- **Web dashboard** - Status surfaces adequately via the `status` MCP tool and Notion Post Log views. A web dashboard adds engineering surface area for marginal operator benefit in v1.
- **Multi-workspace / multi-tenant** - Not needed for the initial operator audience (solo developers, small agencies). V2 candidate.

---

## 17. Moat and Defensibility

The research (AL-391) validated five moat hypotheses. This section elaborates on each and explains why the combination is difficult to commoditize.

### 1. Reddit with per-subreddit rules and Subreddit Catalog (Confirmed)

The Reddit adapter is not simply "post to Reddit." It is a rule-enforcement layer that manages per-subreddit cooldowns, global daily caps, self-promo ratio tracking, flair resolution, and account-age/karma documentation - all derived from a curated Subreddit Catalog that the operator maintains over time. Pipepost has zero Reddit support. Social scheduler MCPs (Buffer, Hypefury) target X and Instagram; they do not address Reddit's API or community norms at all.

The Subreddit Catalog is the non-commoditizable artifact: a curated, maintained table of which subreddits accept what kind of content, at what frequency, with what flair requirements. This catalog is not public data - it is accumulated operator knowledge encoded as structured data. The more subreddits an operator adds to their catalog, the more valuable the tool becomes for their specific content strategy. This is a flywheel with meaningful setup cost: a competitor can ship a Reddit adapter in a week, but they cannot replicate a curated 50-subreddit catalog with six months of posting history and per-sub cooldown calibration.

### 2. GitHub Discussions adapter (Confirmed)

GitHub Discussions is where open-source maintainers and their communities discuss projects. A blog post published to a GitHub Discussions "Show and tell" category in a popular repo (e.g. the MCP SDK repo, a popular AI framework) reaches developers who are not on DEV.to or Hashnode. No existing MCP ships a GitHub Discussions publishing adapter. The combination of dev-platform publishing (DEV.to + Hashnode) with community-platform publishing (GitHub Discussions + Reddit) is our differentiated adapter set.

### 3. StateBackend abstraction with dual YAML and Notion backends (Confirmed)

Pipepost stores local config files and has no structured state management or post-log. Our StateBackend abstraction provides: idempotency, partial-failure recovery, scheduled post queuing, URL write-back to source tasks, and a queryable post log. The dual-backend design (YAML for solo local use, Notion for team/agency use) covers the full operator spectrum without requiring a database.

The StateBackend interface is itself a moat: it is the contract that makes NotionBackend and YamlBackend interchangeable. A third-party developer could contribute a `SqliteBackend` or `PostgresBackend` without touching the adapter code. This extensibility is harder to reproduce in a tool that bakes state into a proprietary format.

### 4. URL write-back to source Notion task (Confirmed)

When the NotionBackend records a successful publish, it appends the live URL back to the source Notion task that originated the content. For agency-os operators, this closes the loop: the Notion task that triggered the blog post writing now has a complete record of where the post was distributed and what the live URLs are. This is not a feature any public competitor ships, because it requires the specific NotionBackend + agency-os integration. For the automatelab operator audience (developers who manage content pipelines in Notion), this is high-value convenience that would be annoying to build themselves.

### 5. Per-sub cooldown and self-promo ratio enforcement (Confirmed)

Reddit account safety is an unsolved UX problem for automated posting tools. The informal rules (5 posts/day global cap, per-subreddit cooldown, 10% self-promo ratio) are widely documented in Reddit moderation guides but not enforced by any existing publishing tool. A developer who automates Reddit posting without these guardrails will get shadow-banned within weeks. Our adapter ships the guardrails by default, making it the responsible choice for any developer who wants to maintain a real Reddit presence alongside automated content distribution.

### Combination effect

The moat is not any single feature - it is the combination. A developer who wants (Reddit with rules) AND (GitHub Discussions) AND (Notion URL write-back) AND (idempotent state management) AND (dual YAML/Notion backends) has no existing tool to reach for. They would have to build all five. This MCP ships all five in one install. The network effect builds as more operators contribute Subreddit Catalog entries and as the `content-distribution` skill matures into a well-documented orchestration pattern.

---

## Tech Stack

- **Python 3.11+**
- **FastMCP** for the MCP server (stdio and SSE transport)
- **PRAW 7.x** for Reddit
- **requests** (or **httpx** for async) for DEV.to, Hashnode, GitHub Discussions, LinkedIn
- **Playwright** for browser fallback (optional install: `[browser]` extra)
- **pydantic v2** for data models and validation
- **filelock** for YamlBackend atomic writes
- **No database driver, no ORM.** YAML + Notion REST only.

---

## Open Questions Summary

| # | Section | Question |
|---|---|---|
| OQ-1 | Sec 5 (DEV.to) | Add DEV.to series support via `extras["series"]` in v1 or v1.1? |
| OQ-2 | Sec 5 (Hashnode) | Confirm `removePost` mutation availability in current Hashnode API. |
| OQ-3 | Sec 5 (GH Discussions) | Multi-repo posting: multiple variants with different `extras["github_repo"]`, or a dedicated multi-repo adapter feature? |
| OQ-4 | Sec 8 (Reddit) | Formalize `owned_domains` field in Profile schema. |
| OQ-5 | Sec 8 (Reddit) | AutoModerator removal detection via `submission.removed` poll - validate against PRAW 7.x behavior. |
| OQ-6 | Sec 9 (LinkedIn) | Hardcode OAuth callback port 8765 or make configurable? |
| OQ-7 | Sec 9 (LinkedIn) | Support LinkedIn article-type posting (Articles API) in v1 or v1.1? |
| OQ-8 | Sec 10 (Medium) | Playwright pre-fill DOM selectors for Medium editor - wrap in try/except; add `--no-prefill` CLI flag. |
| OQ-9 | Sec 11 (Scheduling) | Accept natural-language `schedule_at` (e.g. "tomorrow 9am") in the MCP tool? Recommendation: no - ISO-8601 only. |
| OQ-10 | Sec 13 (Profiles) | Credential storage in Notion: formalize `env:VAR_NAME` resolution pattern before v1 implementation. |
