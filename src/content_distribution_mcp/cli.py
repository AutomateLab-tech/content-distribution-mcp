"""
CLI entry point for Content Distribution MCP.

Entry command: ``content-distribution-mcp``
Configured in pyproject.toml under [project.scripts]:
    content-distribution-mcp = "content_distribution_mcp.cli:cli"

# TODO: add the above entry_points line to pyproject.toml when the package
# is assembled (AL-412 territory).

Subcommands
-----------
serve               Start the FastMCP server (AL-412 territory — placeholder).
drain               Fire due scheduled posts (--once or --loop).
provision-notion    Create the three Notion databases for NotionBackend.
mark-live           Close out a manual Medium publish by recording its live URL.
open-pending        Open Medium compose URLs for pending variants in browser tabs.
status              Print the Post Log, optionally filtered by content_id.

Backend selection
-----------------
Reads ``DISTRIBUTION_BACKEND`` env var (default: ``yaml``).
  yaml   → YamlBackend(base_dir)
  notion → NotionBackend(token, parent_page_id)

Related env vars
-----------------
DISTRIBUTION_BACKEND              "yaml" | "notion"  (default: "yaml")
DISTRIBUTION_YAML_DIR             Override ~/.distribution-mcp base dir
DISTRIBUTION_NOTION_TOKEN         Notion integration token  (notion backend)
DISTRIBUTION_NOTION_PARENT_PAGE_ID  Parent page for DB provisioning  (notion backend)

Python 3.11+.
"""

from __future__ import annotations

import asyncio
import os
import sys
import webbrowser
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Relative imports — sibling modules.
# Modules not yet implemented are guarded with TODO comments.
# ---------------------------------------------------------------------------

from .backends.yaml_backend import YamlBackend  # type: ignore[import]
from .adapters.devto import DevToAdapter  # type: ignore[import]
from .adapters.hashnode import HashnodeAdapter  # type: ignore[import]

# TODO: implement these adapters in AL-412 / subsequent tasks
try:
    from .adapters.github_discussions import GitHubDiscussionsAdapter  # type: ignore[import]
except ImportError:  # pragma: no cover
    GitHubDiscussionsAdapter = None  # type: ignore[assignment,misc]

try:
    from .adapters.reddit import RedditAdapter  # type: ignore[import]
except ImportError:  # pragma: no cover
    RedditAdapter = None  # type: ignore[assignment,misc]

try:
    from .adapters.linkedin import LinkedInAdapter  # type: ignore[import]
except ImportError:  # pragma: no cover
    LinkedInAdapter = None  # type: ignore[assignment,misc]

try:
    from .adapters.medium_browser import MediumBrowserAdapter  # type: ignore[import]
except ImportError:  # pragma: no cover
    MediumBrowserAdapter = None  # type: ignore[assignment,misc]

# TODO: implement NotionBackend in AL-412 / subsequent tasks
try:
    from .backends.notion_backend import NotionBackend  # type: ignore[import]
except ImportError:  # pragma: no cover
    NotionBackend = None  # type: ignore[assignment,misc]

from .scheduler import drain as scheduler_drain  # type: ignore[import]
from .scheduler import worker_loop  # type: ignore[import]


# ---------------------------------------------------------------------------
# Hardcoded adapter map (no plugin discovery)
# ---------------------------------------------------------------------------

def _build_adapters() -> dict[str, object]:
    """Return the hardcoded channel-prefix → adapter instance map.

    Only instantiates adapters whose classes were successfully imported.
    Missing adapters log a debug message and are omitted; the scheduler will
    return ``state=failed`` with ``no-adapter-for-channel`` for those channels.
    """
    adapters: dict[str, object] = {}

    adapters["devto"] = DevToAdapter()
    adapters["hashnode"] = HashnodeAdapter()

    if GitHubDiscussionsAdapter is not None:
        adapters["github-discussions"] = GitHubDiscussionsAdapter()
    else:
        click.echo(
            "warning: github-discussions adapter not available (import failed)",
            err=True,
        )

    if RedditAdapter is not None:
        adapters["reddit"] = RedditAdapter()
    else:
        click.echo("warning: reddit adapter not available (import failed)", err=True)

    if LinkedInAdapter is not None:
        adapters["linkedin"] = LinkedInAdapter()
    else:
        click.echo("warning: linkedin adapter not available (import failed)", err=True)

    if MediumBrowserAdapter is not None:
        adapters["medium-browser"] = MediumBrowserAdapter()
    else:
        click.echo(
            "warning: medium-browser adapter not available (import failed)",
            err=True,
        )

    return adapters


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

