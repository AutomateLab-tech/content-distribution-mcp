"""
Content Distribution MCP Server.

Exposes eight FastMCP tools for publishing, scheduling, and managing
cross-channel content distribution. No LLM calls are made inside this server;
it is a pure I/O orchestrator.

Transport: stdio (default) or SSE. Run with:
    python server.py

Or install and run via the package entry point:
    content-distribution-mcp

Environment variables
---------------------
DISTRIBUTION_BACKEND : str
    Which StateBackend to instantiate. Values: ``"yaml"`` (default), ``"notion"``.
DISTRIBUTION_BACKEND_DIR : str | None
    Directory for YamlBackend storage. Defaults to ``~/.distribution-mcp/``.
DISTRIBUTION_NOTION_PROFILES_DB_ID : str | None
    Notion database ID for Distribution Profiles (NotionBackend only).
DISTRIBUTION_NOTION_SUBREDDIT_CATALOG_DB_ID : str | None
    Notion database ID for Subreddit Catalog (NotionBackend only).
DISTRIBUTION_NOTION_POST_LOG_DB_ID : str | None
    Notion database ID for Post Log (NotionBackend only).
DISTRIBUTION_NOTION_TOKEN : str | None
    Notion integration token (NotionBackend only). Separate from the
    al-notion ``NOTION_KEY`` so permissions can be scoped independently.

Python 3.11+.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from .idempotency import RetryPolicy  # type: ignore[import]
from .models import ChannelHints, Content, PublishResult, Variant  # type: ignore[import]
from . import scheduler  # type: ignore[import]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("content-distribution-mcp")

# ---------------------------------------------------------------------------
# Backend + adapter initialisation
# ---------------------------------------------------------------------------


def _build_backend() -> Any:
    """Instantiate the StateBackend selected by DISTRIBUTION_BACKEND."""
    backend_name = os.environ.get("DISTRIBUTION_BACKEND", "yaml").lower()

    if backend_name == "yaml":
        from pathlib import Path
        from .backends.yaml_backend import YamlBackend  # type: ignore[import]
        storage_dir = os.environ.get(
            "DISTRIBUTION_BACKEND_DIR",
            os.path.expanduser("~/.distribution-mcp"),
        )
        return YamlBackend(base_dir=Path(storage_dir))

    if backend_name == "notion":
        from .backends.notion_backend import NotionBackend  # type: ignore[import]
        return NotionBackend(
            token=os.environ["DISTRIBUTION_NOTION_TOKEN"],
            profiles_db_id=os.environ["DISTRIBUTION_NOTION_PROFILES_DB_ID"],
            subreddit_catalog_db_id=os.environ["DISTRIBUTION_NOTION_SUBREDDIT_CATALOG_DB_ID"],
            post_log_db_id=os.environ["DISTRIBUTION_NOTION_POST_LOG_DB_ID"],
        )

    raise ValueError(
        f"Unknown DISTRIBUTION_BACKEND={backend_name!r}. "
        "Valid values: 'yaml', 'notion'."
    )


def _build_adapter_map() -> dict[str, Any]:
    """Instantiate all channel adapters keyed by their platform prefix."""
    from .adapters.devto import DevToAdapter  # type: ignore[import]
    from .adapters.hashnode import HashnodeAdapter  # type: ignore[import]
    from .adapters.github_discussions import GitHubDiscussionsAdapter  # type: ignore[import]
    from .adapters.reddit import RedditAdapter  # type: ignore[import]
    from .adapters.medium_browser import MediumBrowserAdapter  # type: ignore[import]
    from .adapters.bluesky import BlueskyAdapter  # type: ignore[import]
    from .adapters.linkedin_browser import LinkedInBrowserAdapter  # type: ignore[import]
    from .adapters.twitter_browser import TwitterBrowserAdapter  # type: ignore[import]

    # Legacy LinkedIn API adapter import is conditional — may not exist.
    try:
        from .adapters.linkedin import LinkedInAdapter  # type: ignore[import]
        linkedin = LinkedInAdapter()
    except ImportError:
        linkedin = None

    adapters: dict[str, Any] = {
        "devto": DevToAdapter(),
        "hashnode": HashnodeAdapter(),
        "github_discussions": GitHubDiscussionsAdapter(),
        "github-discussions": GitHubDiscussionsAdapter(),
        "reddit": RedditAdapter(),
        "medium_browser": MediumBrowserAdapter(),
        "medium-browser": MediumBrowserAdapter(),
        "bluesky": BlueskyAdapter(),
        "linkedin_browser": LinkedInBrowserAdapter(),
        "linkedin-browser": LinkedInBrowserAdapter(),
        "twitter_browser": TwitterBrowserAdapter(),
        "twitter-browser": TwitterBrowserAdapter(),
    }
    if linkedin is not None:
        adapters["linkedin"] = linkedin

    return adapters


# Module-level singletons — initialised once at import time.
try:
    state_backend: Any = _build_backend()
    adapter_map: dict[str, Any] = _build_adapter_map()
    retry_policy = RetryPolicy()
except Exception as _init_err:  # noqa: BLE001
    logger.warning(
        "server: backend/adapter init deferred — %s. "
        "Tools will raise on first call. "
        "Set DISTRIBUTION_BACKEND and credentials before use.",
        _init_err,
    )
    state_backend = None  # type: ignore[assignment]
    adapter_map = {}
    retry_policy = RetryPolicy()


# ---------------------------------------------------------------------------
# Helper: guard for uninitialised backend
# ---------------------------------------------------------------------------


def _require_backend() -> Any:
    if state_backend is None:
        raise RuntimeError(
            "StateBackend is not initialised. "
            "Set DISTRIBUTION_BACKEND and required env vars, then restart the server."
        )
    return state_backend


# ---------------------------------------------------------------------------
# Tool 1: publish
# ---------------------------------------------------------------------------


@mcp.tool()
async def publish(
    content: Content,
    variants: list[Variant],
    profile_name: str,
) -> list[dict[str, Any]]:
    """Publish one or more channel variants of a content piece immediately.

    Returns a list of publish results, one per input variant. Each result dict
    contains:

    - ``channel``      (str)        — target channel in ``<platform>:<sub>`` format.
    - ``state``        (str)        — ``"live"`` | ``"needs_browser"`` | ``"failed"`` | ``"queued"``.
    - ``live_url``     (str | null) — public URL of the post; set when ``state="live"``.
    - ``draft_path``   (str | null) — local draft file path; set when ``state="needs_browser"``.
    - ``compose_url``  (str | null) — platform editor URL; set when ``state="needs_browser"``.
    - ``error``        (str | null) — error description; set when ``state="failed"``.
    - ``published_at`` (str | null) — UTC ISO-8601 timestamp of publish; set when live.

    Idempotency guarantee
    ---------------------
    Re-running with the same ``content.id`` + ``channel`` pair returns the
    existing ``live_url`` immediately without making another platform API call.
    Safe to call multiple times without risk of duplicate posts.

    Partial failure behaviour
    -------------------------
    All variants are attempted independently in parallel. A failure on one
    channel does not abort others. Inspect each result's ``state`` individually.
    Re-run to retry only the failed channels (idempotency skips successful ones).

    Retry policy
    ------------
    Transient errors (5xx, 429, network timeout) are retried with exponential
    backoff up to the per-channel limit (Reddit: 1 retry; others: 3 retries).
    Permanent errors (4xx auth, ToS rejection, cooldown) fail immediately.

    Parameters
    ----------
    content : Content
        The canonical content record. ``content.id`` is the idempotency anchor.
    variants : list[Variant]
        One or more channel-specific variants. Each must have ``channel`` set.
        Variants without ``schedule_at`` are published immediately; variants
        with ``schedule_at`` are rejected (use the ``schedule`` tool instead).
    profile_name : str
        Name of the distribution profile to use. Profile carries credentials
        for all target channels.
    """
    backend = _require_backend()
    profile = backend.load_profile(profile_name)

    async def _publish_with_retry(variant: Variant) -> PublishResult:
        return await retry_policy.wrap(
            adapter_map[variant.channel.split(":")[0]],
            variant,
            profile,
            backend,
        ) if variant.channel.split(":")[0] in adapter_map else PublishResult(
            channel=variant.channel,
            state="failed",
            error=f"no-adapter-for-channel: {variant.channel}",
        )

    results = await scheduler.publish_immediate(
        content, variants, profile, adapter_map, backend
    )

    return [r.model_dump(mode="json") for r in results]


# ---------------------------------------------------------------------------
# Tool 2: schedule
# ---------------------------------------------------------------------------


@mcp.tool()
async def schedule(
    content: Content,
    variants: list[Variant],
    profile_name: str,
) -> dict[str, Any]:
    """Enqueue channel variants for future publishing or publish immediately.

    Variants with ``schedule_at`` set (ISO-8601 with timezone offset, e.g.
    ``"2026-05-20T09:00:00+09:00"``) are enqueued for future drain.
    Variants without ``schedule_at`` are published immediately via the normal
    publish path (same retry policy as the ``publish`` tool).

    Returns a dict mapping channel → result:

    - For **immediate** variants: a publish result dict (same shape as ``publish``).
    - For **scheduled** variants: a ``scheduled_id`` string (opaque, use for
      tracking; will appear in ``status`` output once the drain worker fires it).

    Timezone note
    -------------
    ``schedule_at`` must carry a UTC offset. Naive datetimes are rejected.
    The drain worker compares against ``datetime.now(UTC)``. Use the
    ``hints`` tool's ``best_times_utc`` for scheduling suggestions.

    Parameters
    ----------
    content : Content
        The canonical content record.
    variants : list[Variant]
        Mixed list — some variants may have ``schedule_at``, others may not.
    profile_name : str
        Name of the distribution profile to use.
    """
    backend = _require_backend()
    profile = backend.load_profile(profile_name)

    raw = await scheduler.schedule(content, variants, profile, adapter_map, backend)

    # Serialise: PublishResult → dict, str scheduled_id stays as str.
    return {
        channel: (v.model_dump(mode="json") if isinstance(v, PublishResult) else v)
        for channel, v in raw.items()
    }


# ---------------------------------------------------------------------------
# Tool 3: drain
# ---------------------------------------------------------------------------


@mcp.tool()
async def drain(
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Fire all scheduled posts due at or before *now*.

    Retrieves all queued variants from the StateBackend whose ``schedule_at``
    is at or before *now*, then publishes each one via the appropriate adapter.
    The drain is atomic and destructive: each variant is dequeued before being
    fired so no variant is published twice even if drain is called concurrently.

    Intended for:

    - CLI one-shot runs: ``content-distribution-mcp drain``
    - Cron: ``*/5 * * * * content-distribution-mcp drain``
    - Manual trigger when testing scheduled post timing.

    Returns a list of publish result dicts (same shape as ``publish`` results),
    one per variant that was due. Empty list if nothing was due.

    Parameters
    ----------
    now : str | None
        ISO-8601 UTC datetime string used as the drain window boundary.
        Defaults to the current UTC time when ``None``.
        Example: ``"2026-05-20T09:00:00Z"``.
    """
    backend = _require_backend()

    drain_time: datetime | None = None
    if now is not None:
        drain_time = datetime.fromisoformat(now)
        if drain_time.tzinfo is None:
            drain_time = drain_time.replace(tzinfo=timezone.utc)

    results = await scheduler.drain(adapter_map, backend, drain_time)
    return [r.model_dump(mode="json") for r in results]


