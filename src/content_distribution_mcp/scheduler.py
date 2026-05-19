"""
Scheduler core for Content Distribution MCP.

Provides:
- ``publish_immediate`` — fan-out publish across channel adapters in parallel.
- ``schedule``          — enqueue future variants, fire immediate ones now.
- ``drain``             — process due scheduled variants from the backend queue.
- ``worker_loop``       — background polling loop for in-process drain mode.
- ``parse_schedule_at`` — parse ISO-8601 strings with local-TZ defaulting.

Python 3.11+. All public async functions are safe to call from an asyncio
event loop. No LLM calls. No direct I/O — all state flows through
``StateBackend``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # These imports resolve at runtime once the package is fully assembled.
    # TODO: remove TYPE_CHECKING guard when all sibling modules exist.
    from .backends.base import StateBackend  # type: ignore[import]
    from .adapters.base import ChannelAdapter  # type: ignore[import]

from .models import Content, PublishResult, Variant  # type: ignore[import]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channel prefix → adapter key mapping
# ---------------------------------------------------------------------------

# The channel field uses compound format "<platform>:<sub>".  We need only the
# platform prefix to look up the correct adapter.
def _adapter_key(channel: str) -> str:
    """Return the adapter lookup key from a compound channel string.

    Examples
    --------
    ``"devto:main"``                          → ``"devto"``
    ``"reddit:LocalLLaMA"``                   → ``"reddit"``
    ``"github-discussions:owner/repo"``       → ``"github-discussions"``
    ``"medium-browser:main"``                 → ``"medium-browser"``
    ``"linkedin:personal"``                   → ``"linkedin"``
    """
    return channel.split(":")[0]


# ---------------------------------------------------------------------------
# publish_immediate
# ---------------------------------------------------------------------------


async def publish_immediate(
    content: Content,
    variants: list[Variant],
    profile: object,  # Profile — typed as object to avoid hard import cycle
    adapters: dict[str, object],  # dict[str, ChannelAdapter]
    state_backend: object,  # StateBackend
) -> list[PublishResult]:
    """Publish all variants immediately in parallel.

    For each variant:
    1. Look up the adapter by channel prefix.
    2. Run ``adapter.can_publish(variant)`` as a pre-flight check.
    3. If everything passes, call ``adapter.publish(variant, profile, state_backend)``.

    All adapter calls are fanned out concurrently via ``asyncio.gather``.
    An exception from any adapter is caught and returned as a failed
    ``PublishResult`` — it does not abort sibling publishes.

    Parameters
    ----------
    content:
        The canonical content record (used for idempotency key and context).
    variants:
        One or more channel-specific variants to publish.
    profile:
        Distribution profile carrying per-channel credentials.
    adapters:
        Map of adapter key (e.g. ``"devto"``, ``"reddit"``) to
        ``ChannelAdapter`` instance.
    state_backend:
        Persistence backend for idempotency and post-log writes.

    Returns
    -------
    list[PublishResult]
        One result per input variant, in the same order.
    """

    async def _publish_one(variant: Variant) -> PublishResult:
        key = _adapter_key(variant.channel)
        adapter = adapters.get(key)

        if adapter is None:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error=f"no-adapter-for-channel: {variant.channel}",
            )

        ok, reason = adapter.can_publish(variant)  # type: ignore[union-attr]
        if not ok:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error=f"adapter-rejected: {reason}",
            )

        return await adapter.publish(variant, profile, state_backend)  # type: ignore[union-attr]

    tasks = [_publish_one(v) for v in variants]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[PublishResult] = []
    for variant, raw in zip(variants, raw_results):
        if isinstance(raw, BaseException):
            results.append(
                PublishResult(
                    channel=variant.channel,
                    state="failed",
                    error=str(raw),
                )
            )
        else:
            results.append(raw)  # type: ignore[arg-type]

    return results


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------


async def schedule(
    content: Content,
    variants: list[Variant],
    profile: object,  # Profile
    adapters: dict[str, object],  # dict[str, ChannelAdapter]
    state_backend: object,  # StateBackend
) -> dict[str, PublishResult | str]:
    """Enqueue scheduled variants and immediately publish unscheduled ones.

    Variants with ``schedule_at`` set are enqueued via
    ``state_backend.enqueue_scheduled(variant, schedule_at)`` and the returned
    ``scheduled_id`` is stored in the result dict.

    Variants without ``schedule_at`` are passed to ``publish_immediate``
    and their ``PublishResult`` is stored directly.

    Parameters
    ----------
    content:
        Canonical content record.
    variants:
        Mixed list — some may have ``schedule_at``, others may not.
    profile:
        Distribution profile.
    adapters:
        Map of adapter key to ``ChannelAdapter`` instance.
    state_backend:
        Persistence backend.

    Returns
    -------
    dict[str, PublishResult | str]
        Maps channel → ``PublishResult`` (for immediate publishes) or
        ``scheduled_id`` string (for enqueued variants).
    """
    immediate: list[Variant] = []
    scheduled: list[Variant] = []

    for v in variants:
        if v.schedule_at is not None:
            scheduled.append(v)
        else:
            immediate.append(v)

    result: dict[str, PublishResult | str] = {}

    # Enqueue scheduled variants.
    # YamlBackend.enqueue_scheduled takes a single dict; serialise the Variant
    # via model_dump(mode="json") so datetimes become ISO strings on disk.
    for v in scheduled:
        scheduled_id: str = state_backend.enqueue_scheduled(  # type: ignore[union-attr]
            v.model_dump(mode="json")
        )
        result[v.channel] = scheduled_id

    # Fire immediate variants in parallel.
    if immediate:
        immediate_results = await publish_immediate(
            content, immediate, profile, adapters, state_backend
        )
        for r in immediate_results:
            result[r.channel] = r

    return result


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------


async def drain(
    adapters: dict[str, object],  # dict[str, ChannelAdapter]
    state_backend: object,  # StateBackend
    now: datetime | None = None,
) -> list[PublishResult]:
    """Fire all scheduled posts that are due at or before *now*.

    Calls ``state_backend.drain_scheduled(now)`` to atomically retrieve and
    dequeue due ``Variant`` entries, then publishes each one via the
    appropriate adapter.  Profile is retrieved from the state backend using
    the channel default profile — adapters that need a profile must store
    the profile name alongside the variant at enqueue time.

    Parameters
    ----------
    adapters:
        Map of adapter key to ``ChannelAdapter`` instance.
    state_backend:
        Persistence backend.
    now:
        Reference time for the drain window.  Defaults to
        ``datetime.now(timezone.utc)``.

    Returns
    -------
    list[PublishResult]
        Results for every variant that was due and processed.
        Empty list if nothing was due.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # YamlBackend.drain_scheduled takes no args and decides "due" against its
    # own clock. It returns dicts (the shape originally passed to
    # enqueue_scheduled, plus a scheduled_id key we drop before rebuilding the
    # Variant pydantic model).
    due_dicts: list[dict] = state_backend.drain_scheduled()  # type: ignore[union-attr]
    due_variants: list[Variant] = []
    for d in due_dicts:
        d = {k: val for k, val in d.items() if k != "scheduled_id"}
        try:
            due_variants.append(Variant.model_validate(d))
        except Exception as exc:  # noqa: BLE001
            logger.warning("drain: skipping un-rehydratable variant %r: %s", d, exc)

    if not due_variants:
        return []

    async def _fire_one(variant: Variant) -> PublishResult:
        key = _adapter_key(variant.channel)
        adapter = adapters.get(key)
        if adapter is None:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error=f"no-adapter-for-channel: {variant.channel}",
            )
        # Drain does not have a Content object — adapters must be idempotent
        # on re-delivery and must not rely on Content for drain publishes.
        # Profile is loaded by the adapter from the state_backend if needed.
        return await adapter.publish(variant, None, state_backend)  # type: ignore[union-attr]

    raw_results = await asyncio.gather(
        *[_fire_one(v) for v in due_variants],
        return_exceptions=True,
    )

    results: list[PublishResult] = []
    for variant, raw in zip(due_variants, raw_results):
        if isinstance(raw, BaseException):
            results.append(
                PublishResult(
                    channel=variant.channel,
                    state="failed",
                    error=str(raw),
                )
            )
        else:
            results.append(raw)  # type: ignore[arg-type]

    return results


