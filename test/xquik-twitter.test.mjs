import test from "node:test";
import assert from "node:assert/strict";
import {
  XquikTwitterAdapter,
  buildXquikHeaders,
  buildXquikUrl,
  getXquikConfig,
} from "../dist/adapters/xquik-twitter.js";

const XQUIK_ENV_KEYS = [
  "XQUIK_API_KEY",
  "HERMES_TWEET_API_KEY",
  "XQUIK_ACCOUNT",
  "HERMES_TWEET_ACCOUNT",
  "XQUIK_BASE_URL",
];

const fallback = {
  hints() {
    return {
      supported_md_features: ["links"],
      cta_placement: "none",
      canonical_url_supported: false,
      browser_only: true,
    };
  },
  async publish(variant) {
    return {
      channel: variant.channel,
      state: "needs_browser",
      compose_url: `https://twitter.com/compose/tweet?text=${encodeURIComponent(variant.body.slice(0, 280))}`,
    };
  },
  async unpublish() {
    return [false, "manual"];
  },
};

function profile(credentials = {}) {
  return { name: "test", credentials };
}

function variant(overrides = {}) {
  return {
    channel: "twitter",
    title: "Launch",
    body: "Ship the launch update",
    tags: [],
    extras: {},
    ...overrides,
  };
}

async function withCleanEnv(fn) {
  const previous = new Map(XQUIK_ENV_KEYS.map((key) => [key, process.env[key]]));
  for (const key of XQUIK_ENV_KEYS) {
    delete process.env[key];
  }

  try {
    return await fn();
  } finally {
    for (const [key, value] of previous.entries()) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
  }
}

test("falls back to browser compose when no Hermes Tweet key is configured", async () => {
  await withCleanEnv(async () => {
    const adapter = new XquikTwitterAdapter(fallback);
    const result = await adapter.publish(variant(), profile());

    assert.equal(result.state, "needs_browser");
    assert.equal(result.channel, "twitter");
    assert.equal(result.compose_url.startsWith("https://twitter.com/compose/tweet"), true);
  });
});

test("uses Xquik API key auth for automated Twitter publishing", async () => {
  await withCleanEnv(async () => {
    const adapter = new XquikTwitterAdapter(fallback);
    const calls = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ data: { id: "12345" } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    };

    try {
      const result = await adapter.publish(
        variant(),
        profile({ XQUIK_API_KEY: "xq_test", XQUIK_ACCOUNT: "@launch" }),
      );

      assert.equal(calls.length, 1);
      assert.equal(calls[0].url, "https://xquik.com/api/v1/x/tweets");
      assert.equal(calls[0].init.headers["x-api-key"], "xq_test");
      assert.deepEqual(JSON.parse(calls[0].init.body), {
        account: "@launch",
        text: "Ship the launch update",
      });
      assert.equal(result.state, "live");
      assert.equal(result.live_url, "https://x.com/launch/status/12345");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

test("accepts bearer auth and account from channel suffix", async () => {
  await withCleanEnv(async () => {
    const adapter = new XquikTwitterAdapter(fallback);
    const originalFetch = globalThis.fetch;
    let request;
    globalThis.fetch = async (url, init) => {
      request = { url, init };
      return new Response(JSON.stringify({ url: "https://x.com/team/status/9" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    };

    try {
      const result = await adapter.publish(
        variant({ channel: "x:team" }),
        profile({ HERMES_TWEET_API_KEY: "bearer-token", XQUIK_BASE_URL: "https://example.test/root/" }),
      );

      assert.equal(request.url, "https://example.test/root/api/v1/x/tweets");
      assert.equal(request.init.headers.Authorization, "Bearer bearer-token");
      assert.equal(JSON.parse(request.init.body).account, "team");
      assert.equal(result.live_url, "https://x.com/team/status/9");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

test("fails clearly when automated publishing lacks an account", async () => {
  await withCleanEnv(async () => {
    const adapter = new XquikTwitterAdapter(fallback);
    const result = await adapter.publish(variant(), profile({ XQUIK_API_KEY: "xq_test" }));

    assert.equal(result.state, "failed");
    assert.match(result.error, /XQUIK_ACCOUNT/);
  });
});

test("surfaces Hermes Tweet API errors without throwing", async () => {
  await withCleanEnv(async () => {
    const adapter = new XquikTwitterAdapter(fallback);
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => new Response(JSON.stringify({ error: "rate limited" }), { status: 429 });

    try {
      const result = await adapter.publish(
        variant(),
        profile({ XQUIK_API_KEY: "xq_test", XQUIK_ACCOUNT: "@launch" }),
      );

      assert.equal(result.state, "failed");
      assert.match(result.error, /429/);
      assert.match(result.error, /rate limited/);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

test("converts network failures into failed publish results", async () => {
  await withCleanEnv(async () => {
    const adapter = new XquikTwitterAdapter(fallback);
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => {
      throw new TypeError("connection reset");
    };

    try {
      const result = await adapter.publish(
        variant(),
        profile({ XQUIK_API_KEY: "xq_test", XQUIK_ACCOUNT: "@launch" }),
      );

      assert.equal(result.channel, "twitter");
      assert.equal(result.state, "failed");
      assert.match(result.error, /connection reset/);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

test("trims configured credentials and preserves base URL paths", async () => {
  await withCleanEnv(async () => {
    const config = getXquikConfig(
      profile({ XQUIK_API_KEY: " xq_test ", XQUIK_ACCOUNT: " @launch " }),
      variant(),
    );

    assert.equal(config.apiKey, "xq_test");
    assert.equal(config.account, "@launch");
    assert.equal(buildXquikUrl("https://example.test/root/"), "https://example.test/root/api/v1/x/tweets");
    assert.deepEqual(buildXquikHeaders("token").Authorization, "Bearer token");
  });
});