# ---------------------------------------------------------------------------
# Tool 4: status
# ---------------------------------------------------------------------------


@mcp.tool()
def status(
    content_id: str | None = None,
    channel: str | None = None,
) -> list[dict[str, Any]]:
    """Return the current publish state for content pieces in the post log.

    Queries the StateBackend's post log and returns matching entries. At least
    one of ``content_id`` or ``channel`` should be supplied; calling with both
    ``None`` returns up to 200 recent entries (backend default cap).

    Each returned dict contains:

    - ``channel``      (str)        — target channel in ``<platform>:<sub>`` format.
    - ``state``        (str)        — ``"live"`` | ``"queued"`` | ``"needs_browser"``
                                      | ``"failed"`` | ``"taken_down"``.
    - ``live_url``     (str | null) — public URL when ``state="live"``.
    - ``published_at`` (str | null) — UTC ISO-8601 timestamp of successful publish.
    - ``error``        (str | null) — last error message when ``state="failed"``.
    - ``content_id``   (str | null) — content identifier (if stored in backend).
    - ``retry_count``  (int | null) — number of attempts made (if stored in backend).
    - ``next_retry_at``(str | null) — UTC ISO-8601 next retry window (if applicable).

    Use cases
    ---------
    - ``status(content_id="my-post@2026-05-18")`` — all channels for a piece.
    - ``status(channel="reddit:LocalLLaMA")`` — all posts to a subreddit.
    - ``status(content_id="my-post@2026-05-18", channel="devto:main")`` — one entry.
    - ``status()`` — recent 200 entries (monitoring / dashboard use).

    Parameters
    ----------
    content_id : str | None
        Filter by the ``Content.id`` value. Supply to see all channels for one
        piece of content.
    channel : str | None
        Filter by channel in ``<platform>:<sub>`` format. Supply to see all
        posts to a specific channel.
    """
    backend = _require_backend()

    # YamlBackend stores the post log as a list of dicts via mark_published;
    # list_post_log takes keyword filters and returns those dicts directly.
    entries: list[dict[str, Any]] = backend.list_post_log(
        content_id=content_id,
        channel=channel,
    )

    results: list[dict[str, Any]] = []
    for entry in entries:
        row: dict[str, Any] = {
            "channel": entry.get("channel"),
            "state": entry.get("state"),
            "live_url": entry.get("published_url"),
            "published_at": entry.get("updated_at"),
            "error": entry.get("error"),
            "content_id": entry.get("content_id"),
            "retry_count": entry.get("retry_count"),
            "next_retry_at": entry.get("next_retry_at"),
        }
        results.append(row)

    return results


