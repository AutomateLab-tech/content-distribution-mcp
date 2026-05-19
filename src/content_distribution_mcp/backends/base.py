"""
StateBackend Protocol and supporting models for Content Distribution MCP.

The ``StateBackend`` is the single persistence abstraction used by the MCP
runtime. All state — channel profiles, idempotency keys, the post log,
scheduled variants, Reddit cooldown records — flows through this interface.

Two concrete implementations are planned:

- ``YAMLBackend`` (local file, default for open-source users)
- ``NotionBackend`` (reads/writes to the agency-os Tasks + post-log databases)

# TODO: implement YAMLBackend in backends/yaml_backend.py
# TODO: implement NotionBackend in backends/notion_backend.py

Python 3.11+. All models use ``extra="forbid"``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import AnyHttpUrl, BaseModel, ConfigDict

from ..models import PublishResult, Variant


# ---------------------------------------------------------------------------
# Supporting models
# ---------------------------------------------------------------------------


class ChannelConfig(BaseModel):
    """Per-channel configuration stored inside a ``Profile``.

    Adapters receive a ``ChannelConfig`` alongside platform credentials.
    The ``enabled`` flag lets operators disable a channel without removing it.

    Fields
    ------
    channel : str
        Channel identifier in ``<platform>:<sub>`` format (mirrors
        ``Variant.channel``).
    enabled : bool
        If ``False``, the MCP runtime skips this channel during distribution
        without raising an error.
    defaults : dict[str, Any]
        Default ``extras`` values merged with ``Variant.extras`` at publish
        time. Adapter-specific (e.g. ``{"flair": "Discussion"}`` for Reddit).
    """

    model_config = ConfigDict(extra="forbid")

    channel: str
    enabled: bool = True
    defaults: dict[str, Any] = {}


class Profile(BaseModel):
    """Named distribution profile — a reusable set of target channels.

    Operators define profiles like ``developer``, ``social``, ``full`` so they
    can call ``publish_to_profile("developer")`` instead of enumerating channels
    each time.

    Fields
    ------
    name : str
        Unique profile identifier (e.g. ``"developer"``, ``"social"``).
    channels : list[ChannelConfig]
        Ordered list of channel configurations included in this profile.
    description : str | None
        Human-readable description shown by the ``list_profiles`` MCP tool.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    channels: list[ChannelConfig] = []
    description: str | None = None