# ---------------------------------------------------------------------------
# worker_loop
# ---------------------------------------------------------------------------


async def worker_loop(
    adapters: dict[str, object],  # dict[str, ChannelAdapter]
    state_backend: object,  # StateBackend
    poll_interval_sec: int = 60,
) -> None:
    """Run a perpetual drain loop, polling every *poll_interval_sec* seconds.

    Designed for in-process background use (e.g. started as an asyncio task
    inside the FastMCP server's lifespan).  The loop never terminates
    voluntarily — cancel it via the asyncio task handle.

    Exceptions thrown by ``drain`` are logged per-iteration and do NOT crash
    the loop; the next poll attempt proceeds after the normal sleep interval.

    Parameters
    ----------
    adapters:
        Map of adapter key to ``ChannelAdapter`` instance.
    state_backend:
        Persistence backend.
    poll_interval_sec:
        Seconds to sleep between drain calls.  Defaults to 60.
    """
    logger.info(
        "worker_loop started (poll_interval=%ss)", poll_interval_sec
    )
    while True:
        try:
            results = await drain(adapters, state_backend)
            if results:
                for r in results:
                    if r.state == "live":
                        logger.info("drain: published %s → %s", r.channel, r.live_url)
                    else:
                        logger.warning(
                            "drain: failed %s — %s", r.channel, r.error
                        )
        except Exception:  # noqa: BLE001
            logger.exception("worker_loop: unhandled exception in drain tick")
        await asyncio.sleep(poll_interval_sec)