# ---------------------------------------------------------------------------
# Tool 5: unpublish
# ---------------------------------------------------------------------------


@mcp.tool()
def unpublish(
    live_url: str,
    channel: str,
) -> dict[str, Any]:
    """Attempt to remove a published post from a platform.

    Looks up the correct adapter from the channel prefix and calls
    ``adapter.unpublish(live_url)``. Not all platforms support programmatic
    deletion:

    - **DEV.to**: unpublish (sets ``published=false``); does not hard-delete.
    - **Hashnode**: uses ``removePost`` mutation if available.
    - **GitHub Discussions**: uses ``deleteDiscussion`` mutation (requires
      ``admin:discussion`` scope — see README).
    - **Reddit**: deletion works within ~60 minutes of posting via PRAW.
      Some subreddits lock posts sooner.
    - **LinkedIn personal**: ``DELETE /posts/{id}`` via Posts API.
    - **LinkedIn company page**: may require owner-level permissions.
    - **Medium**: always fails — no programmatic delete path for browser-posted
      content. Delete manually in the Medium editor.

    Returns a dict with:

    - ``success`` (bool)       — ``True`` if the post was removed.
    - ``error``   (str | null) — error or platform limitation note on failure.

    Parameters
    ----------
    live_url : str
        The public URL of the post to remove, as stored in the post log's
        ``live_url`` field.
    channel : str
        Channel identifier in ``<platform>:<sub>`` format (e.g.
        ``"reddit:LocalLLaMA"``, ``"devto:main"``). Used to select the adapter.
    """
    adapter_key = channel.split(":")[0]
    adapter = adapter_map.get(adapter_key)

    if adapter is None:
        return {
            "success": False,
            "error": f"no-adapter-for-channel: {channel}",
        }

    try:
        success, reason = adapter.unpublish(live_url)
        return {"success": success, "error": reason}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool 6: hints