def _build_backend() -> object:
    """Instantiate the StateBackend selected by ``DISTRIBUTION_BACKEND``.

    Returns
    -------
    YamlBackend | NotionBackend
        The configured backend instance.

    Raises
    ------
    SystemExit
        If ``DISTRIBUTION_BACKEND=notion`` but the backend class failed to
        import or required env vars are missing.
    """
    backend_name = os.environ.get("DISTRIBUTION_BACKEND", "yaml").lower().strip()

    if backend_name == "notion":
        if NotionBackend is None:
            click.echo(
                "error: DISTRIBUTION_BACKEND=notion but NotionBackend is not installed.\n"
                "       Run: pip install content-distribution-mcp[notion]",
                err=True,
            )
            sys.exit(1)
        token = os.environ.get("DISTRIBUTION_NOTION_TOKEN", "")
        parent_page_id = os.environ.get("DISTRIBUTION_NOTION_PARENT_PAGE_ID", "")
        if not token:
            click.echo(
                "error: DISTRIBUTION_NOTION_TOKEN env var is required for notion backend",
                err=True,
            )
            sys.exit(1)
        return NotionBackend(token=token, parent_page_id=parent_page_id)  # type: ignore[misc]

    # Default: yaml
    yaml_dir = os.environ.get("DISTRIBUTION_YAML_DIR")
    base_dir = Path(yaml_dir) if yaml_dir else Path.home() / ".distribution-mcp"
    return YamlBackend(base_dir=base_dir)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """Content Distribution MCP — cross-post finished content to developer platforms."""


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

@cli.command()
def serve() -> None:
    """Start the FastMCP server (stdio transport by default).

    The actual server implementation lives in ``server.py`` (AL-412 scope).
    This command is a placeholder that imports and delegates to it.
    """
    # TODO: implement server.py in AL-412.
    try:
        from .server import main as server_main  # type: ignore[import]  # noqa: PLC0415

        server_main()
    except ImportError:
        click.echo(
            "error: server module not yet implemented (AL-412 scope).\n"
            "       Run the MCP server directly once server.py exists.",
            err=True,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--once",
    "mode",
    flag_value="once",
    default=True,
    help="Fire all due posts and exit (default).",
)
@click.option(
    "--loop",
    "mode",
    flag_value="loop",
    help="Run the worker loop forever, polling every 60 seconds.",
)
@click.option(
    "--poll-interval",
    default=60,
    show_default=True,
    type=int,
    help="Seconds between polls (--loop only).",
)
def drain(mode: str, poll_interval: int) -> None:
    """Fire scheduled posts that are due now.

    Use ``--once`` (default) for cron jobs:

    \b
        */5 * * * * content-distribution-mcp drain >> ~/.distribution-mcp/drain.log 2>&1

    Use ``--loop`` when running as a long-lived process alongside the MCP server.
    """
    adapters = _build_adapters()
    state_backend = _build_backend()

    if mode == "once":
        results = asyncio.run(scheduler_drain(adapters, state_backend))  # type: ignore[arg-type]
        if not results:
            click.echo("drain: nothing due.")
            return
        for r in results:
            if r.state == "live":
                click.echo(f"  live     {r.channel} → {r.live_url}")
            elif r.state == "needs_browser":
                click.echo(f"  browser  {r.channel} → {r.compose_url}")
            else:
                click.echo(f"  failed   {r.channel} — {r.error}")
    else:
        click.echo(f"Starting worker loop (poll_interval={poll_interval}s). Ctrl-C to stop.")
        try:
            asyncio.run(worker_loop(adapters, state_backend, poll_interval_sec=poll_interval))  # type: ignore[arg-type]
        except KeyboardInterrupt:
            click.echo("\nworker loop stopped.")


