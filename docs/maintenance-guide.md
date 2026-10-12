# Content Distribution MCP maintenance guide

Check channel_hints and credentials before publishing. Use a stable content ID for retries. A scheduled entry still needs the drain worker to deliver it; inspect post_status after the scheduled time.

## Installation and compatibility

Follow the repository README for the supported install method and runtime. Pin the version used in a reproducible example. Keep credentials in your client configuration, outside shared fixtures.

## Verified tool surface

The local compiled server returned these names through MCP `tools/list` on 12 October 2026. Recheck after upgrading.

- `post_publish`
- `post_schedule`
- `post_drain`
- `post_status`
- `post_unpublish`
- `channel_hints`
- `profile_list`
- `subreddit_list`

## First call

Start with `channel_hints`. Required fields: `channel`. Read its input schema before passing a recorded fixture.

## Verify a change

Record the package or snapshot version, exact input, observation time and actual output. Check a materially different input and a valid neighboring case. Treat expected output as a fixture, not a live measurement.