# ---------------------------------------------------------------------------


@mcp.tool()
def hints(channel: str) -> dict[str, Any]:
    """Return static metadata for a channel to inform variant formatting.

    Callers should fetch hints *before* constructing a ``Variant`` so they
    can produce content that fits the platform's constraints without needing
    a round-trip publish attempt.

    Returned dict mirrors ``ChannelHints`` fields:

    - ``max_length``              (int | null)  — max body character count.
    - ``supported_md_features``   (list[str])   — Markdown features rendered correctly.
    - ``tag_vocab``               (list | null) — suggested tag vocabulary (subset).
    - ``cta_placement``           (str)         — ``"top"`` | ``"bottom"`` | ``"footer"``
                                                   | ``"none"``.
    - ``canonical_url_supported`` (bool)        — ``True`` if the platform stores it
                                                   natively.
    - ``browser_only``            (bool)        — ``True`` for Medium (always
                                                   ``state=needs_browser``).

    Channel names
    -------------
    - ``"devto"``              — DEV.to (Forem API v1).
    - ``"hashnode"``           — Hashnode GraphQL API.
    - ``"github_discussions"`` — GitHub Discussions GraphQL API.
    - ``"linkedin"``           — LinkedIn Posts API.
    - ``"reddit"``             — Generic Reddit hints (no flair vocab).
    - ``"reddit:<subreddit>"`` — Subreddit-specific hints including flair vocab
                                  from the Subreddit Catalog (e.g. ``"reddit:LocalLLaMA"``).
    - ``"medium_browser"``     — Medium browser fallback (always ``needs_browser``).

    No LLM calls. Returns static hardcoded data from the adapter.

    Parameters
    ----------
    channel : str
        Bare channel name or compound ``<platform>:<sub>`` format.
    """
    adapter_key = channel.split(":")[0]
    adapter = adapter_map.get(adapter_key)

    if adapter is None:
        raise ValueError(
            f"No adapter registered for channel {channel!r}. "
            f"Available: {sorted(adapter_map.keys())}"
        )

    channel_hints: ChannelHints = adapter.hints()
    return channel_hints.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool 7: list_profiles
