# Operator playbook — content distribution pipeline

This is the concrete recipe for taking a finished blog post on
`automatelab.tech` and pushing it out via `content-distribution-mcp`.

It is scoped to what the suite proves works **today** (DEV.to, end-to-end,
60/60 tests green). Everything else is documented as "deferred" so future
operators know what they're stepping into.

---

## What's shippable today

| Channel | State | Why |
|---|---|---|
| `devto:main` | ✅ shippable | 9 respx integration tests + 1 live `GET /api/users/me` smoke check. API key in `.env`. |
| Scheduler queue (`schedule` + `drain`) | ✅ shippable | 6 round-trip tests against YamlBackend. |
| `status` tool | ✅ shippable | Wired to YamlBackend's `list_post_log`. |
| `hashnode:*` | ⏸ deferred | Adapter still calls `await state_backend.*` against sync YamlBackend, and reads `profile.credentials.*` against the dict profile. Same family of bugs as devto had pre-fix. Needs a respx test pass + surgical rewrite. |
| `github_discussions:*` | ⏸ deferred | Same: `from datetime import UTC` is fine, but `await state_backend.claim_idempotency_key(...)` will TypeError, and `mark_published(result)` does not match the YamlBackend signature. |
| `reddit:*` | ⏸ deferred | Heavily depends on `profile.credentials[...]` (dict-attribute access). |
| `medium_browser:*` | ⏸ deferred | Calls `state_backend.mark_published(result)` with a positional `PublishResult`. |
| `linkedin:*` | ⏸ optional | Only loaded if `adapters/linkedin.py` exists. Not present in install. |

**Bottom line:** publish to `devto:main` works. Everything else needs the
same surgical pass the DEV.to adapter got. See "Fixing the other adapters"
below for the recipe.

---

## Prereqs (one-time)

```powershell
# 1. The MCP is already pip-installed editable from this repo.
#    Verify:
python -c "from content_distribution_mcp.server import mcp, adapter_map, state_backend; print('OK')"

# 2. DEV.to API key lives in C:\Work\automatelab\.env as DEVTO_API_KEY.
#    Confirmed valid against /api/users/me as user 'ratamaha' on 2026-05-19.

# 3. The MCP runs with YamlBackend by default (no Notion env vars needed).
#    Storage dir: %USERPROFILE%\.distribution-mcp
```

