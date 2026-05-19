"""
YamlBackend — file-backed implementation of the StateBackend protocol.

Storage layout (all files live in ``~/.distribution-mcp/`` by default):

* ``profiles.yaml``   — dict[str, Profile]
* ``subreddits.yaml`` — dict[str, SubredditRules]
* ``post-log.yaml``   — list[PublishResult]  (append-only; idempotency source)
* ``pending.yaml``    — list[ScheduledVariant]  (drain queue)
* ``reddit-log.yaml`` — list[RedditPost]  (separate; used for 5/day cap)

All writes are atomic: write to ``<file>.tmp``, fsync, then rename.
File locking uses ``fcntl`` on POSIX; a TODO stub is left for Windows.

Python 3.11+.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # PyYAML

# ---------------------------------------------------------------------------
# Relative imports — these modules may not exist yet.
# TODO: remove the try/except once AL-402 (base.py) is merged.
# ---------------------------------------------------------------------------
try:
    from .base import StateBackend  # type: ignore[import]
except ImportError:  # pragma: no cover — base not yet scaffolded
    StateBackend = object  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# Locking helpers
# ---------------------------------------------------------------------------

if sys.platform != "win32":
    import fcntl

    def _lock(fh: "Any") -> None:
        """Acquire an exclusive advisory lock on *fh* (POSIX only)."""
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _unlock(fh: "Any") -> None:
        """Release the advisory lock on *fh* (POSIX only)."""
        fcntl.flock(fh, fcntl.LOCK_UN)

else:
    # TODO: implement proper file locking on Windows via msvcrt.locking or
    # a lock-file side-car strategy.  For now, no-ops are used so the class
    # works on Windows without crashing; concurrent writes are NOT safe.
    def _lock(fh: "Any") -> None:  # type: ignore[misc]
        pass

    def _unlock(fh: "Any") -> None:  # type: ignore[misc]
        pass


# ---------------------------------------------------------------------------
# YAML serialisation helpers
# ---------------------------------------------------------------------------

_DUMP_KWARGS: dict[str, Any] = {
    "default_flow_style": False,
    "sort_keys": False,
    "allow_unicode": True,
}


def _load(path: Path) -> Any:
    """Read *path* and return the deserialised YAML value (safe_load)."""
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_list(path: Path) -> list[dict[str, Any]]:
    """Read *path* and return a list, defaulting to [] if the file is empty."""
    with path.open("r", encoding="utf-8") as fh:
        result = yaml.safe_load(fh)
        if result is None:
            return []
        if isinstance(result, list):
            return result
        raise ValueError(f"Expected a YAML list in {path}; got {type(result).__name__}")


def _atomic_write(path: Path, data: Any) -> None:
    """Serialise *data* to YAML and write atomically to *path*.

    Algorithm:
    1. Write to ``<path>.tmp``.
    2. fsync the file handle.
    3. Rename (atomic on POSIX; best-effort on Windows — os.replace is as
       close to atomic as Windows allows without transactional NTFS).
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            _lock(fh)
            try:
                yaml.safe_dump(data, fh, **_DUMP_KWARGS)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                _unlock(fh)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up the temp file if something went wrong before the rename.
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# YamlBackend
# ---------------------------------------------------------------------------