# ---------------------------------------------------------------------------


@mcp.tool()
def list_profiles() -> list[str]:
    """Return all distribution profile names in the configured StateBackend.

    Each profile represents a named set of target channels (e.g.
    ``"developer"`` → DEV.to + Hashnode + GitHub Discussions,
    ``"social"`` → LinkedIn + Reddit).

    Returns a list of profile name strings. To inspect a profile's channels
    and settings, load it via the backend (not exposed as a tool to avoid
    leaking credentials).

    Note: ``StateBackend.list_profiles()`` is not in the v1 protocol defined
    by AL-402. This tool attempts a best-effort call; if the backend does not
    implement ``list_profiles()``, it returns an empty list with a logged
    warning. This is a known gap — a ``list_profiles()`` method should be
    added to the ``StateBackend`` protocol in a follow-up task.

    # TODO: add ``list_profiles() -> list[str]`` to StateBackend protocol
    # (AL-402 follow-up). Both YamlBackend and NotionBackend need to implement
    # this before this tool can return meaningful data in all configurations.
    """
    backend = _require_backend()

    if not hasattr(backend, "list_profiles"):
        logger.warning(
            "list_profiles: backend %r does not implement list_profiles(). "
            "Returning empty list. Add list_profiles() to StateBackend protocol.",
            type(backend).__name__,
        )
        return []

    try:
        return backend.list_profiles()
    except NotImplementedError:
        logger.warning(
            "list_profiles: backend %r raised NotImplementedError. "
            "Returning empty list.",
            type(backend).__name__,
        )
        return []


# ---------------------------------------------------------------------------
# Tool 8: list_subreddits
# ---------------------------------------------------------------------------


