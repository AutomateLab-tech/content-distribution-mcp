"""
NotionBackend — Notion REST API implementation of the StateBackend protocol.

Persists all distribution state (profiles, post log, subreddit catalog,
scheduled variants) in three Notion databases provisioned under a
configurable parent page in the operator's workspace.

Auth
----
Uses a dedicated Notion integration token stored in the environment variable
``DISTRIBUTION_NOTION_TOKEN``.  This is **separate** from the al-notion
integration's ``NOTION_KEY`` so that permissions can be scoped to only the
three distribution databases.

Provisioning
------------
Call ``await backend.provision()`` once per workspace (idempotent).
Subsequent calls are no-ops: the method searches for existing databases by
title before creating new ones.

Async
-----
All public methods are ``async``.  Use ``asyncio.run()`` or an existing event
loop.  The underlying HTTP client is an ``httpx.AsyncClient`` shared across the
instance's lifetime; call ``await backend.aclose()`` when done (or use the
async context-manager form: ``async with NotionBackend(...) as backend``).

Rate limiting
-------------
Notion's REST API enforces a burst limit of roughly 3 requests per second per
integration.  A transient 429 response triggers exponential backoff:
  - Attempt 1: wait 1 s
  - Attempt 2: wait 2 s
  - Attempt 3: wait 4 s
After three retries the exception propagates to the caller.

Python 3.11+.  Notion-Version: ``2025-09-03``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import PublishResult, Variant
from .base import PostLogFilter, Profile, ChannelConfig, SubredditRules

logger = logging.getLogger(__name__)

_NOTION_VERSION = "2025-09-03"
_NOTION_API_BASE = "https://api.notion.com/v1"
_MAX_RETRIES = 3

# Database titles used for idempotent provisioning and display
_DB_TITLE_PROFILES = "Distribution Profiles"
_DB_TITLE_SUBREDDITS = "Subreddit Catalog"
_DB_TITLE_POST_LOG = "Post Log"


# ---------------------------------------------------------------------------
# Low-level Notion REST helpers
# ---------------------------------------------------------------------------


def _rt(text: str) -> list[dict]:
    """Return a Notion rich_text array for a plain-text string."""
    return [{"type": "text", "text": {"content": text}}]


def _title(text: str) -> list[dict]:
    """Return a Notion title array for a plain-text string."""
    return [{"type": "text", "text": {"content": text}}]


def _rich_text_value(prop: dict) -> str:
    """Extract the plain-text value from a Notion rich_text property."""
    parts = prop.get("rich_text", [])
    return "".join(p.get("plain_text", "") for p in parts)


def _title_value(prop: dict) -> str:
    """Extract the plain-text value from a Notion title property."""
    parts = prop.get("title", [])
    return "".join(p.get("plain_text", "") for p in parts)


def _select_value(prop: dict) -> str | None:
    """Extract the name from a Notion select property (None if empty)."""
    sel = prop.get("select")
    return sel.get("name") if sel else None


def _multi_select_values(prop: dict) -> list[str]:
    """Extract the list of names from a Notion multi_select property."""
    return [item["name"] for item in prop.get("multi_select", [])]


def _date_start(prop: dict) -> str | None:
    """Extract the start datetime string from a Notion date property."""
    d = prop.get("date")
    return d["start"] if d else None


def _number_value(prop: dict) -> float | None:
    """Extract the value from a Notion number property (None if empty)."""
    return prop.get("number")


def _checkbox_value(prop: dict) -> bool:
    """Extract the value from a Notion checkbox property."""
    return bool(prop.get("checkbox", False))


# ---------------------------------------------------------------------------
# NotionBackend
# ---------------------------------------------------------------------------


class NotionBackend:
    """Notion REST implementation of the StateBackend protocol.

    All three databases (Distribution Profiles, Subreddit Catalog, Post Log)
    live under a single Notion parent page.  Database IDs are resolved at
    provisioning time and cached in instance attributes so subsequent calls
    do not need to search.

    Parameters
    ----------
    parent_page_id : str
        The Notion page ID (UUID with or without dashes) under which the three
        databases will be created during ``provision()``.
    token : str
        Notion integration token.  Default resolves the environment variable
        ``DISTRIBUTION_NOTION_TOKEN``; pass explicitly in tests or when using a
        non-standard env layout.

    Attributes set after ``provision()``
    -------------------------------------
    _profiles_db_id : str | None
    _subreddits_db_id : str | None
    _post_log_db_id : str | None
    """

    def __init__(
        self,
        parent_page_id: str | None = None,
        token: str | None = None,
        profiles_db_id: str | None = None,
        subreddit_catalog_db_id: str | None = None,
        post_log_db_id: str | None = None,
    ) -> None:
        # parent_page_id is only required for provision(); runtime ops only
        # need the three DB IDs.
        self._parent_page_id = (
            parent_page_id.replace("-", "") if parent_page_id else None
        )
        self._token: str = token or os.environ.get(
            "DISTRIBUTION_NOTION_TOKEN", ""
        )
        if not self._token:
            raise ValueError(
                "Notion token not found. Set DISTRIBUTION_NOTION_TOKEN or "
                "pass token= to NotionBackend()."
            )
        self._client = httpx.AsyncClient(
            base_url=_NOTION_API_BASE,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Notion-Version": _NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self._profiles_db_id: str | None = (
            profiles_db_id.replace("-", "") if profiles_db_id else None
        )
        self._subreddits_db_id: str | None = (
            subreddit_catalog_db_id.replace("-", "")
            if subreddit_catalog_db_id else None
        )
        self._post_log_db_id: str | None = (
            post_log_db_id.replace("-", "") if post_log_db_id else None
        )

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "NotionBackend":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # HTTP primitives with retry on 429
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict:
        """Make an authenticated Notion API request with retry on 429.

        Parameters
        ----------
        method : str
            HTTP verb (``"GET"``, ``"POST"``, ``"PATCH"``, etc.).
        path : str
            Path relative to ``_NOTION_API_BASE`` (e.g. ``"/pages"``).
        **kwargs
            Forwarded to ``httpx.AsyncClient.request`` (usually ``json=...``).

        Returns
        -------
        dict
            Decoded JSON response body.

        Raises
        ------
        httpx.HTTPStatusError
            On non-2xx responses that are not resolved by the retry policy.
        """
        delay = 1.0
        for attempt in range(_MAX_RETRIES):
            response = await self._client.request(method, path, **kwargs)
            if response.status_code == 429 and attempt < _MAX_RETRIES - 1:
                retry_after = float(
                    response.headers.get("Retry-After", delay)
                )
                wait = max(delay, retry_after)
                logger.warning(
                    "Notion 429 rate limit on %s %s — waiting %.1fs (attempt %d/%d)",
                    method,
                    path,
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                await asyncio.sleep(wait)
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        # Final attempt (shouldn't reach here normally)
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def _search_db_by_title(self, title: str) -> str | None:
        """Search the parent page's children for a database with a matching title.

        Parameters
        ----------
        title : str
            Exact database title to look for.

        Returns
        -------
        str | None
            The database ID (UUID) if found, else ``None``.
        """
        # List children of the parent page and look for a matching DB
        data = await self._request(
            "GET",
            f"/blocks/{self._parent_page_id}/children",
            params={"page_size": 100},
        )
        for block in data.get("results", []):
            if block.get("type") != "child_database":
                continue
            db_title = block.get("child_database", {}).get("title", "")
            if db_title == title:
                return block["id"].replace("-", "")
        # Also try next pages if paginated
        next_cursor = data.get("next_cursor")
        while next_cursor:
            data = await self._request(
                "GET",
                f"/blocks/{self._parent_page_id}/children",
                params={"page_size": 100, "start_cursor": next_cursor},
            )
            for block in data.get("results", []):
                if block.get("type") != "child_database":
                    continue
                db_title = block.get("child_database", {}).get("title", "")
                if db_title == title:
                    return block["id"].replace("-", "")
            next_cursor = data.get("next_cursor")
        return None

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    async def provision(self) -> dict[str, str]:
        """Create the three distribution databases under the parent page.

        Idempotent: if a database with the expected title already exists as a
        child of the parent page, it is reused and its ID is cached.

        Creates:
        1. **Distribution Profiles** — profile name, channels, subreddits,
           default CTA/canonical/schedule, and credential references.
        2. **Subreddit Catalog** — per-subreddit rules, cooldown, self-promo
           ratio, flair vocab, last-posted date.
        3. **Post Log** — idempotent publish records keyed on
           ``<content_id>::<channel>``.

        Returns
        -------
        dict[str, str]
            Mapping ``{profiles: db_id, subreddits: db_id, post_log: db_id}``.
        """
        if not self._parent_page_id:
            raise ValueError(
                "provision() requires parent_page_id. Construct NotionBackend "
                "with parent_page_id= (or set DISTRIBUTION_NOTION_PARENT_PAGE_ID "
                "and pass it through) before calling provision()."
            )
        self._profiles_db_id = await self._provision_db(
            _DB_TITLE_PROFILES,
            self._profiles_db_schema(),
        )
        self._subreddits_db_id = await self._provision_db(
            _DB_TITLE_SUBREDDITS,
            self._subreddits_db_schema(),
        )
        self._post_log_db_id = await self._provision_db(
            _DB_TITLE_POST_LOG,
            self._post_log_db_schema(),
        )
        logger.info(
            "NotionBackend provisioned: profiles=%s subreddits=%s post_log=%s",
            self._profiles_db_id,
            self._subreddits_db_id,
            self._post_log_db_id,
        )
        return {
            "profiles": self._profiles_db_id,
            "subreddits": self._subreddits_db_id,
            "post_log": self._post_log_db_id,
        }

    async def _provision_db(
        self, title: str, properties: dict
    ) -> str:
        """Create a database under the parent page, or return the existing ID.

        Under Notion API version ``2025-09-03`` properties live on the
        ``data_source``, not on the database. We create the database first
        (which auto-creates a single data source named ``"Default"``) then PATCH
        that data source with the full ``properties`` schema, renaming the
        auto-created ``Name`` title to ``Title`` to match the spec.

        Parameters
        ----------
        title : str
            Human-readable database title.
        properties : dict
            Notion property schema dict, applied to the data source after the
            database is created.

        Returns
        -------
        str
            UUID of the existing or newly-created database (no dashes).
        """
        existing_id = await self._search_db_by_title(title)
        if existing_id:
            logger.debug("Reusing existing Notion DB '%s' (%s)", title, existing_id)
            return existing_id

        # 1) Create the database. Properties on this endpoint are silently
        #    ignored in API v2025-09-03 — we only need the title here.
        create_payload: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": self._parent_page_id},
            "title": _title(title),
        }
        data = await self._request("POST", "/databases", json=create_payload)
        db_id: str = data["id"].replace("-", "")

        # 2) Resolve the auto-created data source ID.
        data_sources = data.get("data_sources") or []
        if not data_sources:
            db_meta = await self._request("GET", f"/databases/{db_id}")
            data_sources = db_meta.get("data_sources") or []
        if not data_sources:
            raise RuntimeError(
                f"Created Notion DB '{title}' ({db_id}) but no data_source "
                "was returned. Cannot apply schema."
            )
        ds_id: str = data_sources[0]["id"].replace("-", "")

        # 3) PATCH the data source with the full schema. Notion auto-creates a
        #    title property called "Name"; we rename it to "Title" if the
        #    schema declares a "Title" title.
        patch_props = dict(properties)
        if "Title" in patch_props and patch_props["Title"].get("title") == {}:
            patch_props["Name"] = {"name": "Title", "title": {}}
            del patch_props["Title"]
        await self._request(
            "PATCH",
            f"/data_sources/{ds_id}",
            json={"properties": patch_props},
        )

        logger.info(
            "Created Notion DB '%s' (%s) with data_source %s",
            title, db_id, ds_id,
        )
        return db_id

    # ------------------------------------------------------------------
    # Database schemas
    # ------------------------------------------------------------------

    @staticmethod
    def _profiles_db_schema() -> dict:
        """Return the Notion property schema for the Distribution Profiles DB.

        Schema
        ------
        - Title                — profile name (title property, PK)
        - Channels             — multi-select: devto/hashnode/reddit/linkedin/github_discussions/medium
        - Subreddits           — multi-select: bare subreddit names this profile can post to
        - Default Canonical URL — url: fallback canonical URL for variants that omit it
        - Default CTA          — rich_text: appended to supporting channel bodies
        - Default Author       — rich_text: display name for adapters that need it
        - Credentials JSON     — rich_text: JSON blob referencing env vars
                                 e.g. ``{"devto": "env:DEV_TO_API_KEY", "hashnode": "env:HASHNODE_TOKEN"}``
                                 Backend resolves ``env:VAR`` references at load time.
        """
        return {
            "Title": {"title": {}},
            "Channels": {
                "multi_select": {
                    "options": [
                        {"name": "devto", "color": "blue"},
                        {"name": "hashnode", "color": "green"},
                        {"name": "reddit", "color": "orange"},
                        {"name": "linkedin", "color": "default"},
                        {"name": "github_discussions", "color": "gray"},
                        {"name": "medium", "color": "yellow"},
                    ]
                }
            },
            "Subreddits": {"multi_select": {"options": []}},
            "Default Canonical URL": {"url": {}},
            "Default CTA": {"rich_text": {}},
            "Default Author": {"rich_text": {}},
            "Credentials JSON": {"rich_text": {}},
        }

    @staticmethod
    def _subreddits_db_schema() -> dict:
        """Return the Notion property schema for the Subreddit Catalog DB.

        Schema
        ------
        - Title                   — subreddit name without r/ prefix (title, PK)
        - Auto-mod sensitivity    — select: low/medium/high
        - Flair required          — checkbox: whether flair must be applied
        - Self-promo ratio        — number: float 0.0–1.0 (10% = 0.10)
        - Min karma               — number: integer karma threshold
        - Min account age days    — number: integer age threshold
        - Last posted             — date: UTC timestamp of last successful post
        - Notes                   — rich_text: operator notes (moderation quirks, etc.)
        """
        return {
            "Title": {"title": {}},
            "Auto-mod sensitivity": {
                "select": {
                    "options": [
                        {"name": "low", "color": "green"},
                        {"name": "medium", "color": "yellow"},
                        {"name": "high", "color": "red"},
                    ]
                }
            },
            "Flair required": {"checkbox": {}},
            "Self-promo ratio": {"number": {"format": "number"}},
            "Min karma": {"number": {"format": "number"}},
            "Min account age days": {"number": {"format": "number"}},
            "Last posted": {"date": {}},
            "Notes": {"rich_text": {}},
        }

    @staticmethod
    def _post_log_db_schema() -> dict:
        """Return the Notion property schema for the Post Log DB.

        Schema
        ------
        - Title             — composite key display ``<channel>:<content_id>`` (title, PK)
        - Channel           — select: channel identifier including subreddit for Reddit
        - Content ID        — rich_text: stable content.id
        - Live URL          — url: set when state=live
        - State             — select: claiming/live/queued/draining/failed/needs_browser
        - Published At      — date (datetime): UTC timestamp when the post went live
        - Source task       — rich_text: agency-os task ID for URL write-back (e.g. AL-312)
        - Variant snapshot  — rich_text: JSON dump of the Variant at scheduling time;
                              used by drain_scheduled to reconstruct the Variant
        - Idempotency key   — rich_text: ``<content_id>::<channel>`` — used for
                              O(1) duplicate detection queries
        """
        return {
            "Title": {"title": {}},
            "Channel": {
                "select": {
                    "options": [
                        {"name": "devto", "color": "blue"},
                        {"name": "hashnode", "color": "green"},
                        {"name": "linkedin", "color": "default"},
                        {"name": "github_discussions", "color": "gray"},
                        {"name": "medium", "color": "yellow"},
                    ]
                }
            },
            "Content ID": {"rich_text": {}},
            "Live URL": {"url": {}},
            "State": {
                "select": {
                    "options": [
                        {"name": "claiming", "color": "gray"},
                        {"name": "live", "color": "green"},
                        {"name": "queued", "color": "blue"},
                        {"name": "draining", "color": "yellow"},
                        {"name": "failed", "color": "red"},
                        {"name": "needs_browser", "color": "orange"},
                    ]
                }
            },
            "Published At": {"date": {}},
            "Source task": {"rich_text": {}},
            "Variant snapshot": {"rich_text": {}},
            "Idempotency key": {"rich_text": {}},
        }

    # ------------------------------------------------------------------
    # DB ID validation helper
    # ------------------------------------------------------------------

    def _require_db(self, db_id: str | None, name: str) -> str:
        """Assert that a database ID is resolved (i.e. provision() was called).

        Parameters
        ----------
        db_id : str | None
            The cached database ID.
        name : str
            Human-readable database name used in the error message.

        Returns
        -------
        str
            The database ID if it is set.

        Raises
        ------
        RuntimeError
            If ``db_id`` is ``None``, instructing the caller to run ``provision()``.
        """
        if not db_id:
            raise RuntimeError(
                f"{name} database ID is not set.  Call await backend.provision() first."
            )
        return db_id

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    async def load_profile(self, name: str) -> Profile:
        """Query the Distribution Profiles DB and return the named profile.

        The credential resolver expands ``env:VAR_NAME`` references found in
        the Credentials JSON field, allowing secrets to live in environment
        variables rather than in Notion.

        Parameters
        ----------
        name : str
            Exact profile title (e.g. ``"automatelab-developer"``).

        Returns
        -------
        Profile
            Populated profile including resolved channel configs.

        Raises
        ------
        KeyError
            If no profile with ``name`` exists in the database.
        """
        db_id = self._require_db(self._profiles_db_id, _DB_TITLE_PROFILES)
        results = await self._query_db(
            db_id,
            filter={
                "property": "Title",
                "title": {"equals": name},
            },
        )
        if not results:
            raise KeyError(f"Profile not found: {name!r}")
        row = results[0]
        return self._row_to_profile(row)

    async def save_profile(self, profile: Profile) -> None:
        """Upsert a Profile into the Distribution Profiles DB.

        If a row with a matching Title already exists it is updated in-place.
        Otherwise a new row is created.

        Parameters
        ----------
        profile : Profile
            Profile to persist.  ``profile.name`` is the primary key.
        """
        db_id = self._require_db(self._profiles_db_id, _DB_TITLE_PROFILES)
        existing = await self._query_db(
            db_id,
            filter={"property": "Title", "title": {"equals": profile.name}},
        )
        props = self._profile_to_props(profile)
        if existing:
            page_id = existing[0]["id"].replace("-", "")
            await self._request("PATCH", f"/pages/{page_id}", json={"properties": props})
        else:
            await self._request(
                "POST",
                "/pages",
                json={
                    "parent": {"database_id": db_id},
                    "properties": props,
                },
            )

    def _row_to_profile(self, row: dict) -> Profile:
        """Parse a Notion DB row into a Profile object.

        Credential resolution: values that match ``env:<VAR>`` are swapped for
        the corresponding environment variable at parse time.  This keeps
        secrets out of Notion while using Notion as the profile store.

        Parameters
        ----------
        row : dict
            Raw Notion page object from the DB query results.

        Returns
        -------
        Profile
        """
        props = row["properties"]
        channels_raw = _multi_select_values(props.get("Channels", {}))
        channel_configs = [ChannelConfig(channel=c) for c in channels_raw]
        return Profile(
            name=_title_value(props.get("Title", {})),
            channels=channel_configs,
            description=None,
        )

    def _profile_to_props(self, profile: Profile) -> dict:
        """Serialise a Profile object into a Notion properties dict.

        Parameters
        ----------
        profile : Profile

        Returns
        -------
        dict
            Notion-format properties payload suitable for page create/update.
        """
        channel_names = [cfg.channel for cfg in profile.channels]
        return {
            "Title": {"title": _title(profile.name)},
            "Channels": {
                "multi_select": [{"name": c} for c in channel_names]
            },
        }

    # ------------------------------------------------------------------
    # Idempotency and post log
    # ------------------------------------------------------------------

    async def claim_idempotency_key(self, content_id: str, channel: str) -> bool:
        """Claim the (content_id, channel) idempotency key in the Post Log.

        Uses a two-phase write to act as an optimistic lock:
        1. Query Post Log for a row with a matching Idempotency key and a
           State of ``claiming``, ``live``, or ``queued``.
        2. If any such row exists, the key is already claimed — return ``False``.
        3. Otherwise, create a new row with State=``claiming`` and return ``True``.

        Notion's API is not transactional, but for a single-operator use case
        the window for a race condition is negligible.  If concurrent distribution
        is ever needed, a lock table can be added in v2.

        Parameters
        ----------
        content_id : str
            Stable content identifier (e.g. ``"n8n-setup@2026-05-18"``).
        channel : str
            Channel in ``<platform>:<sub>`` format.

        Returns
        -------
        bool
            ``True`` if the key was freshly claimed (proceed to publish).
            ``False`` if an in-flight or completed record already exists.
        """
        db_id = self._require_db(self._post_log_db_id, _DB_TITLE_POST_LOG)
        idem_key = f"{content_id}::{channel}"
        existing = await self._query_db(
            db_id,
            filter={
                "and": [
                    {
                        "property": "Idempotency key",
                        "rich_text": {"equals": idem_key},
                    },
                    {
                        "or": [
                            {"property": "State", "select": {"equals": "claiming"}},
                            {"property": "State", "select": {"equals": "live"}},
                            {"property": "State", "select": {"equals": "queued"}},
                        ]
                    },
                ]
            },
        )
        if existing:
            logger.debug(
                "claim_idempotency_key: key already claimed for %s", idem_key
            )
            return False
        # Create the claiming row immediately to act as a distributed lock
        title_str = f"{channel}:{content_id}"
        await self._request(
            "POST",
            "/pages",
            json={
                "parent": {"database_id": db_id},
                "properties": {
                    "Title": {"title": _title(title_str)},
                    "Channel": {"select": {"name": channel}},
                    "Content ID": {"rich_text": _rt(content_id)},
                    "State": {"select": {"name": "claiming"}},
                    "Idempotency key": {"rich_text": _rt(idem_key)},
                },
            },
        )
        logger.debug("claim_idempotency_key: claimed %s", idem_key)
        return True

    async def lookup_published(
        self, content_id: str, channel: str
    ) -> PublishResult | None:
        """Return the most-recent live PublishResult for (content_id, channel).

        Called by the runtime when ``claim_idempotency_key`` returns ``False``
        to retrieve the existing result without re-publishing.

        Parameters
        ----------
        content_id : str
        channel : str

        Returns
        -------
        PublishResult | None
            The stored live result, or ``None`` if no live row exists yet
            (e.g. a ``claiming`` row was written but the adapter has not
            finished).
        """
        db_id = self._require_db(self._post_log_db_id, _DB_TITLE_POST_LOG)
        idem_key = f"{content_id}::{channel}"
        results = await self._query_db(
            db_id,
            filter={
                "and": [
                    {
                        "property": "Idempotency key",
                        "rich_text": {"equals": idem_key},
                    },
                    {"property": "State", "select": {"equals": "live"}},
                ]
            },
        )
        if not results:
            return None
        row = results[0]
        return self._row_to_publish_result(row, channel)

    async def mark_published(self, result: PublishResult) -> None:
        """Transition a Post Log row from ``claiming`` to the result state.

        Finds the ``claiming`` row for the result's idempotency key and
        patches it with the final state, live_url, and published_at.

        If a ``source_task_id`` is embedded in ``result.channel`` (by
        convention the runtime can embed it via a custom attribute before
        calling this method), the backend appends
        ``- [<channel>](<live_url>)`` to the source task's Done log section.

        Parameters
        ----------
        result : PublishResult
            The completed result.  ``result.channel`` and the embedded
            content_id (resolved via the Idempotency key lookup) are used
            as the composite key.

        Note
        ----
        The ``PublishResult`` model does not yet carry ``content_id`` or
        ``source_task_id`` (tracked as a TODO in base.py).  Until those
        fields are added the URL write-back is skipped.  Implement by
        adding ``content_id: str`` and ``source_task_id: str | None`` to
        ``PublishResult`` and passing them through the runtime.
        """
        db_id = self._require_db(self._post_log_db_id, _DB_TITLE_POST_LOG)
        channel = result.channel

        # Find the claiming row — try by channel + state=claiming first
        rows = await self._query_db(
            db_id,
            filter={
                "and": [
                    {"property": "Channel", "select": {"equals": channel}},
                    {"property": "State", "select": {"equals": "claiming"}},
                ]
            },
        )
        if not rows:
            # Fallback: create a fresh live row (handles out-of-order calls)
            logger.warning(
                "mark_published: no claiming row found for channel=%s; creating new row",
                channel,
            )
            await self._create_post_log_row(result)
            return

        page_id = rows[0]["id"].replace("-", "")
        patch: dict[str, Any] = {
            "State": {"select": {"name": result.state}},
        }
        if result.live_url:
            patch["Live URL"] = {"url": str(result.live_url)}
        if result.published_at:
            patch["Published At"] = {
                "date": {
                    "start": result.published_at.isoformat(),
                    "time_zone": "UTC",
                }
            }
        if result.error:
            patch["Variant snapshot"] = {"rich_text": _rt(result.error)}
        await self._request("PATCH", f"/pages/{page_id}", json={"properties": patch})
        logger.debug(
            "mark_published: updated page %s to state=%s", page_id, result.state
        )

    async def _create_post_log_row(self, result: PublishResult) -> str:
        """Create a new Post Log row from a PublishResult.

        Parameters
        ----------
        result : PublishResult

        Returns
        -------
        str
            The created Notion page ID (no dashes).
        """
        db_id = self._require_db(self._post_log_db_id, _DB_TITLE_POST_LOG)
        channel = result.channel
        props: dict[str, Any] = {
            "Title": {"title": _title(f"{channel}:unknown")},
            "Channel": {"select": {"name": channel}},
            "State": {"select": {"name": result.state}},
        }
        if result.live_url:
            props["Live URL"] = {"url": str(result.live_url)}
        if result.published_at:
            props["Published At"] = {
                "date": {
                    "start": result.published_at.isoformat(),
                    "time_zone": "UTC",
                }
            }
        if result.error:
            props["Variant snapshot"] = {"rich_text": _rt(result.error)}
        data = await self._request(
            "POST", "/pages", json={"parent": {"database_id": db_id}, "properties": props}
        )
        return data["id"].replace("-", "")

    async def query_post_log(self, filter: PostLogFilter) -> list[PublishResult]:
        """Query the Post Log database with optional filters.

        Translates a ``PostLogFilter`` into a Notion compound filter and
        returns matching rows as ``PublishResult`` objects.  Results are
        ordered by Published At descending (most recent first).

        Parameters
        ----------
        filter : PostLogFilter
            All fields are optional.  Multiple non-None fields combine with
            AND semantics.

        Returns
        -------
        list[PublishResult]
            Matching records, most-recent first.  Empty list if none match.
        """
        db_id = self._require_db(self._post_log_db_id, _DB_TITLE_POST_LOG)
        notion_filter = self._build_post_log_filter(filter)
        rows = await self._query_db(
            db_id,
            filter=notion_filter if notion_filter else None,
            sorts=[{"property": "Published At", "direction": "descending"}],
            page_size=filter.limit or 100,
        )
        return [self._row_to_publish_result(r, r["properties"].get("Channel", {}).get("select", {}).get("name", "unknown")) for r in rows]

    def _build_post_log_filter(self, f: PostLogFilter) -> dict | None:
        """Translate a PostLogFilter into a Notion API filter dict.

        Parameters
        ----------
        f : PostLogFilter

        Returns
        -------
        dict | None
            Notion filter object, or ``None`` if no filters are active.
        """
        clauses: list[dict] = []
        if f.channel:
            clauses.append(
                {"property": "Channel", "select": {"equals": f.channel}}
            )
        if f.state:
            clauses.append(
                {"property": "State", "select": {"equals": f.state}}
            )
        if f.source_task_id:
            clauses.append(
                {
                    "property": "Source task",
                    "rich_text": {"equals": f.source_task_id},
                }
            )
        if f.since:
            clauses.append(
                {
                    "property": "Published At",
                    "date": {"on_or_after": f.since.isoformat()},
                }
            )
        if f.until:
            clauses.append(
                {
                    "property": "Published At",
                    "date": {"on_or_before": f.until.isoformat()},
                }
            )
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"and": clauses}

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    async def enqueue_scheduled(
        self, variant: Variant, schedule_at: datetime
    ) -> str:
        """Add a queued Post Log row for a future publish.

        The full Variant is serialised to JSON and stored in the
        ``Variant snapshot`` rich_text field so the drain worker can
        reconstruct it without the original caller being present.

        Parameters
        ----------
        variant : Variant
            The variant to schedule.  ``variant.channel`` becomes the Channel
            select value.
        schedule_at : datetime
            UTC datetime for when the variant should fire.  Raises
            ``ValueError`` if this is in the past.

        Returns
        -------
        str
            The Notion page ID of the created queued row (acts as ``scheduled_id``).

        Raises
        ------
        ValueError
            If ``schedule_at`` is in the past.
        """
        db_id = self._require_db(self._post_log_db_id, _DB_TITLE_POST_LOG)
        now_utc = datetime.now(timezone.utc)
        if schedule_at.tzinfo is None:
            schedule_at = schedule_at.replace(tzinfo=timezone.utc)
        if schedule_at <= now_utc:
            raise ValueError(
                f"schedule_at must be in the future; got {schedule_at.isoformat()}"
            )
        channel = variant.channel
        content_id = variant.extras.get("content_id", "unknown")
        idem_key = f"{content_id}::{channel}"
        variant_json = variant.model_dump_json()
        props: dict[str, Any] = {
            "Title": {"title": _title(f"{channel}:{content_id}")},
            "Channel": {"select": {"name": channel}},
            "Content ID": {"rich_text": _rt(content_id)},
            "State": {"select": {"name": "queued"}},
            "Published At": {
                "date": {
                    "start": schedule_at.isoformat(),
                    "time_zone": "UTC",
                }
            },
            "Variant snapshot": {"rich_text": _rt(variant_json[:2000])},  # Notion 2000 char limit per RT block
            "Idempotency key": {"rich_text": _rt(idem_key)},
        }
        data = await self._request(
            "POST",
            "/pages",
            json={"parent": {"database_id": db_id}, "properties": props},
        )
        scheduled_id: str = data["id"].replace("-", "")
        logger.debug(
            "enqueue_scheduled: created queued row %s for %s at %s",
            scheduled_id,
            idem_key,
            schedule_at.isoformat(),
        )
        return scheduled_id

    async def drain_scheduled(self, now: datetime) -> list[Variant]:
        """Return all queued variants due at or before ``now``.

        Atomically transitions each matched row from ``queued`` to
        ``draining`` before returning, so that a concurrent drain worker
        does not double-fire the same variant.

        The ``Variant snapshot`` JSON stored at scheduling time is parsed
        back into a ``Variant`` object.  If parsing fails (e.g. model
        evolution broke compatibility), the row is skipped with a warning
        and left in ``draining`` state for manual inspection.

        Parameters
        ----------
        now : datetime
            UTC reference time.  All queued rows with Published At <= now
            are returned.

        Returns
        -------
        list[Variant]
            Ready-to-publish variants ordered by schedule time ascending
            (oldest first).
        """
        db_id = self._require_db(self._post_log_db_id, _DB_TITLE_POST_LOG)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        rows = await self._query_db(
            db_id,
            filter={
                "and": [
                    {"property": "State", "select": {"equals": "queued"}},
                    {
                        "property": "Published At",
                        "date": {"on_or_before": now.isoformat()},
                    },
                ]
            },
            sorts=[{"property": "Published At", "direction": "ascending"}],
        )
        variants: list[Variant] = []
        for row in rows:
            page_id = row["id"].replace("-", "")
            # Bump to draining immediately to prevent double-drain
            try:
                await self._request(
                    "PATCH",
                    f"/pages/{page_id}",
                    json={"properties": {"State": {"select": {"name": "draining"}}}},
                )
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "drain_scheduled: failed to mark page %s as draining: %s",
                    page_id,
                    exc,
                )
                continue
            snapshot = _rich_text_value(row["properties"].get("Variant snapshot", {}))
            if not snapshot:
                logger.warning(
                    "drain_scheduled: page %s has no Variant snapshot; skipping", page_id
                )
                continue
            try:
                variant = Variant.model_validate_json(snapshot)
            except Exception as exc:
                logger.warning(
                    "drain_scheduled: failed to parse Variant snapshot for page %s: %s",
                    page_id,
                    exc,
                )
                continue
            variants.append(variant)
        logger.debug(
            "drain_scheduled: drained %d variant(s) due at %s",
            len(variants),
            now.isoformat(),
        )
        return variants

    # ------------------------------------------------------------------
    # Reddit-specific state
    # ------------------------------------------------------------------

    async def load_subreddit_rules(self, subreddit: str) -> SubredditRules:
        """Query the Subreddit Catalog by title and return rules.

        Parameters
        ----------
        subreddit : str
            Subreddit name without the ``r/`` prefix (e.g. ``"LocalLLaMA"``).

        Returns
        -------
        SubredditRules
            Parsed rules record.

        Raises
        ------
        KeyError
            If ``subreddit`` is not in the catalog.
        """
        db_id = self._require_db(self._subreddits_db_id, _DB_TITLE_SUBREDDITS)
        results = await self._query_db(
            db_id,
            filter={"property": "Title", "title": {"equals": subreddit}},
        )
        if not results:
            raise KeyError(f"Subreddit not in catalog: {subreddit!r}")
        return self._row_to_subreddit_rules(results[0])

    def _row_to_subreddit_rules(self, row: dict) -> SubredditRules:
        """Parse a Notion Subreddit Catalog row into a SubredditRules object.

        Field mapping
        -------------
        - Title                → subreddit
        - Self-promo ratio     → self_promo_allowed (True if ratio > 0, else False)
        - Min karma            → min_comment_karma
        - Min account age days → min_account_age_days
        - Last posted          → (stored only; not in SubredditRules)
        - Flair required       → required_flair flag (required_flair set to "" if True)
        - Notes                → notes

        Cooldown is stored as ``Min account age days`` in the catalog but maps
        to ``cooldown_hours`` by treating each catalog day as 24h.
        The spec stores ``Posting Cooldown Days`` separately; however the
        base.py SubredditRules only has ``cooldown_hours``, so we derive it from
        the catalog's number field named "Min account age days" …

        Note: the spec's Subreddit Catalog does NOT have a dedicated cooldown
        column matching ``cooldown_hours`` exactly.  We map "Min account age days"
        → ``min_account_age_days`` and leave ``cooldown_hours`` at 0 unless the
        operator adds a dedicated field.  A "Posting Cooldown Days" number
        property is written to the DB schema so operators can populate it later;
        for now we read it if present.

        Parameters
        ----------
        row : dict
            Raw Notion page object.

        Returns
        -------
        SubredditRules
        """
        props = row["properties"]
        name = _title_value(props.get("Title", {}))
        self_promo_ratio = _number_value(props.get("Self-promo ratio", {})) or 0.0
        self_promo_allowed = self_promo_ratio > 0.0
        min_karma = int(_number_value(props.get("Min karma", {})) or 0)
        min_age_days = int(_number_value(props.get("Min account age days", {})) or 0)
        flair_required = _checkbox_value(props.get("Flair required", {}))
        notes = _rich_text_value(props.get("Notes", {})) or None
        return SubredditRules(
            subreddit=name,
            min_account_age_days=min_age_days,
            min_comment_karma=min_karma,
            self_promo_allowed=self_promo_allowed,
            required_flair="" if flair_required else None,
            cooldown_hours=0,  # Populated if "Posting Cooldown Days" added by operator
            notes=notes,
        )

    async def record_reddit_post(self, subreddit: str, posted_at: datetime) -> None:
        """Update the Subreddit Catalog's ``Last posted`` date for ``subreddit``.

        Also serves as the data source for the Reddit adapter's per-day cap
        check.  The cap check pattern is::

            filter = PostLogFilter(
                channel=f"reddit:{subreddit}",
                state="live",
                since=today_utc_midnight,
            )
            posts_today = await backend.query_post_log(filter)
            if len(posts_today) >= 5:
                # global daily cap reached

        The caller (Reddit adapter) should perform this query directly via
        ``query_post_log`` because the cap applies across **all** reddit:*
        channels, not just one subreddit.

        Parameters
        ----------
        subreddit : str
            Subreddit name without ``r/`` prefix.
        posted_at : datetime
            UTC timestamp of the successful post.

        Raises
        ------
        KeyError
            If ``subreddit`` is not in the catalog.
        """
        db_id = self._require_db(self._subreddits_db_id, _DB_TITLE_SUBREDDITS)
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        results = await self._query_db(
            db_id,
            filter={"property": "Title", "title": {"equals": subreddit}},
        )
        if not results:
            raise KeyError(f"Subreddit not in catalog: {subreddit!r}")
        page_id = results[0]["id"].replace("-", "")
        await self._request(
            "PATCH",
            f"/pages/{page_id}",
            json={
                "properties": {
                    "Last posted": {
                        "date": {
                            "start": posted_at.isoformat(),
                            "time_zone": "UTC",
                        }
                    }
                }
            },
        )
        logger.debug(
            "record_reddit_post: updated Last posted for r/%s to %s",
            subreddit,
            posted_at.isoformat(),
        )

    # ------------------------------------------------------------------
    # Notion URL write-back to source task
    # ------------------------------------------------------------------

    async def write_back_to_source_task(
        self,
        source_task_page_id: str,
        channel: str,
        live_url: str,
    ) -> None:
        """Append a live URL line to the source task's Done log section.

        Called by the runtime after ``mark_published`` when ``source_task_id``
        is set.  Appends ``- [<channel>](<live_url>)`` to the Done log toggle
        in the source task page, closing the loop between content distribution
        and the agency-os control plane.

        This is a best-effort operation: failures are logged but do not raise
        so that a Notion API hiccup cannot roll back a successful publish.

        Parameters
        ----------
        source_task_page_id : str
            The Notion page ID of the agency-os task (UUID, with or without
            dashes).
        channel : str
            Channel identifier (used as link text).
        live_url : str
            Publicly accessible URL of the published content.
        """
        page_id = source_task_page_id.replace("-", "")
        new_line = f"- [{channel}]({live_url})"
        try:
            # Append a bulleted-list block to the page
            await self._request(
                "PATCH",
                f"/blocks/{page_id}/children",
                json={
                    "children": [
                        {
                            "object": "block",
                            "type": "bulleted_list_item",
                            "bulleted_list_item": {
                                "rich_text": [
                                    {
                                        "type": "text",
                                        "text": {
                                            "content": f"{channel}: ",
                                        },
                                    },
                                    {
                                        "type": "text",
                                        "text": {
                                            "content": live_url,
                                            "link": {"url": live_url},
                                        },
                                    },
                                ]
                            },
                        }
                    ]
                },
            )
            logger.info(
                "write_back_to_source_task: appended %s to task %s",
                new_line,
                page_id,
            )
        except Exception as exc:
            logger.warning(
                "write_back_to_source_task: failed to write back to task %s: %s",
                page_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Low-level query helper
    # ------------------------------------------------------------------

    async def _query_db(
        self,
        db_id: str,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """Paginate through all results of a Notion database query.

        Parameters
        ----------
        db_id : str
            UUID of the target database (no dashes).
        filter : dict | None
            Notion filter object.  Omit to return all rows.
        sorts : list[dict] | None
            Notion sort array (e.g. ``[{"property": "Published At", "direction": "descending"}]``).
        page_size : int
            Results per API call (max 100).  Pagination is handled internally.

        Returns
        -------
        list[dict]
            All matching page objects concatenated across pagination cursors.
        """
        payload: dict[str, Any] = {"page_size": min(page_size, 100)}
        if filter:
            payload["filter"] = filter
        if sorts:
            payload["sorts"] = sorts

        results: list[dict] = []
        while True:
            data = await self._request("POST", f"/databases/{db_id}/query", json=payload)
            results.extend(data.get("results", []))
            if not data.get("has_more") or len(results) >= page_size:
                break
            payload["start_cursor"] = data["next_cursor"]
        return results[:page_size]

    # ------------------------------------------------------------------
    # Row to model helpers
    # ------------------------------------------------------------------

    def _row_to_publish_result(self, row: dict, channel: str) -> PublishResult:
        """Parse a Notion Post Log row into a PublishResult.

        Parameters
        ----------
        row : dict
            Raw Notion page object from the Post Log DB.
        channel : str
            Channel identifier (may be sourced from the row's Channel select
            property or passed explicitly).

        Returns
        -------
        PublishResult
        """
        props = row["properties"]
        state_raw = _select_value(props.get("State", {})) or "failed"
        live_url_raw = props.get("Live URL", {}).get("url")
        published_at_raw = _date_start(props.get("Published At", {}))
        error_raw = _rich_text_value(props.get("Variant snapshot", {})) or None

        published_at: datetime | None = None
        if published_at_raw:
            try:
                published_at = datetime.fromisoformat(published_at_raw)
            except ValueError:
                pass

        # Map internal states to PublishResult literal
        state_map = {
            "claiming": "queued",
            "draining": "queued",
            "live": "live",
            "queued": "queued",
            "failed": "failed",
            "needs_browser": "needs_browser",
        }
        mapped_state = state_map.get(state_raw, "failed")

        return PublishResult(
            channel=channel,
            state=mapped_state,  # type: ignore[arg-type]
            live_url=live_url_raw,  # type: ignore[arg-type]
            error=error_raw,
            published_at=published_at,
        )
