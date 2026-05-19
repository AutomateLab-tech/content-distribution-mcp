"""
Canonical content models for Content Distribution MCP.

These Pydantic v2 models are the shared data contract between the MCP tools,
StateBackend implementations, and channel adapters. No adapter or backend
should define its own parallel representation of these concepts.

Python 3.11+. All models use ``extra="forbid"`` to catch typos in caller code.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Content — the canonical, platform-agnostic article/post record
# ---------------------------------------------------------------------------


class Content(BaseModel):
    """Canonical representation of a piece of content before channel adaptation.

    A ``Content`` record describes the authoritative version of a post —
    typically the Ghost blog article or the raw draft — before any
    channel-specific transformation has been applied. Adapters receive a
    ``Content`` + ``Variant`` pair; they must not mutate ``Content``.

    Fields
    ------
    id : str
        Stable identifier for this content item, used as the primary key in
        idempotency checks and the post log. Recommended format: ``<slug>@<iso-date>``
        (e.g. ``n8n-webhook-setup@2026-05-18``).
    title : str
        Canonical headline. Channel adapters may shorten or reformat this into
        ``Variant.title`` but the canonical version lives here.
    subtitle : str | None
        Optional deck / subheading. Used by adapters that support subtitles
        (e.g. DEV.to series subtitle, Hashnode subtitle field).
    body_md : str
        Full body in Markdown. Adapters are responsible for converting to the
        channel's required format (HTML, rich text, plain text, etc.).
    cover_image : AnyHttpUrl | None
        Absolute URL to the cover/hero image. Adapters that support images
        (DEV.to, Hashnode) attach this; adapters that do not (Reddit text posts)
        ignore it.
    tags : list[str]
        Platform-agnostic tag list. Adapters map these to the channel's tag
        vocabulary (see ``ChannelHints.tag_vocab``).
    canonical_url : AnyHttpUrl | None
        The SEO canonical URL for this content — typically the Ghost post URL.
        Adapters that natively support canonical_url (DEV.to, Hashnode) pass it
        through. Adapters that do not (LinkedIn, GitHub Discussions) append a
        footer line instead.
    cta_block : str | None
        Optional call-to-action block appended to the body. Plain text or
        minimal Markdown. Adapters place this according to
        ``ChannelHints.cta_placement``.
    author : str
        Display name of the author. Used in GitHub Discussions attribution
        footers and any channel that does not carry author identity implicitly
        through OAuth credentials.
    source_task_id : str | None
        Agency-OS task identifier (e.g. ``AL-312``) that commissioned this
        content. Stored in the post log so operators can query by task.

        # TODO: link to agency_os.models.Task once that module exists
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    subtitle: str | None = None
    body_md: str
    cover_image: AnyHttpUrl | None = None
    tags: list[str] = []
    canonical_url: AnyHttpUrl | None = None
    cta_block: str | None = None
    author: str
    source_task_id: str | None = None


# ---------------------------------------------------------------------------
# Variant — channel-specific adaptation of a Content record
# ---------------------------------------------------------------------------


class Variant(BaseModel):
    """Channel-specific adaptation of a :class:`Content` record.

    A ``Variant`` captures everything that differs between platforms: the
    adjusted title, reformatted body, channel-appropriate tags, and any
    platform-specific knobs stored in ``extras``. One ``Content`` record
    typically has one ``Variant`` per target channel.

    The ``channel`` field encodes both platform and sub-destination using the
    format ``<platform>:<sub>``. Examples:

    - ``devto:main`` — publish to the authenticated DEV.to account
    - ``hashnode:main`` — publish to the authenticated Hashnode blog
    - ``reddit:r/ClaudeAI`` — post to r/ClaudeAI
    - ``reddit:r/LocalLLaMA`` — post to r/LocalLLaMA
    - ``linkedin:personal`` — post to the authenticated LinkedIn personal feed
    - ``github_discussions:automatelab/content-distribution-mcp`` — post to a
      specific repo's Discussions board

    Fields
    ------
    channel : str
        Target channel in ``<platform>:<sub>`` format. Must match a registered
        adapter name.
    title : str
        Channel-adapted headline (may be shorter/different from ``Content.title``
        to fit the platform's character limits or norms).
    body : str
        Channel-adapted body. Format is adapter-defined (Markdown for DEV.to/
        Hashnode, plain text for Reddit, etc.).
    tags : list[str]
        Channel-specific tag list. For Reddit this must be empty (Reddit uses
        flair, not tags). For DEV.to limited to 4 tags.
    canonical_url : AnyHttpUrl | None
        Override the canonical URL for this specific channel. If ``None``,
        adapters fall back to ``Content.canonical_url``.
    cta_block : str | None
        Override the CTA block for this specific channel. If ``None``, adapters
        fall back to ``Content.cta_block``.
    schedule_at : datetime | None
        UTC datetime at which this variant should be published. ``None`` means
        publish immediately. The StateBackend stores scheduled variants via
        :meth:`~backends.base.StateBackend.enqueue_scheduled`.
    extras : dict[str, Any]
        Channel-specific knobs not covered by the standard fields. Each adapter
        documents its accepted keys. Common examples:

        - ``{"flair": "Discussion"}`` for Reddit (required by many subreddits)
        - ``{"category": "Show and tell"}`` for GitHub Discussions (required)
        - ``{"repo": "automatelab/content-distribution-mcp"}`` for GitHub
          Discussions (required)
        - ``{"series": "n8n Workflows"}`` for DEV.to series

        # TODO: replace with typed per-channel extra models once adapters are
        # implemented (devto/extras.py, reddit/extras.py, etc.)
    """

    model_config = ConfigDict(extra="forbid")

    channel: str
    title: str
    body: str
    tags: list[str] = []
    canonical_url: AnyHttpUrl | None = None
    cta_block: str | None = None
    schedule_at: datetime | None = None
    extras: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# PublishResult — outcome of a single channel publish attempt