# ---------------------------------------------------------------------------
# parse_schedule_at — local-TZ default helper
# ---------------------------------------------------------------------------


def parse_schedule_at(s: str, tz: str | None = None) -> datetime:
    """Parse an ISO-8601 datetime string, defaulting naive values to local TZ.

    The MCP spec (Section 11) keeps ``schedule_at`` strict (timezone-aware
    ISO-8601), but operators often supply naive strings from shell scripts or
    Notion date fields.  This helper bridges the gap at the CLI / skill layer
    before values enter the MCP.

    Algorithm
    ---------
    1. Parse *s* with ``datetime.fromisoformat``.
    2. If the result is timezone-aware: convert to UTC and return.
    3. If the result is timezone-naive:
       a. Determine the local offset from the *tz* parameter.
       b. If *tz* is None or unknown, fall back to ``time.tzname[0]``
          (the system's current timezone abbreviation).  If even that
          cannot be resolved, assume UTC.
       c. Attach the resolved offset and convert to UTC.

    Parameters
    ----------
    s:
        ISO-8601 datetime string, e.g.  ``"2026-05-20T09:00:00+09:00"`` or
        the naive ``"2026-05-20T09:00:00"``.
    tz:
        IANA timezone name (e.g. ``"Asia/Tokyo"``) or UTC offset string
        (e.g. ``"+09:00"``).  Takes precedence over the system timezone.
        Pass ``None`` to use the system's local timezone.

    Returns
    -------
    datetime
        Timezone-aware datetime in UTC.

    Raises
    ------
    ValueError
        If *s* cannot be parsed as ISO-8601.
    """
    dt = datetime.fromisoformat(s)

    if dt.tzinfo is not None:
        # Already timezone-aware — normalise to UTC.
        return dt.astimezone(timezone.utc)

    # Timezone-naive: resolve the local offset.
    offset = _resolve_tz_offset(tz)
    dt = dt.replace(tzinfo=offset)
    return dt.astimezone(timezone.utc)


def _resolve_tz_offset(tz: str | None) -> timezone:
    """Return a :class:`~datetime.timezone` for *tz*, falling back to local.

    Parameters
    ----------
    tz:
        IANA name, UTC-offset string (``"+09:00"``), or ``None``.

    Returns
    -------
    datetime.timezone
        A fixed-offset timezone.  Falls back to UTC if nothing can be resolved.
    """
    if tz is not None:
        # Try parsing a raw UTC-offset string like "+09:00" or "-05:30".
        try:
            # Reuse fromisoformat on a dummy date to parse the offset.
            probe = datetime.fromisoformat(f"2000-01-01T00:00:00{tz}")
            if probe.tzinfo is not None:
                return probe.tzinfo  # type: ignore[return-value]
        except ValueError:
            pass

        # Try ``zoneinfo`` (stdlib 3.9+) for IANA names.
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            zi = ZoneInfo(tz)
            # Get the current UTC offset for this zone.
            probe_dt = datetime.now(zi)
            return timezone(probe_dt.utcoffset())  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            logger.debug("parse_schedule_at: could not resolve tz=%r, falling back", tz)

    # Fall back to the system's current local UTC offset.
    local_offset_sec = -time.timezone if not time.daylight else -time.altzone
    return timezone(
        __import__("datetime").timedelta(seconds=local_offset_sec)
    )
