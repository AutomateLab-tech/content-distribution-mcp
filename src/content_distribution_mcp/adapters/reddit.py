"""
Reddit channel adapter for Content Distribution MCP.

Wraps the Reddit API via PRAW 7.x to submit text posts to subreddits.

Authentication
--------------
PRAW OAuth using the "script" app type. The operator profile (a dict) must
contain (flat or under ``credentials``):

    REDDIT_CLIENT_ID
    REDDIT_CLIENT_SECRET
    REDDIT_USERNAME
    REDDIT_PASSWORD
    REDDIT_USER_AGENT

Alternatively supply ``REDDIT_REFRESH_TOKEN`` instead of username+password.

Channel format
--------------
``reddit:<subreddit>`` — the ``r/`` prefix is optional and normalised away.

Gate sequence (enforced inside ``publish`` before submitting)
-------------------------------------------------------------
1. Global 5-posts/day cap   — per-account, from ``count_reddit_posts_today``
2. Per-subreddit cooldown   — from the last live post-log entry for the channel
3. Self-promo ratio         — over the account's last N posts in the subreddit
4. Account age + karma      — against ``SubredditRules`` minimums
5. Flair resolution         — from variant extras or subreddit's flair templates

PRAW is synchronous; PRAW calls are dispatched via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import praw
import praw.exceptions
import praw.models

from ..backends.base import SubredditRules
from ..models import ChannelHints, PublishResult, Variant


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REDDIT_MAX_BODY_CHARS: int = 40_000
_GLOBAL_DAILY_CAP: int = 5
_DEFAULT_SELF_PROMO_RATIO: float = 0.10
_SELF_PROMO_SAMPLE_SIZE: int = 10
_DEFAULT_COOLDOWN_HOURS: int = 168  # 7 days
_AUTOMOD_POLL_INTERVAL_SECS: float = 2.0
_AUTOMOD_POLL_ATTEMPTS: int = 3

_REDDIT_MD_FEATURES: frozenset[str] = frozenset(
    {
        "bold", "italic", "code_inline", "code_block",
        "links", "headers", "lists", "blockquote", "hr", "superscript",
    }
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_r_prefix(subreddit: str) -> str:
    """Drop a leading ``r/`` from a subreddit name."""
    return re.sub(r"^r/", "", subreddit, flags=re.IGNORECASE)


def _parse_subreddit(channel: str) -> str:
    """Return the bare subreddit name from ``reddit:<sub>`` channel."""
    if not channel.startswith("reddit:"):
        raise ValueError(f"Channel {channel!r} is not a Reddit channel")
    return _strip_r_prefix(channel.split(":", 1)[1])


def _profile_get(profile: dict[str, Any], key: str) -> str | None:
    """Read ``key`` from a profile dict (flat or nested under ``credentials``)."""
    if key in profile:
        return str(profile[key])
    creds = profile.get("credentials")
    if isinstance(creds, dict) and key in creds:
        return str(creds[key])
    return None


def _profile_credentials(profile: dict[str, Any]) -> dict[str, str]:
    """Flatten profile credentials into a single dict for PRAW."""
    out: dict[str, str] = {}
    creds = profile.get("credentials")
    if isinstance(creds, dict):
        out.update({k: str(v) for k, v in creds.items()})
    # Flat keys override the nested ones so callers can supply test overrides.
    for key in (
        "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME",
        "REDDIT_PASSWORD", "REDDIT_USER_AGENT", "REDDIT_REFRESH_TOKEN",
        "REDDIT_OWNED_DOMAINS",
    ):
        if key in profile:
            out[key] = str(profile[key])
    return out


def _build_praw_reddit(profile: dict[str, Any]) -> praw.Reddit:
    """Construct a PRAW ``Reddit`` instance from a profile dict.

    Refresh-token OAuth is preferred if ``REDDIT_REFRESH_TOKEN`` is present;
    otherwise username + password (script-app) is used.
    """
    creds = _profile_credentials(profile)
    common = {
        "client_id": creds["REDDIT_CLIENT_ID"],
        "client_secret": creds["REDDIT_CLIENT_SECRET"],
        "user_agent": creds["REDDIT_USER_AGENT"],
    }
    if "REDDIT_REFRESH_TOKEN" in creds:
        return praw.Reddit(refresh_token=creds["REDDIT_REFRESH_TOKEN"], **common)
    return praw.Reddit(
        username=creds["REDDIT_USERNAME"],
        password=creds["REDDIT_PASSWORD"],
        **common,
    )


def _coerce_rules(raw: dict[str, Any] | None, subreddit: str) -> SubredditRules:
    """Coerce a YAML-loaded rules dict (or ``None``) into ``SubredditRules``.

    Unknown subreddits get safe defaults so operators can post ad-hoc.
    """
    if raw is None:
        return SubredditRules(subreddit=subreddit)
    # Keep only keys the model accepts; the model uses ``extra="forbid"``.
    allowed = {
        "subreddit", "min_account_age_days", "min_comment_karma",
        "self_promo_allowed", "required_flair", "cooldown_hours", "notes",
    }
    filtered = {k: v for k, v in raw.items() if k in allowed}
    filtered.setdefault("subreddit", subreddit)
    return SubredditRules.model_validate(filtered)


# ---------------------------------------------------------------------------
# RedditAdapter
# ---------------------------------------------------------------------------


class RedditAdapter:
    """Channel adapter for Reddit text posts via PRAW 7.x.

    Channels handled: ``reddit:<subreddit>`` (the ``r/`` prefix is optional).
    """

    # ------------------------------------------------------------------
    # ChannelAdapter interface
    # ------------------------------------------------------------------

    def hints(self) -> ChannelHints:
        """Return static channel metadata for Reddit text posts."""
        return ChannelHints(
            max_length=_REDDIT_MAX_BODY_CHARS,
            supported_md_features=set(_REDDIT_MD_FEATURES),
            tag_vocab=None,
            cta_placement="none",
            canonical_url_supported=False,
            browser_only=False,
        )

    def can_publish(self, variant: Variant) -> tuple[bool, str]:
        """Return ``(ok, reason)``; structural-only pre-flight check."""
        if not variant.channel.startswith("reddit:"):
            return False, f"channel-not-reddit: {variant.channel}"
        if not variant.title:
            return False, "empty-title"
        if not variant.body:
            return False, "empty-body"
        if not (variant.extras and variant.extras.get("content_id")):
            return False, "missing-content-id-in-variant-extras"
        return True, ""

    async def publish(
        self,
        variant: Variant,
        profile: dict[str, Any] | None,
        state_backend: Any,
    ) -> PublishResult:
        """Publish a variant as a Reddit text post."""
        if profile is None:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-profile",
            )

        content_id = variant.extras.get("content_id") if variant.extras else None
        if not isinstance(content_id, str) or not content_id:
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="missing-content-id-in-variant-extras",
            )

        subreddit_name = _parse_subreddit(variant.channel)

        # --- 1. Idempotency check (sync API on YamlBackend) ---
        claimed = state_backend.claim_idempotency_key(content_id, variant.channel)
        if not claimed:
            existing = state_backend.lookup_published(content_id, variant.channel)
            if existing is not None:
                return PublishResult(
                    channel=variant.channel,
                    state="live",
                    live_url=existing.get("published_url"),
                )
            return PublishResult(
                channel=variant.channel,
                state="failed",
                error="idempotency-claimed-but-no-live-row",
            )

        def _fail(error: str) -> PublishResult:
            state_backend.mark_published(
                content_id, variant.channel,
                state="failed", published_url=None, error=error,
            )
            return PublishResult(
                channel=variant.channel, state="failed", error=error,
            )

        # --- 2. Pre-publish gate audit ---
        try:
            ok, gate_err, flair_id = await self._audit_pre_publish(
                variant, profile, state_backend, subreddit_name,
            )
        except KeyError as exc:
            return _fail(f"missing-credential: {exc}")
        if not ok:
            return _fail(gate_err or "gate-failed")

        # --- 3. PRAW submit ---
        try:
            reddit = await asyncio.to_thread(_build_praw_reddit, profile)
            submission = await asyncio.to_thread(
                self._praw_submit,
                reddit, subreddit_name, variant.title, variant.body, flair_id,
            )
        except praw.exceptions.RedditAPIException as exc:
            return _fail(f"reddit-api-exception: {exc}")
        except Exception as exc:  # noqa: BLE001
            return _fail(f"praw-error: {type(exc).__name__}: {exc}")

        # --- 4. AutoMod removal poll ---
        if await self._poll_automod_removal(submission):
            return _fail("automod-removed: post removed or locked immediately after submission")

        # --- 5. Success — write-back ---
        live_url = getattr(submission, "shortlink", None) or getattr(submission, "url", None)
        now_utc = datetime.now(timezone.utc)
        state_backend.mark_published(
            content_id, variant.channel,
            state="live", published_url=live_url, error=None,
        )
        account = _profile_get(profile, "REDDIT_USERNAME") or "unknown"
        state_backend.record_reddit_post({
            "account": account,
            "subreddit": subreddit_name,
            "content_id": content_id,
            "channel": variant.channel,
            "posted_at": now_utc.isoformat(),
            "url": live_url,
        })

        return PublishResult(
            channel=variant.channel,
            state="live",
            live_url=live_url,
            published_at=now_utc,
        )

    async def unpublish(self, live_url: str, profile: dict[str, Any]) -> bool:
        """Delete a Reddit submission. Returns ``True`` on success."""
        try:
            reddit = await asyncio.to_thread(_build_praw_reddit, profile)
            await asyncio.to_thread(self._praw_delete_submission, reddit, live_url)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # Gate audit
    # ------------------------------------------------------------------

    async def _audit_pre_publish(
        self,
        variant: Variant,
        profile: dict[str, Any],
        state_backend: Any,
        subreddit_name: str,
    ) -> tuple[bool, str | None, str | None]:
        """Run the five sequential gates; return ``(ok, error, flair_id)``."""
        raw_rules = state_backend.load_subreddit_rules(subreddit_name)
        rules = _coerce_rules(raw_rules, subreddit_name)

        # Gate 1 — global daily cap
        account = _profile_get(profile, "REDDIT_USERNAME") or "unknown"
        today_count = state_backend.count_reddit_posts_today(account)
        if today_count >= _GLOBAL_DAILY_CAP:
            return False, (
                f"reddit-cap-reached: {today_count} Reddit posts today "
                f"(global cap is {_GLOBAL_DAILY_CAP})"
            ), None

        # Gate 2 — per-subreddit cooldown
        cooldown_err = self._check_cooldown(subreddit_name, rules, state_backend)
        if cooldown_err is not None:
            return False, cooldown_err, None

        # Gate 3 — self-promo ratio
        promo_err = await self._check_self_promo(subreddit_name, rules, profile)
        if promo_err is not None:
            return False, promo_err, None

        # Gate 4 — account age + karma
        eligibility_err = await self._check_account_eligibility(rules, profile)
        if eligibility_err is not None:
            return False, eligibility_err, None

        # Gate 5 — flair resolution
        flair_ok, flair_err, flair_id = await self._resolve_flair(
            subreddit_name, rules, variant, profile,
        )
        if not flair_ok:
            return False, flair_err, None

        return True, None, flair_id

    # ------------------------------------------------------------------
    # Individual gates
    # ------------------------------------------------------------------

    def _check_cooldown(
        self,
        subreddit: str,
        rules: SubredditRules,
        state_backend: Any,
    ) -> str | None:
        """Gate 2: refuse if we've posted to this subreddit too recently."""
        cooldown_hours = rules.cooldown_hours or _DEFAULT_COOLDOWN_HOURS
        channel = f"reddit:{subreddit}"
        prior = state_backend.list_post_log(channel=channel, state="live")
        if not prior:
            return None

        # Find the most recent published_at across matching rows.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
        for record in prior:
            raw = record.get("updated_at") or record.get("claimed_at")
            if not raw:
                continue
            try:
                posted_at = datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                continue
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
            if posted_at > cutoff:
                hours_ago = round(
                    (datetime.now(timezone.utc) - posted_at).total_seconds() / 3600.0,
                    1,
                )
                return (
                    f"reddit-cooldown: posted to r/{subreddit} {hours_ago}h ago, "
                    f"cooldown is {cooldown_hours}h"
                )
        return None

    async def _check_self_promo(
        self,
        subreddit: str,
        rules: SubredditRules,
        profile: dict[str, Any],
    ) -> str | None:
        """Gate 3: refuse if too much of the account's recent history is self-promo."""
        if not rules.self_promo_allowed:
            return (
                f"self-promo-not-allowed: r/{subreddit} bans self-promotional posts"
            )

        owned_domains: list[str] = []
        raw = _profile_get(profile, "REDDIT_OWNED_DOMAINS")
        if raw:
            owned_domains = [d.lower().strip() for d in raw.splitlines() if d.strip()]
        if not owned_domains:
            return None  # No domains configured — gate passes.

        try:
            reddit = await asyncio.to_thread(_build_praw_reddit, profile)
            submissions = await asyncio.to_thread(
                self._fetch_recent_subreddit_submissions,
                reddit, subreddit, _SELF_PROMO_SAMPLE_SIZE,
            )
        except Exception:  # noqa: BLE001
            return None  # PRAW history fetch failed — fail-open, gate passes.

        if not submissions:
            return None

        self_promo_count = sum(
            1
            for s in submissions
            if any(domain in (getattr(s, "url", "") or "").lower() for domain in owned_domains)
        )
        ratio = self_promo_count / len(submissions)
        if ratio > _DEFAULT_SELF_PROMO_RATIO:
            return (
                f"self-promo-ratio: {ratio:.2f} > {_DEFAULT_SELF_PROMO_RATIO:.2f} "
                f"in r/{subreddit} (last {len(submissions)} posts)"
            )
        return None

    async def _check_account_eligibility(
        self,
        rules: SubredditRules,
        profile: dict[str, Any],
    ) -> str | None:
        """Gate 4: refuse if account age or karma is below subreddit minimums."""
        if rules.min_account_age_days == 0 and rules.min_comment_karma == 0:
            return None

        try:
            reddit = await asyncio.to_thread(_build_praw_reddit, profile)
            me = await asyncio.to_thread(lambda: reddit.user.me())
        except Exception:  # noqa: BLE001
            return None  # Cannot read profile — fail-open.

        if rules.min_account_age_days > 0:
            created_dt = datetime.fromtimestamp(me.created_utc, tz=timezone.utc)
            age_days = (datetime.now(timezone.utc) - created_dt).days
            if age_days < rules.min_account_age_days:
                return (
                    f"account-too-new: {age_days}d old, "
                    f"minimum is {rules.min_account_age_days}d for r/{rules.subreddit}"
                )

        if rules.min_comment_karma > 0:
            total_karma = int(getattr(me, "link_karma", 0)) + int(getattr(me, "comment_karma", 0))
            if total_karma < rules.min_comment_karma:
                return (
                    f"low-karma: {total_karma} karma, "
                    f"minimum is {rules.min_comment_karma} for r/{rules.subreddit}"
                )

        return None

    async def _resolve_flair(
        self,
        subreddit: str,
        rules: SubredditRules,
        variant: Variant,
        profile: dict[str, Any],
    ) -> tuple[bool, str | None, str | None]:
        """Gate 5: resolve the flair template ID for the submission.

        Priority: ``extras.flair_id`` → ``extras.flair_name`` (fuzzy) →
        ``rules.required_flair`` (fuzzy) → first available template if flair
        is required → ``None`` otherwise.
        """
        extras = variant.extras or {}

        explicit = extras.get("flair_id") or extras.get("subreddit_flair_id")
        if explicit:
            return True, None, str(explicit)

        desired = (
            extras.get("flair_name")
            or extras.get("subreddit_flair")
            or rules.required_flair
        )
        flair_required = rules.required_flair is not None

        if desired is None and not flair_required:
            return True, None, None

        try:
            reddit = await asyncio.to_thread(_build_praw_reddit, profile)
            templates = await asyncio.to_thread(
                self._fetch_flair_templates, reddit, subreddit,
            )
        except Exception:  # noqa: BLE001
            templates = []

        if not templates:
            if flair_required:
                return (
                    False,
                    f"flair-required: r/{subreddit} requires flair but no templates available",
                    None,
                )
            return True, None, None

        if desired:
            normalized = str(desired).lower()
            for template in templates:
                if (template.get("flair_text") or "").lower() == normalized:
                    return True, None, template["id"]
            available = [t.get("flair_text", "") for t in templates]
            if flair_required:
                return (
                    False,
                    f"flair-required: no flair matching {desired!r} on r/{subreddit}. "
                    f"Available: {available!r}",
                    None,
                )
            _log.warning(
                "reddit: flair %r not found on r/%s; using first available %r",
                desired, subreddit, templates[0].get("flair_text"),
            )
            return True, None, templates[0]["id"]

        # Required but no desired name — use first template.
        return True, None, templates[0]["id"]

    # ------------------------------------------------------------------
    # PRAW sync helpers (wrapped via asyncio.to_thread by callers)
    # ------------------------------------------------------------------

    @staticmethod
    def _praw_submit(
        reddit: praw.Reddit,
        subreddit: str,
        title: str,
        body: str,
        flair_id: str | None,
    ) -> praw.models.Submission:
        sub = reddit.subreddit(subreddit)
        kwargs: dict[str, Any] = {"title": title, "selftext": body}
        if flair_id:
            kwargs["flair_id"] = flair_id
        return sub.submit(**kwargs)

    @staticmethod
    def _praw_delete_submission(reddit: praw.Reddit, url: str) -> None:
        submission = reddit.submission(url=url)
        submission.delete()

    @staticmethod
    def _fetch_recent_subreddit_submissions(
        reddit: praw.Reddit,
        subreddit: str,
        limit: int,
    ) -> list[praw.models.Submission]:
        me = reddit.user.me()
        candidates = list(me.submissions.new(limit=limit * 3))
        filtered = [
            s for s in candidates
            if s.subreddit.display_name.lower() == subreddit.lower()
        ]
        return filtered[:limit]

    @staticmethod
    def _fetch_flair_templates(
        reddit: praw.Reddit,
        subreddit: str,
    ) -> list[dict[str, Any]]:
        try:
            sub = reddit.subreddit(subreddit)
            return list(sub.flair.link_templates)
        except Exception:  # noqa: BLE001
            return []

    async def _poll_automod_removal(
        self, submission: praw.models.Submission
    ) -> bool:
        """Detect immediate AutoModerator removal or lock after submission."""
        for _ in range(_AUTOMOD_POLL_ATTEMPTS):
            await asyncio.sleep(_AUTOMOD_POLL_INTERVAL_SECS)
            try:
                await asyncio.to_thread(submission._fetch)
                if getattr(submission, "removed", False) or getattr(submission, "locked", False):
                    return True
            except Exception:  # noqa: BLE001
                break
        return False