# ---------------------------------------------------------------------------


class PublishResult(BaseModel):
    """Outcome of a single publish attempt to one channel.

    Returned by channel adapters and stored in the StateBackend post log.
    The ``state`` field is the canonical status; the URL/path fields carry the
    artifact location for each terminal state.

    States
    ------
    live
        Content is publicly accessible. ``live_url`` is set.
    queued
        Accepted by the platform but not yet live (e.g. pending moderation).
        ``live_url`` may be set as a draft preview URL.
    needs_browser
        The adapter cannot publish programmatically. ``compose_url`` and/or
        ``draft_path`` provide the operator with a pre-filled artifact to
        submit manually. Used by the Medium adapter.
    failed
        Publish attempt failed. ``error`` describes the failure. The operator
        may re-run after fixing the root cause.

    Fields
    ------
    channel : str
        The ``<platform>:<sub>`` channel this result corresponds to.
    state : Literal["live", "queued", "needs_browser", "failed"]
        Terminal or semi-terminal publish state.
    live_url : AnyHttpUrl | None
        Public URL of the published content. Set when ``state == "live"`` or,
        for platforms that preview drafts, when ``state == "queued"``.
    draft_path : Path | None
        Local filesystem path to a pre-filled draft file. Used by
        ``needs_browser`` adapters (e.g. Medium HTML draft).
    compose_url : AnyHttpUrl | None
        Platform compose/editor URL with pre-filled query parameters. Used by
        ``needs_browser`` adapters to open the editor in a browser tab.
    error : str | None
        Human-readable error description. Set when ``state == "failed"``.
    published_at : datetime | None
        UTC timestamp when the content went live. Set when ``state == "live"``.
        ``None`` for ``queued``, ``needs_browser``, and ``failed``.
    """

    model_config = ConfigDict(extra="forbid")

    channel: str
    state: Literal["live", "queued", "needs_browser", "failed"]
    live_url: AnyHttpUrl | None = None
    draft_path: Path | None = None
    compose_url: AnyHttpUrl | None = None
    error: str | None = None
    published_at: datetime | None = None


# ---------------------------------------------------------------------------
# ChannelHints — static metadata about a channel's publishing constraints
# ---------------------------------------------------------------------------


class ChannelHints(BaseModel):
    """Static metadata about a channel's publishing constraints and capabilities.

    Returned by ``ChannelAdapter.hints()``. The MCP ``get_hints`` tool exposes
    these to the LLM caller so it can make informed decisions about content
    length, tag selection, and CTA placement before constructing a ``Variant``.

    Fields
    ------
    max_length : int | None
        Maximum character count for the post body. ``None`` means no enforced
        limit (or limit is too high to be practically relevant). Reddit text
        posts are limited to 40,000 chars; LinkedIn posts to ~3,000 chars.
    supported_md_features : set[str]
        Set of Markdown feature tokens the channel renders correctly. Callers
        use this to strip unsupported syntax before posting. Example values:
        ``{"bold", "italic", "code_inline", "code_block", "links", "headers",
        "images", "lists", "tables", "blockquote"}``.
    tag_vocab : list[str] | None
        Suggested/approved tag vocabulary for the channel. ``None`` means the
        channel accepts free-form tags. DEV.to and Hashnode have large but
        finite tag namespaces; hints implementations should return the most
        relevant subset for the automatelab topic area.

        # TODO: populate from adapter-specific tag catalog files once adapters
        # are implemented
    cta_placement : Literal["top", "bottom", "footer", "none"]
        Where the adapter will insert the ``cta_block``. ``"none"`` means the
        adapter strips CTAs (e.g. subreddits that ban self-promotion).
        ``"footer"`` means a horizontal-rule-separated section at the end.
    canonical_url_supported : bool
        ``True`` if the platform natively stores/renders the canonical URL as
        metadata (DEV.to, Hashnode). ``False`` if the adapter must append a
        footer line or omit the canonical URL entirely.
    browser_only : bool
        ``True`` if the adapter cannot publish programmatically and will always
        return ``state="needs_browser"``. Currently ``True`` only for Medium.
    """

    model_config = ConfigDict(extra="forbid")

    max_length: int | None = None
    supported_md_features: set[str] = set()
    tag_vocab: list[str] | None = None
    cta_placement: Literal["top", "bottom", "footer", "none"] = "bottom"
    canonical_url_supported: bool = True
    browser_only: bool = False