class PostLogFilter(BaseModel):
    """Filter criteria for :meth:`StateBackend.query_post_log`.

    All fields are optional; omitting a field means "no filter on that
    dimension." Multiple non-None fields are combined with AND semantics.

    Fields
    ------
    source_task_id : str | None
        Filter by the agency-os task that commissioned the content
        (e.g. ``"AL-312"``).
    channel : str | None
        Filter by channel in ``<platform>:<sub>`` format.
    state : str | None
        Filter by ``PublishResult.state`` (``"live"``, ``"queued"``,
        ``"needs_browser"``, or ``"failed"``).
    since : datetime | None
        Include only results where ``published_at >= since`` (UTC).
    until : datetime | None
        Include only results where ``published_at <= until`` (UTC).
    limit : int | None
        Maximum number of results to return. If ``None``, return all matches.
        Backends should default to a sensible cap (e.g. 200) when ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    source_task_id: str | None = None
    channel: str | None = None
    state: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int | None = None


class SubredditRules(BaseModel):
    """Per-subreddit rules enforced by the Reddit adapter before posting.

    Populated by ``StateBackend.load_subreddit_rules()`` from the subreddit
    catalog (a YAML or Notion table maintained by the operator).

    Fields
    ------
    subreddit : str
        Subreddit name without the ``r/`` prefix (e.g. ``"LocalLLaMA"``).
    min_account_age_days : int
        Minimum account age in days required to post. Common value: 30.
    min_comment_karma : int
        Minimum comment karma required to post. Common value: 100.
    self_promo_allowed : bool
        ``False`` if the subreddit bans self-promotional posts. When ``False``
        the adapter raises ``SelfPromoNotAllowedError`` before posting.
    required_flair : str | None
        If set, the adapter must include this flair. If ``None``, flair is
        optional or not available.
    cooldown_hours : int
        Minimum hours between successive posts to this subreddit from the same
        account. The adapter consults ``StateBackend.record_reddit_post()``
        history to enforce this.
    notes : str | None
        Free-text operator notes about this subreddit (moderation quirks,
        preferred post format, link vs text preference, etc.).
    """

    model_config = ConfigDict(extra="forbid")

    subreddit: str
    min_account_age_days: int = 0
    min_comment_karma: int = 0
    self_promo_allowed: bool = True
    required_flair: str | None = None
    cooldown_hours: int = 0
    notes: str | None = None


# ---------------------------------------------------------------------------
# StateBackend Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StateBackend(Protocol):
    """Persistence abstraction for the Content Distribution MCP runtime.

    All methods are synchronous. Async backends should expose a synchronous
    wrapper (e.g. ``asyncio.run()``) or provide an ``AsyncStateBackend``
    subprotocol in a separate module.

    # TODO: define AsyncStateBackend Protocol in backends/async_base.py once
    # the Notion backend implementation is underway.

    Error contract
    --------------
    - ``KeyError`` is raised when a requested record does not exist and the
      caller did not provide a default (e.g. ``load_profile`` for an unknown
      profile name).
    - ``ValueError`` is raised for invalid inputs (e.g. malformed channel
      string, ``schedule_at`` in the past for ``enqueue_scheduled``).
    - Backend-specific I/O errors propagate as-is and are NOT wrapped.
    """

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def load_profile(self, name: str) -> Profile:
        """Load a named distribution profile.

        Parameters
        ----------
        name : str
            The profile name to load (e.g. ``"developer"``).

        Returns
        -------
        Profile
            The fully-populated profile including all ``ChannelConfig`` entries.

        Raises
        ------
        KeyError
            If no profile with ``name`` exists in the backend store.
        """
        ...

    def save_profile(self, profile: Profile) -> None:
        """Persist a profile, creating it if it does not exist or overwriting
        the existing record if it does.

        This is an upsert — callers do not need to check existence first.

        Parameters
        ----------
        profile : Profile
            The profile to persist. ``profile.name`` is the primary key.
        """
        ...

    # ------------------------------------------------------------------
    # Idempotency and post log
    # ------------------------------------------------------------------

    def claim_idempotency_key(self, content_id: str, channel: str) -> bool:
        """Atomically claim an idempotency key for a (content_id, channel) pair.

        This is the primary guard against duplicate publishes. The MCP runtime
        calls this before invoking an adapter. If the key is already claimed
        (a previous run succeeded or is in progress), the runtime short-circuits
        and returns the existing ``PublishResult`` from ``lookup_published``
        instead of calling the adapter again.

        Atomicity guarantee: in a concurrent scenario where two processes race
        to claim the same key, exactly one must return ``True``. YAML backends
        can approximate this with a file lock; Notion backends use optimistic
        concurrency on row creation.

        Parameters
        ----------
        content_id : str
            The ``Content.id`` value (e.g. ``"n8n-webhook-setup@2026-05-18"``).
        channel : str
            The target channel in ``<platform>:<sub>`` format.

        Returns
        -------
        bool
            ``True`` if this call successfully claimed the key (first time seen).
            ``False`` if the key was already claimed by a previous call.
        """
        ...

    def lookup_published(self, content_id: str, channel: str) -> PublishResult | None:
        """Look up a previously-stored publish result.

        Called by the MCP runtime when ``claim_idempotency_key`` returns
        ``False``, to retrieve the existing result without re-publishing.

        Parameters
        ----------
        content_id : str
            The ``Content.id`` value.
        channel : str
            The target channel in ``<platform>:<sub>`` format.

        Returns
        -------
        PublishResult | None
            The stored result, or ``None`` if no result has been recorded yet
            (e.g. a key was claimed but ``mark_published`` was never called,
            indicating a crashed run).
        """
        ...

    def mark_published(self, result: PublishResult) -> None:
        """Store a ``PublishResult`` in the post log.

        Called by the MCP runtime after each adapter invocation, regardless of
        ``state``. The backend must store the result so it is retrievable via
        ``lookup_published`` and ``query_post_log``.

        If a result for the same ``(channel, content_id)`` already exists (e.g.
        a retry after a failed attempt), the backend overwrites the existing
        record.

        Parameters
        ----------
        result : PublishResult
            The result to persist. ``result.channel`` and the content_id
            embedded in the idempotency key serve as the composite primary key.

        Note
        ----
        Backends must store the ``source_task_id`` from the associated
        ``Content`` record so that ``query_post_log`` can filter by task.
        The caller is responsible for passing a result that includes this
        context; the runtime sets it before calling ``mark_published``.

        # TODO: extend PublishResult with content_id and source_task_id fields
        # once the runtime wiring is implemented (AL-403 or equivalent).
        """
        ...

    def query_post_log(self, filter: PostLogFilter) -> list[PublishResult]:
        """Query the post log with optional filters.

        Parameters
        ----------
        filter : PostLogFilter
            Filter criteria. All fields are optional; omitting them returns all
            records up to ``filter.limit`` (or the backend's default cap).

        Returns
        -------
        list[PublishResult]
            Matching results ordered by ``published_at`` descending (most recent
            first). Returns an empty list when no records match.
        """
        ...

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def enqueue_scheduled(self, variant: Variant, schedule_at: datetime) -> str:
        """Enqueue a variant for future publishing.

        The MCP scheduler calls ``drain_scheduled`` on a periodic tick (e.g.
        every minute) and invokes adapters for variants whose ``schedule_at``
        has passed.

        Parameters
        ----------
        variant : Variant
            The variant to schedule. The variant's ``schedule_at`` field is
            ignored in favour of the explicit ``schedule_at`` parameter so that
            callers can reschedule without mutating the variant.
        schedule_at : datetime
            UTC datetime at which the variant should be published. Must be in
            the future; backends should raise ``ValueError`` if ``schedule_at``
            is in the past.

        Returns
        -------
        str
            A stable ``scheduled_id`` (opaque string) that uniquely identifies
            this scheduled entry. Can be used in future tooling to cancel or
            inspect the scheduled item.
        """
        ...

    def drain_scheduled(self, now: datetime) -> list[Variant]:
        """Retrieve and remove all variants scheduled for ``now`` or earlier.

        Called by the MCP scheduler on each tick. The backend must atomically
        remove returned variants from the queue so they are not returned by
        subsequent calls (i.e. drain is destructive).

        Parameters
        ----------
        now : datetime
            UTC reference time. All variants with ``schedule_at <= now`` are
            returned.

        Returns
        -------
        list[Variant]
            Variants ready for publishing, ordered by ``schedule_at`` ascending
            (oldest first). Returns an empty list when nothing is due.
        """
        ...

    # ------------------------------------------------------------------
    # Reddit-specific state
    # ------------------------------------------------------------------

    def load_subreddit_rules(self, subreddit: str) -> SubredditRules:
        """Load the operator-maintained rules for a subreddit.

        The subreddit catalog is a YAML file or Notion table edited by the
        operator. Rules include minimum karma/age requirements, cooldown
        periods, and self-promo policy.

        Parameters
        ----------
        subreddit : str
            Subreddit name without the ``r/`` prefix (e.g. ``"LocalLLaMA"``).

        Returns
        -------
        SubredditRules
            The rules record for this subreddit.

        Raises
        ------
        KeyError
            If the subreddit is not in the catalog. The Reddit adapter should
            treat an unknown subreddit as unconfigured and either refuse to
            post or use safe defaults — this decision belongs to the adapter,
            not the backend.
        """
        ...

    def record_reddit_post(self, subreddit: str, posted_at: datetime) -> None:
        """Record a successful post to a subreddit for cooldown and cap tracking.

        The Reddit adapter calls this immediately after a post goes live. The
        backend stores the timestamp so that:

        1. **5-post/day global cap**: the adapter can call
           ``query_post_log(PostLogFilter(channel="reddit:*", since=today_start))``
           and count results, but backends may also expose a fast-path counter.
        2. **Per-subreddit cooldown**: on the next post attempt to ``subreddit``,
           the adapter computes ``now - last_posted_at`` and compares against
           ``SubredditRules.cooldown_hours``.

        Parameters
        ----------
        subreddit : str
            Subreddit name without the ``r/`` prefix.
        posted_at : datetime
            UTC timestamp of the successful post (typically ``datetime.utcnow()``
            at time of API confirmation).
        """
        ...