@mcp.tool()
def list_subreddits(
    profile_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return all subreddits in the Subreddit Catalog.

    The Subreddit Catalog is the operator-maintained per-subreddit rule store.
    It encodes cooldown periods, self-promotion ratio limits, flair vocabulary,
    and account age/karma requirements. Inspect this before choosing which
    subreddits to include in a publish run.

    Each returned dict contains:

    - ``name``                (str)        — subreddit name without ``r/`` prefix.
    - ``posting_cooldown_days``(int)       — minimum days between posts.
    - ``self_promo_ratio_max`` (float)     — max fraction of posts that may be
                                             self-promotional (0.0–1.0).
    - ``flair_vocab``         (list[str])  — allowed flair strings. First entry
                                             is the adapter's default flair.
    - ``last_posted_at``      (str | null) — UTC ISO-8601 of most recent post.
    - ``next_eligible_at``    (str | null) — UTC ISO-8601 of next eligible post
                                             (``last_posted_at + posting_cooldown_days``).
                                             ``null`` if never posted.
    - ``account_age_min_days``(int)        — documented minimum account age (informational;
                                             enforced by AutoModerator, not the adapter).
    - ``karma_min``           (int)        — documented karma minimum (informational).
    - ``notes``               (str | null) — operator notes on moderation quirks.

    Filtering by profile
    --------------------
    If ``profile_name`` is supplied, only subreddits listed in that profile's
    ``subreddits`` allowlist are returned. This lets callers see which
    subreddits are active for a given distribution strategy.

    Parameters
    ----------
    profile_name : str | None
        If supplied, filters to subreddits in the named profile's allowlist.
        If ``None``, returns all entries in the catalog.
    """
    backend = _require_backend()

    # Determine which subreddits to show.
    allowed: set[str] | None = None
    if profile_name is not None:
        try:
            profile = backend.load_profile(profile_name)
            # Profile.channels carries ChannelConfig entries; subreddits are
            # stored as a separate field on Profile (YamlBackend) or as
            # multi-select (NotionBackend).
            subreddits_attr = getattr(profile, "subreddits", None)
            if subreddits_attr is not None:
                allowed = set(subreddits_attr)
            else:
                # Fall back: extract reddit:<name> entries from channels.
                allowed = {
                    cfg.channel.split(":", 1)[1]
                    for cfg in (profile.channels or [])
                    if cfg.channel.startswith("reddit:")
                }
        except KeyError:
            raise ValueError(f"Profile {profile_name!r} not found.")

    # Fetch subreddit rules from the catalog.
    # Backend method: load_subreddit_rules(subreddit) or list_subreddits().
    if hasattr(backend, "list_subreddits"):
        all_rules = backend.list_subreddits()
    else:
        # No bulk list method — return empty with a clear note.
        logger.warning(
            "list_subreddits: backend %r does not implement list_subreddits(). "
            "Returning empty list. Add list_subreddits() to StateBackend protocol.",
            type(backend).__name__,
        )
        return []

    results = []
    for rule in all_rules:
        name = getattr(rule, "subreddit", None) or getattr(rule, "name", None)
        if allowed is not None and name not in allowed:
            continue

        # Compute next_eligible_at from last_posted_at + cooldown.
        last_posted_at = getattr(rule, "last_posted_at", None)
        cooldown_days = getattr(rule, "posting_cooldown_days", None)
        next_eligible_at: str | None = None
        if last_posted_at is not None and cooldown_days is not None:
            try:
                from datetime import timedelta
                if isinstance(last_posted_at, str):
                    lp = datetime.fromisoformat(last_posted_at)
                else:
                    lp = last_posted_at
                next_dt = lp + timedelta(days=cooldown_days)
                next_eligible_at = next_dt.isoformat()
            except Exception:  # noqa: BLE001
                next_eligible_at = None

        row: dict[str, Any] = {
            "name": name,
            "posting_cooldown_days": cooldown_days,
            "self_promo_ratio_max": getattr(rule, "self_promo_ratio_max", 0.10),
            "flair_vocab": getattr(rule, "flair_vocab", []),
            "last_posted_at": (
                last_posted_at.isoformat()
                if isinstance(last_posted_at, datetime)
                else last_posted_at
            ),
            "next_eligible_at": next_eligible_at,
            "account_age_min_days": getattr(rule, "account_age_min_days", 0),
            "karma_min": getattr(rule, "karma_min", 0),
            "notes": getattr(rule, "notes", None),
        }
        results.append(row)

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