# ---------------------------------------------------------------------------
# provision-notion
# ---------------------------------------------------------------------------

@cli.command("provision-notion")
@click.option(
    "--parent-page-id",
    required=True,
    help="Notion page ID under which the three databases will be created.",
)
def provision_notion(parent_page_id: str) -> None:
    """Provision the three Notion databases for NotionBackend.

    Creates: Distribution Profiles, Subreddit Catalog, Post Log.
    Prints the resulting database IDs on success.
    """
    if NotionBackend is None:
        click.echo(
            "error: NotionBackend is not installed.\n"
            "       Run: pip install content-distribution-mcp[notion]",
            err=True,
        )
        sys.exit(1)

    token = os.environ.get("DISTRIBUTION_NOTION_TOKEN", "")
    if not token:
        click.echo(
            "error: DISTRIBUTION_NOTION_TOKEN env var is required", err=True
        )
        sys.exit(1)

    async def _run() -> dict[str, str]:
        backend = NotionBackend(token=token, parent_page_id=parent_page_id)  # type: ignore[misc]
        try:
            return await backend.provision()  # type: ignore[union-attr]
        finally:
            await backend.aclose()  # type: ignore[union-attr]

    try:
        db_ids: dict[str, str] = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: provision failed — {exc}", err=True)
        sys.exit(1)

    click.echo("Notion databases created:")
    for db_name, db_id in db_ids.items():
        click.echo(f"  {db_name}: {db_id}")
    click.echo(
        "\nSet these in your environment or profiles.yaml before using the notion backend."
    )


# ---------------------------------------------------------------------------
# mark-live
# ---------------------------------------------------------------------------

@cli.command("mark-live")
@click.argument("content_id")
@click.argument("channel")
@click.argument("live_url")
def mark_live(content_id: str, channel: str, live_url: str) -> None:
    """Record a live URL for a manually-published Medium post.

    Closes out a ``needs_browser`` post log entry by setting its state to
    ``live`` and recording the operator-supplied URL.

    Example:
        content-distribution-mcp mark-live my-post-id medium-browser:main https://medium.com/@me/my-post
    """
    state_backend = _build_backend()

    # Resolve via the medium_browser adapter helper when available.
    if MediumBrowserAdapter is not None:
        try:
            from .adapters.medium_browser import mark_live as _mark_live  # type: ignore[import]  # noqa: PLC0415

            _mark_live(content_id, channel, live_url, state_backend)
            click.echo(f"Marked {channel} as live: {live_url}")
            return
        except (ImportError, AttributeError):
            pass  # Fall through to generic state update below.

    # Generic fallback: update state directly on the backend.
    try:
        state_backend.mark_published(  # type: ignore[union-attr]
            content_id=content_id,
            channel=channel,
            state="live",
            published_url=live_url,
        )
        click.echo(f"Marked {channel} as live: {live_url}")
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# open-pending
# ---------------------------------------------------------------------------

