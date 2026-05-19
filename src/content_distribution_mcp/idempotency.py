"""
Idempotency helpers and retry policy for Content Distribution MCP.

Cross-cutting concerns:
- Canonical idempotency key derivation (content_id, channel) → str
- Transient vs permanent error classification
- Exponential-backoff retry wrapper
- Per-channel retry limits (RetryPolicy)
- Partial-run recovery (recover_partial_run)

Python 3.11+. All public functions are synchronous unless noted.
No LLM calls. No direct I/O — all state flows through StateBackend.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends.base import StateBackend  # type: ignore[import]
    from .adapters.base import ChannelAdapter  # type: ignore[import]

from .models import Content, PublishResult, Variant  # type: ignore[import]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transient error signals
# ---------------------------------------------------------------------------

_TRANSIENT_SIGNALS: tuple[str, ...] = (
    "rate-limit",
    "429",
    "timeout",
    "connection-reset",
    "5xx",
    "503",
    "502",
    "network",
)

_PERMANENT_SIGNALS: tuple[str, ...] = (
    "401",
    "403",
    "validation",
    "unauthorized",
    "forbidden",
    "flair-required",
    "cooldown",
    "cap-reached",
    "self-promo-ratio",
    "automod_removed",
    "global_daily_cap_reached",
    "subreddit_cooldown",
    "self_promo_ratio_exceeded",
)


# ---------------------------------------------------------------------------
# make_idempotency_key
# ---------------------------------------------------------------------------


def make_idempotency_key(content_id: str, channel: str) -> str:
    """Return the canonical idempotency key for a (content_id, channel) pair.

    Format: ``"<content_id>::<channel>"``.

    This key is used by every adapter and backend to guard against duplicate
    publishes. Centralising the format here ensures a single source of truth.

    Parameters
    ----------
    content_id : str
        The ``Content.id`` value — a stable, caller-supplied identifier
        (e.g. ``"n8n-webhook-setup@2026-05-18"``).
    channel : str
        The ``Variant.channel`` value in ``<platform>:<sub>`` format
        (e.g. ``"reddit:LocalLLaMA"``). Reddit variants include the subreddit
        name so each sub gets its own idempotency key.

    Returns
    -------
    str
        Canonical key string of the form ``"<content_id>::<channel>"``.
    """
    return f"{content_id}::{channel}"


# ---------------------------------------------------------------------------
# should_retry
# ---------------------------------------------------------------------------


def should_retry(
    error: str,
    attempt: int,
    max_attempts: int = 3,
) -> tuple[bool, float]:
    """Classify an error as transient or permanent and compute the backoff delay.

    Transient errors are retried with exponential backoff. Permanent errors
    skip retries and transition the entry to ``state=failed`` immediately.

    Transient signals (case-insensitive substring match):
        ``rate-limit``, ``429``, ``timeout``, ``connection-reset``,
        ``5xx``, ``503``, ``502``, ``network``

    Permanent signals (case-insensitive substring match):
        ``401``, ``403``, ``validation``, ``unauthorized``, ``forbidden``,
        ``flair-required``, ``cooldown``, ``cap-reached``,
        ``self-promo-ratio``, ``automod_removed``,
        ``global_daily_cap_reached``, ``subreddit_cooldown``,
        ``self_promo_ratio_exceeded``

    Any error string that does not match a permanent signal and hasn't exceeded
    max_attempts is treated as transient (safe default: assume retriable).

    Parameters
    ----------
    error : str
        Error string from the adapter's ``PublishResult.error`` or exception
        message.
    attempt : int
        The attempt number that just failed (1-based). Used to compute the
        next backoff window.
    max_attempts : int
        Maximum number of total attempts allowed. Default: 3.

    Returns
    -------
    tuple[bool, float]
        ``(should_retry, sleep_seconds)`` where:

        - ``should_retry`` is ``True`` if another attempt should be made.
        - ``sleep_seconds`` is the recommended delay before the next attempt,
          computed as ``min(60, 2 ** attempt)`` seconds. Zero when
          ``should_retry`` is ``False``.
    """
    if attempt >= max_attempts:
        return False, 0.0

    error_lower = error.lower()

    # Permanent signals short-circuit immediately.
    for signal in _PERMANENT_SIGNALS:
        if signal in error_lower:
            logger.debug("error classified as permanent: %r", error)
            return False, 0.0

    # Transient signals → exponential backoff.
    sleep_sec = float(min(60, 2 ** attempt))

    for signal in _TRANSIENT_SIGNALS:
        if signal in error_lower:
            logger.debug(
                "error classified as transient (attempt=%d, sleep=%.0fs): %r",
                attempt,
                sleep_sec,
                error,
            )
            return True, sleep_sec

    # Unrecognised error — treat as transient (conservative default).
    logger.debug(
        "error unrecognised, defaulting to transient (attempt=%d, sleep=%.0fs): %r",
        attempt,
        sleep_sec,
        error,
    )
    return True, sleep_sec


# ---------------------------------------------------------------------------
# retry_publish
# ---------------------------------------------------------------------------


async def retry_publish(
    adapter: "ChannelAdapter",
    variant: Variant,
    profile: object,  # Profile — typed as object to avoid hard import cycle
    state_backend: "StateBackend",
    max_attempts: int = 3,
) -> PublishResult:
    """Wrap ``adapter.publish()`` with retry semantics.

    On each attempt:
    - ``state=live``, ``state=queued``, or ``state=needs_browser`` → return
      immediately (terminal or operator-action state).
    - ``state=failed`` → call ``should_retry(result.error, attempt, max_attempts)``.
      - Transient: sleep the backoff interval, then retry.
      - Permanent: return the failed result immediately.

    Exceptions raised by the adapter are caught and treated as transient
    errors (they are converted to a ``PublishResult(state="failed")``) so
    the retry loop can handle them uniformly.

    Parameters
    ----------
    adapter : ChannelAdapter
        The channel adapter to invoke.
    variant : Variant
        The variant being published.
    profile : Profile
        Distribution profile carrying credentials.
    state_backend : StateBackend
        Persistence backend passed through to the adapter.
    max_attempts : int
        Maximum total attempts. Default: 3.

    Returns
    -------
    PublishResult
        The final result after all retries are exhausted or a terminal state
        is reached.
    """
    attempt = 0
    result: PublishResult | None = None

    while attempt < max_attempts:
        attempt += 1
        logger.info(
            "retry_publish: %s attempt %d/%d", variant.channel, attempt, max_attempts
        )

        try:
            result = await adapter.publish(variant, profile, state_backend)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            error_msg = str(exc)
            logger.warning(
                "retry_publish: adapter raised exception on attempt %d: %s",
                attempt,
                error_msg,
            )
            result = PublishResult(
                channel=variant.channel,
                state="failed",
                error=error_msg,
            )

        # Terminal / non-failed states → return immediately.
        if result.state in ("live", "queued", "needs_browser"):
            return result

        # state == "failed" — classify and decide whether to retry.
        error_str = result.error or "unknown-error"
        do_retry, sleep_sec = should_retry(error_str, attempt, max_attempts)

        if not do_retry:
            logger.info(
                "retry_publish: permanent failure on %s after %d attempt(s): %s",
                variant.channel,
                attempt,
                error_str,
            )
            return result

        logger.info(
            "retry_publish: transient failure on %s (attempt %d/%d), sleeping %.0fs",
            variant.channel,
            attempt,
            max_attempts,
            sleep_sec,
        )
        await asyncio.sleep(sleep_sec)

    # Exhausted all attempts.
    assert result is not None  # guaranteed: loop ran at least once
    logger.warning(
        "retry_publish: max attempts (%d) exhausted for %s: %s",
        max_attempts,
        variant.channel,
        result.error,
    )
    return result


# ---------------------------------------------------------------------------
# RetryPolicy
# ---------------------------------------------------------------------------


class RetryPolicy:
    """Per-channel configurable retry limits.

    Different channels have different failure modes:

    - **Reddit** defaults to 1 retry because most Reddit failures are
      gate-related (subreddit rules, account age, AutoMod) and are permanent.
      Retrying wastes time and risks further account signals.
    - **DEV.to / Hashnode / LinkedIn / GitHub Discussions** default to 3
      retries because transient 5xx and network failures are the common case.
    - The ``"default"`` key applies to any channel not explicitly listed.

    Parameters
    ----------
    limits : dict[str, int] | None
        Map of channel prefix → max attempts. Overrides the built-in defaults
        when supplied. ``"default"`` sets the fallback for unlisted channels.

    Examples
    --------
    >>> policy = RetryPolicy()
    >>> policy.max_attempts_for("reddit:LocalLLaMA")
    1
    >>> policy.max_attempts_for("devto:main")
    3
    >>> policy = RetryPolicy({"devto": 5, "default": 2})
    >>> policy.max_attempts_for("devto:main")
    5
    >>> policy.max_attempts_for("linkedin:personal")
    2
    """

    _BUILTIN_LIMITS: dict[str, int] = {
        "reddit": 1,       # Most reddit failures are permanent gate errors.
        "devto": 3,
        "hashnode": 3,
        "linkedin": 3,
        "github_discussions": 3,
        "github-discussions": 3,
        "medium_browser": 1,   # Always needs_browser; retrying is pointless.
        "medium-browser": 1,
        "default": 3,
    }

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self._limits = dict(self._BUILTIN_LIMITS)
        if limits:
            self._limits.update(limits)

    def max_attempts_for(self, channel: str) -> int:
        """Return the max attempt count for *channel*.

        Looks up the channel prefix (the part before the first ``":"``).
        Falls back to the ``"default"`` key if no specific limit is set.

        Parameters
        ----------
        channel : str
            Full channel identifier in ``<platform>:<sub>`` format.

        Returns
        -------
        int
            Maximum number of publish attempts for this channel.
        """
        prefix = channel.split(":")[0]
        return self._limits.get(prefix, self._limits.get("default", 3))

    async def wrap(
        self,
        adapter: "ChannelAdapter",
        variant: Variant,
        profile: object,
        state_backend: "StateBackend",
    ) -> PublishResult:
        """Apply channel-specific retry policy to an adapter publish call.

        Convenience method that reads ``max_attempts_for(variant.channel)``
        and delegates to ``retry_publish``.

        Parameters
        ----------
        adapter : ChannelAdapter
            The channel adapter to invoke.
        variant : Variant
            The variant being published.
        profile : Profile
            Distribution profile carrying credentials.
        state_backend : StateBackend
            Persistence backend.

        Returns
        -------
        PublishResult
            The final result after retries are exhausted or a terminal state
            is reached.
        """
        max_attempts = self.max_attempts_for(variant.channel)
        return await retry_publish(adapter, variant, profile, state_backend, max_attempts)


# ---------------------------------------------------------------------------
# recover_partial_run
# ---------------------------------------------------------------------------


async def recover_partial_run(
    content_id: str,
    state_backend: "StateBackend",
    adapters: "dict[str, ChannelAdapter]",
    profile: object,  # Profile
    retry_policy: RetryPolicy | None = None,
) -> list[PublishResult]:
    """Re-fire failed or stuck variants for a content piece.

    Queries the post log for ``content_id`` rows with ``state in
    {"failed", "claiming"}`` (``claiming`` = stuck mid-publish from a
    previous crashed run). For each matching entry, rebuilds the
    ``Variant`` from the snapshot stored in the post log, then re-fires
    it through ``retry_policy.wrap()``.

    This function is the recovery path called by the ``recover`` CLI command.
    See ``cli.py`` for the entry point — this function should be called as:

    .. code-block:: python

        # In cli.py: recover command entry point (TODO: wire this up)
        results = asyncio.run(
            recover_partial_run(content_id, state_backend, adapters, profile)
        )

    Parameters
    ----------
    content_id : str
        The ``Content.id`` value to recover.
    state_backend : StateBackend
        Persistence backend for log queries and state updates.
    adapters : dict[str, ChannelAdapter]
        Map of adapter prefix to ``ChannelAdapter`` instance — same map
        used by the MCP server at startup.
    profile : Profile
        Distribution profile carrying credentials. Must be the same profile
        used in the original publish run.
    retry_policy : RetryPolicy | None
        Policy controlling per-channel attempt limits. Defaults to a new
        ``RetryPolicy()`` with built-in limits when ``None``.

    Returns
    -------
    list[PublishResult]
        Results for each recovered variant. Empty list if there are no
        failed or stuck entries for ``content_id``.

    Notes
    -----
    - Variants with ``state=live`` are skipped (already succeeded).
    - A ``Variant`` snapshot must be present in the ``PublishResult`` stored
      by the backend. If a result has no recoverable variant snapshot the
      entry is skipped with a warning.
      # TODO: extend PublishResult to carry a ``variant_snapshot`` field once
      # backends are implemented (AL-402 follow-up).
    """
    from .backends.base import PostLogFilter  # type: ignore[import]

    policy = retry_policy or RetryPolicy()

    recoverable_states = {"failed", "claiming"}

    # Query the post log for this content's failed/stuck entries.
    all_entries: list[PublishResult] = state_backend.query_post_log(  # type: ignore[union-attr]
        PostLogFilter(source_task_id=None)
    )

    # Filter to this content_id and recoverable states.
    # NOTE: PostLogFilter doesn't yet carry content_id directly; we filter
    # in-process until the backend adds a content_id filter.
    candidates = [
        r for r in all_entries
        if getattr(r, "content_id", None) == content_id
        and r.state in recoverable_states
    ]

    if not candidates:
        logger.info(
            "recover_partial_run: no recoverable entries for content_id=%r", content_id
        )
        return []

    logger.info(
        "recover_partial_run: found %d recoverable entries for content_id=%r",
        len(candidates),
        content_id,
    )

    results: list[PublishResult] = []

    for entry in candidates:
        # The variant snapshot must be stored alongside the result.
        variant_snapshot: Variant | None = getattr(entry, "variant_snapshot", None)
        if variant_snapshot is None:
            logger.warning(
                "recover_partial_run: no variant_snapshot on entry channel=%r — skipping",
                entry.channel,
            )
            continue

        adapter_key = variant_snapshot.channel.split(":")[0]
        adapter = adapters.get(adapter_key)
        if adapter is None:
            logger.warning(
                "recover_partial_run: no adapter for channel=%r — skipping",
                entry.channel,
            )
            results.append(
                PublishResult(
                    channel=entry.channel,
                    state="failed",
                    error=f"no-adapter-for-channel: {entry.channel}",
                )
            )
            continue

        logger.info(
            "recover_partial_run: re-firing %r (previous state=%r)",
            entry.channel,
            entry.state,
        )
        result = await policy.wrap(adapter, variant_snapshot, profile, state_backend)
        results.append(result)

    return results