The MCP is already registered with Claude Code (task #19). Verify with
`claude mcp list` if needed.

---

## Step-by-step: publish one blog post to DEV.to

The MCP does NOT do LLM work. The caller (you, or a skill, or a workflow)
generates the channel-specific copy and hands it to the MCP. Below is the
minimum dance for one post.

### Step 1 — Save the DEV.to API key into a Distribution Profile

```powershell
# The profile is just a YAML file under %USERPROFILE%\.distribution-mcp\profiles\.
# YamlBackend reads it via load_profile(profile_name).
```

```python
# scripts/save_profile.py — one-time
import os
from pathlib import Path
from content_distribution_mcp.backends.yaml_backend import YamlBackend

# Read .env manually so we don't need a runtime dep on python-dotenv.
env = {}
for line in Path(r"C:\Work\automatelab\.env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip().strip('"').strip("'")

backend = YamlBackend(base_dir=Path(os.path.expanduser("~/.distribution-mcp")))
backend.save_profile("default", {
    "DEV_TO_API_KEY": env["DEVTO_API_KEY"],
    # Add more channel keys as adapters come online.
})
print("profile saved:", backend.list_profiles())
```

### Step 2 — Publish a single variant via the MCP tool

```python
# scripts/publish_one.py
import asyncio
from content_distribution_mcp.server import publish

async def main():
    results = await publish(
        content={
            "id": "we-introspected-922-mcp-servers@2026-05-19",
            "title": "We introspected 922 MCP servers — here's the shape of the ecosystem",
            "body_md": "# Headline\n\n...body markdown here...",
            "author": "Artyom Rabzonov",
            "tags": ["mcp", "ai", "automation"],
        },
        variants=[{
            "channel": "devto:main",
            "title": "We introspected 922 MCP servers — here's the shape of the ecosystem",
            "body": "# Headline\n\n...DEV.to-flavored markdown here...",
            "tags": ["ai", "automation", "mcp"],
            "canonical_url": "https://automatelab.tech/we-introspected-922-mcp-servers",
            "extras": {"content_id": "we-introspected-922-mcp-servers@2026-05-19"},
        }],
        profile_name="default",
    )
    for r in results:
        print(r)

asyncio.run(main())
```

**Critical detail:** the idempotency key is `(variant.extras["content_id"], variant.channel)`.
The `Variant` model has no `content_id` field of its own — callers MUST populate
`extras["content_id"]` from the canonical `Content.id`. Running the script
twice will return the same `live_url` on the second call without making a
second API request. This is verified by `test_publish_is_idempotent` in
`tests/test_devto_adapter.py`.

### Step 3 — Verify via `status`

```python
from content_distribution_mcp.server import status
rows = status(content_id="we-introspected-922-mcp-servers@2026-05-19")
print(rows)
# [{'channel': 'devto:main', 'state': 'live', 'live_url': 'https://dev.to/ratamaha/...', ...}]
```

### Step 4 — Scheduled publish (optional)

```python
from content_distribution_mcp.server import schedule
out = await schedule(
    content={...},
    variants=[{
        "channel": "devto:main",
        "title": "...",
        "body": "...",
        "schedule_at": "2026-05-20T09:00:00+00:00",   # ISO-8601 with tz
        "extras": {"content_id": "...@2026-05-19"},
    }],
    profile_name="default",
)
# out["devto:main"] == "<32-char hex scheduled_id>"
```

Then fire the queue from cron / Task Scheduler:

```powershell
content-distribution-mcp drain
```

---

## Fixing the other adapters (when needed)

The DEV.to fix in `src/content_distribution_mcp/adapters/devto.py` is the
template. The same five bugs appear in `hashnode.py`,
`github_discussions.py`, `medium_browser.py`, and `reddit.py`:

1. `from ..models import ContentVariant, OperatorProfile, StateBackend` — these
   names don't exist. The real names are `Variant`, plain `dict` for profile,
   and the StateBackend protocol is a duck-type. Drop the import.
2. `variant.content_id` — `Variant` has no such field. Read
   `variant.extras["content_id"]` instead.
3. `profile.credentials["FOO"]` — `profile` is a `dict`. Use
   `profile["FOO"]` (or check `profile.get("credentials", {})` for
   Notion-shaped profiles).
4. `await state_backend.claim_idempotency_key(...)` — YamlBackend is
   sync. Drop the `await`.
5. `state_backend.mark_published(result)` — signature is
   `mark_published(content_id, channel, *, state, published_url, error)`.

Recipe per adapter: read `devto.py` (now correct), then `<adapter>.py`,
then mirror the five surgical fixes. Add a respx test under
`tests/test_<adapter>_adapter.py` mirroring `test_devto_adapter.py`. Run
`python -m pytest tests/` until green.

---

## What the agent layer should do

The MCP intentionally does no LLM work. The agent/skill layer (Claude Code
custom skill, n8n workflow, plain script) is responsible for:

- Generating channel-specific copy from one source markdown file.
- Picking tags from `hints("devto:main").tag_vocab`.
- Setting `canonical_url` to the `automatelab.tech` URL.
- Generating the `content_id` (convention: `<slug>@<YYYY-MM-DD>`).
- Calling `publish` or `schedule`.

The MCP guarantees idempotency, retry on transient errors, and per-channel
constraint reporting via `hints`. That is the contract.

---

## Test commitment

Every code change that touches an adapter or the scheduler MUST keep the
following green:

```powershell
python -m pytest tests/ -v
```

Today: **60 tests, all passing**. That number should only ever grow.
