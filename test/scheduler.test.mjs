import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import yaml from "js-yaml";

import { YamlBackend } from "../dist/backends/yaml.js";
import { scheduleVariants, drain } from "../dist/scheduler.js";

function makeBackend(profiles) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cdmcp-test-"));
  fs.writeFileSync(path.join(dir, "profiles.yaml"), yaml.dump(profiles), "utf8");
  return new YamlBackend(dir);
}

function content(id) {
  return { id, title: "t", body_md: "b", tags: [], author: "a" };
}

test("drain() publishes with the profile that was active at schedule time, not 'default'", async () => {
  const backend = makeBackend({
    default: { credentials: { TOKEN: "default-token" } },
    clientA: { credentials: { TOKEN: "clientA-token" } },
  });

  const clientAProfile = backend.loadProfile("clientA");
  const variant = { channel: "devto:main", title: "t", body: "b", tags: [], extras: {}, schedule_at: "2020-01-01T00:00:00Z" };

  await scheduleVariants(content("post-1"), [variant], clientAProfile, {}, backend);

  const seenProfiles = [];
  const adapters = {
    devto: {
      async publish(_variant, profile) {
        seenProfiles.push(profile.name);
        return { channel: "devto:main", state: "live", live_url: "https://example.test/1" };
      },
    },
  };

  await drain(adapters, backend, new Date("2020-06-01T00:00:00Z"));

  assert.deepEqual(seenProfiles, ["clientA"]);
});

test("scheduling the same (content_id, channel) twice upserts instead of duplicating the queue entry", async () => {
  const backend = makeBackend({ default: { credentials: {} } });
  const profile = backend.loadProfile("default");
  const adapters = {};

  const first = { channel: "devto:main", title: "t", body: "b", tags: [], extras: {}, schedule_at: "2020-01-01T00:00:00Z" };
  const second = { channel: "devto:main", title: "t revised", body: "b revised", tags: [], extras: {}, schedule_at: "2020-02-01T00:00:00Z" };

  await scheduleVariants(content("post-2"), [first], profile, adapters, backend);
  await scheduleVariants(content("post-2"), [second], profile, adapters, backend);

  const due = backend.listScheduled("2099-01-01T00:00:00Z");
  assert.equal(due.length, 1, "expected exactly one queued entry for the same (content_id, channel) pair");
  assert.equal(due[0].schedule_at, "2020-02-01T00:00:00Z", "expected the later schedule call to win");
});