@cli.command("open-pending")
@click.argument("content_id")
@click.option(
    "--no-prefill",
    is_flag=True,
    default=False,
    help="Open tabs without Playwright pre-fill (manual paste).",
)
def open_pending(content_id: str, no_prefill: bool) -> None:
    """Open browser tabs for all pending Medium variants.

    Looks up ``needs_browser`` entries in the Post Log for *content_id* and
    opens the corresponding Medium compose URLs in new browser tabs.

    If the ``medium-browser`` adapter is available and Playwright is installed,
    the tabs will be pre-filled (unless --no-prefill is passed).
    """
    state_backend = _build_backend()

    # Retrieve needs_browser entries for this content_id.
    try:
        entries = state_backend.list_post_log(  # type: ignore[union-attr]
            content_id=content_id, state="needs_browser"
        )
    except AttributeError:
        # Fallback for backends that expose query_post_log instead.
        try:
            from .backends.base import PostLogFilter  # type: ignore[import]  # noqa: PLC0415

            entries = state_backend.query_post_log(  # type: ignore[union-attr]
                PostLogFilter(content_id=content_id, state="needs_browser")  # type: ignore[call-arg]
            )
        except Exception as exc:  # noqa: BLE001
            click.echo(f"error querying post log: {exc}", err=True)
            sys.exit(1)

    if not entries:
        click.echo(f"No pending browser variants found for content_id={content_id!r}.")
        return

    if MediumBrowserAdapter is not None and not no_prefill:
        try:
            from .adapters.medium_browser import open_pending_in_tabs  # type: ignore[import]  # noqa: PLC0415

            asyncio.run(open_pending_in_tabs(content_id, state_backend))
            return
        except (ImportError, AttributeError):
            pass  # Fall through to simple webbrowser.open below.

    # Fallback: open compose URLs via the stdlib webbrowser module.
    opened = 0
    for entry in entries:
        compose_url = (
            entry.get("compose_url")
            if isinstance(entry, dict)
            else getattr(entry, "compose_url", None)
        )
        if not compose_url:
            compose_url = "https://medium.com/new-story"
        click.echo(f"Opening: {compose_url}")
        webbrowser.open_new_tab(str(compose_url))
        opened += 1

    click.echo(f"Opened {opened} tab(s). Paste from ~/.distribution-mcp/drafts/{content_id}/")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--content-id",
    default=None,
    help="Filter results to a specific content_id.",
)
def status(content_id: str | None) -> None:
    """Print the Post Log in table format.

    Shows channel, state, live_url, and published_at for all records,
    optionally filtered to a single content piece.
    """
    from rich.console import Console  # type: ignore[import]  # noqa: PLC0415
    from rich.table import Table  # type: ignore[import]  # noqa: PLC0415

    state_backend = _build_backend()

    # Retrieve entries, supporting both list_post_log and query_post_log APIs.
    try:
        if content_id is not None:
            entries = state_backend.list_post_log(content_id=content_id)  # type: ignore[union-attr]
        else:
            entries = state_backend.list_post_log()  # type: ignore[union-attr]
    except AttributeError:
        try:
            from .backends.base import PostLogFilter  # type: ignore[import]  # noqa: PLC0415

            filt = PostLogFilter(content_id=content_id) if content_id else PostLogFilter()  # type: ignore[call-arg]
            entries = state_backend.query_post_log(filt)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            click.echo(f"error querying post log: {exc}", err=True)
            sys.exit(1)

    if not entries:
        click.echo("No post log entries found.")
        return

    console = Console()
    table = Table(title="Post Log", show_lines=True)
    table.add_column("Content ID", style="dim", no_wrap=True)
    table.add_column("Channel", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Live URL")
    table.add_column("Published At", no_wrap=True)

    for entry in entries:
        # Support both dict (YamlBackend raw output) and object (Pydantic model).
        if isinstance(entry, dict):
            _id = str(entry.get("content_id", ""))
            _channel = str(entry.get("channel", ""))
            _state = str(entry.get("state", ""))
            _url = str(entry.get("published_url") or entry.get("live_url") or "")
            _at = str(entry.get("published_at") or entry.get("updated_at") or "")
        else:
            _id = str(getattr(entry, "content_id", ""))
            _channel = str(getattr(entry, "channel", ""))
            _state = str(getattr(entry, "state", ""))
            _url = str(getattr(entry, "live_url", "") or "")
            _at = str(getattr(entry, "published_at", "") or "")

        state_style = {
            "live": "green",
            "failed": "red",
            "needs_browser": "yellow",
            "queued": "cyan",
            "taken_down": "dim",
        }.get(_state, "")

        table.add_row(
            _id,
            _channel,
            f"[{state_style}]{_state}[/{state_style}]" if state_style else _state,
            _url,
            _at,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
