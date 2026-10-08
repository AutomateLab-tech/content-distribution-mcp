import fs from "fs";
import path from "path";
import yaml from "js-yaml";
import type { StateBackend, Profile, PostLogEntry, SubredditRule, ScheduledItem } from "./base.js";

export class YamlBackend implements StateBackend {
  private baseDir: string;

  constructor(baseDir?: string) {
    this.baseDir = baseDir ?? path.join(process.env.HOME ?? process.env.USERPROFILE ?? "~", ".distribution-mcp");
    fs.mkdirSync(this.baseDir, { recursive: true });
  }

  private read<T>(file: string, fallback: T): T {
    const p = path.join(this.baseDir, file);
    try {
      const raw = fs.readFileSync(p, "utf8");
      return (yaml.load(raw) as T) ?? fallback;
    } catch {
      return fallback;
    }
  }

  private write(file: string, data: unknown): void {
    fs.writeFileSync(path.join(this.baseDir, file), yaml.dump(data), "utf8");
  }

  loadProfile(name: string): Profile {
    const profiles = this.read<Record<string, Omit<Profile, "name">>>("profiles.yaml", {});
    if (!profiles[name]) throw new Error(`Profile '${name}' not found in ${path.join(this.baseDir, "profiles.yaml")}`);
    return { ...profiles[name], name };
  }

  listProfiles(): string[] {
    return Object.keys(this.read<Record<string, unknown>>("profiles.yaml", {}));
  }

  markPublished(entry: PostLogEntry): void {
    const log = this.read<PostLogEntry[]>("post-log.yaml", []);
    const idx = log.findIndex(e => e.content_id === entry.content_id && e.channel === entry.channel);
    if (idx >= 0) log[idx] = entry;
    else log.push(entry);
    this.write("post-log.yaml", log);
  }

  listPostLog(opts?: { content_id?: string; channel?: string }): PostLogEntry[] {
    const log = this.read<PostLogEntry[]>("post-log.yaml", []);
    return log
      .filter(e => {
        if (opts?.content_id && e.content_id !== opts.content_id) return false;
        if (opts?.channel && e.channel !== opts.channel) return false;
        return true;
      })
      .slice(-200);
  }

  getPostLog(contentId: string, channel: string): PostLogEntry | null {
    const log = this.read<PostLogEntry[]>("post-log.yaml", []);
    return log.find(e => e.content_id === contentId && e.channel === channel) ?? null;
  }

  enqueueScheduled(contentId: string, channel: string, variant: unknown, scheduledAt: string): string {
    const queue = this.read<ScheduledItem[]>("scheduled.yaml", []);
    // JSON-encode the pair rather than joining with a delimiter: content_id
    // and channel are arbitrary caller-supplied strings, so a plain
    // "a::b" join can collide across different pairs (e.g. contentId="post",
    // channel="devto:main::x" vs. contentId="post::devto:main", channel="x")
    // and dequeueScheduled()'s exact-id filter would then drop both.
    const id = JSON.stringify([contentId, channel]);
    // Drop every existing entry for this pair, not just the first match: a
    // queue written by the pre-fix version can already hold more than one
    // duplicate for the same (content_id, channel), and this is the only
    // path that touches that pair again.
    const deduped = queue.filter(e => !(e.content_id === contentId && e.channel === channel));
    deduped.push({ id, content_id: contentId, channel, variant, schedule_at: scheduledAt });
    this.write("scheduled.yaml", deduped);
    return id;
  }

  listScheduled(before?: string): ScheduledItem[] {
    const queue = this.read<ScheduledItem[]>("scheduled.yaml", []);
    if (!before) return queue;
    return queue.filter(e => e.schedule_at <= before);
  }

  dequeueScheduled(id: string): void {
    const queue = this.read<ScheduledItem[]>("scheduled.yaml", []);
    this.write("scheduled.yaml", queue.filter(e => e.id !== id));
  }

  loadSubredditRules(subreddit: string): SubredditRule | null {
    const catalog = this.read<Record<string, SubredditRule>>("subreddit-catalog.yaml", {});
    return catalog[subreddit] ?? null;
  }

  listSubreddits(): SubredditRule[] {
    return Object.values(this.read<Record<string, SubredditRule>>("subreddit-catalog.yaml", {}));
  }

  updateSubredditLastPosted(subreddit: string, postedAt: string): void {
    const catalog = this.read<Record<string, SubredditRule>>("subreddit-catalog.yaml", {});
    if (catalog[subreddit]) catalog[subreddit].last_posted_at = postedAt;
    this.write("subreddit-catalog.yaml", catalog);
  }
}
