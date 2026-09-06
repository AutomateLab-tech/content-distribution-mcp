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

test("re-scheduling collapses a legacy queue that already holds duplicates for the same pair", async () => {
  const backend = makeBackend({ default: { credentials: {} } });
  const profile = backend.loadProfile("default");

  // Simulate a scheduled.yaml written by the pre-fix release: two duplicate
  // entries for the same (content_id, channel), plus one unrelated channel
  // that must survive untouched.
  const legacyQueue = [
    { id: "post-3::devto:main::2020-01-01T00:00:00Z", content_id: "post-3", channel: "devto:main", variant: {}, schedule_at: "2020-01-01T00:00:00Z" },
    { id: "post-3::devto:main::2020-01-01T00:00:00Z", content_id: "post-3", channel: "devto:main", variant: {}, schedule_at: "2020-01-01T00:00:00Z" },
    { id: "post-3::hashnode:main::2020-01-01T00:00:00Z", content_id: "post-3", channel: "hashnode:main", variant: {}, schedule_at: "2020-01-01T00:00:00Z" },
  ];
  fs.writeFileSync(path.join(backend.baseDir ?? "", "scheduled.yaml"), yaml.dump(legacyQueue), "utf8");

  const variant = { channel: "devto:main", title: "t", body: "b", tags: [], extras: {}, schedule_at: "2020-03-01T00:00:00Z" };
  await scheduleVariants(content("post-3"), [variant], profile, {}, backend);

  const all = backend.listScheduled();
  const devtoEntries = all.filter(e => e.channel === "devto:main");
  const hashnodeEntries = all.filter(e => e.channel === "hashnode:main");

  assert.equal(devtoEntries.length, 1, "re-scheduling should collapse pre-existing duplicates for the same pair, not just skip past them");
  assert.equal(devtoEntries[0].schedule_at, "2020-03-01T00:00:00Z");
  assert.equal(hashnodeEntries.length, 1, "an unrelated channel's entry must be left alone");
});

test("a plain '::' join would let two distinct (content_id, channel) pairs collide onto the same id", async () => {
  const backend = makeBackend({ default: { credentials: {} } });
  const profile = backend.loadProfile("default");

  // "post" + "devto:main::x" and "post::devto:main" + "x" both join to
  // "post::devto:main::x" under a naive `${a}::${b}` id. Scheduling both
  // must keep two independent entries.
  const variantA = { channel: "devto:main::x", title: "t", body: "b", tags: [], extras: {}, schedule_at: "2020-01-01T00:00:00Z" };
  const variantB = { channel: "x", title: "t", body: "b", tags: [], extras: {}, schedule_at: "2020-01-01T00:00:00Z" };

  const idA = await scheduleVariants(content("post"), [variantA], profile, {}, backend).then(r => r["devto:main::x"]);
  const idB = await scheduleVariants(content("post::devto:main"), [variantB], profile, {}, backend).then(r => r["x"]);

  assert.notEqual(idA, idB, "distinct (content_id, channel) pairs must not collide onto the same queue id");

  const all = backend.listScheduled();
  assert.equal(all.length, 2, "both entries must survive independently");

  backend.dequeueScheduled(idA);
  const remaining = backend.listScheduled();
  assert.equal(remaining.length, 1, "dequeuing one id must not also remove the other pair's entry");
  assert.equal(remaining[0].content_id, "post::devto:main");
});