class YamlBackend(StateBackend):
    """Concrete StateBackend that persists all state as YAML files.

    Parameters
    ----------
    base_dir:
        Root directory for all YAML files.  Defaults to
        ``~/.distribution-mcp``.  The directory (and empty YAML files) are
        created on first instantiation if they do not already exist.
    """

    _PROFILES_FILE = "profiles.yaml"
    _SUBREDDITS_FILE = "subreddits.yaml"
    _POST_LOG_FILE = "post-log.yaml"
    _PENDING_FILE = "pending.yaml"
    _REDDIT_LOG_FILE = "reddit-log.yaml"

    def __init__(
        self,
        base_dir: Path = Path.home() / ".distribution-mcp",
    ) -> None:
        self.base_dir = base_dir
        self._ensure_storage()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        """Return the current UTC datetime.

        Overridable in unit tests::

            backend._now = lambda: datetime(2024, 1, 1, tzinfo=timezone.utc)
        """
        return datetime.now(timezone.utc)

    def _path(self, filename: str) -> Path:
        """Return the full path for a storage file inside *base_dir*."""
        return self.base_dir / filename

    def _ensure_storage(self) -> None:
        """Create *base_dir* and initialise any missing YAML files."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        for filename, default in (
            (self._PROFILES_FILE, {}),
            (self._SUBREDDITS_FILE, {}),
            (self._POST_LOG_FILE, []),
            (self._PENDING_FILE, []),
            (self._REDDIT_LOG_FILE, []),
        ):
            p = self._path(filename)
            if not p.exists():
                _atomic_write(p, default)

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def save_profile(self, name: str, profile: dict[str, Any]) -> None:
        """Persist *profile* under the given *name* in ``profiles.yaml``.

        Parameters
        ----------
        name:
            Human-readable profile identifier (e.g. ``"dev-platforms"``).
        profile:
            Arbitrary profile data dict (channels, defaults, etc.).
        """
        p = self._path(self._PROFILES_FILE)
        profiles: dict[str, Any] = _load(p)
        profiles[name] = profile
        _atomic_write(p, profiles)

    def load_profile(self, name: str) -> dict[str, Any] | None:
        """Return the profile data for *name*, or ``None`` if not found.

        Parameters
        ----------
        name:
            Profile identifier to look up.
        """
        profiles: dict[str, Any] = _load(self._path(self._PROFILES_FILE))
        return profiles.get(name)

    def list_profiles(self) -> list[str]:
        """Return all known profile names in insertion order."""
        profiles: dict[str, Any] = _load(self._path(self._PROFILES_FILE))
        return list(profiles.keys())

    # ------------------------------------------------------------------
    # Subreddit rules
    # ------------------------------------------------------------------

    def save_subreddit_rules(
        self, subreddit: str, rules: dict[str, Any]
    ) -> None:
        """Persist subreddit *rules* for *subreddit* in ``subreddits.yaml``.

        Parameters
        ----------
        subreddit:
            Subreddit name without the ``r/`` prefix (e.g. ``"LocalLLaMA"``).
        rules:
            Dict conforming to the SubredditRules schema (cooldown_hours,
            require_flair, min_karma, etc.).
        """
        p = self._path(self._SUBREDDITS_FILE)
        subs: dict[str, Any] = _load(p)
        subs[subreddit] = rules
        _atomic_write(p, subs)

    def load_subreddit_rules(self, subreddit: str) -> dict[str, Any] | None:
        """Return cached rules for *subreddit*, or ``None`` if unknown.

        Parameters
        ----------
        subreddit:
            Subreddit name without the ``r/`` prefix.
        """
        subs: dict[str, Any] = _load(self._path(self._SUBREDDITS_FILE))
        return subs.get(subreddit)

    def list_subreddits(self) -> list[str]:
        """Return all subreddit names with saved rules."""
        subs: dict[str, Any] = _load(self._path(self._SUBREDDITS_FILE))
        return list(subs.keys())

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def claim_idempotency_key(
        self, content_id: str, channel: str
    ) -> bool:
        """Attempt to claim the ``(content_id, channel)`` idempotency slot.

        Scans ``post-log.yaml`` for an existing record with matching
        ``content_id`` **and** ``channel``.  If a record exists with
        ``state`` in ``{"live", "queued"}`` the slot is already taken and
        this method returns ``False`` — the caller must not re-publish.

        If no live/queued record exists, a stub entry with
        ``state="claiming"`` is appended and ``True`` is returned.  The
        caller is responsible for later updating state to ``"live"`` or
        ``"failed"`` via :meth:`mark_published`.

        Parameters
        ----------
        content_id:
            Stable content identifier (e.g. Ghost post slug or UUID).
        channel:
            Adapter / platform identifier (e.g. ``"devto"``, ``"reddit/LocalLLaMA"``).

        Returns
        -------
        bool
            ``True`` if the key was claimed (caller may proceed), ``False``
            if it was already live/queued (caller must skip).
        """
        p = self._path(self._POST_LOG_FILE)
        log: list[dict[str, Any]] = _load_list(p)

        for record in log:
            if (
                record.get("content_id") == content_id
                and record.get("channel") == channel
                and record.get("state") in {"live", "queued"}
            ):
                return False

        stub: dict[str, Any] = {
            "content_id": content_id,
            "channel": channel,
            "state": "claiming",
            "claimed_at": self._now().isoformat(),
            "published_url": None,
            "error": None,
        }
        log.append(stub)
        _atomic_write(p, log)
        return True

    def mark_published(
        self,
        content_id: str,
        channel: str,
        *,
        state: str = "live",
        published_url: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update the most recent ``claiming`` stub for ``(content_id, channel)``.

        Finds the last record in ``post-log.yaml`` with matching
        ``content_id``, ``channel``, and ``state="claiming"``, then
        overwrites its ``state``, ``published_url``, and ``error`` fields
        in-place and persists the file.

        Parameters
        ----------
        content_id:
            Same value passed to :meth:`claim_idempotency_key`.
        channel:
            Same value passed to :meth:`claim_idempotency_key`.
        state:
            Terminal state — ``"live"`` (success), ``"failed"``, or
            ``"queued"`` (e.g. for Medium browser-handoff variants).
        published_url:
            Canonical URL of the published content, if available.
        error:
            Error message when *state* is ``"failed"``.
        """
        p = self._path(self._POST_LOG_FILE)
        log: list[dict[str, Any]] = _load_list(p)

        # Walk in reverse to find the most recent claiming stub.
        for record in reversed(log):
            if (
                record.get("content_id") == content_id
                and record.get("channel") == channel
                and record.get("state") == "claiming"
            ):
                record["state"] = state
                record["published_url"] = published_url
                record["error"] = error
                record["updated_at"] = self._now().isoformat()
                break

        _atomic_write(p, log)

    # ------------------------------------------------------------------
    # Post-log lookups
    # ------------------------------------------------------------------

    def lookup_published(
        self, content_id: str, channel: str
    ) -> dict[str, Any] | None:
        """Return the most recent ``state=live`` record for ``(content_id, channel)``.

        Scans ``post-log.yaml`` and returns the last matching record with
        ``state="live"``, or ``None`` if none exists.

        Parameters
        ----------
        content_id:
            Content identifier to search for.
        channel:
            Channel identifier to search for.
        """
        log: list[dict[str, Any]] = _load_list(self._path(self._POST_LOG_FILE))
        result: dict[str, Any] | None = None
        for record in log:
            if (
                record.get("content_id") == content_id
                and record.get("channel") == channel
                and record.get("state") == "live"
            ):
                result = record  # keep iterating to get the most recent
        return result

    def list_post_log(
        self,
        *,
        content_id: str | None = None,
        channel: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return post-log records, optionally filtered by field values.

        Parameters
        ----------
        content_id:
            If given, only return records matching this content_id.
        channel:
            If given, only return records matching this channel.
        state:
            If given, only return records with this state value.
        """
        log: list[dict[str, Any]] = _load_list(self._path(self._POST_LOG_FILE))
        results = []
        for record in log:
            if content_id is not None and record.get("content_id") != content_id:
                continue
            if channel is not None and record.get("channel") != channel:
                continue
            if state is not None and record.get("state") != state:
                continue
            results.append(record)
        return results

    # ------------------------------------------------------------------
    # Scheduler queue
    # ------------------------------------------------------------------

    def enqueue_scheduled(self, variant: dict[str, Any]) -> str:
        """Append *variant* to ``pending.yaml`` and return a new scheduled_id.

        A UUID4 hex string is generated and stored as ``scheduled_id`` on
        the variant dict before appending.  The original *variant* dict is
        **not** mutated — a shallow copy with the injected key is written.

        Parameters
        ----------
        variant:
            ScheduledVariant data dict.  Must include at least
            ``schedule_at`` (ISO-8601 datetime string) and ``content_id``.

        Returns
        -------
        str
            The newly generated ``scheduled_id`` (UUID4 hex, 32 chars).
        """
        p = self._path(self._PENDING_FILE)
        pending: list[dict[str, Any]] = _load_list(p)

        scheduled_id = uuid.uuid4().hex
        entry = {**variant, "scheduled_id": scheduled_id}
        pending.append(entry)
        _atomic_write(p, pending)
        return scheduled_id

    def drain_scheduled(self) -> list[dict[str, Any]]:
        """Return all due variants and remove them from ``pending.yaml``.

        Partitions ``pending.yaml`` into **due** (``schedule_at <= now``)
        and **not-due** records.  Rewrites the file with only the not-due
        records, then returns the due ones for the caller to process.

        ``schedule_at`` values are expected to be ISO-8601 strings with
        timezone info (e.g. produced by :meth:`_now`).  Records missing
        ``schedule_at`` are treated as immediately due.

        Returns
        -------
        list[dict[str, Any]]
            Due variants, in the order they were originally enqueued.
        """
        p = self._path(self._PENDING_FILE)
        pending: list[dict[str, Any]] = _load_list(p)
        now = self._now()

        due: list[dict[str, Any]] = []
        not_due: list[dict[str, Any]] = []

        for variant in pending:
            raw_schedule = variant.get("schedule_at")
            if raw_schedule is None:
                due.append(variant)
                continue
            try:
                schedule_at = datetime.fromisoformat(str(raw_schedule))
                # Ensure timezone-aware for comparison.
                if schedule_at.tzinfo is None:
                    schedule_at = schedule_at.replace(tzinfo=timezone.utc)
                if schedule_at <= now:
                    due.append(variant)
                else:
                    not_due.append(variant)
            except (ValueError, TypeError):
                # Unparseable schedule_at — treat as due to avoid perpetual
                # queue blockage.
                due.append(variant)

        _atomic_write(p, not_due)
        return due

    def cancel_scheduled(self, scheduled_id: str) -> bool:
        """Remove the variant with *scheduled_id* from ``pending.yaml``.

        Parameters
        ----------
        scheduled_id:
            The ID returned by :meth:`enqueue_scheduled`.

        Returns
        -------
        bool
            ``True`` if a record was found and removed, ``False`` otherwise.
        """
        p = self._path(self._PENDING_FILE)
        pending: list[dict[str, Any]] = _load_list(p)
        original_len = len(pending)
        pending = [v for v in pending if v.get("scheduled_id") != scheduled_id]
        if len(pending) == original_len:
            return False
        _atomic_write(p, pending)
        return True

    # ------------------------------------------------------------------
    # Reddit-specific logging (5/day cap enforcement)
    # ------------------------------------------------------------------

    def record_reddit_post(self, record: dict[str, Any]) -> None:
        """Append *record* to ``reddit-log.yaml`` (separate from post-log).

        This log is used exclusively by the Reddit adapter to enforce the
        5-posts-per-day-per-account ceiling.  Keeping it separate prevents
        reddit-specific entries from polluting the main post-log queries.

        Parameters
        ----------
        record:
            Dict with at minimum ``account``, ``subreddit``, ``content_id``,
            and ``posted_at`` (ISO-8601 string).
        """
        p = self._path(self._REDDIT_LOG_FILE)
        log: list[dict[str, Any]] = _load_list(p)
        entry = {**record, "recorded_at": self._now().isoformat()}
        log.append(entry)
        _atomic_write(p, log)

    def count_reddit_posts_today(self, account: str) -> int:
        """Return the number of Reddit posts made today by *account*.

        "Today" is defined as calendar day in UTC matching :meth:`_now`.
        Scans ``reddit-log.yaml`` and counts records where ``account``
        matches and ``posted_at`` falls within the current UTC day.

        Parameters
        ----------
        account:
            Reddit account identifier (username or app-key alias).
        """
        p = self._path(self._REDDIT_LOG_FILE)
        log: list[dict[str, Any]] = _load_list(p)
        today = self._now().date()
        count = 0
        for entry in log:
            if entry.get("account") != account:
                continue
            raw = entry.get("posted_at")
            if raw is None:
                continue
            try:
                posted_at = datetime.fromisoformat(str(raw))
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=timezone.utc)
                if posted_at.astimezone(timezone.utc).date() == today:
                    count += 1
            except (ValueError, TypeError):
                continue
        return count

    def list_reddit_log(
        self,
        *,
        account: str | None = None,
        subreddit: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return Reddit log entries, optionally filtered.

        Parameters
        ----------
        account:
            If given, only return entries for this account.
        subreddit:
            If given, only return entries for this subreddit.
        """
        log: list[dict[str, Any]] = _load_list(self._path(self._REDDIT_LOG_FILE))
        results = []
        for entry in log:
            if account is not None and entry.get("account") != account:
                continue
            if subreddit is not None and entry.get("subreddit") != subreddit:
                continue
            results.append(entry)
        return results

    # ------------------------------------------------------------------
    # Utility / debug
    # ------------------------------------------------------------------

    def purge_all(self) -> None:
        """Wipe all YAML files back to their empty defaults.

        **Destructive** — intended only for tests and dev resets.  Do NOT
        call in production code.
        """
        for filename, default in (
            (self._PROFILES_FILE, {}),
            (self._SUBREDDITS_FILE, {}),
            (self._POST_LOG_FILE, []),
            (self._PENDING_FILE, []),
            (self._REDDIT_LOG_FILE, []),
        ):
            _atomic_write(self._path(filename), default)
