import { test, afterEach } from "node:test";
import assert from "node:assert/strict";
import { GetXAPITwitterAdapter } from "../dist/adapters/getxapi-twitter.js";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const variant = { channel: "twitter_getxapi:main", title: "", body: "Hello world", tags: [], extras: {} };
const profile = { name: "client", credentials: { GETXAPI_API_KEY: "test-api-key", GETXAPI_AUTH_TOKEN: "test-account-token", GETXAPI_ENABLE_ACTIONS: "true" } };
const adapter = new GetXAPITwitterAdapter();

test("sends both credentials and reads the documented nested tweet ID", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "https://api.getxapi.com/twitter/tweet/create");
    assert.equal(options.headers.Authorization, "Bearer test-api-key");
    assert.deepEqual(JSON.parse(options.body), { auth_token: "test-account-token", text: "Hello world" });
    return new Response(JSON.stringify({ status: "success", data: { id: "1234567890" } }));
  };
  const result = await adapter.publish(variant, profile);
  assert.equal(result.state, "live");
  assert.equal(result.live_url, "https://x.com/i/web/status/1234567890");
});

test("missing credentials or write opt-in prevents any request", async () => {
  globalThis.fetch = async () => { assert.fail("must not send a request"); };
  for (const key of ["GETXAPI_API_KEY", "GETXAPI_AUTH_TOKEN", "GETXAPI_ENABLE_ACTIONS"]) {
    const credentials = { ...profile.credentials };
    delete credentials[key];
    assert.equal((await adapter.publish(variant, { ...profile, credentials })).state, "failed");
  }
});

test("rejects empty and oversized text without truncating or posting", async () => {
  globalThis.fetch = async () => { assert.fail("must not send a request"); };
  for (const body of ["   ", "x".repeat(281)]) {
    assert.equal((await adapter.publish({ ...variant, body }, profile)).state, "failed");
  }
});

test("preserves complete Unicode text at the character limit", async () => {
  const body = "😀".repeat(280);
  globalThis.fetch = async (_url, options) => {
    assert.equal(JSON.parse(options.body).text, body);
    return new Response(JSON.stringify({ status: "success", data: { id: "123" } }));
  };
  assert.equal((await adapter.publish({ ...variant, body }, profile)).state, "live");
});

test("does not report unconfirmed responses as live", async () => {
  for (const data of [{}, { status: "error", msg: "failed" }, { status: "success", data: {} }, { status: "success", data: { id: "invalid" } }]) {
    globalThis.fetch = async () => new Response(JSON.stringify(data));
    const result = await adapter.publish(variant, profile);
    assert.equal(result.state, "failed");
    assert.equal(result.live_url, undefined);
  }
});

test("does not copy credentials echoed in error responses into logs", async () => {
  globalThis.fetch = async () => new Response("test-api-key test-account-token", { status: 401 });
  const result = await adapter.publish(variant, profile);
  assert.equal(result.state, "failed");
  assert.match(result.error, /401/);
  assert.doesNotMatch(result.error, /test-api-key|test-account-token/);
});
